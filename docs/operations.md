# Operating the paper trading milestone

Use this project for mock execution and deterministic replay. A runnable simulation is the acceptance target for this milestone. There is no live-market feed, broker login or real-order route. Follow the README for the sample replay and analytics commands for the final build.

## Build and test

Install a C++20 compiler and CMake 3.20 or newer. Python is only needed for the analysis/replay wrapper, not the C++ core. The core has no third-party C++ library dependencies.

Linux, with GCC or Clang:

```sh
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --parallel
ctest --test-dir build --output-on-failure
```

Windows, from a Visual Studio developer PowerShell with the C++ workload installed:

```powershell
cmake -S . -B build
cmake --build build --config Release --parallel
ctest --test-dir build -C Release --output-on-failure
```

Clang or GCC with Ninja can use the single-configuration Release commands on Windows as well. Multi-configuration generators place executables under `build/Release`; single-configuration generators usually place them directly under `build`.

Use a separate directory for sanitizer verification with GCC/Clang on a supported platform:

```sh
cmake -S . -B build-sanitize -DCMAKE_BUILD_TYPE=Debug -DTRADING_SANITIZERS=ON
cmake --build build-sanitize --parallel
ctest --test-dir build-sanitize --output-on-failure
```

The CMake sanitizer option is implemented for GCC/Clang, not MSVC. Sanitized timings are unsuitable for latency claims.

## Replay workflow

1. Begin with synthetic sample quotes and paper-only configuration. Record the exact configuration, input file and code revision with each result.
2. Set the instrument's tick size, lot size, bands and freeze quantity explicitly for any imported data. Instrument ID is local to the replay; it is not automatically mapped to a broker symbol.
3. Use monotonic nanosecond event time, increasing sequence numbers and complete top-of-book snapshots. Do not combine multiple sessions into one engine without defining the reset boundary.
4. Set order latency, adverse slippage, displayed sizes and rejection injection deliberately. Fee settings are illustrative and need a verified schedule before any financial interpretation.
5. Inspect the audit: signals, accepted/rejected orders, partial fills, cancellations and kill reasons. Compare filled quantity, net inventory, total fees and marked PnL.
6. At the end, cancel working orders and report inventory. End-of-file and shutdown do not sell remaining inventory. Include that inventory when comparing runs.

Replay advances event time rather than wall-clock time. It cannot establish broker round-trip latency or prove fill quality against a live exchange. Data with delayed timestamps also cannot measure real feed latency.

An external feed adapter must call the heartbeat even when no quotes arrive. Without a scheduler or independent heartbeat source, no in-process model can detect a silent feed during the period when it receives no calls. Disconnect handling must invoke the engine disconnect method and stop routing new work.

## Stops, errors and recovery

- A kill is latched. Inspect the reason, outstanding-order cancellations, last valid quote and residual inventory before starting a new session.
- A full audit queue sets `audit_failed()`. Drain output promptly and mark the run incomplete; increasing queue size alone is not a substitute for keeping the consumer healthy.
- Regressing and duplicate events do not produce fills. A newer malformed snapshot stops the engine. Fix imported data rather than accepting an invalid market state.
- Submit IDs increase for the whole session, including pre-trade rejects. A replacement cancels first and uses a fresh ID. There is no network retry/reconciliation protocol in this milestone.
- Local cancellations are immediate in the simulator. Real broker cancel acknowledgments, cancel/fill races and ambiguous submission timeouts need a separate reconciled lifecycle before any live adapter.
- The network paper runner journals each normalized tick before passing it to the engine and stores order events plus account snapshots after processing. Resume replays the journal, checks every committed event batch and snapshot, repairs a torn final line, and cancels working paper orders before polling again. A reconciliation mismatch or stale recovery gap stops the session. A latched risk halt remains latched. This reconstructs only the local paper ledger; there is no external broker state to reconcile.

## Measuring latency

Measure the supplied in-memory benchmark in an optimized build and report p50, p90, p99 and p99.9 together with CPU, OS, compiler, build flags, sample count and benchmark scope. An execution-method timing excludes parsing, audit file writes, network transit and broker/exchange processing. Do not present it as exchange tick-to-trade latency or a production service-level guarantee.

