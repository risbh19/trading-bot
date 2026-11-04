"""Offline dashboard views and the read-only loopback HTTP boundary."""

from http.client import HTTPConnection
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
from live.monitoring import SessionExporter, make_server
from test_monitoring import single_records, portfolio_records


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name)
        self.now = 1000.0

    def write(self, manifest, health):
        (self.output / 'session.json').write_text(json.dumps(manifest), encoding='utf-8')
        (self.output / 'health.json').write_text(json.dumps(health), encoding='utf-8')

    def exporter(self, records=None):
        manifest, health = records or single_records()
        self.write(manifest, health)
        return SessionExporter(self.output, clock=lambda: self.now)

    def server(self, exporter):
        server = make_server(exporter, port=0)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()

        def close():
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)
            self.assertFalse(worker.is_alive())

        self.addCleanup(close)
        return server

    def request(self, server, path, method='GET', headers=None):
        connection = HTTPConnection(*server.server_address, timeout=3)
        try:
            connection.request(method, path, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_single_snapshot_uses_reported_values_without_inventing_balances(self):
        view = self.exporter().dashboard_snapshot()
        self.assertEqual(view['schema_version'], 1)
        self.assertEqual(view['mode'], 'paper')
        self.assertTrue(view['valid'])
        self.assertTrue(view['fresh'])
        self.assertTrue(view['healthy'])
        self.assertEqual(view['state'], 'healthy')
        self.assertEqual(view['account'], dict(pnl_inr=-1.25, open_orders=1))
        self.assertIsNone(view['execution_enabled'])
        self.assertEqual(view['report_age_seconds'], 5)
        self.assertEqual(view['max_age_seconds'], 20)
        self.assertEqual(view['process_started_utc_s'], 900)
        self.assertEqual(view['updated_utc_s'], 995)
        self.assertEqual(len(view['sources']), 1)
        source = view['sources'][0]
        self.assertEqual(source['symbol'], 'TEST.NS')
        self.assertEqual(source['state'], 'healthy')
        self.assertEqual(source['source_age_seconds'], 60)
        self.assertEqual(source['counters']['fetch_attempts'], 12)
        self.assertEqual(source['counters']['accepted_quotes'], 7)
        self.assertEqual(source['counters']['failures'], 2)

    def test_portfolio_snapshot_shares_one_account_and_bounded_sources(self):
        view = self.exporter(portfolio_records()).dashboard_snapshot()
        self.assertTrue(view['execution_enabled'])
        self.assertEqual(view['account']['equity_inr'], 100001.25)
        self.assertEqual(view['account']['cash_inr'], 99800)
        self.assertEqual(view['account']['pnl_inr'], 1.25)
        self.assertEqual(view['account']['fees_inr'], .25)
        self.assertEqual(view['account']['reserved_cash_inr'], 100)
        self.assertEqual([source['symbol'] for source in view['sources']], ['FIRST.NS', 'SECOND.NS'])
        self.assertEqual(view['sources'][0]['source_age_seconds'], 59)
        self.assertEqual(view['sources'][1]['counters']['accepted_quotes'], 6)
        self.assertNotIn('instruments', view['account'])

    def test_source_age_advances_and_missing_timestamp_is_unknown(self):
        manifest, health = single_records()
        exporter = self.exporter((manifest, health))
        self.now += 2
        self.assertEqual(exporter.dashboard_snapshot()['sources'][0]['source_age_seconds'], 62)
        health['last_source_time_s'] = None
        health['status'] = 'starting'
        self.write(manifest, health)
        self.assertIsNone(exporter.dashboard_snapshot()['sources'][0]['source_age_seconds'])

    def test_stale_report_clears_values_and_retains_diagnostic_source_age(self):
        exporter = self.exporter(portfolio_records())
        self.assertTrue(exporter.dashboard_snapshot()['healthy'])
        self.now = 1016
        view = exporter.dashboard_snapshot()
        self.assertTrue(view['valid'])
        self.assertFalse(view['fresh'])
        self.assertFalse(view['healthy'])
        self.assertEqual(view['state'], 'healthy')  # Last reported state, not liveness.
        self.assertEqual(view['report_age_seconds'], 21)
        self.assertIsNone(view['account'])
        self.assertIsNone(view['execution_enabled'])
        self.assertEqual(view['sources'][0]['source_age_seconds'], 75)
        self.assertTrue(all(source['counters'] is None for source in view['sources']))

    def test_missing_corrupt_and_changed_generation_never_keep_prior_values(self):
        manifest, health = portfolio_records()
        exporter = self.exporter((manifest, health))
        self.assertTrue(exporter.dashboard_snapshot()['healthy'])
        for cause in ('missing', 'corrupt', 'generation', 'identity'):
            with self.subTest(cause=cause):
                self.write(manifest, health)
                path = self.output / 'health.json'
                if cause == 'missing':
                    path.unlink()
                elif cause == 'corrupt':
                    path.write_text('{', encoding='utf-8')
                elif cause == 'generation':
                    self.write(dict(manifest, resume_count=1), health)
                else:
                    self.write(dict(manifest, session_id='another-account'),
                               dict(health, session_id='another-account'))
                view = exporter.dashboard_snapshot()
                self.assertFalse(view['valid'])
                self.assertFalse(view['fresh'])
                self.assertFalse(view['healthy'])
                self.assertEqual(view['state'], 'unavailable')
                self.assertIsNone(view['account'])
                self.assertIsNone(view['execution_enabled'])
                self.assertEqual(view['sources'], [])

    def test_waiting_stopped_and_halted_have_distinct_execution_states(self):
        for state, canonical, finished in (('waiting_for_fresh_quotes', 'waiting', False),
                                            ('stopped', 'stopped', True),
                                            ('halted', 'halted', True)):
            with self.subTest(state=state):
                manifest, health = portfolio_records()
                manifest['status'] = 'finished' if finished else 'running'
                health['status'] = state
                health['account'].update(execution_enabled=0, killed=int(finished))
                health['sources']['FIRST.NS']['status'] = 'waiting_for_fresh_quote'
                view = self.exporter((manifest, health)).dashboard_snapshot()
                self.assertTrue(view['valid'])
                self.assertTrue(view['fresh'])
                self.assertFalse(view['healthy'])
                self.assertEqual(view['state'], canonical)
                self.assertFalse(view['execution_enabled'])
                self.assertEqual(view['sources'][0]['state'], 'waiting')

    def test_api_is_an_allowlist_without_session_identifiers_paths_or_errors(self):
        manifest, health = portfolio_records()
        manifest['private_note'] = str(self.output)
        health.update(error='private-error-token')
        health['sources']['FIRST.NS']['last_error'] = 'private-source-error'
        health['account']['private_note'] = 'private-account-note'
        view = self.exporter((manifest, health)).dashboard_snapshot()
        serialized = json.dumps(view)
        for private in (str(self.output), 'portfolio-one', 'private-error-token',
                        'private-source-error', 'private-account-note', 'private_note',
                        'resume_count', 'identity', 'session_id', 'kill_reason'):
            self.assertNotIn(private, serialized)
        self.assertEqual(set(view), {'schema_version', 'mode', 'valid', 'fresh', 'healthy',
            'state', 'report_age_seconds', 'max_age_seconds', 'process_started_utc_s',
            'updated_utc_s', 'account', 'execution_enabled', 'sources'})
        self.assertTrue(all(set(source) == {'symbol', 'state', 'source_age_seconds', 'counters'}
                            for source in view['sources']))

    def test_http_serves_static_assets_json_and_security_headers(self):
        server = self.server(self.exporter())
        for path, mime in (('/', 'text/html'), ('/dashboard', 'text/html'),
                           ('/dashboard.css', 'text/css'),
                           ('/dashboard.js', 'application/javascript'),
                           ('/api/snapshot', 'application/json')):
            with self.subTest(path=path):
                status, headers, body = self.request(server, path)
                self.assertEqual(status, 200)
                self.assertIn(mime, headers['Content-Type'])
                self.assertEqual(headers['Cache-Control'], 'no-store')
                self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
                self.assertEqual(headers['Referrer-Policy'], 'no-referrer')
                policy = headers['Content-Security-Policy']
                for directive in ("default-src 'none'", "script-src 'self'",
                                  "style-src 'self'", "connect-src 'self'",
                                  "frame-ancestors 'none'", "object-src 'none'"):
                    self.assertIn(directive, policy)
                self.assertNotIn('Access-Control-Allow-Origin', headers)
                self.assertGreater(len(body), 0)
        status, _, body = self.request(server, '/api/snapshot')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['account']['pnl_inr'], -1.25)
        for path in ('/', '/dashboard.js', '/api/snapshot'):
            status, _, body = self.request(server, path, 'HEAD')
            self.assertEqual(status, 200)
            self.assertEqual(body, b'')

    def test_http_stale_and_invalid_json_clear_account_without_hiding_exporter_health(self):
        exporter = self.exporter()
        server = self.server(exporter)
        self.assertTrue(json.loads(self.request(server, '/api/snapshot')[2])['healthy'])
        self.now = 1016
        status, _, body = self.request(server, '/api/snapshot')
        self.assertEqual(status, 200)
        view = json.loads(body)
        self.assertFalse(view['fresh'])
        self.assertIsNone(view['account'])
        self.assertIsNone(view['sources'][0]['counters'])
        self.assertEqual(self.request(server, '/healthz')[0], 200)
        self.assertEqual(self.request(server, '/readyz')[0], 503)
        (self.output / 'health.json').unlink()
        status, _, body = self.request(server, '/api/snapshot')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['state'], 'unavailable')
        self.assertEqual(json.loads(body)['sources'], [])
        self.assertEqual(self.request(server, '/metrics')[0], 200)

    def test_http_symbol_text_is_json_data_and_never_injected_into_html(self):
        manifest, health = single_records()
        symbol = 'TEST"><img src=x onerror=alert(1)>'
        manifest['symbol'] = health['symbol'] = symbol
        server = self.server(self.exporter((manifest, health)))
        status, _, body = self.request(server, '/api/snapshot')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['sources'][0]['symbol'], symbol)
        self.assertNotIn(symbol.encode(), self.request(server, '/')[2])

    def test_host_and_origin_boundary_rejects_foreign_requests_before_reading_reports(self):
        exporter = self.exporter()
        server = self.server(exporter)
        port = server.server_port
        with patch.object(exporter, 'dashboard_snapshot', side_effect=AssertionError('Must not read')):
            for host in ('evil.example', f'127.0.0.1.evil:{port}', f'127.0.0.1:{port + 1}',
                         f'[::1]:{port}', f'127.0.0.1:{port}@evil.example'):
                with self.subTest(host=host):
                    self.assertEqual(self.request(server, '/api/snapshot', headers={'Host': host})[0], 400)
            for origin in ('https://evil.example', 'null', f'http://localhost:{port + 1}',
                           f'http://127.0.0.1.evil:{port}'):
                with self.subTest(origin=origin):
                    self.assertEqual(self.request(server, '/api/snapshot', headers={'Origin': origin})[0], 403)
        for host in (f'127.0.0.1:{port}', f'localhost:{port}'):
            with self.subTest(allowed_host=host):
                self.assertEqual(self.request(server, '/api/snapshot', headers={'Host': host})[0], 200)
        for origin in (f'http://127.0.0.1:{port}', f'http://localhost:{port}'):
            with self.subTest(allowed_origin=origin):
                self.assertEqual(self.request(server, '/api/snapshot', headers={'Origin': origin})[0], 200)
        for headers, expected in (
            ([], 400),
            ([('Host', f'127.0.0.1:{port}'), ('Host', 'evil.example')], 400),
            ([('Host', f'127.0.0.1:{port}'),
              ('Origin', f'http://127.0.0.1:{port}'), ('Origin', 'https://evil.example')], 403),
        ):
            with self.subTest(ambiguous_headers=headers):
                connection = HTTPConnection(*server.server_address, timeout=3)
                try:
                    connection.putrequest('GET', '/api/snapshot', skip_host=True)
                    for name, value in headers:
                        connection.putheader(name, value)
                    connection.endheaders()
                    response = connection.getresponse()
                    self.assertEqual(response.status, expected)
                    response.read()
                finally:
                    connection.close()

    def test_http_rejects_file_paths_queries_and_mutations_without_writing_reports(self):
        exporter = self.exporter()
        server = self.server(exporter)
        before = {path.name: path.read_bytes() for path in self.output.iterdir()}
        for path in ('/session.json', '/health.json', '/../../session.json',
                     '/api/snapshot?path=health.json', '/?session=elsewhere',
                     '/dashboard.js/../session.json'):
            with self.subTest(path=path):
                self.assertEqual(self.request(server, path)[0], 404)
        for method in ('POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'):
            with self.subTest(method=method):
                self.assertEqual(self.request(server, '/api/snapshot', method)[0], 405)
        after = {path.name: path.read_bytes() for path in self.output.iterdir()}
        self.assertEqual(after, before)


if __name__ == '__main__':
    unittest.main(verbosity=2)
