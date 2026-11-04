# Trading Bot — Paper Trading First

A structured C++20 mock trading engine with deterministic replay, inline risk checks and Python analysis. Practice with synthetic/CSV data or **broker-free delayed Indian equity quotes**, using simulated money. No API key, broker account or real-order connection is needed. Real-order mode always rejects requests.

## Quick start

Requires CMake 3.20+, a C++20 compiler (GCC, Clang or MSVC), and optionally Python 3.10+ for analytics and replay integration tests. No third-party C++ or Python packages are required.

```sh
git clone https://github.com/risbh19/trading-bot.git
cd trading-bot
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --config Release
ctest --test-dir build -C Release --output-on-failure
```

Linux / Ninja single-configuration build:

```sh
./build/paper_trader --mode paper
python research/backtest.py --binary build/paper_trader
```

Windows PowerShell:

```powershell
.\scripts\run-paper.ps1
python research/backtest.py --binary build/Release/paper_trader.exe
```

For Ninja on Windows, the Python binary argument is `build/paper_trader.exe`. The PowerShell launcher finds either build layout. Run commands from the repository root.

The default replay creates `reports/audit.csv` and `reports/summary.json`. Python also creates `reports/backtest/backtest.json`. The supplied 20-tick fixture produces 8 fills, ends flat, and loses **847 paise after 187 paise of fees**. This is an accounting example, not a profitable strategy or market performance result.

## Broker-free market-price practice

After building, start a broker-free paper session with **INR 100,000 virtual capital**:

```powershell
.\scripts\run-live-paper.ps1
```

Or use `python -m live.run --symbol RELIANCE.NS` on any supported platform. This connects actual **delayed/indicative** Yahoo NSE/BSE prices to the same C++ strategy, risk checks and PaperBroker. It requires no broker account. The console shows source age, virtual equity, PnL, position and fills. Outside market hours it records the quote and stops without enabling orders.

This public feed supplies completed one-minute closes, not a real-time executable order book. Spread/depth are simulated. Reports are in unique `reports/network-paper/` session folders. A crash-interrupted session can be recovered with `python -m live.run --resume <session-folder>`; recovery replays and reconciles the saved paper ledger, then cancels any working orders before polling again. Cleanly finished sessions cannot be resumed. See the [setup, recovery, data limits and report guide](docs/network-paper.md).

## Mock trading controls

Edit `config/paper.conf` to change strategy size, simulated order latency, adverse slippage, rejection injection, risk limits and fees. Configuration rejects unknown/duplicate keys and invalid numeric bounds. Money/prices use integer **paise**, quantity is units, and timestamps are monotonic nanoseconds.

```sh
./build/paper_trader --input data/sample_ticks.csv --config config/paper.conf \
  --audit reports/custom-audit.csv --summary reports/custom-summary.json
python research/generate_ticks.py --rows 1000 --seed 42 --output reports/synthetic.csv
python research/backtest.py --binary build/paper_trader --input reports/synthetic.csv
```

Use one line for the replay command in PowerShell, or use the launcher with `-InputFile` / `-ConfigFile`. CSV columns are shown in `data/sample_ticks.csv`. A record is a complete top-of-book snapshot. The final `session_open=0` record cancels working orders. Ctrl+C requests a stop at the next processing boundary. Exit codes: `0` completed, `2` invalid input/I/O, `3` risk halt; inspect the audit and summary.

The C++ `Engine` exposes `submit`, `modify`, `cancel`, `heartbeat`, `disconnect`, and `kill` for manual/scheduled mock orders or a future market-data adapter. Call mutation methods from one owning thread. IDs increase across the session, including rejected requests. The example momentum strategy is deliberately simple. Python invokes the actual C++ engine, so strategy/risk/fees cannot drift between implementations.

## What is implemented

