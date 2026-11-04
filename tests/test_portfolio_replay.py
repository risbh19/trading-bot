"""Native multi-instrument replay with independent account and fee reconstruction."""

import csv
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

BINARY = Path(sys.argv.pop(1)).resolve()
ROOT = Path(sys.argv.pop(1)).resolve()
sys.path.insert(0, str(ROOT))
from research.validation import FEE_KEYS, fee_total, read_config, setting


class PortfolioReplayTests(unittest.TestCase):
    def run_replay(self, output, *, ticks=None, config=None):
        output.mkdir()
        result = subprocess.run([
            str(BINARY), '--mode', 'paper', '--input', str(ticks or ROOT / 'data/portfolio_ticks.csv'),
            '--config', str(config or ROOT / 'config/portfolio.conf'),
            '--audit', str(output / 'audit.csv'), '--summary', str(output / 'summary.json')],
            capture_output=True, text=True, timeout=30)
        report = json.loads((output / 'summary.json').read_text()) if (output / 'summary.json').exists() else None
        return result, report

    def test_deterministic_shared_account_and_independent_ledger(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, report = self.run_replay(root / 'a')
            second, repeated = self.run_replay(root / 'b')
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(report, repeated)
            self.assertEqual((root / 'a/audit.csv').read_bytes(), (root / 'b/audit.csv').read_bytes())
            self.assertFalse(report['risk_halted'])
            self.assertEqual(report['ticks'], 40)
            self.assertEqual(report['initial_capital_paise'], 10_000_000)  # Once for the whole universe.
            with (root / 'a/audit.csv').open(newline='') as stream:
                events = list(csv.DictReader(stream))
            rates = {}
            for instrument, filename in ((1, 'portfolio-a.conf'), (2, 'portfolio-b.conf')):
                config = read_config((ROOT / 'config' / filename).read_bytes())
                rates[instrument] = {key: setting(config, key) for key in FEE_KEYS}
            cash, fees, positions = dict.fromkeys((1, 2), 0), dict.fromkeys((1, 2), 0), dict.fromkeys((1, 2), 0)
            orders, fills_by_tick, fill_symbols = {}, {}, set()
            for row in events:
                instrument = int(row['instrument'])
                key = instrument, int(row['order_id'])
                units, price, paid = int(row['quantity']), int(row['price_paise']), int(row['fee_paise'])
                if row['event'] == 'accepted':
                    self.assertNotIn(key, orders)
                    orders[key] = dict(remaining=units, turnover=0, fees=0, terminal=False)
                elif row['event'] == 'fill':
                    self.assertIn(key, orders)
                    order = orders[key]
                    self.assertFalse(order['terminal'])
                    self.assertTrue(0 < units <= order['remaining'])
                    order['turnover'] += units * price
                    expected_fee = fee_total(order['turnover'], row['side'], rates[instrument])
                    self.assertEqual(paid, expected_fee - order['fees'])
                    order['fees'] = expected_fee
                    order['remaining'] -= units
                    order['terminal'] = order['remaining'] == 0
                    delta_position = units if row['side'] == 'buy' else -units
                    delta_cash = (-price * units if row['side'] == 'buy' else price * units) - paid
                    positions[instrument] += delta_position
                    cash[instrument] += delta_cash
                    fees[instrument] += paid
                    fills_by_tick.setdefault((instrument, int(row['time_ns'])), []).append(
                        (delta_position, delta_cash))
                    fill_symbols.add(instrument)
                elif row['event'] in ('cancelled', 'rejected'):
                    self.assertIn(key, orders)
                    self.assertFalse(orders[key]['terminal'])
                    self.assertEqual(units, orders[key]['remaining'])
                    orders[key]['terminal'] = True
                self.assertEqual(int(row['position']), positions[instrument])
            self.assertEqual(fill_symbols, {1, 2})
            self.assertTrue(all(order['terminal'] for order in orders.values()))
            self.assertIn((1, 1), orders)
            self.assertIn((2, 1), orders)
            running_cash, running_positions, marks = dict.fromkeys((1, 2), 0), dict.fromkeys((1, 2), 0), dict.fromkeys((1, 2), 0)
            peak = drawdown = 0
            with (ROOT / 'data/portfolio_ticks.csv').open(newline='') as stream:
                for tick in csv.DictReader(stream):
                    instrument, now = int(tick['instrument']), int(tick['time_ns'])
                    marks[instrument] = (int(tick['bid_paise']) + int(tick['ask_paise'])) // 2
                    for delta_position, delta_cash in fills_by_tick.get((instrument, now), ()):
                        running_positions[instrument] += delta_position
                        running_cash[instrument] += delta_cash
                    pnl = sum(running_cash[i] + running_positions[i] * marks[i] for i in (1, 2))
                    peak = max(peak, pnl)
                    drawdown = max(drawdown, peak - pnl)
            self.assertEqual(report['max_drawdown_paise'], drawdown)
            total_pnl = sum(cash[i] + positions[i] * marks[i] for i in (1, 2))
            self.assertEqual(report['virtual_cash_paise'], 10_000_000 + sum(cash.values()))
            self.assertEqual(report['virtual_equity_paise'], 10_000_000 + total_pnl)
            self.assertEqual(report['pnl_paise'], total_pnl)
            self.assertEqual(report['fees_paise'], sum(fees.values()))
            self.assertEqual(report['gross_notional_paise'], sum(positions[i] * marks[i] for i in (1, 2)))
            self.assertEqual(report['open_orders'], 0)
            self.assertEqual(report['reserved_cash_paise'], 0)
            self.assertGreaterEqual(report['virtual_cash_paise'], 0)
            for state in report['instruments']:
                instrument = state['instrument']
                self.assertEqual(state['position'], positions[instrument])
                self.assertEqual(state['cash_flow_paise'], cash[instrument])
                self.assertEqual(state['fees_paise'], fees[instrument])

    def test_live_duplicate_universe_and_constituent_capital_are_rejected(self):
        result = subprocess.run([str(BINARY), '--mode', 'live'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('Live mode is disabled', result.stderr)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = (ROOT / 'config/portfolio-a.conf').read_text()
            a, b = root / 'a.conf', root / 'b.conf'
            a.write_text(base)
            manifest = root / 'portfolio.conf'
            for case, content in (('duplicate', base), ('capital', base.replace('instrument_id=1', 'instrument_id=2') +
                                                            '\ninitial_capital_paise=10000000\n'),
                                  ('short', base.replace('instrument_id=1', 'instrument_id=2').replace(
                                      'allow_short=false', 'allow_short=true'))):
                b.write_text(content)
                manifest.write_text('instrument_config=a.conf\ninstrument_config=b.conf\n')
                with self.subTest(case=case):
                    result, report = self.run_replay(root / case, config=manifest)
                    self.assertEqual(result.returncode, 2)
                    self.assertIsNone(report)

    def test_input_output_collisions_preserve_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'portfolio.conf'
            source = ROOT / 'config/portfolio-a.conf'
            path.write_text(f'instrument_config={source.as_posix()}\n')
            original = path.read_bytes()
            result = subprocess.run([
                str(BINARY), '--input', str(ROOT / 'data/portfolio_ticks.csv'), '--config', str(path),
                '--audit', str(path), '--summary', str(Path(temporary) / 'summary.json')],
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn('must be distinct', result.stderr)
            self.assertEqual(path.read_bytes(), original)

    def test_warmup_barrier_and_global_clock_regression(self):
        header = 'time_ns,sequence,instrument,bid_paise,ask_paise,bid_quantity,ask_quantity,session_open\n'
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            incomplete = root / 'incomplete.csv'
            incomplete.write_bytes((ROOT / 'data/sample_ticks.csv').read_bytes())
            result, report = self.run_replay(root / 'warmup', ticks=incomplete)
            self.assertEqual(result.returncode, 2)
            self.assertIsNone(report)
            with (root / 'warmup/audit.csv').open(newline='') as stream:
                self.assertFalse(any(row['event'] == 'accepted' for row in csv.DictReader(stream)))
            regressed = root / 'regressed.csv'
            regressed.write_text(header + '0,1,1,10000,10010,10,10,1\n0,1,2,10000,10010,10,10,1\n'
                                 '100000000,2,1,10020,10030,10,10,1\n50000000,2,2,10020,10030,10,10,1\n')
            result, report = self.run_replay(root / 'regressed', ticks=regressed)
            self.assertEqual(result.returncode, 3)
            self.assertTrue(report['risk_halted'])
            self.assertEqual(report['stop_reason'], 'invalid')
            self.assertEqual(report['open_orders'], 0)
            self.assertEqual(report['fills'], 0)

    def test_existing_outputs_are_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            audit = root / 'audit.csv'
            audit.write_text('previous report\n')
            result = subprocess.run([
                str(BINARY), '--config', str(ROOT / 'config/portfolio.conf'),
                '--input', str(ROOT / 'data/portfolio_ticks.csv'),
                '--audit', str(audit), '--summary', str(root / 'summary.json')],
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn('already exist', result.stderr)
            self.assertEqual(audit.read_text(), 'previous report\n')


if __name__ == '__main__':
    unittest.main(verbosity=2)
