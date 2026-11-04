# Paper monitoring runbook

This dashboard and exporter observe one paper-session directory. They never place, cancel or resume orders. Delayed source bars, fees and fills retain the runner's simulation limits. The monitoring bundle is separate from the native trading runner and contains no native library, session reports, account credentials or notification receiver.

## Start and verify

1. Start the single-symbol or portfolio paper runner using its normal guide. Record its printed report directory. Use the same local filesystem and host for the exporter.
2. In another terminal, start monitoring with the actual report path:

   ```sh
   python -m live.monitoring --session /absolute/path/to/SESSION --port 9400 --max-age 20
   ```

   Windows, from the repository or extracted bundle:

   ```powershell
   .\scripts\run-monitoring.ps1 -Session 'C:\absolute\path\to\SESSION'
   ```

3. Open [the local dashboard](http://127.0.0.1:9400/). Confirm the displayed session condition and report age. A healthy exporter process alone does not prove a healthy runner.
4. Use `/metrics` for Prometheus and `/readyz` for session readiness. `/healthz` checks only the exporter. The default report-age limit is 20 seconds; runner health normally updates every five seconds.

Use a dedicated exporter/port for each report directory. The exporter pins the first valid session identity and universe. To observe another account, stop the exporter and start it explicitly with that account's directory.

## Respond to conditions

| Dashboard condition | Action |
| --- | --- |
| Healthy | Observe report age and source ages. A fresh report can still contain delayed source bars; it is not an executable market quote. |
| Starting, waiting or retrying | The runner is waiting for acceptable source data. Review its terminal and local alerts. The dashboard cannot clear a native risk latch or fresh-quote barrier. |
| Stale | Financial values are hidden because health updates stopped. Check the runner terminal, host and disk. Preserve the report directory; do not treat hidden values as zero exposure. |
| Stopped | The runner ended cleanly. Pending orders were cancelled by the runner; remaining inventory stays marked. A finished session cannot be resumed. |
| Halted | A failure or risk stop was reported. Preserve the journal, audit, summary and alerts before investigation. Fix the reported cause and follow the runner recovery rules. |
| Unavailable or connection lost | Check the selected directory, exporter terminal and local port. Invalid, mixed-generation or corrupt reports must be investigated rather than edited to look healthy. |

A browser connection failure clears the displayed financial data. The dashboard is a current snapshot, not a historical ledger or a guarantee that the operating-system process remains alive between reports.

## Stop and recovery

Stop monitoring with Ctrl+C in its terminal. This leaves the runner unchanged. To stop the paper account, use Ctrl+C in the runner's terminal and check its final summary.

For an interrupted running account, follow [portfolio recovery](https://github.com/risbh19/trading-bot/blob/main/docs/portfolio-streaming.md) or [single-symbol recovery](https://github.com/risbh19/trading-bot/blob/main/docs/network-paper.md) in the full repository. Recovery requires the original matching configuration/native binary and exclusive session ownership. Never delete an active owner lock, edit the canonical journal, overwrite an audit or copy reports into a new account to bypass recovery. The monitoring-only bundle cannot perform recovery.

After a successful resume, the new health generation must match the manifest. Monitoring counters cover the new runner process. Stale or previous-generation data cannot restore a healthy display.

## Build and transfer a monitoring bundle

From a reviewed source checkout with Python 3.10+:

```sh
python scripts/package-monitoring.py --output outputs/paper-monitoring.zip
```

The builder refuses an existing output. Use a new path for each build or remove an old artifact deliberately. It copies a fixed allowlist of monitoring source/assets, documentation and Prometheus examples. ZIP entries are sorted, use a fixed timestamp and file mode, and are stored without compression. Identical source bytes produce identical archive bytes, independent of source modification times.

Record the printed SHA-256 with the reviewed commit. Verify the transferred archive against that digest before extracting:

```powershell
Get-FileHash -Algorithm SHA256 -LiteralPath .\paper-monitoring.zip
Expand-Archive -LiteralPath .\paper-monitoring.zip -DestinationPath .\paper-monitoring
```

On Unix, use `sha256sum paper-monitoring.zip` and your usual ZIP extraction tool. The archive's `manifest.json` lists SHA-256 values for every payload file. It detects accidental changes; it is not an independent signature. Use the externally recorded archive digest and trusted source/transfer route.

Change into the extracted directory and run the same `python -m live.monitoring` command with an absolute report path. Python 3.10+ must already be installed on the target host. The bundle does not install Python, build C++, start a service or change firewall settings.

Give the exporter read access only to the required reports and keep it on the runner's host. It listens only on `127.0.0.1`. Any remote access requires a separately configured authenticated transport. The included Prometheus configuration and alert rules evaluate local conditions; they configure no email, webhook or external notification receiver.

Full native-runner deployment packaging, unattended service supervision and notification deployment remain separate work.
