"""Continuously consume public delayed INR quotes into the local C++ PaperBroker."""

import argparse
import asyncio
import csv
from dataclasses import asdict
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import signal
import sys
import time
import uuid

from .feeds import Quote, QuoteFeedError, QuoteFeedUnavailable, YahooQuoteFeed
from .health import RetryPolicy, SessionHealth
from .native import PaperEngine
from .recovery import (SessionJournal, SessionLock, atomic_json, config_digest,
                       create_manifest, load_manifest)

ROOT = Path(__file__).resolve().parents[1]
AUDIT_FIELDS = ['time_ns', 'order_id', 'event', 'side', 'status', 'reason', 'price_paise',
                'quantity', 'fee_paise', 'position', 'equity_paise']


def configuration(path: Path) -> dict:
    settings = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.split('#', 1)[0].strip()
        if line:
            key, value = line.split('=', 1)
            settings[key.strip()] = value.strip()
    return settings


def money(value: int) -> str:
    return ('-' if value < 0 else '') + f'{abs(value) // 100:,}.{abs(value) % 100:02d}'


def find_library() -> Path:
    for directory in (ROOT / 'build', ROOT / 'build/Release'):
        for name in ('paper_engine.dll', 'libpaper_engine.dll', 'libpaper_engine.so', 'libpaper_engine.dylib'):
            if (directory / name).is_file():
                return directory / name
    raise FileNotFoundError('Build the paper_engine CMake target first; see README.md')


