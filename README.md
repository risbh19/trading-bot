# Trading Bot

C++20 paper trading and deterministic replay. All money is represented in integer paise.

## Milestones

1. Foundation: CMake, fixed-point arithmetic, validated top-of-book snapshots, bounded SPSC queue.
2. Execution: paper broker, risk reservations, simulated latency/partial fills/fees, kill switches.
3. Replay: sample strategy, CSV feed, full audit trail, Python analytics using the same C++ engine.
4. Verification: risk/chaos tests, latency percentiles, CI and deployment notes.

## Build and test

Requires CMake 3.20+ and a C++20 compiler (GCC, Clang, or MSVC).

```sh
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --config Release
ctest --test-dir build -C Release --output-on-failure
```

Paper/mock only. There are no broker connections or real orders. Sample data and fees are illustrative.
