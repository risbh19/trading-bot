"""Offline tests of the actual portfolio library, journal and multi-feed scheduler."""

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

LIBRARY = Path(sys.argv.pop(1)).resolve()
ROOT = Path(sys.argv.pop(1)).resolve()
sys.path.insert(0, str(ROOT))
from live.feeds import Quote, QuoteFeedError, QuoteFeedUnavailable
from live.health import RetryPolicy
from live.portfolio_native import PaperPortfolioEngine
from live.portfolio_run import run_portfolio
from live.portfolio_session import PortfolioSession, PortfolioSpec
from live.recovery import atomic_json
from research.validation import FEE_KEYS, fee_total, read_config, setting

CONFIG = ROOT / 'config/network-portfolio.json'


class SpecTests(unittest.TestCase):
    def test_bundle_freezes_all_account_and_constituent_bytes(self):
        spec = PortfolioSpec.load(CONFIG)
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'session'
            manifest = spec.create_bundle(output)
            self.assertEqual(spec.validate_bundle(output), manifest)
            source = output / 'configs/1.conf'
            original = source.read_bytes()
            source.write_bytes(original + b'\n# modified\n')
            with self.assertRaisesRegex(ValueError, 'constituent'):
                spec.validate_bundle(output)
            source.write_bytes(original)
            manifest['status'] = 'finished'
            atomic_json(output / 'session.json', manifest)
            with self.assertRaisesRegex(ValueError, 'Finished'):
                spec.validate_bundle(output)

    def test_invalid_or_changed_universe_is_rejected(self):
        original = json.loads(CONFIG.read_text())
        for member in original['instruments']:
            member['config'] = str((CONFIG.parent / member['config']).resolve())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / 'spec.json'
            path.write_text(json.dumps(original))
            spec = PortfolioSpec.load(path)
            output = root / 'session'
            spec.create_bundle(output)
            changed = json.loads(json.dumps(original))
            changed['account']['initial_capital_paise'] += 1
            path.write_text(json.dumps(changed))
            with self.assertRaisesRegex(ValueError, 'changed'):
                PortfolioSpec.load(path).validate_bundle(output)
            for mutate in (
                lambda r: r.update(mode='live'),
                lambda r: r['instruments'][1].update(symbol=r['instruments'][0]['symbol']),
                lambda r: r['instruments'][0].update(quantity=True),
                lambda r: r['account'].update(initial_capital_paise=True),
                lambda r: r.update(unknown=True),
            ):
                changed = json.loads(json.dumps(original))
                mutate(changed)
                path.write_text(json.dumps(changed))
                with self.assertRaises(ValueError):
                    PortfolioSpec.load(path)


