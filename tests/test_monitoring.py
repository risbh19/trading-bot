"""Read-only monitoring snapshots and loopback HTTP integration, without a native build."""

import copy
from http.client import HTTPConnection
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from live.monitoring import SessionExporter, make_server
from live import monitoring
from live import recovery
from live.recovery import atomic_json


def single_records():
    manifest = dict(format_version=1, paper_api_version=2, session_id='session-one',
                    symbol='TEST.NS', status='running', resume_count=0)
    health = dict(schema_version=1, mode='paper', counters_scope='current_process',
                  session_id='session-one', symbol='TEST.NS', resume_count=0,
                  started_utc_s=900.0, updated_utc_s=995.0, status='healthy',
                  consecutive_failures=0, next_retry_utc_s=None, last_error=None,
                  last_source_time_s=940, last_response_utc_s=995,
                  source_age_s=55, fetch_attempts=12, responses=11,
                  accepted_quotes=7, duplicate_quotes=4, transient_failures=2,
                  scheduled_retries=2, recoveries=1, validation_failures=0,
                  position=2, pnl_paise=-125, open_orders=1, engine_killed=False)
    return manifest, health


def portfolio_records():
    members = [dict(instrument=1, symbol='FIRST.NS'), dict(instrument=2, symbol='SECOND.NS')]
    manifest = dict(format_version=1, kind='paper_portfolio', portfolio_api_version=1,
                    session_id='portfolio-one', status='running', resume_count=0,
                    identity=dict(members=members))
    account = dict(initial_capital_paise=10_000_000, cash_paise=9_980_000,
                   equity_paise=10_000_125, pnl_paise=125, fees_paise=25,
                   gross_notional_paise=20_125, reserved_cash_paise=10_000,
                   open_orders=1, killed=0, audit_failed=0, kill_reason=0,
                   instrument_count=2, ready=1, execution_enabled=1,
                   instruments=[])
    sources = {}
    for member in members:
        sources[member['symbol']] = dict(member, status='healthy', fetch_attempts=10,
            responses=9, accepted=6, duplicates=3, failures=1, retries=1,
            validation_failures=0, consecutive_failures=0, last_error=None,
            next_retry_utc_s=None, source_time_utc_s=940 + member['instrument'],
            source_age_s=54)
    health = dict(schema_version=1, mode='paper', counters_scope='current_process',
                  started_utc_s=900.0, updated_utc_s=995.0, session_id='portfolio-one',
                  resume_count=0, status='healthy', account=account, sources=sources,
                  waiting_for_fresh_quotes=[], stop_reason='running', error=None)
    return manifest, health


def samples(text):
    """Extract only emitted samples, leaving HELP/TYPE documentation out of checks."""
    return {line.split()[0]: float(line.split()[1]) for line in text.splitlines()
            if line and not line.startswith('#')}


class MonitoringTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.now = 1000.0

    def write(self, manifest, health):
        (self.root / 'session.json').write_text(json.dumps(manifest), encoding='utf-8')
        (self.root / 'health.json').write_text(json.dumps(health), encoding='utf-8')

    def exporter(self, manifest=None, health=None, **kwargs):
        if manifest is None:
            manifest, health = single_records()
        self.write(manifest, health)
        return SessionExporter(self.root, clock=lambda: self.now, **kwargs)

    def test_single_snapshot_and_money_units(self):
        exporter = self.exporter()
        result = exporter.snapshot()
        self.assertTrue(result.valid)
        self.assertTrue(result.fresh)
        self.assertTrue(result.healthy)
        self.assertEqual(result.state, 'healthy')
        self.assertEqual(result.age, 5)
        values = samples(exporter.render_metrics())
        self.assertEqual(values['trading_paper_snapshot_valid'], 1)
        self.assertEqual(values['trading_paper_pnl_inr'], -1.25)
        self.assertEqual(values['trading_paper_open_orders'], 1)
        self.assertEqual(values['trading_paper_source_age_seconds{symbol="TEST.NS"}'], 60)
        self.assertEqual(values['trading_paper_feed_accepted_quotes_total{symbol="TEST.NS"}'], 7)
        self.assertEqual(values['trading_paper_feed_failures_total{symbol="TEST.NS"}'], 2)
        states = {key: value for key, value in values.items()
                  if key.startswith('trading_paper_state{')}
        self.assertEqual(len(states), 7)
        self.assertEqual(sum(states.values()), 1)
        self.assertEqual(states['trading_paper_state{state="healthy"}'], 1)

    def test_portfolio_universe_and_shared_account_metrics(self):
        exporter = self.exporter(*portfolio_records())
        self.assertTrue(exporter.snapshot().healthy)
        values = samples(exporter.render_metrics())
        expected = dict(cash_inr=99_800, equity_inr=100_001.25, pnl_inr=1.25,
                        fees_inr=.25, gross_notional_inr=201.25,
                        reserved_cash_inr=100, execution_enabled=1)
        for name, value in expected.items():
            self.assertEqual(values['trading_paper_' + name], value)
        self.assertEqual(values['trading_paper_source_age_seconds{symbol="FIRST.NS"}'], 59)
        self.assertEqual(values['trading_paper_source_age_seconds{symbol="SECOND.NS"}'], 58)
        for symbol in ('FIRST.NS', 'SECOND.NS'):
            self.assertEqual(values[f'trading_paper_feed_responses_total{{symbol="{symbol}"}}'], 9)

    def test_stale_snapshot_keeps_diagnostics_and_omits_actionable_values(self):
        exporter = self.exporter(max_age=20)
        self.assertTrue(exporter.snapshot().healthy)
        self.now = 1016
        result = exporter.snapshot()
        self.assertTrue(result.valid)
        self.assertFalse(result.fresh)
        self.assertFalse(result.healthy)
        self.assertEqual(result.age, 21)
        values = samples(exporter.render_metrics())
        self.assertEqual(values['trading_paper_snapshot_valid'], 1)
        self.assertEqual(values['trading_paper_snapshot_fresh'], 0)
        self.assertEqual(values['trading_paper_healthy'], 0)
        self.assertEqual(values['trading_paper_snapshot_age_seconds'], 21)
        self.assertNotIn('trading_paper_pnl_inr', values)
        self.assertFalse(any('feed_' in key for key in values))

    def test_feed_age_advances_without_file_updates_and_missing_quote_is_not_zero(self):
        manifest, health = single_records()
        exporter = self.exporter(manifest, health)
        self.assertEqual(samples(exporter.render_metrics())[
            'trading_paper_source_age_seconds{symbol="TEST.NS"}'], 60)
        self.now += 3
        self.assertEqual(samples(exporter.render_metrics())[
            'trading_paper_source_age_seconds{symbol="TEST.NS"}'], 63)
        health['last_source_time_s'] = None
        health['source_age_s'] = None
        self.write(manifest, health)
        self.assertNotIn('trading_paper_source_age_seconds{symbol="TEST.NS"}',
                         samples(exporter.render_metrics()))

    def test_clean_terminal_stop_is_distinct_from_halted_or_healthy(self):
        manifest, health = single_records()
        manifest['status'] = 'finished'
        health.update(status='stopped', engine_killed=True, risk_halted=False, open_orders=0)
        exporter = self.exporter(manifest, health)
        result = exporter.snapshot()
        self.assertTrue(result.valid)
        self.assertEqual(result.state, 'stopped')
        self.assertFalse(result.healthy)
        values = samples(exporter.render_metrics())
        self.assertEqual(values['trading_paper_running'], 0)
        self.assertEqual(values['trading_paper_halted'], 0)
        health.update(status='halted', risk_halted=True)
        self.write(manifest, health)
        self.assertEqual(exporter.snapshot().state, 'halted')
        self.assertEqual(samples(exporter.render_metrics())['trading_paper_halted'], 1)
        health['status'] = 'healthy'
        self.write(manifest, health)
        self.assertFalse(exporter.snapshot().valid)

    def test_pause_and_retry_statuses_are_not_ready(self):
        manifest, health = single_records()
        for status, canonical in (('starting', 'starting'), ('retrying', 'retrying'),
                                  ('awaiting_fresh_quote', 'waiting')):
            with self.subTest(status=status):
                health['status'] = status
                self.write(manifest, health)
                result = SessionExporter(self.root, clock=lambda: self.now).snapshot()
                self.assertTrue(result.valid)
                self.assertFalse(result.healthy)
                self.assertEqual(result.state, canonical)
        manifest, health = portfolio_records()
        health['status'] = 'waiting_for_fresh_quotes'
        health['account']['execution_enabled'] = 0
        self.assertEqual(self.exporter(manifest, health).snapshot().state, 'waiting')

    def test_unreadable_or_replaced_identity_never_returns_cached_health(self):
        manifest, health = single_records()
        exporter = self.exporter(manifest, health)
        self.assertTrue(exporter.snapshot().healthy)
        (self.root / 'health.json').unlink()
        result = exporter.snapshot()
        self.assertFalse(result.valid)
        self.assertFalse(result.healthy)
        self.assertIsNone(result.data)
        self.assertEqual(result.state, 'unavailable')
        values = samples(exporter.render_metrics())
        self.assertEqual(values['trading_paper_snapshot_valid'], 0)
        self.assertNotIn('trading_paper_pnl_inr', values)
        manifest['session_id'] = health['session_id'] = 'different-session'
        self.write(manifest, health)
        self.assertFalse(exporter.snapshot().valid)

    def test_resume_generation_must_match_and_legacy_zero_remains_readable(self):
        manifest, health = single_records()
        del health['resume_count']
        exporter = self.exporter(manifest, health)
        self.assertTrue(exporter.snapshot().valid)
        manifest['resume_count'] = 1
        self.write(manifest, health)
        self.assertFalse(exporter.snapshot().valid)
        health['resume_count'] = 1
        health['started_utc_s'] = 994
        self.write(manifest, health)
        self.assertTrue(exporter.snapshot().valid)

    def test_universe_is_pinned_and_instrument_mapping_must_match(self):
        manifest, health = portfolio_records()
        exporter = self.exporter(manifest, health)
        self.assertTrue(exporter.snapshot().valid)
        health['sources']['FIRST.NS']['instrument'] = 2
        self.write(manifest, health)
        self.assertFalse(exporter.snapshot().valid)
        manifest, health = portfolio_records()
        manifest['identity']['members'][0]['symbol'] = 'CHANGED.NS'
        health['sources']['CHANGED.NS'] = health['sources'].pop('FIRST.NS')
        health['sources']['CHANGED.NS']['symbol'] = 'CHANGED.NS'
        self.write(manifest, health)
        self.assertFalse(exporter.snapshot().valid)

    def test_maximum_source_universe_is_bounded(self):
        for count in (32, 33):
            with self.subTest(count=count):
                manifest, health = portfolio_records()
                source = health['sources']['FIRST.NS']
                members, sources = [], {}
                for instrument in range(1, count + 1):
                    member = dict(instrument=instrument, symbol=f'S{instrument}.NS')
                    members.append(member)
                    sources[member['symbol']] = dict(copy.deepcopy(source), **member)
                manifest['identity']['members'] = members
                health['sources'] = sources
                health['account']['instrument_count'] = count
                self.assertEqual(self.exporter(manifest, health).snapshot().valid, count == 32)

    def test_manifest_replacement_retries_once_and_repeated_races_are_bounded(self):
        manifest, health = single_records()
        exporter = self.exporter(manifest, health)
        original_read = monitoring._read
        calls = []

        def finish_after_first_manifest(path):
            result = original_read(path)
            calls.append(path.name)
            if len(calls) == 1:
                manifest['status'] = 'finished'
                health.update(status='stopped', engine_killed=True, open_orders=0)
                self.write(manifest, health)
            return result

        with patch('live.monitoring._read', side_effect=finish_after_first_manifest):
            result = exporter.snapshot()
        self.assertTrue(result.valid)
        self.assertEqual(result.state, 'stopped')
        self.assertEqual(len(calls), 6)
        calls.clear()

        def repeatedly_replace_manifest(path):
            result = original_read(path)
            calls.append(path.name)
            if path.name == 'session.json':
                manifest['race_marker'] = len(calls)
                self.write(manifest, health)
            return result

        with patch('live.monitoring._read', side_effect=repeatedly_replace_manifest):
            result = exporter.snapshot()
        self.assertFalse(result.valid)
        self.assertEqual(len(calls), 6)

    def test_report_reader_does_not_block_atomic_replacement(self):
        manifest, health = single_records()
        self.write(manifest, health)
        path = self.root / 'health.json'
        original_bytes = path.read_bytes()
        replacement = dict(health, status='retrying', updated_utc_s=996)
        original_fstat = monitoring.os.fstat
        original_replace = recovery.os.replace
        replaced = []
        blocked = threading.Event()
        workers, errors = [], []

        def track_replace(source, target):
            try:
                return original_replace(source, target)
            except PermissionError:
                blocked.set()
                raise

        def replace_in_worker():
            try:
                atomic_json(path, replacement)
                replaced.append(True)
            except BaseException as error:
                errors.append(error)

        def replace_after_open(fd):
            # Hold the reader until a real Windows replace attempt has failed.
            # Returning lets _read close it, so the writer's bounded retry works.
            if monitoring.os.name == 'nt':
                worker = threading.Thread(target=replace_in_worker, daemon=True)
                workers.append(worker)
                worker.start()
                self.assertTrue(blocked.wait(timeout=3), 'Expected Windows target-open conflict')
            else:
                replace_in_worker()
            return original_fstat(fd)

        with patch('live.monitoring.os.fstat', side_effect=replace_after_open), \
                patch('live.recovery.os.replace', side_effect=track_replace):
            raw, observed = monitoring._read(path)
            for worker in workers:
                worker.join(timeout=3)
                self.assertFalse(worker.is_alive(), 'Replacement did not finish after reader closed')
        self.assertEqual(errors, [])
        self.assertEqual(replaced, [True])
        self.assertEqual(raw, original_bytes)
        self.assertEqual(observed, health)
        self.assertEqual(json.loads(path.read_text(encoding='utf-8')), replacement)

    def test_windows_report_retry_is_bounded_and_preserves_complete_files(self):
        manifest, health = single_records()
        self.write(manifest, health)
        path = self.root / 'health.json'
        original = path.read_bytes()
        replacement = dict(health, status='retrying')
        error = PermissionError('simulated sharing conflict')
        error.winerror = 32
        with patch('live.recovery.os.name', 'nt'), \
                patch('live.recovery.os.replace', side_effect=error) as replace, \
                patch('live.recovery.time.sleep') as sleep:
            with self.assertRaises(PermissionError):
                atomic_json(path, replacement)
        self.assertEqual(replace.call_count, 6)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [.005, .01, .02, .04, .08])
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(json.loads(path.with_name('health.json.tmp').read_text(encoding='utf-8')),
                         replacement)

    def test_report_retry_succeeds_and_other_errors_are_immediate(self):
        source, target = self.root / 'source', self.root / 'target'
        conflict = PermissionError('simulated target-open conflict')
        conflict.winerror = 5
        with patch('live.recovery.os.name', 'nt'), \
                patch('live.recovery.os.replace', side_effect=[conflict, conflict, None]) as replace, \
                patch('live.recovery.time.sleep') as sleep:
            recovery._replace_report(source, target)
        self.assertEqual(replace.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [.005, .01])
        for platform, winerror in (('nt', 123), ('posix', 5)):
            with self.subTest(platform=platform, winerror=winerror):
                error = PermissionError('not an eligible sharing conflict')
                error.winerror = winerror
                with patch('live.recovery.os.name', platform), \
                        patch('live.recovery.os.replace', side_effect=error) as replace, \
                        patch('live.recovery.time.sleep') as sleep:
                    with self.assertRaises(PermissionError):
                        recovery._replace_report(source, target)
                self.assertEqual(replace.call_count, 1)
                sleep.assert_not_called()

    def test_configured_labels_are_escaped_and_control_characters_are_rejected(self):
        manifest, health = single_records()
        symbol = 'TEST"\\NS'
        manifest['symbol'] = health['symbol'] = symbol
        exporter = self.exporter(manifest, health)
        self.assertTrue(exporter.snapshot().valid)
        self.assertIn('symbol="TEST\\"\\\\NS"', exporter.render_metrics())
        manifest['symbol'] = health['symbol'] = 'TEST\nINJECTED.NS'
        self.assertFalse(self.exporter(manifest, health).snapshot().valid)
        manifest['symbol'] = health['symbol'] = 'TEST' + chr(0xd800) + '.NS'
        invalid = self.exporter(manifest, health)
        self.assertFalse(invalid.snapshot().valid)
        self.assertIn('trading_paper_snapshot_valid 0', invalid.render_metrics())
        invalid.render_metrics().encode('utf-8')

    def test_invalid_schemas_numbers_and_time_relationships_fail_closed(self):
        mutations = (
            lambda m, h: h.update(schema_version=True),
            lambda m, h: h.update(mode='live'),
            lambda m, h: h.update(counters_scope='session'),
            lambda m, h: h.update(session_id='unmatched'),
            lambda m, h: h.update(symbol='OTHER.NS'),
            lambda m, h: h.update(fetch_attempts=True),
            lambda m, h: h.update(responses=-1),
            lambda m, h: h.update(pnl_paise=float('nan')),
            lambda m, h: h.update(updated_utc_s=float('inf')),
            lambda m, h: h.update(updated_utc_s=None),
            lambda m, h: h.update(started_utc_s=996),
            lambda m, h: h.update(updated_utc_s=1001),
            lambda m, h: h.update(last_source_time_s=996),
            lambda m, h: m.update(resume_count=True),
            lambda m, h: m.update(format_version=True),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                manifest, health = single_records()
                mutate(manifest, health)
                self.assertFalse(self.exporter(manifest, health).snapshot().valid)

    def test_backward_scrape_clock_is_invalid_until_time_catches_up(self):
        exporter = self.exporter()
        self.assertTrue(exporter.snapshot().valid)
        self.now = 1001
        self.assertTrue(exporter.snapshot().valid)
        self.now = 999
        self.assertFalse(exporter.snapshot().valid)
        self.now = 1000
        self.assertFalse(exporter.snapshot().valid)
        self.now = 1001
        self.assertTrue(exporter.snapshot().valid)

    def test_strict_bounded_json_and_diagnostics_do_not_leak(self):
        manifest, health = single_records()
        health['last_error'] = 'secret-error-C:/private/session'
        exporter = self.exporter(manifest, health)
        text = exporter.render_metrics()
        self.assertNotIn('session-one', text)
        self.assertNotIn('secret-error', text)
        self.assertNotIn(str(self.root), text)
        path = self.root / 'health.json'
        for payload in ('{', '[]', '{"schema_version":1,"schema_version":1}',
                        ' ' * (1024 * 1024 + 1)):
            with self.subTest(length=len(payload)):
                path.write_text(payload, encoding='utf-8')
                result = exporter.snapshot()
                self.assertFalse(result.valid)
                self.assertFalse(result.healthy)

    def test_bad_configuration_is_rejected(self):
        for max_age in (0, -1, True, float('nan'), float('inf')):
            with self.subTest(max_age=max_age), self.assertRaises(ValueError):
                SessionExporter(self.root, max_age=max_age, clock=lambda: self.now)

    def test_loopback_http_routes_readiness_and_read_only_behavior(self):
        exporter = self.exporter()
        before = {path.name: path.read_bytes() for path in self.root.iterdir()}
        server = make_server(exporter, port=0)
        self.assertEqual(server.server_address[0], '127.0.0.1')
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()

        def request(method, path):
            connection = HTTPConnection(*server.server_address, timeout=3)
            try:
                connection.request(method, path)
                response = connection.getresponse()
                return response.status, response.getheader('Content-Type'), response.read()
            finally:
                connection.close()

        try:
            status, content_type, body = request('GET', '/metrics')
            self.assertEqual(status, 200)
            self.assertIn('text/plain', content_type)
            self.assertIn(b'trading_paper_snapshot_valid 1', body)
            self.assertEqual(request('GET', '/healthz')[0], 200)
            self.assertEqual(request('GET', '/readyz')[0], 200)
            status, _, body = request('HEAD', '/metrics')
            self.assertEqual(status, 200)
            self.assertEqual(body, b'')
            for method in ('POST', 'PUT', 'PATCH', 'DELETE'):
                self.assertEqual(request(method, '/metrics')[0], 405)
            for path in ('/', '/session.json', '/../session.json', '/metrics?path=session.json'):
                self.assertEqual(request('GET', path)[0], 404)
            self.now = 1020
            self.assertEqual(request('GET', '/healthz')[0], 200)
            self.assertEqual(request('GET', '/readyz')[0], 503)
            (self.root / 'health.json').unlink()
            self.assertEqual(request('GET', '/metrics')[0], 200)
            self.assertEqual(request('GET', '/readyz')[0], 503)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)
        # Only the test's own removal changed session state; HTTP requests never write it.
        self.assertEqual((self.root / 'session.json').read_bytes(), before['session.json'])
        self.assertFalse((self.root / 'health.json').exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
