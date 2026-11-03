# Reproducible paper research

Research commands invoke the same C++ strategy, risk manager, fees and paper broker. Every run requires a new or empty output directory and saves the exact input/configuration bytes, SHA-256 hashes, executable hash, native audit/summary and independently checked research report. Money uses integer paise.

## Replay and execution scenarios

```sh
python research/backtest.py --binary build/paper_trader --output-dir reports/baseline
python research/backtest.py --binary build/paper_trader --slippage-ticks 2 --participation-bps 5000 --output-dir reports/cost-stress
```

For a Windows Visual Studio build use `build/Release/paper_trader.exe`. Run each command on one line in PowerShell.

`participation_bps` limits the fraction of displayed depth available to the simulator on each side of each new snapshot: 10000 means 100%, 5000 means 50%, and 0 permits no fills. All orders share the cap in accepted-ID FIFO order, rounded down to complete lots. IOC/market remainders are cancelled. Duplicate quotes cannot refresh depth. The default 10000 preserves previous results.

Slippage remains a deterministic adverse number of ticks. Latency, limit-price protection and cash/risk checks remain active. Compare configurations with a fixed strategy. PnL need not decrease monotonically with more slippage because acceptance and the subsequent strategy path can change.

This is a top-of-book sensitivity model. A new snapshot does not prove that new executable volume exists. Queue position, hidden orders, market impact, depth beyond the touch and calibrated fill probabilities remain absent.

## Data and accounting checks

Preflight rejects malformed rows, invalid numeric bounds, duplicate/regressing timestamps or sequences, mixed instruments, crossed/off-tick/out-of-band quotes, invalid depth and invalid session flags. Research requires strictly increasing timestamps so each audit fill maps to one snapshot; the lower-level engine still accepts equal-time snapshots with increasing sequences.

Reports count sequence gaps, maximum time gap and zero-depth rows. Gaps are reported rather than interpolated. The native stale-feed stop stays active; a halted replay has `completed=false`. This CSV format uses relative session timestamps and lacks calendar, timezone and corporate-action metadata. Validation does not establish that data is point-in-time exchange data.

The independent ledger reconstructs cumulative turnover/fees for every order and checks lifecycle, fill latency, price/limit protection, lots, shared liquidity, running position, cash, fees, marked PnL, drawdown and terminal order counts. A mismatch fails before `backtest.json` is written. Native diagnostic artifacts remain; use a fresh output directory for another attempt.

Terminal inventory is marked at the final midprice without liquidation. `gross_pnl_paise` adds fees back to net PnL; `adverse_slippage_paise` measures price movement from the contemporaneous touch. Neither adds hypothetical terminal liquidation costs.

## Rolling train/test evaluation

```sh
python research/generate_ticks.py --rows 1000 --seed 42 --output reports/research-input.csv
python research/walk_forward.py --binary build/paper_trader --input reports/research-input.csv --train-rows 400 --test-rows 100 --gap-rows 10 --momentum-ticks 1 2 3 --output-dir reports/walk-forward
```

Each fold contains a rolling training window, an excluded gap, then a later holdout. The next fold advances by the holdout length, so holdouts never overlap. Row ranges are zero-based and end-exclusive. Incomplete trailing rows are counted and excluded.

Only training net PnL ranks momentum thresholds; ties choose the lower threshold. The chosen configuration is tested once on that fold's holdout. Halted/incomplete training candidates cannot win. A halted/incomplete holdout fails the experiment instead of being silently dropped. Later training may include earlier holdouts once those rows are historical.

Each window starts a new strategy, account, orders and risk state. Its first quote initializes the strategy, and terminal inventory is marked without carrying into the next fold. `sum_of_independent_test_pnl_paise` sums separate experiments; it is not a continuous capital curve or annualized return. Repeated human tuning against these holdouts requires a further untouched final dataset.

## Fee evidence

The supplied rates remain illustrative fixtures. This change does not select or certify a broker/product tariff.

Copy `config/fee-profile.example.json`. Verify rates and rounding against the selected broker/product/exchange's applicable tariff and contract notes, then update the matching configuration. Record the broker, exchange, segment, product, evidence reference, HTTPS source links, verification owner/date and effective date range before setting `status` to `verified`.

```sh
python research/backtest.py --binary build/paper_trader --config config/selected-paper.conf --fee-profile config/selected-fees.json --as-of 2026-11-04 --require-verified-fees --output-dir reports/verified-paper
```

The same fee flags work with walk-forward. Missing verification/evidence, mismatched rates/product/currency, invalid dates, an out-of-range applicability date or future verification date fails the gate. Reports use `declared_verified`: an operator attestation, not automatic auditing of source pages or contract notes.

The engine rounds each component upward to paise on cumulative order turnover, applies one brokerage cap per order and GST to brokerage/exchange/SEBI charges. Check these conventions and missing components against contract notes. A profile cannot correct a different billing/rounding model. Relative timestamps cannot prove `--as-of` matches the trading date; the dataset owner must establish that relationship.