class PortfolioStreamTests(unittest.TestCase):
    @contextlib.contextmanager
    def session(self, root, *, resume=False):
        spec = PortfolioSpec.load(CONFIG)
        output = Path(root) / 'session'
        manifest = spec.validate_bundle(output) if resume else spec.create_bundle(output)
        with PaperPortfolioEngine(LIBRARY, output / 'portfolio.conf') as engine:
            session = PortfolioSession(engine, spec, output, manifest, resume=resume)
            try:
                if not resume:
                    session.suspend(0, 'startup')
                yield session, engine, output
            finally:
                session.close()  # No durable shutdown: emulate process loss for recovery tests.

    def prime(self, session):
        session.accept(1, Quote('RELIANCE.NS', 10000, 1000, market_open=True), 0, 1060)
        self.assertFalse(session.engine.snapshot()['execution_enabled'])
        session.accept(2, Quote('TCS.NS', 20000, 1000, market_open=True), 1, 1060)
        self.assertTrue(session.engine.snapshot()['execution_enabled'])

    def test_account_pause_requires_a_new_source_bar_from_every_symbol(self):
        with tempfile.TemporaryDirectory() as temporary, self.session(temporary) as (session, engine, output):
            self.prime(session)
            session.accept(1, Quote('RELIANCE.NS', 10100, 1060, market_open=True), 10_000_000, 1120)
            self.assertEqual(engine.snapshot()['open_orders'], 1)
            session.suspend(20_000_000, 'test outage')
            self.assertEqual(engine.snapshot()['open_orders'], 0)
            session.accept(2, Quote('TCS.NS', 20100, 1060, market_open=True), 30_000_000, 1120)
            self.assertFalse(engine.snapshot()['execution_enabled'])
            self.assertFalse(session.accept(1, Quote('RELIANCE.NS', 10100, 1060, market_open=True),
                                            40_000_000, 1120))
            self.assertFalse(engine.snapshot()['execution_enabled'])
            self.assertEqual(engine.snapshot()['open_orders'], 0)
            session.accept(1, Quote('RELIANCE.NS', 10200, 1120, market_open=True), 50_000_000, 1180)
            self.assertTrue(engine.snapshot()['execution_enabled'])
            self.assertEqual(engine.snapshot()['open_orders'], 0)  # Resume itself cannot generate orders.
            session.accept(2, Quote('TCS.NS', 20200, 1120, market_open=True), 60_000_000, 1180)
            session.accept(2, Quote('TCS.NS', 20195, 1180, market_open=True), 80_000_000, 1240)
            state = engine.snapshot()
            config = read_config((output / 'configs/2.conf').read_bytes())
            rates = {key: setting(config, key) for key in FEE_KEYS}
            expected_fee = fee_total(20197, 'buy', rates)
            self.assertEqual(state['fees_paise'], expected_fee)
            self.assertEqual(state['cash_paise'], 10_000_000 - 20197 - expected_fee)
            self.assertEqual([member['position'] for member in state['instruments']], [0, 1])
            self.assertEqual(session.fills, 1)

    def test_prepared_tick_torn_tail_and_recovery_cancel_every_pending_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            original_wall = time.time_ns() - 1_000_000_000
            with self.session(temporary) as (session, engine, output):
                self.prime(session)
                session.accept(1, Quote('RELIANCE.NS', 10100, 1060, market_open=True), 10_000_000, 1120)
                fields = session._quote_fields(2, Quote('TCS.NS', 20100, 1060, market_open=True),
                                                20_000_000, 1120)
                session.journal.append_operation(dict(kind='operation', id=session.next_id, op='tick',
                    prepared_wall_time_ns=original_wall, **fields))
            with (output / 'session.jsonl').open('ab') as stream:
                stream.write(b'{"kind":"checkpoint"')
            with self.session(temporary, resume=True) as (session, engine, output):
                self.assertEqual(session.last_commit_wall_time_ns, original_wall)
                self.assertEqual(engine.snapshot()['open_orders'], 2)
                session.recover(original_wall + 10_000_000)
                self.assertEqual(engine.snapshot()['open_orders'], 0)
                self.assertFalse(engine.snapshot()['execution_enabled'])
                self.assertEqual(session.pending, {1, 2})
                audit = (output / 'audit.csv').read_text()
                self.assertEqual(audit.count('accepted'), 2)
                self.assertEqual(audit.count('cancelled'), 2)

    def test_old_prepared_intent_cannot_hide_restart_downtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            old_wall = time.time_ns() - 600_000_000_000
            with self.session(temporary) as (session, engine, output):
                self.prime(session)
                fields = session._quote_fields(1, Quote('RELIANCE.NS', 10100, 1060, market_open=True),
                                                10_000_000, 1120)
                session.journal.append_operation(dict(kind='operation', id=session.next_id, op='tick',
                    prepared_wall_time_ns=old_wall, **fields))
            with self.session(temporary, resume=True) as (session, engine, output):
                session.recover(time.time_ns())
                self.assertTrue(engine.snapshot()['killed'])
                self.assertEqual(engine.snapshot()['kill_reason'], 4)
                self.assertEqual(engine.snapshot()['open_orders'], 0)
                with self.assertRaisesRegex(RuntimeError, 'halted'):
                    session.accept(2, Quote('TCS.NS', 20100, 1060, market_open=True),
                                   session.last_engine_time + 1, 1120)

    def test_mismatched_shared_ledger_preserves_existing_audit_and_refuses_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.session(temporary) as (session, engine, output):
                self.prime(session)
            original_audit = (output / 'audit.csv').read_bytes()
            lines = (output / 'session.jsonl').read_text().splitlines()
            checkpoint = json.loads(lines[-1])
            checkpoint['snapshot']['cash_paise'] += 1
            lines[-1] = json.dumps(checkpoint)
            (output / 'session.jsonl').write_text('\n'.join(lines) + '\n')
            with self.assertRaisesRegex(RuntimeError, 'reconciliation failed'):
                with self.session(temporary, resume=True):
                    pass
            self.assertEqual((output / 'audit.csv').read_bytes(), original_audit)

    def test_undurable_healthy_heartbeats_disconnect_and_clock_regression_replay_safely(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.session(temporary) as (session, engine, output):
                self.prime(session)
                session.heartbeat(50_000_000)
                session.disconnect(60_000_000, 'test disconnected')
            with self.session(temporary, resume=True) as (session, engine, output):
                self.assertTrue(engine.snapshot()['killed'])
                self.assertEqual(engine.snapshot()['kill_reason'], 3)
                self.assertEqual(session.error, 'test disconnected')
        with tempfile.TemporaryDirectory() as temporary:
            with self.session(temporary) as (session, engine, output):
                self.prime(session)
            with self.session(temporary, resume=True) as (session, engine, output):
                session.recover(session.last_commit_wall_time_ns - 1)
                self.assertTrue(engine.snapshot()['killed'])
                self.assertIn('clock regressed', session.error)

    def test_eight_simulated_hours_restore_all_symbols_and_one_account(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.session(temporary) as (session, engine, output):
                for index in range(480):
                    source = 1000 + index * 60
                    session.accept(1, Quote('RELIANCE.NS', 10000 + index % 3, source, market_open=True),
                                   index * 60_000_000_000, source + 60)
                    session.accept(2, Quote('TCS.NS', 20000 + index % 3, source, market_open=True),
                                   index * 60_000_000_000 + 1, source + 60)
                before = engine.snapshot()
                self.assertEqual(session.quotes, 960)
            with self.session(temporary, resume=True) as (session, engine, output):
                self.assertEqual(engine.snapshot(), before)
                self.assertEqual(session.quotes, 960)
                session.recover(time.time_ns())
                self.assertFalse(engine.snapshot()['execution_enabled'])
                self.assertEqual(engine.snapshot()['cash_paise'], 10_000_000)
                self.assertEqual(engine.snapshot()['open_orders'], 0)

    def test_native_wrapper_enforces_owner_and_invalid_input_halts_account(self):
        with tempfile.TemporaryDirectory() as temporary, self.session(temporary) as (session, engine, output):
            with ThreadPoolExecutor(max_workers=1) as pool:
                with self.assertRaisesRegex(RuntimeError, 'owning thread'):
                    pool.submit(engine.snapshot).result()
            with self.assertRaises(ValueError):
                engine.tick(1, 0, 1, 100, 101, True)
            self.assertTrue(engine.snapshot()['killed'])

    def args(self, root):
        return argparse.Namespace(config=CONFIG, library=LIBRARY, poll_seconds=15, duration=5,
                                  max_feed_retries=3, output_dir=Path(root), resume=None)

    def test_scheduler_transient_failure_and_closed_market_stop_cleanly(self):
        class Feed:
            def __init__(self, symbol, fail=False, opened=True):
                self.symbol, self.fail, self.opened, self.calls = symbol, fail, opened, 0
            def fetch(self):
                self.calls += 1
                if self.fail and self.calls == 1:
                    raise QuoteFeedUnavailable('offline fixture timeout')
                return Quote(self.symbol, 10000, int(time.time()) - 60, market_open=self.opened)
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            feeds = {1: Feed('RELIANCE.NS', fail=True, opened=False), 2: Feed('TCS.NS')}
            code = asyncio.run(run_portfolio(self.args(temporary), feeds,
                                             retry_policy=RetryPolicy(base_delay_s=0.001)))
            self.assertEqual(code, 0)
            output = next(Path(temporary).iterdir())
            report = json.loads((output / 'summary.json').read_text())
            health = json.loads((output / 'health.json').read_text())
            self.assertFalse(report['risk_halted'])
            self.assertEqual(report['stop_reason'], 'market_closed')
            self.assertEqual(report['open_orders'], 0)
            self.assertEqual(report['orders_accepted'], 0)
            self.assertEqual(health['sources']['RELIANCE.NS']['failures'], 1)
            self.assertEqual(health['status'], 'stopped')
            self.assertEqual(json.loads((output / 'session.json').read_text())['status'], 'finished')

    def test_retry_exhaustion_and_invalid_payloads_halt_the_whole_account(self):
        class Feed:
            def __init__(self, symbol, error=None):
                self.symbol, self.error, self.calls = symbol, error, 0
            def fetch(self):
                self.calls += 1
                if self.error:
                    raise self.error
                return Quote(self.symbol, 20000, int(time.time()) - 60, market_open=True)
        for error, count in ((QuoteFeedUnavailable('offline outage'), 2),
                             (QuoteFeedError('offline invalid payload'), 1)):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as temporary, \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                feeds = {1: Feed('RELIANCE.NS', error), 2: Feed('TCS.NS')}
                code = asyncio.run(run_portfolio(self.args(temporary), feeds,
                    retry_policy=RetryPolicy(max_retries=1, base_delay_s=0.001)))
                output = next(Path(temporary).iterdir())
                report = json.loads((output / 'summary.json').read_text())
                health = json.loads((output / 'health.json').read_text())
                self.assertEqual(code, 3)
                self.assertEqual(feeds[1].calls, count)
                self.assertTrue(report['risk_halted'])
                self.assertEqual(report['open_orders'], 0)
                self.assertEqual(health['status'], 'halted')
                self.assertEqual(health['sources']['RELIANCE.NS']['validation_failures'], int(count == 1))

    def test_monitoring_failure_prevents_fetch_and_cancels_account(self):
        class Feed:
            def __init__(self, symbol): self.symbol, self.calls = symbol, 0
            def fetch(self):
                self.calls += 1
                raise AssertionError('No request should start after monitoring failure')
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()), \
                patch('live.portfolio_run.PortfolioMonitor.flush', side_effect=OSError('offline disk failure')):
            feeds = {1: Feed('RELIANCE.NS'), 2: Feed('TCS.NS')}
            self.assertEqual(asyncio.run(run_portfolio(self.args(temporary), feeds)), 3)
            report = json.loads(next(Path(temporary).glob('*/summary.json')).read_text())
            self.assertTrue(report['risk_halted'])
            self.assertEqual(report['open_orders'], 0)
            self.assertEqual(sum(feed.calls for feed in feeds.values()), 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
