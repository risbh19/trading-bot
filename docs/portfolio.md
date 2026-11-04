# Shared-account paper portfolio

`paper_portfolio` is an offline multi-instrument replay runner. It coordinates the same native per-instrument engine, strategy, fees, matching and local risk checks under one INR cash account. It sends no real orders and has no authenticated broker integration.

The supplied example uses two synthetic instruments (IDs 1 and 2), interleaves their snapshots, and shares **INR 100,000 virtual capital once**. These are not ticker recommendations or exchange data. Constituents start with zero capital; assigning full capital to each engine is rejected.

## Run

Build normally, then run from the repository root:

```sh
./build/paper_portfolio --mode paper --config config/portfolio.conf --input data/portfolio_ticks.csv
```

For a Windows Visual Studio build:

```powershell
.\build\Release\paper_portfolio.exe --mode paper --config config/portfolio.conf --input data/portfolio_ticks.csv
```

Default outputs are `reports/portfolio/audit.csv` and `reports/portfolio/summary.json`. Existing outputs are refused; use fresh `--audit` and `--summary` paths for another run. Input/configuration/executable/output collisions and aliases are rejected before writing.

## Configuration and data

`config/portfolio.conf` defines shared capital, gross notional, marked loss, total open orders and submission windows. Repeat `instrument_config` for 1..32 instruments. Paths resolve relative to that portfolio configuration. Each constituent uses the ordinary engine configuration, a unique instrument ID, zero `initial_capital_paise`, `product=cnc` and `allow_short=false`. Unknown/duplicate account keys or invalid metadata fail before replay.

Input has the same eight columns as the single-instrument CSV, but rows may carry different configured IDs. Global `time_ns` must never regress; equal times across instruments are allowed and rows execute in file order. Sequences increase independently for each instrument. Duplicate/regressing per-instrument snapshots are dropped without refreshing liquidity. Unknown instruments, malformed quotes and a regressing global clock fail closed.

Every configured instrument must initialize before the coordinator permits orders. Per-symbol strategies still observe warm-up prices, but cannot submit before that barrier. Incomplete initialization at end of input is an error. This is a single replay session: timestamps are relative nanoseconds, and account request limits do not reset at midnight.

## Account controls

- **One cash budget:** pending buys reserve their limit turnover plus remaining cumulative fees across the entire universe. Partial fills update reservations using cumulative per-order fees; cancellation releases only the unfilled reservation.
- **Fill-time cash protection:** an account guard checks actual incremental fees and the projected remaining reservations before each fill. Even unusual fee configurations that make a sale a cash debit cannot create an overdraft.
- **Gross exposure:** positions and pending buys contribute to a conservative gross bound. The highest current mark or working buy limit values the worst long position for each instrument. Pending sells do not offset buys, and different symbols cannot net their exposure.
- **Loss stops between fills:** the aggregate marked PnL excludes starting capital. Mark changes are checked before matching, and account health is checked after each emitted fill before another order can fill. A breach cancels the remaining orders across all symbols.
- **Shared order limits:** total open orders and rolling second/minute/session request limits apply in addition to each engine's local limits. New valid requests that reach account admission consume the shared request budget even if a later global/local check rejects them. IDs are consumed on rejection and are keyed by `(instrument, order_id)`.
- **Feed/session safety:** all initialized symbols receive heartbeat checks before each input tick. One stale or otherwise halted constituent halts the account and cancels all working orders. A fresh quote cannot clear the latch. A closed constituent cancels all pending orders and blocks new orders until every constituent is open again.
- **Audit backpressure:** a full native or account audit queue halts the portfolio and completes cancellation. An incomplete audit cannot produce a successful replay result.

The coordinator currently admits limit orders only. DAY and IOC behavior, adverse slippage, participation caps, lot/freeze constraints, per-symbol bands and position limits remain enforced by each native engine. Its internal automatic submission path is disabled; only the account coordinator can admit orders. Account checks scan the bounded universe/orders; standalone engine latency measurements do not measure this portfolio coordinator.

## Reports and boundaries

Audit rows add `instrument` before the ordinary native event fields. `position` and `equity_paise` on those rows describe that instrument; account totals appear in the summary. The summary includes shared cash/NAV, net PnL, fees, gross marked notional, remaining cash reservations, total open orders, drawdown and each constituent's position/cash flow/fees/PnL.

Clean completion cancels orders and marks inventory without liquidating or carrying it into another replay. A partial/failed input can leave diagnostic audit data; exit 0 means complete, 2 means argument/input/I/O failure, and 3 means a latched risk halt.

Replaying the saved input reproduces this offline account. For public delayed multi-symbol prices, durable shared-account sessions and restart recovery, use the separate [streaming portfolio runner](portfolio-streaming.md). The existing single-symbol public-feed runner remains available. Single-symbol research walk-forward evaluation does not automatically become a portfolio return calculation.

All prices/depth and fee defaults in the fixture remain illustrative. Correlations, FX, margin, settlement, corporate actions, exchange calendars, external broker fills and broker acknowledgments are not modeled.
