"""Read-only, loopback Prometheus exporter for one explicit paper-session directory."""

import argparse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import math
import os
from pathlib import Path
import stat
import sys
import time

MAX_FILE_BYTES = 1024 * 1024
STATES = ('starting', 'healthy', 'retrying', 'waiting', 'stopped', 'halted', 'unavailable')
COUNTERS = {
    'fetch_attempts': ('fetch_attempts', 'fetch_attempts'),
    'responses': ('responses', 'responses'),
    'accepted_quotes': ('accepted_quotes', 'accepted'),
    'duplicate_quotes': ('duplicate_quotes', 'duplicates'),
    'failures': ('transient_failures', 'failures'),
    'retries': ('scheduled_retries', 'retries'),
    'validation_failures': ('validation_failures', 'validation_failures'),
}
CONTENT_TYPE = 'text/plain; version=0.0.4; charset=utf-8'


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def _bad_constant(_):
    raise ValueError('Nonfinite JSON value')


def _open_report(path):
    if os.name != 'nt':
        return os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    # Ordinary CRT opens deny deletion/rename while held. The runner atomically
    # replaces these files: a monitoring read must not cause its write to fail.
    import ctypes
    from ctypes import wintypes
    import msvcrt

    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    create = kernel32.CreateFileW
    create.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    create.restype = wintypes.HANDLE
    close = kernel32.CloseHandle
    close.argtypes = (wintypes.HANDLE,)
    close.restype = wintypes.BOOL
    handle = create(str(path), 0x80000000, 0x00000007, None, 3, 0x00000080, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        close(handle)
        raise


def _read(path):
    # A bounded regular-file read; never consume journals or follow HTTP path input.
    fd = _open_report(path)
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('Expected a regular report file')
        raw = stream.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError('Report exceeds size bound')
    result = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs, parse_constant=_bad_constant)
    if not isinstance(result, dict):
        raise ValueError('Expected a JSON object')
    return raw, result


def _integer(value, *, minimum=0):
    if type(value) is not int or not minimum <= value <= 2**63 - 1:
        raise ValueError('Invalid integer')
    return value


