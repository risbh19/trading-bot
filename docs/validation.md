# Validation record

Measured on 28 October 2026 using Windows NT build 26200, an Intel64 Family 6 Model 170 CPU, Clang 21.1.0 via Zig 0.16.0, CMake Release (`-O3 -DNDEBUG`), without native tuning or LTO. This is a local development-machine result, not a service guarantee.

## Correctness

All four CTest groups passed locally:

| Group | Coverage |
| --- | --- |
| `core` | Bounded fixed-point arithmetic, quote validation, queue capacity/wraparound and a 100,000-item producer/consumer run. |
| `execution` | Pending exposure, notional/quantity/band/lot/freeze/open-order limits, sliding request windows, deterministic rejects, latency, partial IOC, FIFO liquidity, per-order fee cap, duplicate data/IDs, disconnect/stale/session stops, loss kill, replacement, adverse gaps, reducing inventory at the notional cap and audit saturation. |
| `hot_path` | No ordinary/aligned C++ heap allocations across 200 submit/match cycles, health and shutdown. |
| `replay` | Four Python integration cases: byte-identical repeated replay and fee/position reconciliation, live-mode rejection, config/path validation, and stale-feed cancellation before any fill. |

The default 20-tick synthetic fixture produces 8 fills, final quantity 0, fees 187 paise and net marked PnL -847 paise. The negative result is intentional evidence that costs are included; it is not a strategy assessment.

An additional 1,000-tick synthetic run (`generate_ticks.py --rows 1000 --seed 42`) completed with 200 accepted orders, 362 rejected decisions, 108 fills, fees 2,793 paise, final quantity 37 and marked PnL -10,388 paise. Working orders were cancelled at the end; residual inventory remained marked. Both runs reconciled total fill fees and net units against the C++ summary.

## In-memory latency sample

20,000 samples per row after warmup, nearest-rank percentiles, nanoseconds:

| Component | p50 | p90 | p99 | p99.9 |
| --- | ---: | ---: | ---: | ---: |
| Back-to-back timer calls | 0 | 100 | 100 | 100 |
| Book snapshot validation/update | 100 | 100 | 100 | 100 |
| Tick + strategy + risk + paper match + audit enqueue | 2,200 | 3,100 | 5,700 | 10,900 |

The timer quantization limits interpretation of small values. Timer overhead has not been subtracted. The pipeline run included 20,790 accepted orders, 10,080 fill events and zero rejected decisions including warmup; it resets the engine every 100 ticks outside the measured interval to avoid measuring exhausted-session paths. This distribution mixes matching, signal and submission work according to the synthetic price pattern. It is not a worst-case load test of all 64 working orders or a long session near its request ceiling.

Parsing, file output, networking, broker acknowledgments and exchange matching are excluded. Do not add assumed external latency to claim a production target. Re-run `latency_bench` on the deployment CPU with representative feed/order load.

## Automated checks

The GitHub workflow defines Windows and Linux Release builds plus Linux address/undefined-behavior sanitizer checks. Local results above do not imply that a hosted run has passed; inspect the repository's Actions page for the current revision. Sanitizer support and timings differ by platform.
