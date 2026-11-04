"""Broker-free delayed multi-symbol quotes with one durable paper portfolio."""

import argparse
import asyncio
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import signal
import sys
import time
import uuid

from .feeds import QuoteFeedError, QuoteFeedUnavailable, YahooQuoteFeed
from .health import RetryPolicy
from .portfolio_native import PaperPortfolioEngine
from .portfolio_session import PortfolioSession, PortfolioSpec
from .recovery import SessionLock, atomic_json
from .run import find_library, money

ROOT = Path(__file__).resolve().parents[1]


class PortfolioMonitor:
    def __init__(self, session):
        self.session = session
        self.alerts = (session.output / 'alerts.jsonl').open('a', encoding='utf-8')
        self.status = None
        self.last_flush = 0.0
        self.started = time.time()
        self.sources = {member['instrument']: dict(symbol=member['symbol'], status='starting',
            fetch_attempts=0, responses=0, accepted=0, duplicates=0, failures=0, retries=0,
            validation_failures=0, consecutive_failures=0, last_error=None, next_retry_utc_s=None)
            for member in session.spec.members}

    def _alert(self, event, **fields):
        self.alerts.write(json.dumps(dict(schema_version=1, kind='paper_portfolio',
            session_id=self.session.session_id, time_utc_s=time.time(), event=event, **fields),
            sort_keys=True) + '\n')
        self.alerts.flush()

    def unavailable(self, instrument, error, delay):
        record = self.sources[instrument]
        record['failures'] += 1
        record['consecutive_failures'] += 1
        record['last_error'] = str(error)
        record['next_retry_utc_s'] = time.time() + delay if delay is not None else None
        status = 'retrying' if delay is not None else 'halted'
        if record['status'] != status:
            self._alert('feed_unavailable' if delay is not None else 'feed_retry_exhausted',
                        instrument=instrument, symbol=record['symbol'], error=str(error))
        record['status'] = status
        record['retries'] += delay is not None

    def received(self, instrument, changed):
        record = self.sources[instrument]
        record['responses'] += 1
        record['accepted' if changed else 'duplicates'] += 1
        record['consecutive_failures'] = 0
        record['next_retry_utc_s'] = None
        status = 'waiting_for_fresh_quote' if instrument in self.session.pending else 'healthy'
        if record['status'] != status:
            self._alert('feed_ready' if status == 'healthy' else 'feed_waiting_for_fresh_quote',
                        instrument=instrument, symbol=record['symbol'])
        record['status'] = status
        if changed:
            record['last_error'] = None

    def invalid(self, instrument, error):
        record = self.sources[instrument]
        record['validation_failures'] += 1
        record['last_error'], record['status'] = str(error), 'halted'
        self._alert('feed_invalid', instrument=instrument, symbol=record['symbol'], error=str(error))

    def flush(self, *, force=False, finished=False, code=0):
        if not force and time.monotonic() - self.last_flush < 5:
            return
        snapshot = self.session.engine.snapshot()
        status = ('halted' if code else 'stopped') if finished else (
            'halted' if snapshot['killed'] else
            'healthy' if snapshot['execution_enabled'] else 'waiting_for_fresh_quotes')
        if status != self.status:
            event = ('portfolio_resumed' if status == 'healthy' else
                     'portfolio_suspended' if status == 'waiting_for_fresh_quotes' else
                     'portfolio_halted' if status == 'halted' else 'portfolio_stopped')
            self._alert(event, status=status, reason=self.session.suspension_reason,
                        stop_reason=self.session.stop_reason)
            self.status = status
        sources = {}
        for instrument, record in self.sources.items():
            source_time = self.session.last_sources[instrument]
            sources[record['symbol']] = dict(record, instrument=instrument,
                source_time_utc_s=source_time,
                source_age_s=max(0, time.time() - source_time) if source_time is not None else None)
        atomic_json(self.session.output / 'health.json', dict(schema_version=1, mode='paper',
            counters_scope='current_process', started_utc_s=self.started, updated_utc_s=time.time(),
            session_id=self.session.session_id, status=status, account=snapshot, sources=sources,
            waiting_for_fresh_quotes=[self.session.spec.by_id[i]['symbol'] for i in sorted(self.session.pending)],
            stop_reason=self.session.stop_reason, error=self.session.error))
        self.last_flush = time.monotonic()

    def close(self):
        self.alerts.close()


