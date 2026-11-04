"""Offline tests of the actual C++ library and broker-free session controller."""

from pathlib import Path
import argparse
import asyncio
import contextlib
import io
import json
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

LIBRARY = Path(sys.argv.pop(1)).resolve()
ROOT = Path(sys.argv.pop(1)).resolve()
sys.path.insert(0, str(ROOT))
from live.feeds import Quote, QuoteFeedError, QuoteFeedUnavailable
from live.health import RetryPolicy, SessionHealth
from live.native import PaperEngine
from live.run import PaperSession, configuration, run_session, suspend_for_retry
from live.recovery import SessionLock, create_manifest


class NetworkPaperTests(unittest.TestCase):
    def runner_args(self, root):
        return argparse.Namespace(poll_seconds=15, max_source_age=1800, duration=5,
                                  config=ROOT / 'config/network-paper.conf',
                                  library=LIBRARY, output_dir=Path(root))

    def make_resumable_session(self, root, engine, *, resume=False, manifest=None):
        output = Path(root) / 'paper-session'
        config = ROOT / 'config/network-paper.conf'
        if not resume and manifest is None:
            manifest = create_manifest(output, 'TEST.NS', config.read_bytes())
        session = PaperSession(engine, configuration(config), 'TEST.NS', output,
                               resume=resume, manifest=manifest)
        return output, manifest, session

    def test_nonfinite_duration_rejected(self):
        args = argparse.Namespace(poll_seconds=15, max_source_age=1800, duration=float('nan'))
        with self.assertRaises(ValueError):
            asyncio.run(run_session(args))

    def test_quote_time_risk_halt_cannot_be_reported_as_closed(self):
        class FakeEngine:
            def __init__(self, *_):
                self.state = dict(position=0, cash_paise=0, fees_paise=0, pnl_paise=0,
                                  bid_paise=10000, ask_paise=10002, open_orders=0,
                                  killed=0, audit_failed=0, kill_reason=0)
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def heartbeat(self, _): pass
            def events(self): return iter(())
            def snapshot(self): return self.state.copy()
            def tick(self, *_):
                self.state['killed'] = 1
                self.state['kill_reason'] = 8
                return 1
            def shutdown(self): self.state['killed'] = 1
            def disconnect(self): self.state['killed'] = 1
        class Feed:
            symbol = 'TEST.NS'
            def fetch(self): return Quote(self.symbol, 10000, int(time.time()))
        with tempfile.TemporaryDirectory() as temporary:
            args = argparse.Namespace(poll_seconds=15, max_source_age=1800, duration=5,
                                      config=ROOT / 'config/network-paper.conf',
                                      library=LIBRARY, output_dir=Path(temporary))
            with patch('live.run.PaperEngine', FakeEngine), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(asyncio.run(run_session(args, Feed())), 3)
            report_path = next(Path(temporary).glob('*/summary.json'))
            report = json.loads(report_path.read_text())
            self.assertEqual(report['stop_reason'], 'risk_halt')
            self.assertTrue(report['risk_halted'])

    def test_real_engine_persists_and_reports_virtual_balance(self):
        config = ROOT / 'config/network-paper.conf'
        with tempfile.TemporaryDirectory() as temporary, PaperEngine(LIBRARY, config) as engine:
            session = PaperSession(engine, configuration(config), 'TEST.NS', Path(temporary))
            try:
                self.assertTrue(session.accept(Quote('TEST.NS', 10000, 1000, market_open=True), 0, 1100))
                self.assertTrue(session.accept(Quote('TEST.NS', 10100, 1060, market_open=True), 10_000_000, 1160))
                self.assertEqual(engine.snapshot()['open_orders'], 1)
                self.assertTrue(session.accept(Quote('TEST.NS', 10095, 1120, market_open=True), 20_000_000, 1220))
                state = engine.snapshot()
                self.assertEqual(state['position'], 1)
                self.assertGreater(state['fees_paise'], 0)
                engine.shutdown()
                session.stop_reason = 'test_complete'
                report = session.report(finished=True)
                self.assertEqual(report['virtual_cash_paise'], 10_000_000 + state['cash_paise'])
                self.assertEqual(report['virtual_equity_paise'], 10_000_000 + state['pnl_paise'])
                self.assertEqual(report['fills'], 1)
                self.assertEqual(report['open_orders'], 0)
                self.assertEqual(report['feed_status'], 'delayed/indicative')
                self.assertTrue((Path(temporary) / 'ticks.csv').is_file())
            finally:
                session.close()

    def test_duplicates_do_not_fill_and_heartbeat_cancels(self):
        config = ROOT / 'config/network-paper.conf'
        with tempfile.TemporaryDirectory() as temporary, PaperEngine(LIBRARY, config) as engine:
            session = PaperSession(engine, configuration(config), 'TEST.NS', Path(temporary))
            try:
                session.accept(Quote('TEST.NS', 10000, 1000, market_open=True), 0, 1100)
                quote = Quote('TEST.NS', 10100, 1060, market_open=True)
                session.accept(quote, 10_000_000, 1160)
                self.assertFalse(session.accept(quote, 20_000_000, 1175))
                self.assertEqual(engine.snapshot()['position'], 0)
                self.assertEqual(session.quotes, 2)
                engine.heartbeat(181_000_000_000)
                self.assertTrue(engine.snapshot()['killed'])
                self.assertEqual(engine.snapshot()['open_orders'], 0)
            finally:
                session.close()

    def test_stale_regressing_and_wrong_quotes_rejected(self):
        config = ROOT / 'config/network-paper.conf'
        with tempfile.TemporaryDirectory() as temporary, PaperEngine(LIBRARY, config) as engine:
            session = PaperSession(engine, configuration(config), 'TEST.NS', Path(temporary))
            try:
                with self.assertRaisesRegex(ValueError, 'stale'):
                    session.accept(Quote('TEST.NS', 10000, 1000, market_open=True), 0, 2801)
                self.assertEqual(engine.snapshot()['position'], 0)
                session.accept(Quote('TEST.NS', 10000, 1060, market_open=True), 1, 1160)
                with self.assertRaisesRegex(ValueError, 'regressed'):
                    session.accept(Quote('TEST.NS', 10000, 1000, market_open=True), 2, 1160)
                with self.assertRaisesRegex(ValueError, 'Unexpected'):
                    session.accept(Quote('OTHER.NS', 10000, 1120, market_open=True), 3, 1220)
            finally:
                session.close()

    def test_closed_market_never_generates_orders(self):
        config = ROOT / 'config/network-paper.conf'
        with tempfile.TemporaryDirectory() as temporary, PaperEngine(LIBRARY, config) as engine:
            session = PaperSession(engine, configuration(config), 'TEST.NS', Path(temporary))
            try:
                session.accept(Quote('TEST.NS', 10000, 1000), 0, 99999)
                report = session.report()
                self.assertEqual(report['stop_reason'], 'market_closed')
                self.assertEqual(report['orders_accepted'], 0)
                self.assertEqual(report['virtual_equity_paise'], 10_000_000)
            finally:
                session.close()

    def test_no_virtual_overdraft_and_invalid_native_input(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / 'small.conf'
            config.write_text((ROOT / 'config/network-paper.conf').read_text().replace(
                'initial_capital_paise=10000000', 'initial_capital_paise=10050'))
            with PaperEngine(LIBRARY, config) as engine:
                self.assertEqual(engine.tick(0, 1, 9999, 10001, 100), 1)
                self.assertEqual(engine.tick(10_000_000, 2, 10099, 10101, 100), 1)
                events = list(engine.events())
                self.assertTrue(any(event['reason'] == 'funds' for event in events))
                self.assertEqual(engine.snapshot()['open_orders'], 0)
                with self.assertRaises(ValueError):
                    engine.tick(20_000_000, 3, 1, 2, -1)
                self.assertTrue(engine.snapshot()['killed'])

    def test_recovery_replays_a_prepared_tick_cancels_orders_and_repairs_torn_tail(self):
        config = ROOT / 'config/network-paper.conf'
        with tempfile.TemporaryDirectory() as temporary:
            with PaperEngine(LIBRARY, config) as engine:
                output, manifest, session = self.make_resumable_session(temporary, engine)
                session.accept(Quote('TEST.NS', 10000, 1000, market_open=True), 0, 1060)
                operation = {
                    'kind': 'operation', 'id': session.next_operation_id, 'op': 'tick',
                    'elapsed_ns': 10_000_000, 'sequence': 2, 'bid': 10099, 'ask': 10101,
                    'quantity': session.quantity, 'session_open': True,
                    'source': {'symbol': 'TEST.NS', 'price_paise': 10100, 'source_time_s': 1060,
                               'currency': 'INR', 'market_open': True, 'delayed': True},
                    'source_age_s': 60, 'received_utc_s': 1120,
                }
                session.journal.append_operation(operation)  # Crash after durable intent, before engine call.
                session.close()
            with (output / 'session.jsonl').open('ab') as journal:
                journal.write(b'{"kind":"checkpoint","id":')  # Torn final write is safely discarded.
            with PaperEngine(LIBRARY, config) as engine:
                _, _, recovered = self.make_resumable_session(
                    temporary, engine, resume=True, manifest=manifest)
                self.assertEqual(recovered.quotes, 2)
                self.assertEqual(recovered.sequence, 2)
                self.assertEqual(engine.snapshot()['open_orders'], 1)
                self.assertFalse(engine.snapshot()['killed'])
                recovered.heartbeat(recovered.last_engine_time + 1)
                recovered.cancel_all(recovered.last_engine_time + 1)
                state = engine.snapshot()
                self.assertEqual(state['position'], 0)
                self.assertEqual(state['open_orders'], 0)
                self.assertFalse(state['killed'])
                report = recovered.report()
                self.assertTrue(report['resumed'])
                self.assertEqual(report['quotes'], 2)
                self.assertEqual(report['orders_accepted'], 1)
                self.assertEqual(report['open_orders'], 0)
                audit = (output / 'audit.csv').read_text()
                self.assertEqual(audit.count('accepted'), 1)
                self.assertEqual(audit.count('cancelled'), 1)
                recovered.close()

    def test_reconciliation_mismatch_fails_closed(self):
        config = ROOT / 'config/network-paper.conf'
        with tempfile.TemporaryDirectory() as temporary:
            with PaperEngine(LIBRARY, config) as engine:
                output, manifest, session = self.make_resumable_session(temporary, engine)
                session.accept(Quote('TEST.NS', 10000, 1000, market_open=True), 0, 1060)
                session.close()
            lines = (output / 'session.jsonl').read_text().splitlines()
            checkpoint = json.loads(lines[2])
            checkpoint['snapshot']['cash_paise'] += 1
            lines[2] = json.dumps(checkpoint, sort_keys=True, separators=(',', ':'))
            (output / 'session.jsonl').write_text('\n'.join(lines) + '\n')
            with PaperEngine(LIBRARY, config) as engine:
                with self.assertRaisesRegex(RuntimeError, 'reconciliation failed'):
                    self.make_resumable_session(temporary, engine, resume=True, manifest=manifest)
                self.assertEqual(engine.snapshot()['position'], 0)

    def test_disconnect_remains_latched_after_recovery(self):
        config = ROOT / 'config/network-paper.conf'
        with tempfile.TemporaryDirectory() as temporary:
            with PaperEngine(LIBRARY, config) as engine:
                output, manifest, session = self.make_resumable_session(temporary, engine)
                session.accept(Quote('TEST.NS', 10000, 1000, market_open=True), 0, 1060)
                session.disconnect()
                self.assertTrue(engine.snapshot()['killed'])
                session.close()
            with PaperEngine(LIBRARY, config) as engine:
                _, _, recovered = self.make_resumable_session(
                    temporary, engine, resume=True, manifest=manifest)
                self.assertTrue(engine.snapshot()['killed'])
                self.assertEqual(engine.snapshot()['kill_reason'], 3)  # Reject::disconnected
                with self.assertRaisesRegex(RuntimeError, 'halted'):
                    recovered.accept(Quote('TEST.NS', 10000, 1060, market_open=True), 1, 1120)
                recovered.close()

    def test_stale_restart_gap_latches_kill_instead_of_resuming(self):
        config = ROOT / 'config/network-paper.conf'
        with tempfile.TemporaryDirectory() as temporary:
            with PaperEngine(LIBRARY, config) as engine:
                output, manifest, session = self.make_resumable_session(temporary, engine)
                session.accept(Quote('TEST.NS', 10000, 1000, market_open=True), 0, 1060)
                session.close()
            with PaperEngine(LIBRARY, config) as engine:
                _, _, recovered = self.make_resumable_session(
                    temporary, engine, resume=True, manifest=manifest)
                recovered.heartbeat(recovered.last_tick_time + recovered.stale_after_ns + 1)
                self.assertTrue(engine.snapshot()['killed'])
                self.assertEqual(engine.snapshot()['kill_reason'], 4)  # Reject::stale
                self.assertEqual(engine.snapshot()['open_orders'], 0)
                with self.assertRaisesRegex(RuntimeError, 'halted'):
                    recovered.accept(Quote('TEST.NS', 10000, 1060, market_open=True),
                                     recovered.last_engine_time + 1, 1120)
                recovered.close()

    def test_eight_simulated_hours_replay_and_session_lock(self):
        config = ROOT / 'config/network-paper.conf'
        with tempfile.TemporaryDirectory() as temporary:
            with PaperEngine(LIBRARY, config) as engine:
                output, manifest, session = self.make_resumable_session(temporary, engine)
                lock = SessionLock(output / 'session.lock')
                lock.acquire()
                with self.assertRaisesRegex(RuntimeError, 'already running'):
                    SessionLock(output / 'session.lock').acquire()
                lock.release()
                for index in range(480):  # Eight simulated hours at one quote per minute.
                    source_time = 1_000 + index * 60
                    price = 10_000 + (index % 3)
                    self.assertTrue(session.accept(
                        Quote('TEST.NS', price, source_time, market_open=True),
                        index * 60_000_000_000, source_time + 60))
                before = engine.snapshot()
                session.close()  # Process loss: only the durable journal survives.
            with PaperEngine(LIBRARY, config) as engine:
                _, _, recovered = self.make_resumable_session(
                    temporary, engine, resume=True, manifest=manifest)
                self.assertEqual(recovered.quotes, 480)
                self.assertEqual(recovered.last_source, 1_000 + 479 * 60)
                self.assertEqual(engine.snapshot()['position'], before['position'])
                self.assertEqual(engine.snapshot()['cash_paise'], before['cash_paise'])
                self.assertEqual(engine.snapshot()['fees_paise'], before['fees_paise'])
                recovered.heartbeat(recovered.last_engine_time + 1)
                recovered.cancel_all(recovered.last_engine_time + 1)
                self.assertEqual(engine.snapshot()['open_orders'], 0)
                recovered.close()

    def test_transient_failure_recovers_then_closed_market_stops_cleanly(self):
        class Feed:
            symbol = 'TEST.NS'
            calls = 0
            def fetch(self):
                self.calls += 1
                if self.calls == 1:
                    raise QuoteFeedUnavailable('simulated timeout')
                return Quote(self.symbol, 10000, int(time.time()) - 60)
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            feed = Feed()
            code = asyncio.run(run_session(self.runner_args(temporary), feed,
                                            retry_policy=RetryPolicy(base_delay_s=0.001)))
            output = next(Path(temporary).iterdir())
            summary = json.loads((output / 'summary.json').read_text())
            health = json.loads((output / 'health.json').read_text())
            alerts = [json.loads(line) for line in (output / 'alerts.jsonl').read_text().splitlines()]
            self.assertEqual(code, 0)
            self.assertEqual(feed.calls, 2)
            self.assertEqual(summary['stop_reason'], 'market_closed')
            self.assertFalse(summary['risk_halted'])
            self.assertEqual(summary['open_orders'], 0)
            self.assertEqual(health['transient_failures'], 1)
            self.assertEqual(health['recoveries'], 1)
            self.assertEqual(health['status'], 'stopped')
            self.assertIn('feed_recovered', [row['event'] for row in alerts])

    def test_retry_exhaustion_and_invalid_payloads_fail_closed(self):
        for error, expected_calls in ((QuoteFeedUnavailable('simulated outage'), 2),
                                      (QuoteFeedError('simulated bad payload'), 1)):
            class Feed:
                symbol = 'TEST.NS'
                calls = 0
                def fetch(self):
                    self.calls += 1
                    raise error
            with self.subTest(error=error), tempfile.TemporaryDirectory() as temporary, \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                feed = Feed()
                code = asyncio.run(run_session(self.runner_args(temporary), feed,
                                                retry_policy=RetryPolicy(max_retries=1, base_delay_s=0.001)))
                output = next(Path(temporary).iterdir())
                summary = json.loads((output / 'summary.json').read_text())
                health = json.loads((output / 'health.json').read_text())
                self.assertEqual(code, 3)
                self.assertEqual(feed.calls, expected_calls)
                self.assertTrue(summary['risk_halted'])
                self.assertEqual(summary['open_orders'], 0)
                self.assertEqual(health['status'], 'halted')
                self.assertEqual(health['validation_failures'], int(expected_calls == 1))

    def test_retry_cancels_pending_orders_and_cannot_clear_a_stale_halt(self):
        config = ROOT / 'config/network-paper.conf'
        with tempfile.TemporaryDirectory() as temporary, PaperEngine(LIBRARY, config) as engine:
            output = Path(temporary)
            session = PaperSession(engine, configuration(config), 'TEST.NS', output)
            health = SessionHealth(output, 'TEST.NS', 'test')
            try:
                session.accept(Quote('TEST.NS', 10000, 1000, market_open=True), 0, 1060)
                quote = Quote('TEST.NS', 10100, 1060, market_open=True)
                session.accept(quote, 10_000_000, 1120)
                self.assertEqual(engine.snapshot()['open_orders'], 1)
                delay = suspend_for_retry(session, health, RetryPolicy(),
                                          QuoteFeedUnavailable('test timeout'), 20_000_000)
                self.assertEqual(delay, 5)
                self.assertEqual(engine.snapshot()['open_orders'], 0)
                self.assertEqual(engine.snapshot()['position'], 0)
                self.assertFalse(engine.snapshot()['killed'])
                self.assertFalse(session.accept(quote, 30_000_000, 1130))
                self.assertEqual(engine.snapshot()['position'], 0)
                self.assertEqual(session.journal.transactions[-1][0]['op'], 'cancel_all')
                session.heartbeat(session.last_tick_time + session.stale_after_ns + 1)
                self.assertTrue(engine.snapshot()['killed'])
                with self.assertRaisesRegex(RuntimeError, 'halted'):
                    session.accept(Quote('TEST.NS', 10100, 1120, market_open=True),
                                   session.last_engine_time + 1, 1180)
            finally:
                health.close()
                session.close()

    def test_invalid_quote_types_and_completed_bar_revisions_are_rejected(self):
        config = ROOT / 'config/network-paper.conf'
        with tempfile.TemporaryDirectory() as temporary, PaperEngine(LIBRARY, config) as engine:
            session = PaperSession(engine, configuration(config), 'TEST.NS', Path(temporary))
            try:
                for quote in (Quote('TEST.NS', True, 1000), Quote('TEST.NS', 10000, True),
                              Quote('TEST.NS', 10000, 1000, market_open=1),
                              Quote('TEST.NS', 100_000_001, 1000)):
                    with self.subTest(quote=quote), self.assertRaises(ValueError):
                        session.accept(quote, 0, 1060)
                self.assertEqual(session.quotes, 0)
                session.accept(Quote('TEST.NS', 10000, 1000, market_open=True), 0, 1060)
                with self.assertRaisesRegex(ValueError, 'revised'):
                    session.accept(Quote('TEST.NS', 10001, 1000, market_open=True), 1, 1061)
                self.assertEqual(session.last_quote.price_paise, 10000)
                self.assertEqual(session.quotes, 1)
            finally:
                session.close()

    def test_monitoring_write_failure_halts_before_fetch(self):
        class Feed:
            symbol = 'TEST.NS'
            calls = 0
            def fetch(self):
                self.calls += 1
                return Quote(self.symbol, 10000, int(time.time()) - 60)
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()), \
                patch('live.health.atomic_json', side_effect=OSError('simulated monitoring disk failure')):
            feed = Feed()
            self.assertEqual(asyncio.run(run_session(self.runner_args(temporary), feed)), 3)
            summary = json.loads(next(Path(temporary).glob('*/summary.json')).read_text())
            self.assertEqual(feed.calls, 0)
            self.assertTrue(summary['risk_halted'])
            self.assertEqual(summary['open_orders'], 0)

    def test_prepared_operation_recovery_retains_original_downtime_anchor(self):
        config = ROOT / 'config/network-paper.conf'
        with tempfile.TemporaryDirectory() as temporary:
            original_wall = time.time_ns() - 600_000_000_000
            with PaperEngine(LIBRARY, config) as engine:
                output, manifest, session = self.make_resumable_session(temporary, engine)
                session.accept(Quote('TEST.NS', 10000, 1000, market_open=True), 0, 1060)
                session.journal.append_operation(dict(kind='operation', id=session.next_operation_id,
                    op='tick', prepared_wall_time_ns=original_wall, elapsed_ns=10_000_000,
                    sequence=2, bid=10099, ask=10101, quantity=session.quantity, session_open=True,
                    source=dict(symbol='TEST.NS', price_paise=10100, source_time_s=1060,
                                currency='INR', market_open=True, delayed=True),
                    source_age_s=60, received_utc_s=1120))
                session.close()
            with PaperEngine(LIBRARY, config) as engine:
                _, _, recovered = self.make_resumable_session(temporary, engine, resume=True, manifest=manifest)
                try:
                    self.assertEqual(recovered.last_commit_wall_time_ns, original_wall)
                    downtime = time.time_ns() - recovered.last_commit_wall_time_ns
                    recovered.heartbeat(recovered.last_engine_time + downtime)
                    self.assertTrue(engine.snapshot()['killed'])
                    self.assertEqual(engine.snapshot()['open_orders'], 0)
                finally:
                    recovered.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