Start with a portable Release build. Enable `TRADING_NATIVE` only for a binary that runs on the same CPU family. Enable `TRADING_LTO` only if supported and compare measured distributions. Neither setting is a substitute for correctness checks.

For a later Linux feed service, investigate these changes one at a time and retain them only when measurements justify them:

| Change | What to verify |
| --- | --- |
| CPU affinity and isolated cores | Tail latency, scheduler interference and impact on the rest of the host. |
| Busy polling | Latency benefit against continuous CPU/power use; avoid starving the audit consumer. |
| NIC interrupt affinity and receive settings | Packet loss, jitter and queue behavior on the actual NIC/driver. |
| Stable CPU frequency/power settings | Thermal behavior and sustained tail latency. |
| Pre-faulted memory, huge pages or locked memory | Actual page-fault/TLB benefit and available memory limits. |
| Hardware timestamps or cycle counters | Clock domain, synchronization, calibration and cross-core behavior. |

Use `perf`/flame graphs to locate CPU time and cache profiling to test specific hypotheses. Keep profiling runs separate from benchmark distributions. Colocation, direct exchange access, kernel bypass and NIC hardware features are future infrastructure choices, not capabilities of this retail paper simulator.

## Next integration milestones

1. Extend long-running simulation and failure-injection coverage; compare accounting against independent calculations and validate journal corruption/recovery behavior.
2. Add a selected broker's authenticated market-data adapter, symbol metadata, reconnect/resubscribe logic, token handling and heartbeat scheduler. Keep real orders disabled.
3. Before any broker adapter is considered, add broker-side order-ID reconciliation, acknowledgments, partial/late fills, cancel/fill races and account-level limits. The current durable journal reconciles only paper-engine events.
4. Verify current broker policy, instrument/product fees, exchange sessions/holidays, capital/margin/settlement treatment and any required registration/tagging before designing a live gateway.
5. Add a live adapter only as a separately reviewed milestone with explicit user authorization. The current `LiveBroker` stub always rejects requests.

## Remaining prioritized backlog

| Priority | Work |
| --- | --- |
| P1 implemented | Reproducible research and independent accounting/execution checks; safe public polling retries and local metrics/alerts; shared-account multi-instrument capital/exposure/loss/order controls; broker-free multi-symbol sessions with native pause barriers, frozen configuration bundles and journal reconciliation/recovery; read-only Prometheus exporter with heartbeat/generation validation and tested alert rules. See [research](research.md), [network practice](network-paper.md), [portfolio](portfolio.md), [portfolio streaming](portfolio-streaming.md) and [monitoring](monitoring.md). |
| P1 remaining | Verify a selected broker/product's tariff/rounding against contract notes; source point-in-time historical data and calibrate execution models; add authenticated streams. Broker acknowledgments/reconciliation require a selected broker. Configure a monitoring deployment and explicit notification receivers separately; none are sent by the exporter. Current defaults remain broker-free paper simulation. |
| P2 implemented | Read-only desktop/mobile account dashboard, reproducible monitoring-only ZIP and a [monitoring operational runbook](monitoring-runbook.md). CI checks the real browser and publishes a short-lived monitoring artifact. |
| P2 remaining | Full native-runner deployment packaging, unattended supervision and notification deployment. The monitoring ZIP contains the view/exporter only and cannot run or recover a paper account. |

For future verification, consult the [SEBI retail algorithmic-trading circular](https://www.sebi.gov.in/legal/circulars/feb-2025/safer-participation-of-retail-investors-in-algorithmic-trading_91614.html), its subsequent amendments and exchange implementation standards, plus the chosen broker's current documentation. The [Zerodha Kite Connect FAQ](https://support.zerodha.com/category/trading-and-markets/general-kite/kite-api/articles/kite-connect-api-faqs) is one broker-specific starting point. These links are references to check when selecting an integration; this repository does not certify regulatory compliance or apply a universal broker rate limit.
