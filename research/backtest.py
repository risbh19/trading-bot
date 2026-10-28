"""Replay using the actual C++ strategy, risk checks and broker; no Python signal fork."""

import argparse
import csv
import json
from pathlib import Path
import subprocess


def run_backtest(binary: Path, ticks: Path, config: Path, output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    audit = (output / 'audit.csv').resolve()
    summary = (output / 'summary.json').resolve()
    command = [str(binary.resolve()), '--mode', 'paper', '--input', str(ticks.resolve()),
               '--config', str(config.resolve()), '--audit', str(audit), '--summary', str(summary)]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode not in (0, 3):
        raise RuntimeError(result.stderr.strip() or f'Replay failed: {result.returncode}')
    report = json.loads(summary.read_text(encoding='utf-8'))
    with audit.open(newline='', encoding='utf-8') as stream:
        fills = [row for row in csv.DictReader(stream) if row['event'] == 'fill']
    report['fill_turnover_paise'] = sum(int(row['price_paise']) * int(row['quantity']) for row in fills)
    report['buy_units'] = sum(int(row['quantity']) for row in fills if row['side'] == 'buy')
    report['sell_units'] = sum(int(row['quantity']) for row in fills if row['side'] == 'sell')
    if sum(int(row['fee_paise']) for row in fills) != report['fees_paise']:
        raise RuntimeError('Audit fee reconciliation failed')
    if report['buy_units'] - report['sell_units'] != report['position']:
        raise RuntimeError('Audit position reconciliation failed')
    report['engine'] = 'same C++ strategy/risk/paper broker'
    report['dataset'] = str(ticks.resolve())
    report['terminal_inventory_is_marked_not_liquidated'] = True
    (output / 'backtest.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--input', type=Path, default=Path('data/sample_ticks.csv'))
    parser.add_argument('--config', type=Path, default=Path('config/paper.conf'))
    parser.add_argument('--output-dir', type=Path, default=Path('reports/backtest'))
    args = parser.parse_args()
    print(json.dumps(run_backtest(args.binary, args.input, args.config, args.output_dir), indent=2))


if __name__ == '__main__':
    main()
