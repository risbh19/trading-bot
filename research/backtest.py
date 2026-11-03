"""Replay the C++ paper engine with input snapshots and independent ledger checks."""

import argparse
import json
from pathlib import Path
import subprocess

try:
    from .validation import (digest, fee_provenance, fresh_output, read_config,
                             reconcile_audit, validate_data)
except ImportError:  # Direct script invocation.
    from validation import (digest, fee_provenance, fresh_output, read_config,
                            reconcile_audit, validate_data)


def run_backtest(binary: Path, ticks: Path, config: Path, output: Path, *,
                 fee_profile: Path | None = None, as_of: str | None = None,
                 require_verified_fees: bool = False,
                 slippage_ticks: int | None = None, participation_bps: int | None = None) -> dict:
    config_data = config.read_bytes()
    settings = read_config(config_data)
    for key, value, maximum in (('slippage_ticks', slippage_ticks, 1000),
                                ('participation_bps', participation_bps, 10_000)):
        if value is not None:
            if type(value) is not int or not 0 <= value <= maximum:
                raise ValueError(f'{key} must be an integer from 0 through {maximum}')
            settings[key] = str(value)
    if slippage_ticks is not None or participation_bps is not None:
        config_data = ''.join(f'{key}={value}\n' for key, value in sorted(settings.items())).encode('utf-8')
    input_data = ticks.read_bytes()
    rows, quality = validate_data(input_data, settings)
    provenance = fee_provenance(settings, fee_profile, as_of, require_verified_fees)
    binary_hash = digest(binary.read_bytes())
    fresh_output(output, [binary, ticks, config] + ([fee_profile] if fee_profile else []))
    # Native replay consumes the exact bytes validated here, not mutable source paths.
    saved_ticks, saved_config = output / 'ticks.csv', output / 'config.conf'
    saved_ticks.write_bytes(input_data)
    saved_config.write_bytes(config_data)
    if fee_profile:
        profile_data = fee_profile.read_bytes()
        if digest(profile_data) != provenance['profile_sha256']:
            raise RuntimeError('Fee profile changed while preparing the replay')
        (output / 'fee-profile.json').write_bytes(profile_data)
    audit, summary = (output / 'audit.csv').resolve(), (output / 'summary.json').resolve()
    command = [str(binary.resolve()), '--mode', 'paper', '--input', str(saved_ticks.resolve()),
               '--config', str(saved_config.resolve()), '--audit', str(audit), '--summary', str(summary)]
    result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=300)
    if digest(binary.read_bytes()) != binary_hash:
        raise RuntimeError('Replay executable changed during the run')
    if result.returncode not in (0, 3):
        raise RuntimeError(result.stderr.strip() or f'Replay failed: {result.returncode}')
    report = json.loads(summary.read_text(encoding='utf-8'))
    if report.get('mode') != 'paper' or bool(report['risk_halted']) != (result.returncode == 3):
        raise RuntimeError('Native paper-mode/exit-status reconciliation failed')
    report.update(reconcile_audit(report, audit, rows, settings))
    report.update(engine='same C++ strategy/risk/paper broker', dataset=str(ticks.resolve()),
                  binary_sha256=binary_hash, config_sha256=digest(config_data),
                  data_quality=quality, fee_provenance=provenance,
                  execution_model=dict(slippage_ticks=int(settings.get('slippage_ticks', 1)),
                                       participation_bps=int(settings.get('participation_bps', 10_000))),
                  completed=not report['risk_halted'] and report['stop_reason'] == 'end_of_replay',
                  terminal_inventory_is_marked_not_liquidated=True)
    (output / 'backtest.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--input', type=Path, default=Path('data/sample_ticks.csv'))
    parser.add_argument('--config', type=Path, default=Path('config/paper.conf'))
    parser.add_argument('--output-dir', type=Path, default=Path('reports/backtest'))
    parser.add_argument('--slippage-ticks', type=int)
    parser.add_argument('--participation-bps', type=int)
    parser.add_argument('--fee-profile', type=Path)
    parser.add_argument('--as-of', help='Fee applicability date, YYYY-MM-DD')
    parser.add_argument('--require-verified-fees', action='store_true')
    args = parser.parse_args()
    print(json.dumps(run_backtest(args.binary, args.input, args.config, args.output_dir,
                                 fee_profile=args.fee_profile, as_of=args.as_of,
                                 require_verified_fees=args.require_verified_fees,
                                 slippage_ticks=args.slippage_ticks,
                                 participation_bps=args.participation_bps), indent=2))


if __name__ == '__main__':
    main()