class PaperSession:
    """One owner, one symbol, one virtual account; never sends broker orders."""

    def __init__(self, engine, settings, symbol, output, *, max_source_age_s=1800, spread_ticks=1,
                 simulated_quantity=100, resume=False, manifest=None):
        self.engine, self.symbol, self.output = engine, symbol, output
        self.manifest = manifest or {}
        self.resume_count = int(self.manifest.get('resume_count', 0))
        self.recovered = bool(resume)
        self.capital = int(settings.get('initial_capital_paise', '0'))
        self.tick_size = int(settings.get('tick_size_paise', '1'))
        self.lot_size = int(settings.get('lot_size', '1'))
        self.stale_after_ns = int(settings.get('stale_after_ns', str(180 * 1_000_000_000)))
        if (self.capital <= 0 or self.tick_size <= 0 or self.lot_size <= 0 or self.stale_after_ns <= 0 or
                settings.get('allow_short', 'false') != 'false' or settings.get('product', 'cnc') != 'cnc'):
            raise ValueError('Network practice requires positive virtual capital, CNC and shorting disabled')
        if spread_ticks <= 0 or simulated_quantity < self.lot_size:
            raise ValueError('Simulation spread and quantity must be positive and cover a lot')
        self.max_source_age_s = max_source_age_s
        self.spread = spread_ticks * self.tick_size
        self.quantity = simulated_quantity - simulated_quantity % self.lot_size
        self.instrument = int(settings.get('instrument_id', '1'))
        self.last_source = None
        self.last_quote = None
        self.last_age = None
        self.sequence = self.quotes = self.fills = self.accepted = self.rejected = 0
        self.peak = self.drawdown = 0
        self.stop_reason = 'running'
        self.risk_halted = False
        self.error = None
        self.last_tick_time = None
        self.last_engine_time = 0
        self.last_runtime_time = 0
        self.last_commit_wall_time_ns = None
        self.next_operation_id = 1
        self.terminal = False
        self.clean_shutdown = False

        manifest_hash = self.manifest.get('config_sha256') or config_digest(
            json.dumps(settings, sort_keys=True, separators=(',', ':')).encode('utf-8'))
        self.session_id = self.manifest.get('session_id', output.name)
        self.journal = SessionJournal(output / 'session.jsonl', resume=resume, symbol=symbol,
                                      config_sha256=manifest_hash)
        audit_path = output / ('audit.csv.recovery.tmp' if resume else 'audit.csv')
        ticks_path = output / ('ticks.csv.recovery.tmp' if resume else 'ticks.csv')
        self.audit_path, self.ticks_path = audit_path, ticks_path
        self._open_derived(audit_path, ticks_path, 'w')
        try:
            if resume:
                self._restore()
                os.replace(audit_path, output / 'audit.csv')
                os.replace(ticks_path, output / 'ticks.csv')
                self._open_derived(output / 'audit.csv', output / 'ticks.csv', 'a')
            self.raw = (output / 'source-quotes.jsonl').open('a', encoding='utf-8')
        except Exception:
            self._close_derived()
            self.journal.close()
            raise

    def _open_derived(self, audit_path, ticks_path, mode):
        self.audit = audit_path.open(mode, newline='', encoding='utf-8')
        self.writer = csv.DictWriter(self.audit, fieldnames=AUDIT_FIELDS)
        self.ticks_file = ticks_path.open(mode, newline='', encoding='utf-8')
        self.tick_writer = csv.writer(self.ticks_file)
        if mode == 'w':
            self.writer.writeheader()
            self.tick_writer.writerow(['time_ns', 'sequence', 'instrument', 'bid_paise', 'ask_paise',
                                      'bid_quantity', 'ask_quantity', 'session_open'])

    def _close_derived(self):
        for name in ('audit', 'ticks_file'):
            stream = getattr(self, name, None)
            if stream is not None and not stream.closed:
                stream.close()

    @staticmethod
    def _normalized_events(engine):
        events = []
        for event in engine.events():
            item = dict(event)
            item['side'] = 'buy' if item['side'] == 0 else 'sell'
            events.append(item)
        return events

    @staticmethod
    def _operation_time(operation):
        return operation.get('time_ns', operation.get('elapsed_ns', 0))

    def _execute(self, operation):
        kind = operation['op']
        result = None
        if kind == 'tick':
            result = self.engine.tick(operation['elapsed_ns'], operation['sequence'], operation['bid'],
                                      operation['ask'], operation['quantity'], operation['session_open'])
        elif kind == 'heartbeat':
            self.engine.heartbeat(operation['time_ns'])
        elif kind == 'cancel_all':
            self.engine.cancel_all(operation['time_ns'])
        elif kind == 'disconnect':
            self.engine.disconnect()
        elif kind == 'shutdown':
            self.engine.shutdown()
        else:
            raise ValueError(f'Unknown paper-session operation: {kind}')
        return result, self._normalized_events(self.engine), self.engine.snapshot()

    def _checkpoint(self, operation, result, events, state, wall_time_ns=None):
        return {'kind': 'checkpoint', 'id': operation['id'], 'result': result,
                'events': events, 'snapshot': state,
                'wall_time_ns': wall_time_ns if wall_time_ns is not None else time.time_ns()}

    def _record_outputs(self, operation, result, events, state, checkpoint):
        for event in events:
            self.writer.writerow(event)
            self.fills += event['event'] == 'fill'
            self.accepted += event['event'] == 'accepted'
            self.rejected += event['event'] in ('risk', 'rejected')
        if operation['op'] == 'tick' and result == 1:
            source = Quote(**operation['source'])
            self.sequence = operation['sequence']
            self.quotes += 1
            self.last_tick_time = operation['elapsed_ns']
            self.last_quote = source
            self.last_source = source.source_time_s
            self.last_age = operation['source_age_s']
            self.tick_writer.writerow([operation['elapsed_ns'], operation['sequence'], self.instrument,
                                       operation['bid'], operation['ask'], self.quantity, self.quantity,
                                       int(operation['session_open'])])
        self.last_engine_time = max(self.last_engine_time, self._operation_time(operation))
        self.last_runtime_time = max(self.last_runtime_time, self._operation_time(operation))
        self.last_commit_wall_time_ns = checkpoint['wall_time_ns']
        self.peak = max(self.peak, state['pnl_paise'])
        self.drawdown = max(self.drawdown, self.peak - state['pnl_paise'])
        self.audit.flush()
        self.ticks_file.flush()
        if state['audit_failed']:
            raise RuntimeError('C++ audit queue overflowed; paper session is incomplete')
        if operation['op'] == 'shutdown' and not self.terminal:
            self.clean_shutdown = True
        if operation['op'] == 'shutdown' or state['killed']:
            self.terminal = True

    def _restore(self):
        transactions = self.journal.transactions
        for index, (operation, saved) in enumerate(transactions):
            if saved is None and index != len(transactions) - 1:
                raise ValueError('Only the last paper-session operation may lack a checkpoint')
            result, events, state = self._execute(operation)
            if saved is not None:
                if (saved.get('result') != result or saved.get('events') != events or
                        saved.get('snapshot') != state):
                    raise RuntimeError(
                        f'Paper-session reconciliation failed at operation {operation["id"]}; '
                        'saved order events or ledger state do not match replay'
                    )
                checkpoint = saved
            else:
                # Recovery must retain the intent's original clock. Assigning
                # "now" here would conceal downtime after a pre-checkpoint crash.
                checkpoint = self._checkpoint(operation, result, events, state,
                    wall_time_ns=operation.get('prepared_wall_time_ns', self.last_commit_wall_time_ns or 0))
                self.journal.append_checkpoint(checkpoint)
            self._record_outputs(operation, result, events, state, checkpoint)
            self.next_operation_id = operation['id'] + 1
        self.terminal = self.terminal or bool(self.engine.snapshot()['killed'])
        self.audit.flush()
        self.ticks_file.flush()
        self._close_derived()

    def _commit_operation(self, kind, fields, invoke=None):
        operation = {'kind': 'operation', 'id': self.next_operation_id, 'op': kind,
                     'prepared_wall_time_ns': time.time_ns(), **fields}
        self.journal.append_operation(operation)
        result, events, state = self._execute(operation) if invoke is None else self._execute_with(operation, invoke)
        checkpoint = self._checkpoint(operation, result, events, state)
        self.journal.append_checkpoint(checkpoint)
        self.next_operation_id += 1
        self._record_outputs(operation, result, events, state, checkpoint)
        return result, state

    def _execute_with(self, operation, invoke):
        result = invoke()
        return result, self._normalized_events(self.engine), self.engine.snapshot()

    def accept(self, quote: Quote, elapsed_ns: int, wall_time_s: int) -> bool:
        if self.engine.snapshot()['killed']:
            raise RuntimeError('Paper engine is halted; no more quotes may be accepted')
        if quote.symbol != self.symbol or quote.currency != 'INR' or quote.delayed is not True:
            raise ValueError('Unexpected provider symbol/currency/data classification')
        if (type(quote.price_paise) is not int or not 0 < quote.price_paise <= 100_000_000 or
                type(quote.source_time_s) is not int or quote.source_time_s < 0 or
                type(quote.market_open) is not bool or type(wall_time_s) is not int or wall_time_s < 0 or
                type(elapsed_ns) is not int or not 0 <= elapsed_ns <= 2**63 - 1):
            raise ValueError('Invalid provider price/timestamp/session flag or runtime clock')
        age = wall_time_s - quote.source_time_s
        self.raw.write(json.dumps({**asdict(quote), 'received_utc_s': wall_time_s,
                                   'source_age_s': max(0, age), 'monotonic_ns': elapsed_ns},
                                  sort_keys=True) + '\n')
        self.raw.flush()
        if age < -5:
            raise ValueError('Provider timestamp is in the future')
        if quote.market_open and age > self.max_source_age_s:
            raise ValueError(f'Provider quote is stale ({age}s); no quote accepted')
        if self.last_source is not None and quote.source_time_s < self.last_source:
            raise ValueError('Provider timestamp regressed')
        # Repeated HTTP responses cannot replenish simulated liquidity or generate signals.
        if quote.market_open and quote.source_time_s == self.last_source:
            if self.last_quote is not None and quote.price_paise != self.last_quote.price_paise:
                raise ValueError('Provider revised an already accepted completed bar')
            self.last_quote, self.last_age = quote, max(0, age)
            return False
        mid = quote.price_paise // self.tick_size * self.tick_size
        bid, ask = mid - self.spread, mid + self.spread
        sequence = self.sequence + 1
        operation = {
            'elapsed_ns': elapsed_ns, 'sequence': sequence, 'bid': bid, 'ask': ask,
            'quantity': self.quantity, 'session_open': bool(quote.market_open),
            'source': asdict(quote), 'source_age_s': max(0, age),
            'received_utc_s': wall_time_s,
        }
        result, state = self._commit_operation('tick', operation)
        if result != 1:
            raise ValueError('C++ engine rejected the normalized quote')
        if not quote.market_open:
            self.stop_reason = 'market_closed'
        return True

    def heartbeat(self, now_ns: int):
        before = self.engine.snapshot()
        stale_due = (self.last_tick_time is not None and
                     now_ns - self.last_tick_time > self.stale_after_ns and not before['killed'])
        time_regressed = now_ns < self.last_runtime_time
        if stale_due or time_regressed:
            self._commit_operation('heartbeat', {'time_ns': now_ns})
        else:
            self.engine.heartbeat(now_ns)
            self.last_runtime_time = now_ns
            after = self.engine.snapshot()
            if after['killed'] and not before['killed']:
                raise RuntimeError('Engine halted outside a durable heartbeat checkpoint')

    def cancel_all(self, now_ns: int):
        self._commit_operation('cancel_all', {'time_ns': now_ns})

    def disconnect(self):
        self._commit_operation('disconnect', {'time_ns': self.last_engine_time})

    def shutdown(self):
        if self.terminal and self.engine.snapshot()['killed']:
            return
        self._commit_operation('shutdown', {'time_ns': self.last_engine_time})

    def report(self, *, finished=False):
        state = self.engine.snapshot()
        report = {
            'mode': 'paper', 'feed_status': 'delayed/indicative', 'symbol': self.symbol,
            'currency': 'INR', 'session_id': self.session_id, 'initial_capital_paise': self.capital,
            'virtual_cash_paise': self.capital + state['cash_paise'],
            'virtual_equity_paise': self.capital + state['pnl_paise'],
            'position': state['position'], 'pnl_paise': state['pnl_paise'],
            'fees_paise': state['fees_paise'], 'max_drawdown_paise': self.drawdown,
            'open_orders': state['open_orders'], 'orders_accepted': self.accepted,
            'orders_rejected': self.rejected, 'fills': self.fills, 'quotes': self.quotes,
            'last_price_paise': self.last_quote.price_paise if self.last_quote else None,
            'source_time_utc_s': self.last_quote.source_time_s if self.last_quote else None,
            'source_age_at_receipt_s': self.last_age, 'stop_reason': self.stop_reason,
            'error': self.error, 'finished': finished, 'audit_complete': not bool(state['audit_failed']),
            'risk_halted': (bool(state['killed']) and not self.clean_shutdown) or self.risk_halted,
            'engine_kill_reason_code': state['kill_reason'],
            'fill_model': 'completed 1m close plus simulated spread, depth and latency',
            'terminal_inventory_is_marked_not_liquidated': True,
            'resumed': self.recovered, 'resume_count': self.resume_count,
            'journal_operations': self.next_operation_id - 1,
        }
        atomic_json(self.output / 'summary.json', report)
        return report

    def close(self):
        self._close_derived()
        raw = getattr(self, 'raw', None)
        if raw is not None and not raw.closed:
            raw.close()
        self.journal.close()