def _number(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError('Invalid timestamp')
    return value


def _flag(value):
    if type(value) not in (int, bool) or value not in (0, 1):
        raise ValueError('Invalid flag')
    return bool(value)


def _symbol(value):
    # Symbols are bounded configured labels, not error text or per-session identifiers.
    if (not isinstance(value, str) or not 1 <= len(value) <= 128 or
            any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError('Invalid symbol')
    value.encode('utf-8')  # Escaped lone surrogates are legal JSON, not UTF-8 labels.
    return value


def _escape(value):
    return value.replace('\\', '\\\\').replace('\n', '\\n').replace('"', '\\"')


@dataclass(frozen=True)
class Observation:
    valid: bool
    fresh: bool
    healthy: bool
    state: str = 'unavailable'
    age: float | None = None
    data: dict | None = None


class SessionExporter:
    """No engine, network-feed, owner-lock or journal dependency; never changes reports."""

    def __init__(self, path: Path, *, max_age=20.0, clock=time.time):
        if (type(max_age) not in (int, float) or not math.isfinite(max_age) or
                not 5 <= max_age <= 300):
            raise ValueError('max-age must be finite and between 5 and 300 seconds')
        self.path, self.max_age, self.clock = Path(path).resolve(), max_age, clock
        self.identity = None
        self.last_now = None

    def _validate(self, manifest, health, now):
        if (type(manifest.get('format_version')) is not int or manifest['format_version'] != 1 or
                manifest.get('status') not in ('running', 'finished') or
                not isinstance(manifest.get('session_id'), str) or
                not 1 <= len(manifest['session_id']) <= 128 or
                health.get('session_id') != manifest['session_id'] or
                type(health.get('schema_version')) is not int or health['schema_version'] != 1 or
                health.get('mode') != 'paper' or health.get('counters_scope') != 'current_process'):
            raise ValueError('Incompatible report identity')
        resume = _integer(manifest.get('resume_count'))
        if _integer(health.get('resume_count', 0)) != resume:
            raise ValueError('Reports belong to different process generations')
        started, updated = _number(health.get('started_utc_s')), _number(health.get('updated_utc_s'))
        if not started <= updated <= now:
            raise ValueError('Invalid report clock')
        state = {'awaiting_fresh_quote': 'waiting', 'waiting_for_fresh_quotes': 'waiting'}.get(
            health.get('status'), health.get('status'))
        if state not in STATES[:-1] or (manifest['status'] == 'finished' and state not in ('stopped', 'halted')):
            raise ValueError('Invalid lifecycle state')
        portfolio = manifest.get('kind') == 'paper_portfolio'
        money_fields = {'pnl': 'pnl_paise'}
        if portfolio:
            identity = manifest.get('identity')
            members = identity.get('members') if isinstance(identity, dict) else None
            if not isinstance(members, list) or not 1 <= len(members) <= 32:
                raise ValueError('Invalid portfolio universe')
            universe = {}
            for member in members:
                if not isinstance(member, dict):
                    raise ValueError('Invalid member')
                symbol, instrument = _symbol(member.get('symbol')), _integer(member.get('instrument'), minimum=1)
                if symbol in universe or instrument in universe.values():
                    raise ValueError('Repeated member')
                universe[symbol] = instrument
            sources = health.get('sources')
            if not isinstance(sources, dict) or set(sources) != set(universe):
                raise ValueError('Mismatched sources')
            account = health.get('account')
            if not isinstance(account, dict) or _integer(account.get('instrument_count')) != len(universe):
                raise ValueError('Invalid account')
            killed, audit_failed = _flag(account.get('killed')), _flag(account.get('audit_failed'))
            enabled, ready = _flag(account.get('execution_enabled')), _flag(account.get('ready'))
            money_fields.update(cash='cash_paise', equity='equity_paise', fees='fees_paise',
                                gross_notional='gross_notional_paise', reserved_cash='reserved_cash_paise')
            source_time_key = 'source_time_utc_s'
        else:
            if manifest.get('kind') is not None:
                raise ValueError('Unsupported report kind')
            symbol = _symbol(manifest.get('symbol'))
            if health.get('symbol') != symbol:
                raise ValueError('Mismatched symbol')
            universe, sources, account = {symbol: None}, {symbol: health}, health
            killed, audit_failed = _flag(health.get('engine_killed')), False
            enabled, ready = state == 'healthy' and not killed, True
            source_time_key = 'last_source_time_s'
        key = json.dumps([manifest['session_id'], portfolio, universe,
                          manifest.get('identity') if portfolio else manifest.get('config_sha256')],
                         sort_keys=True, separators=(',', ':'))
        if self.identity is not None and self.identity != key:
            raise ValueError('Configured session identity changed')
        values = {name + '_inr': _integer(account.get(field),
                  minimum=0 if name in ('fees', 'gross_notional', 'reserved_cash') else -(2**63)) / 100
                  for name, field in money_fields.items()}
        values['open_orders'] = _integer(account.get('open_orders'))
        normalized_sources = {}
        for symbol, record in sources.items():
            if not isinstance(record, dict):
                raise ValueError('Invalid source')
            if portfolio and (record.get('symbol') != symbol or
                              _integer(record.get('instrument'), minimum=1) != universe[symbol]):
                raise ValueError('Invalid source identity')
            source_time = record.get(source_time_key)
            if source_time is not None and _number(source_time) > updated:
                raise ValueError('Future source time')
            counters = {name: _integer(record.get(fields[int(portfolio)]))
                        for name, fields in COUNTERS.items()}
            normalized_sources[symbol] = dict(counters=counters, time=source_time)
        healthy = state == 'healthy' and not killed and not audit_failed and enabled and ready
        if state == 'healthy' and not healthy:
            raise ValueError('Inconsistent healthy state')
        self.identity = key
        return state, updated, dict(started=started, values=values, sources=normalized_sources,
                                   execution_enabled=enabled, portfolio=portfolio)

    def snapshot(self):
        try:
            for _ in range(2):
                before, manifest = _read(self.path / 'session.json')
                _, health = _read(self.path / 'health.json')
                after, _ = _read(self.path / 'session.json')
                if before == after:
                    # Sample after reading: a concurrent report write can otherwise
                    # have a timestamp a few microseconds ahead of scrape start.
                    now = _number(self.clock())
                    if self.last_now is not None and now < self.last_now:
                        return Observation(False, False, False)
                    self.last_now = now
                    state, updated, data = self._validate(manifest, health, now)
                    data['now'] = now
                    age = now - updated
                    fresh = age <= self.max_age
                    return Observation(True, fresh, fresh and state == 'healthy', state, age, data)
            return Observation(False, False, False)
        except (OSError, ValueError, TypeError, OverflowError, RecursionError):
            return Observation(False, False, False)

    def render_metrics(self):
        observed = self.snapshot()
        lines = []

        def emit(name, help_text, value, labels=None, metric_type='gauge'):
            full = 'trading_paper_' + name
            if full not in emitted:
                lines.extend((f'# HELP {full} {help_text}', f'# TYPE {full} {metric_type}'))
                emitted.add(full)
            label = '{' + ','.join(f'{key}="{_escape(value)}"' for key, value in labels.items()) + '}' if labels else ''
            lines.append(f'{full}{label} {int(value) if isinstance(value, bool) else value}')

        emitted = set()
        emit('snapshot_valid', 'Report identity, schema and clocks are valid.', observed.valid)
        emit('snapshot_fresh', 'Report heartbeat is within the configured age limit.', observed.fresh)
        emit('healthy', 'A fresh report says the paper session is healthy.', observed.healthy)
        emit('running', 'A fresh valid report is nonterminal.', observed.fresh and
             observed.state not in ('stopped', 'halted', 'unavailable'))
        emit('halted', 'The last valid report says halted; clean stops are excluded.',
             observed.valid and observed.state == 'halted')
        for state in STATES:
            emit('state', 'Last reported session state; consult snapshot_fresh for liveness.',
                 observed.state == state, {'state': state})
        if observed.valid:
            emit('snapshot_age_seconds', 'Age of the report heartbeat at this scrape.', observed.age)
            emit('process_started_timestamp_seconds', 'Start time of the reporting runner process.',
                 observed.data['started'])
            for symbol, record in sorted(observed.data['sources'].items()):
                if record['time'] is not None:
                    source_age = observed.data['now'] - record['time']
                    emit('source_age_seconds', 'Age of the last accepted source bar at this scrape.',
                         source_age, {'symbol': symbol})
        if observed.fresh:
            for name, value in observed.data['values'].items():
                emit(name, 'Last fresh paper-account ' + name + '.', value)
            if observed.data['portfolio']:
                emit('execution_enabled', 'Fresh portfolio execution gate is enabled.',
                     observed.state == 'healthy' and observed.data['execution_enabled'])
            for symbol, record in sorted(observed.data['sources'].items()):
                for name, value in record['counters'].items():
                    emit('feed_' + name + '_total', 'Current runner-process feed ' + name + '.',
                         value, {'symbol': symbol}, 'counter')
        return '\n'.join(lines) + '\n'


def make_server(exporter, port=9400):
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError('port must be 0..65535')

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(2)

        def log_message(self, *_):
            pass

        def _reply(self, code, body, content_type='text/plain; charset=utf-8'):
            body = body.encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Connection', 'close')
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(body)

        def do_GET(self):
            if self.path == '/metrics':
                self._reply(200, exporter.render_metrics(), CONTENT_TYPE)
            elif self.path == '/healthz':
                self._reply(200, 'ok\n')
            elif self.path == '/readyz':
                ready = exporter.snapshot().healthy
                self._reply(200 if ready else 503, 'ready\n' if ready else 'not ready\n')
            else:
                self._reply(404, 'not found\n')

        do_HEAD = do_GET

        def do_POST(self):
            self._reply(405, 'method not allowed\n')

        do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_POST

    return HTTPServer(('127.0.0.1', port), Handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session', required=True, type=Path, help='One explicit report directory')
    parser.add_argument('--port', type=int, default=9400)
    parser.add_argument('--max-age', type=float, default=20.0)
    args = parser.parse_args(argv)
    try:
        if not 1 <= args.port <= 65535:
            raise ValueError('port must be 1..65535')
        exporter = SessionExporter(args.session, max_age=args.max_age)
        with make_server(exporter, args.port) as server:
            print(f'Paper monitoring: http://127.0.0.1:{args.port}/metrics (read only)', flush=True)
            try:
                server.serve_forever(poll_interval=0.2)
            except KeyboardInterrupt:
                pass
        return 0
    except (OSError, ValueError) as error:
        print(f'Monitoring startup failed: {error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
