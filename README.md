# Trading Bot

A C++20 paper trading and deterministic replay project for studying execution and risk controls.

## Incremental delivery

1. Foundation: CMake, fixed-point types, top-of-book validation, bounded SPSC queue.
2. Execution: inline risk controls, deterministic paper broker, order lifecycle and inventory.
3. Replay: sample strategy, CSV feed, audit output and Python analytics using the same C++ engine.
4. Verification: risk/chaos tests, latency percentiles, CI and operating documentation.

Paper trading comes first. The project does not connect to a broker or place real orders. Sample data and fee settings are illustrative.
