"""End-to-end replay validation, including deterministic artifacts and fail-closed CLI."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

BINARY = Path(sys.argv.pop(1)).resolve()
ROOT = Path(sys.argv.pop(1)).resolve()
sys.path.insert(0, str(ROOT / 'research'))
from backtest import run_backtest  # noqa: E402


class ReplayTests(unittest.TestCase):
    def test_deterministic_replay_and_reconciliation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            a = run_backtest(BINARY, ROOT / 'data/sample_ticks.csv', ROOT / 'config/paper.conf', root / 'a')
            b = run_backtest(BINARY, ROOT / 'data/sample_ticks.csv', ROOT / 'config/paper.conf', root / 'b')
            self.assertEqual(a, b)
            self.assertEqual((root / 'a/audit.csv').read_bytes(), (root / 'b/audit.csv').read_bytes())
            self.assertEqual(a['ticks'], 20)
            self.assertEqual(a['fills'], 8)
            self.assertEqual(a['position'], 0)
            self.assertEqual(a['pnl_paise'], -847)
            self.assertEqual(a['fees_paise'], 187)
            self.assertFalse(a['risk_halted'])

    def test_live_rejected(self):
        result = subprocess.run([str(BINARY), '--mode', 'live'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('Live mode is disabled', result.stderr)

    def test_bad_config_and_file_collision(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / 'bad.conf'
            config.write_text('mode=paper\nunknown_key=1\n', encoding='utf-8')
            result = subprocess.run([str(BINARY), '--config', str(config)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn('Unknown config key', result.stderr)
            original = config.read_bytes()
            result = subprocess.run([str(BINARY), '--config', str(config), '--audit', str(config)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(config.read_bytes(), original)

    def test_stale_feed_stops_before_fill(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lines = (ROOT / 'data/sample_ticks.csv').read_text(encoding='utf-8').splitlines()
            ticks = root / 'stale.csv'
            ticks.write_text('\n'.join(lines[:3] + ['3000000000,3,1,10020,10030,10,10,1']) + '\n', encoding='utf-8')
            report = run_backtest(BINARY, ticks, ROOT / 'config/paper.conf', root / 'results')
            self.assertTrue(report['risk_halted'])
            self.assertEqual(report['stop_reason'], 'stale')
            self.assertEqual(report['fills'], 0)
            self.assertEqual(report['open_orders'], 0)


if __name__ == '__main__':
    unittest.main()
