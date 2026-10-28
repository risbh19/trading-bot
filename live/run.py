"""Continuously consume public delayed INR quotes into the local C++ PaperBroker."""

import argparse
import asyncio
import csv
from dataclasses import asdict
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import signal
import sys
import time
import uuid

from .feeds import Quote, YahooQuoteFeed
from .native import PaperEngine

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
    """One owner, one symbol, one new virtual account; never sends broker orders."""
    def __init__(self, engine, settings, symbol, output, *, max_source_age_s=1800, spread_ticks=1,
                 simulated_quantity=100):
        self.engine, self.symbol, self.output = engine, symbol, output
        self.capital = int(settings.get('initial_capital_paise', '0'))
        self.tick_size = int(settings.get('tick_size_paise', '1'))
        self.lot_size = int(settings.get('lot_size', '1'))
        if (self.capital <= 0 or self.tick_size <= 0 or settings.get('allow_short', 'false') != 'false'
                or settings.get('product', 'cnc') != 'cnc'):
            raise ValueError('Network practice requires positive virtual capital, CNC and shorting disabled')
        if spread_ticks <= 0 or simulated_quantity < self.lot_size:
            raise ValueError('Simulation spread and quantity must be positive and cover a lot')
        self.max_source_age_s = max_source_age_s
        self.spread = spread_ticks * self.tick_size
        self.quantity = simulated_quantity - simulated_quantity % self.lot_size
        self.last_source = None
        self.last_quote = None
        self.last_age = None
        self.sequence = self.quotes = self.fills = self.accepted = self.rejected = 0
        self.peak = self.drawdown = 0
        self.stop_reason = 'running'
        self.risk_halted = False
        self.error = None
        self.audit = (output / 'audit.csv').open('w', newline='', encoding='utf-8')
        self.writer = csv.DictWriter(self.audit, fieldnames=AUDIT_FIELDS)
        self.writer.writeheader()
        self.raw = (output / 'source-quotes.jsonl').open('w', encoding='utf-8')
        self.ticks_file = (output / 'ticks.csv').open('w', newline='', encoding='utf-8')
        self.tick_writer = csv.writer(self.ticks_file)
        self.tick_writer.writerow(['time_ns', 'sequence', 'instrument', 'bid_paise', 'ask_paise',
                                  'bid_quantity', 'ask_quantity', 'session_open'])
        self.instrument = int(settings.get('instrument_id', '1'))

    def accept(self, quote: Quote, elapsed_ns: int, wall_time_s: int) -> bool:
        if quote.symbol != self.symbol or quote.currency != 'INR' or not quote.delayed:
            raise ValueError('Unexpected provider symbol/currency/data classification')
        age = wall_time_s - quote.source_time_s
        if age < -5:
            raise ValueError('Provider timestamp is in the future')
        self.last_quote, self.last_age = quote, max(0, age)
        self.raw.write(json.dumps({**asdict(quote), 'received_utc_s': wall_time_s,
                                   'source_age_s': self.last_age, 'monotonic_ns': elapsed_ns}) + '\n')
        self.raw.flush()
        if quote.market_open and age > self.max_source_age_s:
            raise ValueError(f'Provider quote is stale ({age}s); no quote accepted')
        if self.last_source is not None and quote.source_time_s < self.last_source:
            raise ValueError('Provider timestamp regressed')
        # Repeated HTTP responses cannot replenish simulated liquidity or generate signals.
        if quote.market_open and quote.source_time_s == self.last_source:
            return False
        mid = quote.price_paise // self.tick_size * self.tick_size
        bid, ask = mid - self.spread, mid + self.spread
        self.sequence += 1
        if self.engine.tick(elapsed_ns, self.sequence, bid, ask, self.quantity, quote.market_open) != 1:
            raise ValueError('C++ engine rejected the normalized quote')
        self.last_source = quote.source_time_s
        self.quotes += 1
        self.tick_writer.writerow([elapsed_ns, self.sequence, self.instrument, bid, ask,
                                   self.quantity, self.quantity, int(quote.market_open)])
        self.ticks_file.flush()
        self.drain()
        if not quote.market_open:
            self.stop_reason = 'market_closed'
        return True

    def drain(self):
        for event in self.engine.events():
            event['side'] = 'buy' if event['side'] == 0 else 'sell'
            self.writer.writerow(event)
            self.fills += event['event'] == 'fill'
            self.accepted += event['event'] == 'accepted'
            self.rejected += event['event'] in ('risk', 'rejected')
        self.audit.flush()
        state = self.engine.snapshot()
        if state['audit_failed']:
            raise RuntimeError('C++ audit queue overflowed; run is incomplete')
        self.peak = max(self.peak, state['pnl_paise'])
        self.drawdown = max(self.drawdown, self.peak - state['pnl_paise'])

    def report(self, *, finished=False):
        self.drain()
        state = self.engine.snapshot()
        report = {
            'mode': 'paper', 'feed_status': 'delayed/indicative', 'symbol': self.symbol,
            'currency': 'INR', 'initial_capital_paise': self.capital,
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
            'risk_halted': self.risk_halted, 'engine_kill_reason_code': state['kill_reason'],
            'fill_model': 'completed 1m close plus simulated spread, depth and latency',
            'terminal_inventory_is_marked_not_liquidated': True,
        }
        temporary = self.output / 'summary.json.tmp'
        temporary.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
        temporary.replace(self.output / 'summary.json')
        return report

    def close(self):
        self.audit.close()
        self.raw.close()
        self.ticks_file.close()


