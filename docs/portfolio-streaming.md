# Broker-free multi-symbol paper sessions

This runner polls public delayed/indicative completed one-minute closes into one native `PaperPortfolio` account. The default examples are `RELIANCE.NS` and `TCS.NS` with **INR 100,000 virtual capital shared once**. They are workflow examples, not investment selections. No broker account, API key, deposit or real-order connection is used.

## Start and stop

Build the latest source normally so `paper_engine` includes the additive portfolio API. Then, from the repository root:

```powershell
.\scripts\run-network-portfolio.ps1
.\scripts\run-network-portfolio.ps1 -Duration 60
```

Cross-platform Python:

```sh
python -m live.portfolio_run --config config/network-portfolio.json --duration 60
```

Use `--library` for a custom shared-library location. Python 3.10+ and the same C++20/CMake build requirements apply. A missing portfolio API produces a rebuild error. The single-engine ABI remains version 2; the additive portfolio ABI is version 1.

Duration 0 runs until Ctrl+C, a closed market or a failure/risk stop. Ctrl+C and normal duration completion cancel pending orders and stop the context. Inventory remains held and marked at the last accepted simulated quote; no automatic liquidation occurs. A closed session cannot be reopened.

## Universe and account configuration

`config/network-portfolio.json` declares all shared-account limits, source-age ceiling and 1..32 unique symbols. Each entry points to a native constituent configuration and specifies simulated spread/depth. IDs come from those constituent files and must also be unique.

Constituents require INR equity symbols, CNC, shorting disabled, explicit tick/lot/stale metadata and zero per-instrument capital. The native engine validates remaining configuration bounds. Relative config paths resolve against the JSON specification's directory. The supplied tick, bands, liquidity, fees and momentum strategy are illustrative simulation choices.

Each run freezes the effective universe/account identity, exact constituent bytes and native library SHA-256. Generated `portfolio.conf` and `configs/<id>.conf` live in that session folder. Account limits, symbol mapping, simulated depth/spread, source-age policy, all native config bytes and the library fingerprint must match on resume. Comments/line-ending changes to a constituent also change its fingerprint. A different native build requires a fresh session or the original saved build; matching API numbers alone do not permit recovery across changed engine code.

## Account pause and fresh-quote barrier

Every new session begins with native execution disabled. Each symbol must provide a validated open-session completed bar before execution is enabled. Enabling itself creates no order; the strategy waits for a subsequent accepted price update.

A transient failure on any source durably pauses the whole account and cancels all pending orders. Healthy sources can still update marks while paused, but cannot submit new orders. Every symbol must then advance beyond its own saved source timestamp before the account can resume. Repeated HTTP responses and unchanged source timestamps never clear this barrier or refresh liquidity.

Requests run in worker threads; one event-loop thread owns every native call and journal mutation. The owner continues heartbeat checks during requests and retries. One stale constituent or native risk halt cancels the entire portfolio, and source recovery never clears a latched halt.

The default allows three retries after an initial transient transport/server failure, with 5/10/20-second backoff. `Retry-After` can extend waits up to the policy's 60-second bound; a longer server delay stops the account instead of retrying too early. `--max-feed-retries 0` disables retries; 0..10 is allowed. Permanent HTTP, invalid payloads, future/stale/regressing/revised bars, wrong symbol/currency and simulation-band violations stop the account.

Public source prices are delayed one-minute closes, not executable bid/ask depth. Spread/depth/slippage/latency and fills remain simulated. The default accepted source age is 30 minutes, while the native unique-quote silence limit is 180 seconds. Neither clock is reset by duplicate responses.

## Crash recovery

Resume an interrupted session with its printed path and the same specification:

```sh
python -m live.portfolio_run --config config/network-portfolio.json --resume reports/network-portfolio/SESSION
```

```powershell
.\scripts\run-network-portfolio.ps1 -Resume 'reports/network-portfolio/SESSION'
```

Recovery acquires an exclusive owner lock and checks every frozen configuration file. It replays durable intents into a fresh native paper context, comparing each result, tagged event batch, shared account snapshot and per-instrument snapshot. Mismatched or corrupt complete records fail closed. An incomplete final line is discarded; an intent without a checkpoint is replayed once in the new simulated context.

The original prepared-intent wall time remains the downtime anchor when recovering a missing checkpoint. Assigning recovery time would conceal a long interruption; this is also corrected for newly recorded single-symbol sessions. Legacy single-symbol intents without an original time use the previous checkpoint conservatively.

After reconciliation, recovery advances the native clock, cancels every old pending order and establishes a new fresh-quote barrier. A stale restart or backwards wall clock halts before any network request. A healthy restart requires a later validated bar from every symbol before new orders can be admitted. This is local paper-ledger recovery; no external broker account is reconciled.

Audit and normalized-tick CSVs rebuild through temporary files. Canonical journal data controls recovery; failed reconciliation preserves the existing CSVs. Native or journal/monitoring I/O failure stops execution, with no claim of a complete audit after canonical write failure.

## Reports

Each session has a unique folder under `reports/network-portfolio/`:

| File | Contents |
| --- | --- |
| `session.json` | Frozen identity/fingerprint, session ID, running/finished state and resume count. |
| `portfolio.conf`, `configs/` | Generated account configuration and exact constituent bytes. |
| `session.jsonl` | Namespaced portfolio journal with durable operations, events and full account/member checkpoints. |
| `audit.csv` | Instrument-tagged native strategy/order/fill/cancel/risk events. |
| `ticks.csv` | Accepted normalized snapshots for every instrument. |
| `source-quotes.jsonl` | Provider quotes with their original bar timestamp and receipt time. |
| `summary.json` | Shared cash/NAV/PnL, reservations, all positions, source sequences, barriers and audit/stop status. |
| `health.json` | Account health and per-source polling/retry/validation counters and source ages. |
| `alerts.jsonl` | Local feed and portfolio pause/resume/stop transitions. |

Health counters cover the current process; ledger/source sequence counters recover from the journal. Alerts append across resume and suppress repeated identical states. No email, webhook or external notification is sent. Clean shutdown reports `risk_halted=false` while the native context is intentionally killed and execution remains disabled.

Exit codes are 0 for clean completion/closed market, 2 for startup/argument/configuration failure, and 3 for a running feed/risk/I/O stop. Partial initialization at a closed market is a valid zero-order stop. Fees are still illustrative; source freshness and ledger correctness do not establish profitable execution or exchange-data accuracy.

## Validation scope

Offline fixtures exercise the actual shared library: fresh-source barriers, cross-symbol suspension, pending-order recovery, torn tails, ledger/config mismatches, original downtime anchors, backwards clocks, owner locks, thread ownership, retry exhaustion, malformed data, I/O failures and eight simulated hours with 960 accepted quotes. Native API tests also run under sanitizers. Tests require no internet.

Authenticated broker streams, exchange calendars/corporate actions, external account reconciliation and external monitoring integrations remain separate work. The existing single-symbol runner and offline portfolio replay commands remain available.
