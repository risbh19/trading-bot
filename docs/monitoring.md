# Read-only paper-session monitoring

The exporter makes a single-symbol or multi-symbol session's existing local reports available to Prometheus. It does not load the native library, poll a quote provider, acquire the session owner lock, change orders or restart a session. Stopping the exporter leaves the paper runner unchanged. Real orders remain disabled; delayed source prices, default fees and execution fills still have the simulation limits described in the runner guides.

## Start the exporter

Use Python 3.10+ from the repository root, in a separate terminal from the paper runner:

```sh
python -m live.monitoring --session reports/network-portfolio/SESSION --port 9400 --max-age 20
```

For a single-symbol account, pass its printed `reports/network-paper/SESSION` directory instead. Replace `SESSION` with the actual directory. The exporter reads that explicit directory and pins the session identity and symbol universe after its first valid snapshot. It never selects the newest report folder or silently switches to a different account. Start another exporter on another port for another account, and add that port as a separate Prometheus target.

The standard-library exporter has no extra Python package or C++ build requirement. It binds only `127.0.0.1`; there is no public-bind option. Port defaults to 9400. `--max-age` defaults to 20 seconds and accepts finite values from 5 to 300 seconds. Health reports normally update at least every five seconds while the runner is active. A shorter freshness threshold can produce expected stale transitions around scheduling delays.

| Endpoint | Response |
| --- | --- |
| `GET /metrics` | Prometheus text metrics, HTTP 200 even when a report is missing, invalid or stale. Read the diagnostic metrics below. |
| `GET /healthz` | HTTP 200 when the exporter can handle the request. This is exporter process health. |
| `GET /readyz` | HTTP 200 only when the paper snapshot is valid, fresh and healthy; HTTP 503 otherwise, including a clean stopped session. |

`HEAD` supports the same routes. Other paths, including requests with query parameters, are rejected; mutation methods are rejected. The HTTP API does not accept file paths or return raw reports, local paths or error strings. Keep Prometheus on the same host, or provide a separately configured authenticated transport if a remote collector is required.

## Interpret the metrics

Exporter availability and runner health are separate signals. Prometheus's `up` indicates whether `/metrics` was scraped successfully. A reachable exporter can report an unreadable file or a runner that stopped updating.

| Metric | Meaning |
| --- | --- |
| `trading_paper_snapshot_valid` | Manifest and health files pass format, paper-mode, identity, generation, universe, numeric and timestamp checks. |
| `trading_paper_snapshot_fresh` | A valid health report is no older than `--max-age`. |
| `trading_paper_healthy` | The report is valid, fresh and reports a healthy account. |
| `trading_paper_running` | The report is valid, fresh and nonterminal. Starting, retrying and waiting states count as running. |
| `trading_paper_halted` | A valid report's last state is halted. This remains visible after that report ages. |
| `trading_paper_snapshot_age_seconds` | Time since the validated report's update timestamp. |
| `trading_paper_state{state="..."}` | Fixed one-hot last-reported state: `starting`, `healthy`, `retrying`, `waiting`, `stopped`, `halted` or `unavailable`. This is not a liveness signal. |
| `trading_paper_process_started_timestamp_seconds` | Start time of the runner process that wrote the report. |
| `trading_paper_source_age_seconds{symbol="..."}` | Current time minus the source's original quote timestamp. Recomputed on every scrape, including stale reports. |

The exporter never carries a cached healthy or execution-enabled value across a failed read. Missing, oversized, malformed, unsupported or mismatched reports produce `snapshot_valid=0`, `snapshot_fresh=0`, `healthy=0`, `running=0` and state `unavailable`. Reads are bounded to one MiB per file. Nonfinite values, negative counters, impossible/future timestamps and a backwards exporter wall clock also invalidate the observation. The manifest is read on both sides of the health read; a changing report generation is retried a bounded number of times rather than combined as one snapshot.

New reports include `resume_count`. A report from a previous process generation cannot mark a resumed session healthy. Legacy reports without that field are accepted only when the manifest has never resumed (`resume_count=0`). A finished manifest cannot accompany a nonterminal health state. The exporter verifies report identity and structure; the runner's journal recovery remains responsible for accounting reconciliation and native/configuration fingerprint checks.

A stale but otherwise valid report has `snapshot_fresh=0`, `healthy=0` and `running=0`. Financial values and feed counters are omitted until a fresh valid report returns. Report age, source age and last-reported terminal state remain diagnostic evidence. Do not interpret absent financial samples as zero cash or zero exposure.

Every fresh valid snapshot exports `trading_paper_pnl_inr` and `trading_paper_open_orders`. Portfolio snapshots also export `cash_inr`, `equity_inr`, `fees_inr`, `gross_notional_inr`, `reserved_cash_inr` and `execution_enabled`, all with the `trading_paper_` prefix. Monetary values are converted from integer paise to INR. These values represent the simulated account.

Feed counters share the `symbol` label and `trading_paper_` prefix:

- `feed_fetch_attempts_total` and `feed_responses_total`
- `feed_accepted_quotes_total` and `feed_duplicate_quotes_total`
- `feed_failures_total`, `feed_retries_total` and `feed_validation_failures_total`

Counters cover the current runner process and reset on resume. Prometheus `rate()` can account for counter resets; use the exported process start timestamp when investigating a restart. Symbol labels come from the pinned manifest universe, limited to 32 symbols. Session IDs, paths, timestamps and exception text are not metric labels. The text output and metric names follow the [Prometheus exposition format](https://prometheus.io/docs/instrumenting/exposition_formats/) and [metric and label naming guidance](https://prometheus.io/docs/practices/naming/).

A clean shutdown intentionally kills the native engine and cancels pending orders while leaving marked inventory. Its monitoring state is `stopped`, with `halted=0`. A halt indicates a reported failure or risk stop; the native killed flag alone does not distinguish these outcomes. An exporter cannot independently prove the operating-system process is alive between report updates, or detect every failure of its own host. Its freshness checks complement the native runner's safety controls.

## Connect Prometheus

The example [configuration](../monitoring/prometheus.yml) scrapes the loopback exporter every five seconds and loads [alert rules](../monitoring/alerts.yml). Run a local Prometheus installation from the repository root:

```sh
prometheus --config.file=monitoring/prometheus.yml --web.listen-address=127.0.0.1:9090
```

The example expects the exporter on port 9400. Keep the job name `paper-session` when using the supplied rules, or update their selectors. The rules cover an unreachable exporter, invalid reports, stale nonterminal sessions, reported halts and an account waiting for healthy data for two minutes. A clean stopped session is excluded from stale/waiting alerts. If an account is intentionally retired, remove its scrape target after recording the final reports.

These examples evaluate alerts inside Prometheus. They configure no Alertmanager receiver, email, webhook or external notification and send no messages. Receiver setup and deployment remain separate work. Alerts observe a paper account; they never cancel, resume or place orders.

Validate examples with `promtool` from the `monitoring` directory:

```sh
promtool check config prometheus.yml
promtool check rules alerts.yml
promtool test rules alerts.test.yml
```

CI validates configuration and alert behavior with Prometheus 3.5.0. The alert fixtures cover healthy, unavailable, invalid, stale, clean stopped, halted, persistently waiting and recovered sessions. The offline Python suite separately exercises report validation, changing timestamps, generation mismatches, bounded reads, source ages, label escaping and HTTP behavior.