async def run_session(args, feed=None) -> int:
    if (not 15 <= args.poll_seconds <= 60 or not 60 <= args.max_source_age <= 3600 or
            not math.isfinite(args.duration) or args.duration < 0):
        raise ValueError('poll-seconds must be 15..60, max-source-age 60..3600, duration nonnegative')
    feed = feed or YahooQuoteFeed(args.symbol)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    output = args.output_dir / f'{stamp}-{feed.symbol}-{uuid.uuid4().hex[:8]}'
    output.mkdir(parents=True, exist_ok=False)
    settings = configuration(args.config)
    (output / 'config.conf').write_bytes(args.config.read_bytes())
    started = time.monotonic_ns()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    previous_handlers = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, lambda *_: loop.call_soon_threadsafe(stop.set))
    print(f'PAPER ONLY | {feed.symbol} | Yahoo delayed/indicative prices | no broker account', flush=True)
    print('Spread/depth/fills are simulated. Each launch creates a fresh virtual account.', flush=True)
    print(f'Reports: {output}', flush=True)
    task = None
    session = None
    code = 0
    try:
        with PaperEngine(args.library or find_library(), args.config) as engine:
            session = PaperSession(engine, settings, feed.symbol, output, max_source_age_s=args.max_source_age)
            try:
                next_poll = 0.0
                while not stop.is_set():
                    elapsed_ns = time.monotonic_ns() - started
                    if args.duration and elapsed_ns >= args.duration * 1_000_000_000:
                        session.stop_reason = 'duration_complete'
                        break
                    engine.heartbeat(elapsed_ns)
                    session.drain()
                    if engine.snapshot()['killed']:
                        session.stop_reason = 'risk_halt'
                        session.risk_halted = True
                        code = 3
                        break
                    if task is None and time.monotonic() >= next_poll:
                        task = asyncio.create_task(asyncio.to_thread(feed.fetch))
                    if task is not None and task.done():
                        quote = task.result()
                        task = None
                        changed = session.accept(quote, time.monotonic_ns() - started, int(time.time()))
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
                    # Heartbeat keeps running while HTTP is waiting in its worker thread.
                    await asyncio.sleep(0.1)
                if stop.is_set():
                    session.stop_reason = 'interrupted'
            except Exception as error:
                engine.disconnect()
                session.stop_reason = 'feed_or_io_error'
                session.error = str(error)
                code = 3
                print(f'Paper trading stopped: {error}', file=sys.stderr, flush=True)
            finally:
                if engine.snapshot()['killed'] and code == 0:
                    session.stop_reason = 'risk_halt'
                    session.risk_halted = True
                    code = 3
                engine.shutdown()
                report = session.report(finished=True)
                print(f"Stopped: {report['stop_reason']} | pending orders {report['open_orders']} | "
                      f"virtual cash INR {money(report['virtual_cash_paise'])}", flush=True)
    finally:
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if session is not None:
            session.close()
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
    return code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--symbol', default='RELIANCE.NS', help='Example symbol, not a recommendation')
    parser.add_argument('--config', type=Path, default=ROOT / 'config/network-paper.conf')
    parser.add_argument('--library', type=Path)
    parser.add_argument('--poll-seconds', type=float, default=15)
    parser.add_argument('--max-source-age', type=int, default=1800)
    parser.add_argument('--duration', type=float, default=0, help='0 runs until stop, closed market or error')
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'reports/network-paper')
    args = parser.parse_args()
    try:
        return asyncio.run(run_session(args))
    except (OSError, ValueError, RuntimeError) as error:
        print(f'Error: {error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
