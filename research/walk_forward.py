"""Rolling chronological training/holdout windows, using only the native paper engine."""

import argparse
import json
from pathlib import Path

try:
    from .backtest import run_backtest
    from .validation import digest, fee_provenance, fresh_output, read_config, validate_data, write_ticks
except ImportError:
    from backtest import run_backtest
    from validation import digest, fee_provenance, fresh_output, read_config, validate_data, write_ticks


def windows(count: int, train_rows: int, test_rows: int, gap_rows: int):
    if (any(type(value) is not int for value in (train_rows, test_rows, gap_rows)) or
            train_rows < 2 or test_rows < 2 or gap_rows < 0):
        raise ValueError('Training/test windows need at least two rows; gap must be nonnegative')
    if count < train_rows + gap_rows + test_rows:
        raise ValueError('Not enough data for a complete training/gap/test fold')
    for start in range(0, count - train_rows - gap_rows - test_rows + 1, test_rows):
        train_end = start + train_rows
        test_start = train_end + gap_rows
        yield start, train_end, test_start, test_start + test_rows


def run_walk_forward(binary: Path, ticks: Path, config: Path, output: Path, *,
                     train_rows: int, test_rows: int, gap_rows: int = 1,
                     momentum_ticks=(1, 2, 3), fee_profile: Path | None = None,
                     as_of: str | None = None, require_verified_fees: bool = False) -> dict:
    candidates = sorted(set(momentum_ticks))
    if not candidates or len(candidates) > 100 or any(
            type(value) is not int or not 1 <= value <= 10_000 for value in candidates):
        raise ValueError('Supply 1 through 100 distinct momentum thresholds in [1, 10000]')
    source, config_data = ticks.read_bytes(), config.read_bytes()
    settings = read_config(config_data)
    rows, quality = validate_data(source, settings)
    splits = list(windows(len(rows), train_rows, test_rows, gap_rows))
    provenance = fee_provenance(settings, fee_profile, as_of, require_verified_fees)
    fresh_output(output, [binary, ticks, config] + ([fee_profile] if fee_profile else []))
    (output / 'source.csv').write_bytes(source)
    (output / 'source.conf').write_bytes(config_data)
    if fee_profile:
        profile_data = fee_profile.read_bytes()
        if digest(profile_data) != provenance['profile_sha256']:
            raise RuntimeError('Fee profile changed while preparing the experiment')
        fee_profile = output / 'fee-profile.json'
        fee_profile.write_bytes(profile_data)
    folds = []
    for index, (start, train_end, test_start, test_end) in enumerate(splits, 1):
        fold_dir = output / f'fold-{index:03d}'
        fold_dir.mkdir()
        training, holdout = fold_dir / 'train.csv', fold_dir / 'test.csv'
        write_ticks(training, rows[start:train_end])
        write_ticks(holdout, rows[test_start:test_end])
        scores, configs = [], {}
        for threshold in candidates:
            candidate = dict(settings, momentum_ticks=str(threshold))
            candidate_config = fold_dir / f'momentum-{threshold}.conf'
            candidate_config.write_text(''.join(f'{key}={value}\n' for key, value in sorted(candidate.items())),
                                        encoding='utf-8')
            configs[threshold] = candidate_config
            result = run_backtest(binary, training, candidate_config, fold_dir / f'train-{threshold}',
                                  fee_profile=fee_profile, as_of=as_of,
                                  require_verified_fees=require_verified_fees)
            scores.append(dict(momentum_ticks=threshold, pnl_paise=result['pnl_paise'],
                               eligible=result['completed'], stop_reason=result['stop_reason']))
        eligible = [score for score in scores if score['eligible']]
        if not eligible:
            raise RuntimeError(f'Fold {index}: all training candidates halted or were incomplete')
        chosen = max(eligible, key=lambda score: (score['pnl_paise'], -score['momentum_ticks']))
        # Holdout runs once, after selection; its result never ranks candidates.
        tested = run_backtest(binary, holdout, configs[chosen['momentum_ticks']], fold_dir / 'holdout',
                              fee_profile=fee_profile, as_of=as_of,
                              require_verified_fees=require_verified_fees)
        if not tested['completed']:
            raise RuntimeError(f'Fold {index}: holdout halted/incomplete; inspect its saved report')
        folds.append(dict(fold=index, training_rows=[start, train_end],
                          test_rows=[test_start, test_end],
                          train_last_time_ns=rows[train_end - 1]['time_ns'],
                          test_first_time_ns=rows[test_start]['time_ns'],
                          training_scores=scores, selected_momentum_ticks=chosen['momentum_ticks'],
                          test_pnl_paise=tested['pnl_paise'], test_fees_paise=tested['fees_paise'],
                          test_position=tested['position'], test_fills=tested['fills'],
                          test_config_sha256=tested['config_sha256']))
    report = dict(mode='paper', data_quality=quality, fee_provenance=provenance,
                  source_config_sha256=digest(config_data),
                  selection='training net PnL only; ties choose the lower momentum threshold',
                  row_ranges='zero-based, end-exclusive; test windows never overlap',
                  cold_start_each_window=True, terminal_inventory_is_marked_not_liquidated=True,
                  train_rows=train_rows, test_rows=test_rows, gap_rows=gap_rows,
                  unused_tail_rows=len(rows) - splits[-1][3], folds=folds,
                  sum_of_independent_test_pnl_paise=sum(fold['test_pnl_paise'] for fold in folds),
                  note='Each window resets strategy, cash, orders and risk. Fold sum is not a continuous portfolio return.')
    (output / 'walk-forward.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--config', type=Path, default=Path('config/paper.conf'))
    parser.add_argument('--output-dir', type=Path, default=Path('reports/walk-forward'))
    parser.add_argument('--train-rows', type=int, required=True)
    parser.add_argument('--test-rows', type=int, required=True)
    parser.add_argument('--gap-rows', type=int, default=1)
    parser.add_argument('--momentum-ticks', type=int, nargs='+', default=[1, 2, 3])
    parser.add_argument('--fee-profile', type=Path)
    parser.add_argument('--as-of')
    parser.add_argument('--require-verified-fees', action='store_true')
    args = parser.parse_args()
    print(json.dumps(run_walk_forward(
        args.binary, args.input, args.config, args.output_dir, train_rows=args.train_rows,
        test_rows=args.test_rows, gap_rows=args.gap_rows, momentum_ticks=args.momentum_ticks,
        fee_profile=args.fee_profile, as_of=args.as_of,
        require_verified_fees=args.require_verified_fees), indent=2))


if __name__ == '__main__':
    main()
