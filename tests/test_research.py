"""Research preflight, accounting tamper detection and native chronological validation."""

import copy
import csv
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

BINARY = Path(sys.argv.pop(1)).resolve()
ROOT = Path(sys.argv.pop(1)).resolve()
sys.path.insert(0, str(ROOT))
from research.backtest import run_backtest
from research.validation import (FEE_KEYS, fee_provenance, fee_total, fresh_output,
                                 read_config, reconcile_audit, setting, validate_data, write_ticks)
from research.walk_forward import run_walk_forward, windows

CONFIG = ROOT / 'config/paper.conf'
TICKS = ROOT / 'data/sample_ticks.csv'


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.settings = read_config(CONFIG.read_bytes())

    def test_bad_data_is_rejected_before_replay(self):
        rows = list(csv.reader(io.StringIO(TICKS.read_text(encoding='utf-8'))))
        mutations = [
            (2, 0, rows[1][0]), (2, 1, rows[1][1]), (2, 2, '2'),
            (2, 3, '10021'), (2, 3, '10040'), (2, 4, '999999999'),
            (2, 5, '1000001'), (2, 5, '-1'), (2, 7, '2'), (2, 3, 'NaN'),
            (2, 0, str(2**64)), (2, 1, '0'),
        ]
        for row, column, value in mutations:
            with self.subTest(column=column, value=value):
                changed = copy.deepcopy(rows)
                changed[row][column] = value
                stream = io.StringIO()
                csv.writer(stream).writerows(changed)
                with self.assertRaises(ValueError):
                    validate_data(stream.getvalue().encode('utf-8'), self.settings)

    def test_quality_records_gaps_and_zero_depth(self):
        rows, quality = validate_data(TICKS.read_bytes(), self.settings)
        self.assertEqual(quality['rows'], 20)
        self.assertEqual(quality['missing_sequences'], 0)
        self.assertEqual(quality['zero_depth_rows'], 1)
        self.assertEqual(rows[-1]['session_open'], 0)

    def test_fee_reference_caps_cumulative_turnover(self):
        rates = dict.fromkeys(FEE_KEYS, 0)
        rates.update(brokerage_ppm=10_000, brokerage_cap_paise=50)
        self.assertEqual(fee_total(40_000, 'buy', rates), 50)
        self.assertEqual(fee_total(100_000, 'buy', rates), 50)
        rates.update(stt_sell_ppm=250, stamp_buy_ppm=30, gst_ppm=180_000)
        self.assertEqual(fee_total(1_000_000, 'buy', rates), 89)
        self.assertEqual(fee_total(1_000_000, 'sell', rates), 309)

    def test_fee_attestation_is_explicit_and_date_bound(self):
        self.assertEqual(fee_provenance(self.settings, None, None, False)['status'], 'illustrative')
        with self.assertRaisesRegex(ValueError, 'requires a verified'):
            fee_provenance(self.settings, None, None, True)
        fixture = dict(schema_version=1, status='verified', currency='INR', product='cnc',
                       broker='synthetic-test-broker', exchange='synthetic-test-exchange',
                       segment='test', verified_by='test fixture', evidence='synthetic contract-note fixture',
                       valid_from='2024-01-01', valid_through='2024-12-31', verified_on='2024-01-01',
                       sources=['https://example.invalid/test-evidence'],
                       rates={key: setting(self.settings, key) for key in FEE_KEYS})
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'fees.json'
            path.write_text(json.dumps(fixture), encoding='utf-8')
            result = fee_provenance(self.settings, path, '2024-06-01', True)
            self.assertEqual(result['status'], 'declared_verified')
            for as_of in (None, '2025-01-01', 'bad-date'):
                with self.subTest(as_of=as_of), self.assertRaises(ValueError):
                    fee_provenance(self.settings, path, as_of, True)
            for changed in (dict(fixture, evidence=''), dict(fixture, sources=[]),
                            dict(fixture, product='mis'), dict(fixture, rates={}),
                            dict(fixture, verified_on='9999-01-01')):
                path.write_text(json.dumps(changed), encoding='utf-8')
                with self.assertRaises(ValueError):
                    fee_provenance(self.settings, path, '2024-06-01', True)

    def test_outputs_cannot_clobber_inputs_or_reuse_stale_reports(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'results'
            with self.assertRaises(ValueError):
                fresh_output(Path(temporary), [output / 'source.csv'])
            fresh_output(output, [TICKS])
            (output / 'summary.json').write_text('{}', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'empty'):
                fresh_output(output, [TICKS])

    def test_split_boundaries_gap_and_incomplete_tail(self):
        self.assertEqual(list(windows(20, 8, 4, 1)), [(0, 8, 9, 13), (4, 12, 13, 17)])
        for sizes in ((1, 4, 0), (4, 1, 0), (8, 4, -1), (18, 4, 0)):
            with self.subTest(sizes=sizes), self.assertRaises(ValueError):
                list(windows(20, *sizes))


class NativeResearchTests(unittest.TestCase):
    def test_snapshots_and_cost_scenarios_use_actual_engine(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = run_backtest(BINARY, TICKS, CONFIG, root / 'base')
            capped = run_backtest(BINARY, TICKS, CONFIG, root / 'capped',
                                  participation_bps=5000, slippage_ticks=2)
            none = run_backtest(BINARY, TICKS, CONFIG, root / 'none', participation_bps=0)
            self.assertEqual(baseline['pnl_paise'], -847)
            self.assertEqual(baseline['gross_pnl_paise'], -660)
            self.assertEqual(baseline['adverse_slippage_paise'],
                             5 * (baseline['buy_units'] + baseline['sell_units']))
            self.assertEqual((root / 'base/ticks.csv').read_bytes(), TICKS.read_bytes())
            self.assertEqual((root / 'base/config.conf').read_bytes(), CONFIG.read_bytes())
            self.assertTrue(baseline['completed'])
            self.assertEqual(capped['execution_model'], dict(slippage_ticks=2, participation_bps=5000))
            self.assertEqual(none['fills'], 0)
            self.assertEqual(none['cash_paise'], 0)
            self.assertEqual(none['fees_paise'], 0)
            self.assertTrue(none['completed'])

    def test_native_summary_and_audit_tampering_is_detected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = run_backtest(BINARY, TICKS, CONFIG, root / 'run')
            config = read_config(CONFIG.read_bytes())
            rows, _ = validate_data(TICKS.read_bytes(), config)
            audit = root / 'run/audit.csv'
            for field in ('cash_paise', 'position', 'fees_paise', 'pnl_paise', 'max_drawdown_paise'):
                bad = dict(result)
                bad[field] += 1
                with self.subTest(field=field), self.assertRaises(RuntimeError):
                    reconcile_audit(bad, audit, rows, config)
            with audit.open(newline='', encoding='utf-8') as stream:
                reader = csv.DictReader(stream)
                fields, events = reader.fieldnames, list(reader)
            for field in ('fee_paise', 'quantity', 'price_paise'):
                bad_events = copy.deepcopy(events)
                fill = next(row for row in bad_events if row['event'] == 'fill')
                fill[field] = str(int(fill[field]) + 1)
                tampered = root / f'{field}.csv'
                with tampered.open('w', newline='', encoding='utf-8') as stream:
                    writer = csv.DictWriter(stream, fieldnames=fields)
                    writer.writeheader()
                    writer.writerows(bad_events)
                with self.subTest(field=field), self.assertRaises(RuntimeError):
                    reconcile_audit(result, tampered, rows, config)

    def test_holdout_changes_cannot_change_the_first_training_selection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = run_walk_forward(BINARY, TICKS, CONFIG, root / 'original',
                                     train_rows=8, test_rows=4)
            settings = read_config(CONFIG.read_bytes())
            rows, _ = validate_data(TICKS.read_bytes(), settings)
            for tick in rows[9:]:
                tick['bid_paise'] += 50
                tick['ask_paise'] += 50
            changed = root / 'changed.csv'
            write_ticks(changed, rows)
            second = run_walk_forward(BINARY, changed, CONFIG, root / 'changed',
                                      train_rows=8, test_rows=4)
            a, b = first['folds'][0], second['folds'][0]
            self.assertEqual(a['training_scores'], b['training_scores'])
            self.assertEqual(a['selected_momentum_ticks'], b['selected_momentum_ticks'])
            self.assertEqual(a['test_config_sha256'], b['test_config_sha256'])
            self.assertEqual(first['unused_tail_rows'], 3)
            for previous, current in zip(first['folds'], first['folds'][1:]):
                self.assertLessEqual(previous['test_rows'][1], current['test_rows'][0])
            for fold in first['folds']:
                self.assertLess(fold['train_last_time_ns'], fold['test_first_time_ns'])
            self.assertTrue(first['cold_start_each_window'])

    def test_halted_training_is_never_selected_as_best_candidate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rows, _ = validate_data(TICKS.read_bytes(), read_config(CONFIG.read_bytes()))
            for tick in rows[2:]:
                tick['time_ns'] += 3_000_000_000
            stale = root / 'stale.csv'
            write_ticks(stale, rows)
            with self.assertRaisesRegex(RuntimeError, 'all training candidates halted'):
                run_walk_forward(BINARY, stale, CONFIG, root / 'results', train_rows=8, test_rows=4)
            self.assertFalse((root / 'results/walk-forward.json').exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
