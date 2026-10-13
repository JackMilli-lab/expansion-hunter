# Operations

Run live collection with `scan` or `watch` and serve persisted reports with `serve`. SQLite WAL allows the API process to read completed runs while the CLI writes observations. A killed, failed or cancelled run is retained diagnostically and never replaces the last completed report. Processes should not share a writable database across a network filesystem.

The scanner is GET-only. It does not trade, send alerts, ingest social narratives automatically, or assume zero existing portfolio exposure. Account/portfolio files and narrative evidence must come from trusted operators or separate adapters. API report authentication is optional for local operation and configured through `COCKPIT_API_TOKEN`; internet-facing operation also requires deployment-specific TLS, access controls and monitoring.

## Start small

1. Install the project and run its tests.
2. Review the explicit symbol map, excluded asset IDs, quote currency, fee/slippage assumptions, account equity, liquidity thresholds and data provider URLs.
3. Use `config/smoke.json` to check source access with five ranked assets and two primary timeframes. It explicitly uses Binance's official public data host. Do not silently switch providers or assume a failed derivatives endpoint is safe funding.
4. Inspect `provider_issues`, `errors`, per-timeframe `issues`, missing rule inputs, and trade reasons in the JSON report. A complete scan can legitimately have no entries.
5. Increase universe size and timeframes after measuring source latency and request-weight limits. `watch` waits its interval after completion, so runs never overlap within one scanner instance.
6. Supply fresh verified portfolio state only after confirming the configured equity/quote units. For confirmed high-score candidates, the scanner recollects data and macro context before final trade planning. Every plan remains advisory.

Separate CLI instances can still start concurrent scans against the same providers. Use one supervised ingestion process per database; host-local request throttles are not shared across processes. Detect stuck RUNNING rows through `/api/status` and review process logs. Interrupted scans are unpublished; a restart creates a new run and does not invent continuation data.

## Source diagnostics

- HTTP 451/403 or an endpoint unavailable in the operator's location is an explicit provider failure. Choose a legally available documented data provider through configuration; do not disable TLS or proxy policy.
- HTTP 429 and transient 5xx/transport failures retry only within a bounded safe-GET budget. Long `Retry-After` values fail visibly.
- Malformed, non-finite, boolean-as-number, impossible price or timestamp payloads are rejected or marked unavailable.
- Exchange pairs are verified against exchange metadata. A delisted or renamed pair requires a reviewed mapping change; symbol-only aliases are not guessed.
- Insufficient candle warm-up, gaps, duplicate times, mixed sources, stale bars or partial VWAP sessions produce unknown/blocked signals.
- Missing or incomplete 1% order-book coverage blocks claimed full depth; absence of an execution quote/depth cannot establish liquidity.
- Public futures liquidation and sampled long/short data are not promoted to reliable liquidation clusters or market-wide exposure.
- Global seven-day deltas use only completed local observations near seven days apart. Start-up missing history is expected.

## Retention and recovery

Each completed scan archives compressed full snapshots, final reports, global observations and source knowledge timestamps. Storage grows with universe size, bars and scan cadence. Back up the database using SQLite's online backup API or a controlled checkpoint/backup procedure, and test restores before setting a retention policy. The application intentionally does not auto-delete audit history. Export the latest asset snapshot with `inspect --asset-id` for offline evaluation.

Threshold changes can change future decisions; preserve settings and code versions alongside deployed database backups. Schema version 1 is embedded in snapshots/reports. A changed live universe is not a valid historical backtest universe. Use original archived metadata and include delisted assets in any strategy-performance study.

## Live validation in this workspace

On 2026-10-03, a short real-source run successfully collected CoinGecko ranked metadata/global data and Binance BTC/ETH spot candles, quote volume and order books from `data-api.binance.vision`. The default `api.binance.com` and USD-M futures `fapi.binance.com` returned HTTP 451. The run recorded missing derivatives, universe exclusions/mapping diagnostics and conservative trade decisions; no orders were submitted. This checks integration and missing-data behavior, not predictive edge or full-500 throughput.
