"""Frozen portfolio configuration and durable multi-symbol paper-session replay."""

import csv
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
import threading
import uuid

from .feeds import Quote, YahooQuoteFeed
from .recovery import SessionJournal, atomic_json, config_digest

ACCOUNT_KEYS = {'initial_capital_paise', 'max_gross_notional_paise', 'max_daily_loss_paise',
                'max_open_orders', 'orders_per_second', 'orders_per_minute', 'orders_per_session'}
AUDIT_FIELDS = ['instrument', 'time_ns', 'order_id', 'event', 'side', 'status', 'reason',
                'price_paise', 'quantity', 'fee_paise', 'position', 'equity_paise']
TICK_FIELDS = ['time_ns', 'sequence', 'instrument', 'bid_paise', 'ask_paise',
               'bid_quantity', 'ask_quantity', 'session_open']


def _settings(data):
    result = {}
    for line in data.decode('utf-8').splitlines():
        line = line.split('#', 1)[0].strip()
        if not line:
            continue
        if '=' not in line:
            raise ValueError('Expected portfolio constituent key=value')
        key, value = (part.strip() for part in line.split('=', 1))
        if key in result:
            raise ValueError(f'Duplicate constituent configuration key: {key}')
        result[key] = value
    return result


def _durable_bytes(path, data):
    with path.open('xb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


class PortfolioSpec:
    def __init__(self, account, members, max_source_age_s):
        self.account, self.members, self.max_source_age_s = account, members, max_source_age_s
        self.by_id = {member['instrument']: member for member in members}
        self.identity = dict(format_version=1, engine='paper_portfolio', portfolio_api_version=1,
                             account=account, max_source_age_s=max_source_age_s,
                             members=[{key: member[key] for key in
                                       ('instrument', 'symbol', 'config_sha256', 'quantity', 'spread_ticks')}
                                      for member in members])
        self.fingerprint = config_digest(json.dumps(self.identity, sort_keys=True,
                                                     separators=(',', ':')).encode('utf-8'))

    @classmethod
    def load(cls, path: Path):
        record = json.loads(path.read_text(encoding='utf-8'))
        if (not isinstance(record, dict) or set(record) !=
                {'format_version', 'mode', 'account', 'max_source_age_s', 'instruments'} or
                type(record['format_version']) is not int or record['format_version'] != 1 or
                record['mode'] != 'paper'):
            raise ValueError('Unsupported portfolio stream specification; mode must be paper')
        account = record['account']
        if (not isinstance(account, dict) or set(account) != ACCOUNT_KEYS or
                any(type(value) is not int or value <= 0 for value in account.values())):
            raise ValueError('Specify every shared-account limit as a positive integer')
        age = record['max_source_age_s']
        if type(age) is not int or not 60 <= age <= 3600:
            raise ValueError('max_source_age_s must be 60..3600')
        universe = record['instruments']
        if not isinstance(universe, list) or not 1 <= len(universe) <= 32:
            raise ValueError('Portfolio streaming requires 1..32 instruments')
        members, ids, symbols = [], set(), set()
        for item in universe:
            if (not isinstance(item, dict) or set(item) != {'symbol', 'config', 'quantity', 'spread_ticks'} or
                    not isinstance(item['config'], str) or not item['config']):
                raise ValueError('Each instrument needs symbol, config, quantity and spread_ticks')
            symbol = YahooQuoteFeed(item['symbol']).symbol  # Validation only; no request.
            data = (path.parent / item['config']).read_bytes()
            settings = _settings(data)
            try:
                instrument, tick, lot, stale = (int(settings[key]) for key in
                    ('instrument_id', 'tick_size_paise', 'lot_size', 'stale_after_ns'))
                lower, upper = int(settings.get('lower_band_paise', '1')), int(settings.get('upper_band_paise', '100000000'))
            except (KeyError, ValueError) as error:
                raise ValueError('Constituents need explicit instrument/tick/lot/stale metadata') from error
            if (settings.get('mode', 'paper') != 'paper' or settings.get('product') != 'cnc' or
                    settings.get('allow_short') != 'false' or int(settings.get('initial_capital_paise', '0')) != 0 or
                    not 0 < instrument <= 2**32 - 1 or not 0 < tick <= 100_000_000 or
                    not 0 < lot <= 1_000_000 or not 0 < stale <= 3600_000_000_000 or
                    not 0 < lower <= upper <= 100_000_000):
                raise ValueError('Constituents require valid INR/CNC metadata, no shorting and zero allocated capital')
            quantity, spread = item['quantity'], item['spread_ticks']
            if (type(quantity) is not int or not lot <= quantity <= 1_000_000 or
                    type(spread) is not int or not 1 <= spread <= 1000):
                raise ValueError('Invalid simulated depth/spread')
            if instrument in ids or symbol in symbols:
                raise ValueError('Portfolio IDs and symbols must be unique')
            ids.add(instrument)
            symbols.add(symbol)
            members.append(dict(instrument=instrument, symbol=symbol, config_data=data,
                                config_sha256=config_digest(data), tick_size=tick, lot_size=lot,
                                stale_after_ns=stale, lower_band=lower, upper_band=upper,
                                quantity=quantity - quantity % lot, spread_ticks=spread))
        return cls(dict(account), members, age)

    def native_config(self):
        lines = ['mode=paper'] + [f'{key}={value}' for key, value in sorted(self.account.items())]
        lines += [f'instrument_config=configs/{member["instrument"]}.conf' for member in self.members]
        return ('\n'.join(lines) + '\n').encode('utf-8')

    def create_bundle(self, output: Path):
        output.mkdir(parents=True, exist_ok=False)
        (output / 'configs').mkdir()
        for member in self.members:
            _durable_bytes(output / 'configs' / f'{member["instrument"]}.conf', member['config_data'])
        _durable_bytes(output / 'portfolio.conf', self.native_config())
        manifest = dict(kind='paper_portfolio', format_version=1, portfolio_api_version=1,
                        session_id=uuid.uuid4().hex, bundle_sha256=self.fingerprint,
                        identity=self.identity, status='running', resume_count=0,
                        created_utc=datetime.now(timezone.utc).isoformat())
        atomic_json(output / 'session.json', manifest)
        return manifest

    def validate_bundle(self, output: Path):
        manifest = json.loads((output / 'session.json').read_text(encoding='utf-8'))
        if (not isinstance(manifest, dict) or not isinstance(manifest.get('session_id'), str) or
                not manifest['session_id'] or type(manifest.get('resume_count')) is not int or
                manifest['resume_count'] < 0 or
                manifest.get('kind') != 'paper_portfolio' or manifest.get('format_version') != 1 or
                manifest.get('portfolio_api_version') != 1 or manifest.get('status') != 'running' or
                manifest.get('bundle_sha256') != self.fingerprint or manifest.get('identity') != self.identity):
            raise ValueError('Finished, incompatible or changed portfolio bundle; refusing recovery')
        if (output / 'portfolio.conf').read_bytes() != self.native_config():
            raise ValueError('Saved portfolio account configuration differs')
        for member in self.members:
            if (output / 'configs' / f'{member["instrument"]}.conf').read_bytes() != member['config_data']:
                raise ValueError('Saved portfolio constituent configuration differs')
        return manifest


class PortfolioSession:
    def __init__(self, engine, spec: PortfolioSpec, output: Path, manifest, *, resume=False):
        self.engine, self.spec, self.output, self.manifest = engine, spec, output, manifest
        self.owner = threading.get_ident()
        if resume:
            if manifest.get('engine_sha256') != engine.binary_sha256:
                raise ValueError('Native portfolio binary differs from the saved session; refusing recovery')
        else:
            manifest['engine_sha256'] = engine.binary_sha256
            atomic_json(output / 'session.json', manifest)
        self.session_id = manifest['session_id']
        self.sequences = dict.fromkeys(spec.by_id, 0)
        self.last_sources = dict.fromkeys(spec.by_id)
        self.last_ticks = dict.fromkeys(spec.by_id)
        self.last_quotes = {}
        self.pending = set(spec.by_id)
        self.baseline = dict(self.last_sources)
        self.suspension_reason = 'startup'
        self.quotes = self.fills = self.accepted = self.rejected = 0
        self.peak = self.drawdown = 0
        self.next_id = 1
        self.last_engine_time = self.last_runtime_time = 0
        self.last_commit_wall_time_ns = None
        self.stop_reason, self.error = 'running', None
        self.terminal = self.clean_shutdown = self.journal_failed = False
        self.resumed = resume
        header = dict(kind='header', format_version=1, engine='paper_portfolio',
                      portfolio_api_version=1, bundle_sha256=spec.fingerprint,
                      engine_sha256=engine.binary_sha256)
        self.journal = SessionJournal(output / 'session.jsonl', resume=resume,
                                      symbol='portfolio', config_sha256=spec.fingerprint, header=header)
        suffix = '.recovery.tmp' if resume else ''
        self.audit_path, self.ticks_path = output / ('audit.csv' + suffix), output / ('ticks.csv' + suffix)
        try:
            self._open(self.audit_path, self.ticks_path, 'w' if resume else 'x')
            if resume:
                self._restore()
                self._close_derived()
                os.replace(self.audit_path, output / 'audit.csv')
                os.replace(self.ticks_path, output / 'ticks.csv')
                self._open(output / 'audit.csv', output / 'ticks.csv', 'a')
            self.raw = (output / 'source-quotes.jsonl').open('a', encoding='utf-8')
        except Exception:
            self._close_derived()
            self.journal.close()
            raise

    def _open(self, audit, ticks, mode):
        self.audit = audit.open(mode, newline='', encoding='utf-8')
        self.ticks_file = ticks.open(mode, newline='', encoding='utf-8')
        self.audit_writer = csv.DictWriter(self.audit, fieldnames=AUDIT_FIELDS)
        self.tick_writer = csv.DictWriter(self.ticks_file, fieldnames=TICK_FIELDS)
        if mode != 'a':
            self.audit_writer.writeheader()
            self.tick_writer.writeheader()

    def _close_derived(self):
        for name in ('audit', 'ticks_file'):
            stream = getattr(self, name, None)
            if stream is not None and not stream.closed:
                stream.close()

    def _quote_fields(self, instrument, quote, now, received):
        member = self.spec.by_id.get(instrument)
        if (member is None or quote.symbol != member['symbol'] or quote.currency != 'INR' or quote.delayed is not True or
                type(quote.price_paise) is not int or not 0 < quote.price_paise <= 100_000_000 or
                type(quote.source_time_s) is not int or quote.source_time_s < 0 or type(quote.market_open) is not bool or
                type(received) is not int or received < 0 or type(now) is not int or not 0 <= now <= 2**63 - 1):
            raise ValueError('Invalid portfolio source symbol/currency/price/time/classification')
        age = received - quote.source_time_s
        if age < -5 or (quote.market_open and age > self.spec.max_source_age_s):
            raise ValueError(f'{quote.symbol}: future or stale provider quote')
        previous = self.last_sources[instrument]
        if previous is not None and quote.source_time_s < previous:
            raise ValueError(f'{quote.symbol}: provider timestamp regressed')
        if (previous == quote.source_time_s and instrument in self.last_quotes and
                quote.price_paise != self.last_quotes[instrument].price_paise):
            raise ValueError(f'{quote.symbol}: provider revised an accepted completed bar')
        mid = quote.price_paise // member['tick_size'] * member['tick_size']
        spread = member['spread_ticks'] * member['tick_size']
        bid, ask = mid - spread, mid + spread
        if not member['lower_band'] <= bid <= ask <= member['upper_band']:
            raise ValueError(f'{quote.symbol}: normalized quote exceeds simulation bands')
        return dict(instrument=instrument, time_ns=now, sequence=self.sequences[instrument] + 1,
                    bid=bid, ask=ask, quantity=member['quantity'], session_open=quote.market_open,
                    source=asdict(quote), received_utc_s=received, source_age_s=max(0, age))

    def _execute(self, operation):
        kind, now = operation['op'], operation['time_ns']
        result = None
        if kind == 'tick':
            source = Quote(**operation['source'])
            expected = self._quote_fields(operation['instrument'], source, now, operation['received_utc_s'])
            if any(operation.get(key) != value for key, value in expected.items()):
                raise ValueError('Saved portfolio quote/sequence metadata is inconsistent')
            result = self.engine.tick(operation['instrument'], now, operation['sequence'],
                                      operation['bid'], operation['ask'], operation['quantity'],
                                      operation['session_open'])
        else:
            self.engine.heartbeat(now)  # Replay the exact clock even after undurable healthy heartbeats.
            if kind == 'suspend':
                self.engine.suspend(now)
            elif kind == 'enable':
                if self.pending:
                    raise RuntimeError('Cannot bypass the fresh-quote portfolio barrier')
                result = self.engine.resume_execution(now)
            elif kind == 'disconnect':
                self.engine.disconnect()
            elif kind == 'shutdown':
                result = not bool(self.engine.snapshot()['killed'])
                self.engine.shutdown()
            elif kind != 'heartbeat':
                raise ValueError(f'Unknown portfolio journal operation: {kind}')
        events = []
        for raw in self.engine.events():
            item = dict(raw)
            item['side'] = 'buy' if item['side'] == 0 else 'sell'
            events.append(item)
        return result, events, self.engine.snapshot()

    def _record(self, operation, result, events, state, checkpoint):
        for event in events:
            self.audit_writer.writerow(event)
            self.fills += event['event'] == 'fill'
            self.accepted += event['event'] == 'accepted'
            self.rejected += event['event'] in ('risk', 'rejected')
        kind = operation['op']
        if kind == 'suspend':
            self.pending = set(self.spec.by_id)
            self.baseline = dict(self.last_sources)
            self.suspension_reason = operation['reason']
        elif kind == 'enable' and result:
            self.suspension_reason = None
        elif kind == 'shutdown':
            self.clean_shutdown = bool(result)
        elif kind == 'disconnect':
            self.error = operation.get('reason', self.error)
        elif kind == 'tick' and result == 1:
            instrument = operation['instrument']
            source = Quote(**operation['source'])
            self.sequences[instrument] = operation['sequence']
            self.last_sources[instrument] = source.source_time_s
            self.last_ticks[instrument] = operation['time_ns']
            self.last_quotes[instrument] = source
            self.quotes += 1
            baseline = self.baseline.get(instrument)
            if source.market_open and (baseline is None or source.source_time_s > baseline):
                self.pending.discard(instrument)
            self.tick_writer.writerow(dict(time_ns=operation['time_ns'], sequence=operation['sequence'],
                instrument=instrument, bid_paise=operation['bid'], ask_paise=operation['ask'],
                bid_quantity=operation['quantity'], ask_quantity=operation['quantity'],
                session_open=int(operation['session_open'])))
        self.last_engine_time = self.last_runtime_time = operation['time_ns']
        self.last_commit_wall_time_ns = checkpoint['wall_time_ns']
        self.terminal = self.terminal or bool(state['killed'])
        self.peak = max(self.peak, state['pnl_paise'])
        self.drawdown = max(self.drawdown, self.peak - state['pnl_paise'])
        self.audit.flush()
        self.ticks_file.flush()
        if state['audit_failed']:
            raise RuntimeError('Portfolio audit overflowed; session is incomplete')

    def _restore(self):
        for operation, saved in self.journal.transactions:
            result, events, state = self._execute(operation)
            if saved is not None:
                if saved.get('result') != result or saved.get('events') != events or saved.get('snapshot') != state:
                    raise RuntimeError(f'Portfolio reconciliation failed at operation {operation["id"]}')
                checkpoint = saved
            else:
                # The original durable intent is the downtime anchor, not recovery time.
                checkpoint = dict(kind='checkpoint', id=operation['id'], result=result, events=events,
                                  snapshot=state, wall_time_ns=operation['prepared_wall_time_ns'])
                self.journal.append_checkpoint(checkpoint)
            self._record(operation, result, events, state, checkpoint)
            self.next_id = operation['id'] + 1

    def _commit(self, kind, now, **fields):
        if threading.get_ident() != self.owner:
            raise RuntimeError('Portfolio journal must be called by its owning thread')
        operation = dict(kind='operation', id=self.next_id, op=kind, time_ns=now,
                         prepared_wall_time_ns=time.time_ns(), **fields)
        try:
            self.journal.append_operation(operation)
            result, events, state = self._execute(operation)
            checkpoint = dict(kind='checkpoint', id=operation['id'], result=result, events=events,
                              snapshot=state, wall_time_ns=time.time_ns())
            self.journal.append_checkpoint(checkpoint)
            self.next_id += 1
            self._record(operation, result, events, state, checkpoint)
            return result, state
        except Exception:
            self.journal_failed = True
            try:
                self.engine.disconnect()
            except Exception:
                pass
            self.terminal = True
            raise

    def suspend(self, now, reason):
        self._commit('suspend', now, reason=reason)

    def accept(self, instrument, quote, now, received):
        if self.engine.snapshot()['killed']:
            raise RuntimeError('Portfolio is halted')
        fields = self._quote_fields(instrument, quote, now, received)
        self.raw.write(json.dumps(dict(instrument=instrument, received_utc_s=received, **asdict(quote)),
                                  sort_keys=True) + '\n')
        self.raw.flush()
        if quote.market_open and self.last_sources[instrument] == quote.source_time_s:
            return False
        fields.pop('time_ns')
        result, state = self._commit('tick', now, **fields)
        if result != 1:
            raise RuntimeError('Native portfolio rejected a validated quote')
        if not state['killed'] and not self.pending and not state['execution_enabled']:
            result, _ = self._commit('enable', now)
            if not result:
                raise RuntimeError('Native portfolio refused the completed fresh-quote barrier')
        return True

    def heartbeat(self, now):
        before = self.engine.snapshot()
        stale = any(last is not None and now - last > self.spec.by_id[instrument]['stale_after_ns']
                    for instrument, last in self.last_ticks.items())
        if now < self.last_runtime_time or (stale and not before['killed']):
            self._commit('heartbeat', now)
        else:
            self.engine.heartbeat(now)
            self.last_runtime_time = now
            if self.engine.snapshot() != before or list(self.engine.events()):
                raise RuntimeError('Portfolio state changed outside a durable heartbeat')

    def recover(self, wall_now_ns):
        if self.last_commit_wall_time_ns is None:
            self.suspend(0, 'restart')
            return 0
        regressed = wall_now_ns < self.last_commit_wall_time_ns
        downtime = max(0, wall_now_ns - self.last_commit_wall_time_ns)
        now = self.last_engine_time + downtime
        if now > 2**63 - 1:
            raise RuntimeError('Recovered portfolio clock is outside native bounds')
        if not self.terminal:
            if regressed:
                self._commit('disconnect', now, reason='Wall clock regressed during portfolio recovery')
            else:
                self.suspend(now, 'restart')
        return now

    def disconnect(self, now, reason='Portfolio disconnected'):
        self._commit('disconnect', now, reason=reason)

    def shutdown(self, now):
        if not self.engine.snapshot()['killed']:
            self._commit('shutdown', now)

    def report(self, *, finished=False):
        state = self.engine.snapshot()
        report = dict(state, mode='paper', feed_status='delayed/indicative',
                      session_id=self.session_id, resumed=self.resumed, finished=finished,
                      engine_sha256=self.manifest['engine_sha256'],
                      quotes=self.quotes, fills=self.fills, orders_accepted=self.accepted,
                      orders_rejected=self.rejected, max_drawdown_paise=self.drawdown,
                      stop_reason=self.stop_reason, error=self.error,
                      risk_halted=bool(state['killed']) and not self.clean_shutdown,
                      audit_complete=not bool(state['audit_failed']) and not self.journal_failed,
                      waiting_for_fresh_quotes=[self.spec.by_id[i]['symbol'] for i in sorted(self.pending)],
                      suspension_reason=self.suspension_reason, journal_operations=self.next_id - 1,
                      sources={member['symbol']: dict(source_time_utc_s=self.last_sources[member['instrument']],
                              sequence=self.sequences[member['instrument']]) for member in self.spec.members},
                      terminal_inventory_is_marked_not_liquidated=True)
        atomic_json(self.output / 'summary.json', report)
        return report

    def close(self):
        self._close_derived()
        raw = getattr(self, 'raw', None)
        if raw is not None:
            raw.close()
        self.journal.close()