def suspend_for_retry(session, health, policy, error, now_ns):
    """Cancel durably before scheduling another request; never clear a risk halt."""
    session.cancel_all(now_ns)
    delay = policy.delay(health.metrics['consecutive_failures'] + 1, error.retry_after_s)
    health.unavailable(error, delay)
    health.flush(session.engine.snapshot(), force=True)
    return delay


async def run_session(args, feed=None, *, retry_policy=None) -> int:
    if (not 15 <= args.poll_seconds <= 60 or not 60 <= args.max_source_age <= 3600 or
            not math.isfinite(args.duration) or args.duration < 0):
        raise ValueError('poll-seconds must be 15..60, max-source-age 60..3600, duration nonnegative')
    policy = retry_policy or RetryPolicy(max_retries=getattr(args, 'max_feed_retries', 3))
    feed = feed or YahooQuoteFeed(args.symbol)
    requested_config = args.config.read_bytes()
    resume_path = getattr(args, 'resume', None)
    if resume_path:
        output = Path(resume_path).resolve()
        manifest = load_manifest(output, feed.symbol, requested_config)
        config_path = output / 'config.conf'
    else:
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        output = (args.output_dir / f'{stamp}-{feed.symbol}-{uuid.uuid4().hex[:8]}').resolve()
        manifest = create_manifest(output, feed.symbol, requested_config)
        config_path = output / 'config.conf'
    settings = configuration(config_path)
    lock = SessionLock(output / 'session.lock')
    lock.acquire()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    previous_handlers = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, lambda *_: loop.call_soon_threadsafe(stop.set))
    print(f'PAPER ONLY | {feed.symbol} | Yahoo delayed/indicative prices | no broker account', flush=True)
    if resume_path:
        print(f'Recovering and reconciling session: {output}', flush=True)
    else:
        print('Spread/depth/fills are simulated. Use --resume with this report path to recover this account.', flush=True)
    print(f'Reports: {output}', flush=True)
    task = None
    session = None
    health = None
    monitoring_failed = False
    code = 0
    recovered_now = 0
    try:
        with PaperEngine(args.library or find_library(), config_path) as engine:
            session = PaperSession(engine, settings, feed.symbol, output,
                                   max_source_age_s=args.max_source_age,
                                   resume=bool(resume_path), manifest=manifest)
            runtime_started = time.monotonic_ns()
            base_elapsed_ns = 0
            try:
                health = SessionHealth(output, feed.symbol, session.session_id)
                health.flush(engine.snapshot(), force=True)
                if resume_path:
                    wall_now = time.time_ns()
                    if session.last_commit_wall_time_ns is None:
                        downtime_ns = 0
                    elif wall_now < session.last_commit_wall_time_ns:
                        downtime_ns = session.stale_after_ns + 1
                    else:
                        downtime_ns = wall_now - session.last_commit_wall_time_ns
                    recovered_now = session.last_engine_time + downtime_ns
                    if recovered_now > 2**63 - 1:
                        raise RuntimeError('Recovered monotonic time is out of range; refusing to continue')
                    base_elapsed_ns = recovered_now
                    if not session.terminal:
                        session.heartbeat(recovered_now)
                        session.cancel_all(recovered_now)
                    manifest['resume_count'] = int(manifest.get('resume_count', 0)) + 1
                    session.resume_count = manifest['resume_count']
                    atomic_json(output / 'session.json', manifest)
                if session.terminal or engine.snapshot()['killed']:
                    session.stop_reason = 'risk_halt' if engine.snapshot()['killed'] else 'already_stopped'
                    session.risk_halted = bool(engine.snapshot()['killed'])
                    code = 3 if session.risk_halted else 0
                else:
                    next_poll = 0.0
                    while not stop.is_set():
                        runtime_elapsed = time.monotonic_ns() - runtime_started
                        elapsed_ns = base_elapsed_ns + runtime_elapsed
                        if args.duration and runtime_elapsed >= args.duration * 1_000_000_000:
                            session.stop_reason = 'duration_complete'
                            break
                        session.heartbeat(elapsed_ns)
                        if engine.snapshot()['killed']:
                            session.stop_reason = 'risk_halt'
                            session.risk_halted = True
                            code = 3
                            break
                        if task is None and time.monotonic() >= next_poll:
                            health.attempt()
                            task = asyncio.create_task(asyncio.to_thread(feed.fetch))
                        if task is not None and task.done():
                            try:
                                quote = task.result()
                            except QuoteFeedUnavailable as error:
                                task = None
                                # Suspend execution before waiting for transport recovery.
                                # Fresh quote validation and the stale heartbeat remain mandatory.
                                delay = suspend_for_retry(
                                    session, health, policy, error,
                                    base_elapsed_ns + time.monotonic_ns() - runtime_started)
                                if delay is None:
                                    raise
                                next_poll = time.monotonic() + delay
                                print(f'Feed unavailable; working paper orders cancelled. Retry in {delay:g}s.',
                                      file=sys.stderr, flush=True)
                                continue
                            task = None
                            changed = session.accept(
                                quote, base_elapsed_ns + time.monotonic_ns() - runtime_started, int(time.time()))
                            health.received(quote, changed)
                            health.flush(engine.snapshot(), force=True)
                            report = session.report()
                            print(f"{quote.symbol}: INR {money(quote.price_paise)} | source age {session.last_age}s | "
                                  f"{'new quote' if changed else 'unchanged'} | virtual equity INR "
                                  f"{money(report['virtual_equity_paise'])} | PnL INR {money(report['pnl_paise'])} | "
                                  f"position {report['position']} | fills {report['fills']}", flush=True)
                            if engine.snapshot()['killed']:
                                session.stop_reason = 'risk_halt'
                                session.risk_halted = True
                                code = 3
                                break
                            if not quote.market_open:
                                print('Regular market session is closed; no virtual order was enabled by this quote.', flush=True)
                                break
                            next_poll = time.monotonic() + args.poll_seconds
                        health.flush(engine.snapshot())
                        await asyncio.sleep(0.1)
                    if stop.is_set():
                        session.stop_reason = 'interrupted'
            except Exception as error:
                try:
                    session.disconnect()
                except Exception:
                    engine.disconnect()
                session.stop_reason = 'feed_or_io_error'
                session.error = str(error)
                code = 3
                if health is not None:
                    try:
                        health.metrics['last_error'] = str(error)
                        if isinstance(error, (QuoteFeedError, ValueError)) and not isinstance(error, QuoteFeedUnavailable):
                            health.metrics['validation_failures'] += 1
                        health.transition('halted', 'session_error', error=str(error))
                        health.flush(engine.snapshot(), force=True)
                    except OSError:
                        monitoring_failed = True
                print(f'Paper trading stopped: {error}', file=sys.stderr, flush=True)
            finally:
                if engine.snapshot()['killed'] and code == 0:
                    session.stop_reason = 'risk_halt'
                    session.risk_halted = True
                    code = 3
                try:
                    session.shutdown()
                except Exception as error:
                    code = 3
                    session.error = session.error or f'Could not durably stop paper engine: {error}'
                    engine.shutdown()
                report = session.report(finished=True)
                if health is not None and not monitoring_failed:
                    try:
                        health.metrics.update(stop_reason=session.stop_reason,
                                              risk_halted=report['risk_halted'],
                                              last_error=session.error or health.metrics['last_error'],
                                              next_retry_utc_s=None)
                        health.transition('halted' if code else 'stopped',
                                          'session_halted' if code else 'session_stopped',
                                          reason=session.stop_reason)
                        health.flush(engine.snapshot(), force=True)
                    except OSError as error:
                        code = 3
                        session.stop_reason = 'monitoring_io_error'
                        session.error = str(error)
                        report = session.report(finished=True)
                manifest['status'] = 'finished'
                manifest['finished_utc'] = datetime.now(timezone.utc).isoformat()
                manifest['resume_count'] = session.resume_count
                atomic_json(output / 'session.json', manifest)
                print(f"Stopped: {report['stop_reason']} | pending orders {report['open_orders']} | "
                      f"virtual cash INR {money(report['virtual_cash_paise'])}", flush=True)
    finally:
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if session is not None:
            session.close()
        if health is not None:
            health.close()
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        lock.release()
    return code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--symbol', default='RELIANCE.NS', help='Example symbol, not a recommendation')
    parser.add_argument('--config', type=Path, default=ROOT / 'config/network-paper.conf')
    parser.add_argument('--library', type=Path)
    parser.add_argument('--poll-seconds', type=float, default=15)
    parser.add_argument('--max-source-age', type=int, default=1800)
    parser.add_argument('--max-feed-retries', type=int, default=3, help='0..10 retries for transient transport/server errors')
    parser.add_argument('--duration', type=float, default=0, help='0 runs until stop, closed market or error')
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'reports/network-paper')
    parser.add_argument('--resume', type=Path, help='Recover a running session from its report directory')
    args = parser.parse_args()
    try:
        return asyncio.run(run_session(args))
    except (OSError, ValueError, RuntimeError) as error:
        print(f'Error: {error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