async def run_portfolio(args, feeds=None, *, retry_policy=None):
    if (not 15 <= args.poll_seconds <= 60 or not math.isfinite(args.duration) or args.duration < 0):
        raise ValueError('poll-seconds must be 15..60 and duration finite/nonnegative')
    policy = retry_policy or RetryPolicy(max_retries=getattr(args, 'max_feed_retries', 3))
    spec = PortfolioSpec.load(args.config)
    feeds = feeds if feeds is not None else {
        member['instrument']: YahooQuoteFeed(member['symbol']) for member in spec.members}
    if set(feeds) != set(spec.by_id) or any(
            feeds[i].symbol != spec.by_id[i]['symbol'] for i in spec.by_id):
        raise ValueError('Portfolio feed mapping differs from the frozen universe')
    resume = getattr(args, 'resume', None)
    if resume:
        output = Path(resume).resolve()
    else:
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        output = (args.output_dir / f'{stamp}-portfolio-{uuid.uuid4().hex[:8]}').resolve()
        spec.create_bundle(output)
    tasks, session, monitor = {}, None, None
    previous_handlers = {}
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    code, base = 0, 0
    with SessionLock(output / 'session.lock'):
        manifest = spec.validate_bundle(output)  # Recheck after acquiring the exclusive owner lock.
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, lambda *_: loop.call_soon_threadsafe(stop.set))
        try:
            with PaperPortfolioEngine(args.library or find_library(), output / 'portfolio.conf') as engine:
                session = PortfolioSession(engine, spec, output, manifest, resume=bool(resume))
                started = time.monotonic_ns()
                now = lambda: base + time.monotonic_ns() - started
                monitor_failed = False
                active_instrument = None
                try:
                    if resume:
                        base = session.recover(time.time_ns())
                        manifest['resume_count'] += 1
                        atomic_json(output / 'session.json', manifest)
                    else:
                        session.suspend(0, 'startup')
                    monitor = PortfolioMonitor(session)
                    monitor.flush(force=True)
                    print(f'PAPER ONLY | shared INR {money(spec.account["initial_capital_paise"])} | '
                          f'{", ".join(member["symbol"] for member in spec.members)}', flush=True)
                    print(f'Delayed/indicative public prices; spread/depth/fills simulated. Reports: {output}', flush=True)
                    next_poll = dict.fromkeys(feeds, 0.0)
                    if engine.snapshot()['killed']:
                        session.stop_reason, code = 'risk_halt', 3
                    else:
                        while not stop.is_set():
                            if args.duration and time.monotonic_ns() - started >= args.duration * 1_000_000_000:
                                session.stop_reason = 'duration_complete'
                                break
                            session.heartbeat(now())
                            if engine.snapshot()['killed']:
                                session.stop_reason, code = 'risk_halt', 3
                                break
                            for instrument in sorted(feeds):
                                if instrument not in tasks and time.monotonic() >= next_poll[instrument]:
                                    monitor.sources[instrument]['fetch_attempts'] += 1
                                    tasks[instrument] = asyncio.create_task(asyncio.to_thread(feeds[instrument].fetch))
                            responses = []
                            # Observe failures before accepting other responses from this owner-loop batch.
                            for instrument in sorted(list(tasks)):
                                task = tasks[instrument]
                                if not task.done():
                                    continue
                                del tasks[instrument]
                                active_instrument = instrument
                                try:
                                    responses.append((instrument, task.result()))
                                except QuoteFeedUnavailable as error:
                                    session.suspend(now(), f'feed_unavailable:{spec.by_id[instrument]["symbol"]}')
                                    delay = policy.delay(monitor.sources[instrument]['consecutive_failures'] + 1,
                                                         error.retry_after_s)
                                    monitor.unavailable(instrument, error, delay)
                                    monitor.flush(force=True)
                                    if delay is None:
                                        raise
                                    next_poll[instrument] = time.monotonic() + delay
                                    print(f'{spec.by_id[instrument]["symbol"]}: account paused; retry in {delay:g}s',
                                          file=sys.stderr, flush=True)
                            closed = False
                            for instrument, quote in responses:
                                active_instrument = instrument
                                if engine.snapshot()['killed']:
                                    session.stop_reason, code = 'risk_halt', 3
                                    break
                                changed = session.accept(instrument, quote, now(), int(time.time()))
                                monitor.received(instrument, changed)
                                report = session.report()
                                monitor.flush(force=True)
                                next_poll[instrument] = time.monotonic() + args.poll_seconds
                                if changed:
                                    print(f'{quote.symbol}: INR {money(quote.price_paise)} | '
                                          f'portfolio NAV INR {money(report["equity_paise"])} | '
                                          f'working orders {report["open_orders"]} | '
                                          f'{"enabled" if report["execution_enabled"] else "paused"}', flush=True)
                                if engine.snapshot()['killed']:
                                    session.stop_reason, code = 'risk_halt', 3
                                    break
                                if not quote.market_open:
                                    session.stop_reason, closed = 'market_closed', True
                                    break
                            if closed or code:
                                break
                            monitor.flush()
                            await asyncio.sleep(0.1)
                        if stop.is_set():
                            session.stop_reason = 'interrupted'
                except asyncio.CancelledError:
                    session.stop_reason = 'interrupted'
                    raise
                except Exception as error:
                    try:
                        session.disconnect(now(), str(error))
                    except Exception:
                        session.journal_failed = True
                        engine.disconnect()
                    session.stop_reason, session.error, code = 'feed_or_io_error', str(error), 3
                    print(f'Paper portfolio stopped: {error}', file=sys.stderr, flush=True)
                    if monitor is not None:
                        try:
                            if (active_instrument is not None and isinstance(error, (QuoteFeedError, ValueError))
                                    and not isinstance(error, QuoteFeedUnavailable)):
                                monitor.invalid(active_instrument, error)
                            monitor.flush(force=True, finished=True, code=code)
                        except OSError:
                            monitor_failed = True
                finally:
                    try:
                        session.shutdown(now())
                    except Exception as error:
                        session.journal_failed = True
                        session.error = session.error or f'Could not durably stop portfolio: {error}'
                        engine.shutdown()
                        code = 3
                    if engine.snapshot()['killed'] and not session.clean_shutdown and code == 0:
                        session.stop_reason, code = 'risk_halt', 3
                    report = session.report(finished=True)
                    if monitor is not None and not monitor_failed:
                        try:
                            monitor.flush(force=True, finished=True, code=code)
                        except OSError as error:
                            session.stop_reason, session.error, code = 'monitoring_io_error', str(error), 3
                            report = session.report(finished=True)
                    manifest.update(status='finished', finished_utc=datetime.now(timezone.utc).isoformat())
                    atomic_json(output / 'session.json', manifest)
                    print(f'Stopped: {report["stop_reason"]} | working orders {report["open_orders"]} | '
                          f'shared cash INR {money(report["cash_paise"])}', flush=True)
        finally:
            for task in tasks.values():
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks.values(), return_exceptions=True)
            try:
                if session is not None:
                    session.close()
                if monitor is not None:
                    monitor.close()
            finally:
                for signum, handler in previous_handlers.items():
                    signal.signal(signum, handler)
    return code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT / 'config/network-portfolio.json')
    parser.add_argument('--library', type=Path)
    parser.add_argument('--poll-seconds', type=float, default=15)
    parser.add_argument('--max-feed-retries', type=int, default=3)
    parser.add_argument('--duration', type=float, default=0)
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'reports/network-portfolio')
    parser.add_argument('--resume', type=Path)
    args = parser.parse_args()
    try:
        return asyncio.run(run_portfolio(args))
    except (OSError, ValueError, RuntimeError) as error:
        print(f'Error: {error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