- Validated top-of-book snapshots and fixed-point arithmetic; bounded SPSC audit queue.
- Limit/market orders, DAY/IOC, latency, shared displayed liquidity, partial fills, reject injection and cancel-first replacement.
- Position and pending-order risk, order/notional/band/lot/freeze limits, rolling submission limits, daily marked-loss stop, stale/disconnect/manual kill and cancel-all.
- Fill-time risk approval before accounting changes; adverse market-price protection.
- Cumulative per-order fees with configurable brokerage, STT, exchange, SEBI, stamp duty and GST components.
- Deterministic audit/replay, independent cash/fee/PnL/liquidity reconciliation, allocation checks and latency percentile harness.
- Research input/config snapshots and hashes, execution-cost scenarios, chronological train/test selection and explicit fee evidence. See the [research guide](docs/research.md).
- Bounded public-feed retries that cancel pending orders first, source-quality checks, local health metrics and transition alerts.
- A multi-instrument paper replay coordinator with one shared cash account, pending reservations, aggregate exposure/loss/order limits and fill-time account guards. See the [portfolio guide](docs/portfolio.md).
- Broker-free multi-symbol streaming into that shared account, account-wide outage pauses, fresh-source barriers and journal recovery. Start with `python -m live.portfolio_run` or the [streaming portfolio guide](docs/portfolio-streaming.md).

## Project layout

```text
include/trading/   # Book, strategy, risk, broker, fees, execution, queue
app/              # CSV/config parsing and replay runner (outside the hot path)
research/         # Python backtest wrapper and synthetic feed generator
live/             # Public delayed quotes and persistent native-engine session
config/           # Explicit paper/instrument/risk/fee configuration
data/             # Small reproducible synthetic fixture
tests/            # Core, execution/chaos, allocation and replay verification
bench/            # In-memory latency distributions
docs/             # Architecture, operating limits and measured validation
scripts/          # Windows mock-run launcher
.github/workflows # Windows/Linux builds and Linux sanitizers
```

## Important model boundaries

Each engine handles one instrument and one session. `paper_portfolio` provides offline shared-account replay; `live.portfolio_run` connects 1..32 public quote sources to a durable shared paper account. `live.run` remains the simpler single-symbol runner. All model top-of-book liquidity rather than exchange queues, depth or market impact. Orders fill only on a later snapshot after configured latency. Terminal inventory is marked at mid and **is not automatically liquidated**. Cash reservations include pending buys and fees; margin, settlement, derivatives and MIS square-off are not modeled. A real-time broker feed, dashboard and production order gateway are not included.

Fee defaults are illustrative arithmetic fixtures, not current CNC/MIS/NRML tariffs. Set product-specific fees and instrument constraints from verified metadata before interpreting any economic results. Submission rate limits are simulation settings; local cancels are immediate and do not model broker request throttling/cancel races. Real-feed paper trading is a later adapter step.

## Latency and further work

```sh
./build/latency_bench
```

The harness reports p50/p90/p99/p99.9 and a timer baseline. It measures in-memory book/strategy/risk/paper matching with audit enqueueing; it excludes CSV, disk, network and broker/exchange processing. A microsecond engine measurement is not an exchange tick-to-trade guarantee. Optional tuning: `-DTRADING_NATIVE=ON`, `-DTRADING_LTO=ON`; sanitizer checks: `-DTRADING_SANITIZERS=ON` on supported GCC/Clang hosts.

Read-only Prometheus monitoring is available for either network runner:

```sh
python -m live.monitoring --session reports/network-portfolio/SESSION
```

Pass the runner's actual session directory. The loopback exporter reports stale, invalid, paused, stopped and halted sessions separately; [monitoring](docs/monitoring.md) includes metrics, scrape configuration and tested alert rules.

See [architecture](docs/architecture.md), [operations and roadmap](docs/operations.md), [research validation](docs/research.md), [paper portfolio](docs/portfolio.md), [portfolio streaming/recovery](docs/portfolio-streaming.md), and [validation](docs/validation.md). Durable single/multi-symbol paper sessions, research validation, public-feed resilience, local health reporting, Prometheus integration and shared-account portfolio risk are implemented. Remaining work includes broker-specific fee/data validation, authenticated streams and notification receiver/deployment setup.
