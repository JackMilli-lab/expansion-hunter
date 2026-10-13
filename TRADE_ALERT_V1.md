# Trade Alert V1 — start and use

Trade Alert is an optional service inside the existing Expansion Hunter application. It watches verified, liquid Binance USDT **spot** pairs, records directional setups and displays them at `/trade-alert`. It uses public data and never places orders. **SHORT describes downside structure; this spot service does not open short positions.** The existing `app` command and cockpit remain available.

## Start locally

Use Python 3.11 or newer. Open a terminal in the repository folder, then run:

```bash
python -m pip install -e .
expansion-hunter --config config/settings.json trade-alert
```

Open **http://127.0.0.1:8000/trade-alert**. Leave the terminal running; press **Ctrl+C** to stop. No separate frontend installation is needed.

The explicit `--config` preserves the repository's official `https://data-api.binance.vision` REST configuration and fallback. Running just `expansion-hunter trade-alert` uses the Python settings defaults, which currently select `api.binance.com`; that host can be restricted on some networks. Public WebSocket access must also work independently.

The database is `data/cockpit.sqlite3` under this configuration. Its resolved path is printed at startup. Existing data is retained. Starting this command enables the dedicated Trade Alert workers; opening its page while only the original `app` service is running does not enable them.

## Read the dashboard

The page refreshes received scanner status once per second. A refreshing page is not proof that an individual coin has fresh data.

| Status/count | Meaning |
| --- | --- |
| Requested | Configured upper limit, not measured coverage. |
| Eligible/selected | Native trading USDT spot pairs meeting the volume filter; selected is capped by configuration. |
| Scanned | Assets whose current runtime status has been published. |
| Warming up | Candle history is still loading. |
| Ready | Fresh price and sufficient fresh, contiguous critical 1m/5m history. Context or other evidence may still be unavailable. |
| Partial | Insufficient critical candle history. Read the data-quality details and issues. |
| Stale | Price or critical candles are too old. Old signals are excluded from the fresh-signal cards. |
| Failed | Assets with recorded input/evaluation issues. This can overlap the other counts. |
| Excluded | Exact reasons include stablecoin base, inactive/non-spot instrument, malformed ticker or insufficient quote volume. |

The signal cards show bias, phase, trigger, invalidation, observed structural targets, score, reason and evidence. `UNAVAILABLE` is deliberate. An empty signal panel can be the correct result.

Inspect machine-readable live status at **`GET /api/trade-alert/status`** and persisted alerts/outcomes/misses at **`GET /api/trade-alert/history`**. The original `/health` endpoint also reports service status. If a dashboard token is configured, enter it in the page's access-token box; API requests use `Authorization: Bearer <token>`.

For the last persisted status, including its true timestamp:

```bash
expansion-hunter --config config/settings.json trade-alert-status
```

This command reads a cache. It does not start scanning or prove that the service is still running.

## Signal rules and configuration

Edit [config/trade_alert.json](config/trade_alert.json), then restart the service. Defaults select at most **500** pairs with at least **$5 million native 24h quote volume**, excluding the configured stablecoin bases. Market cap is not required. The service uses candle-only streams for 1m, 5m, 15m, 1h and 4h, sharded into 100 symbols per connection by default. It keeps 120 bars per frame and warms histories with concurrency 4 under the existing provider request budgets.

Five-minute candles define causal support/resistance and pressure. Completed one-minute candles confirm breaks of previously recorded levels. Context frames do not delay that confirmation.

| Phase | Default condition |
| --- | --- |
| `STRUCTURE_WATCH` | Continuation pressure score at least 40 and price on the setup side within 0.75 ATR of the detected level. Separate sweep/reclaim reversal hypotheses also use this phase and can have an unavailable score. |
| `TRADE_ALERT` | Continuation score at least 65, at least 2 level tests, directional structure strength at least 0.10, and directional native taker fraction at least 0.52. Native flow is required by default. |
| `BREAK_CONFIRMED` | A fresh, completed 1m candle formed after trigger discovery closes beyond the known level by **more than** `max(0.05 × setup ATR, 1 × native tick size)`, in the setup direction. Volume changes quality, not whether that close happened. |
| `INVALIDATED` | A completed candle breaks structural invalidation, loses a confirmed break beyond its buffer, or the setup expires after 3,600 seconds. Default cooldown is 300 seconds. |

A wick crossing and closing back inside is recorded as sweep evidence; it is not a confirmed break. The dedicated V1 phases above are distinct from the original Fast Ignition lifecycle. Do not interpret this service as adding every original acceptance/expansion state to V1.

Overextension is marked `LATE`/`MISSED` where applicable: defaults include 1.5 ATR displacement, 3% beyond the trigger, and a 3% completed 1m body guard against new chasing setups. Late, expired and restart-stale entries are suppressed from notification delivery. Earlier setup alerts superseded by an invalidation are suppressed; `INVALIDATED` events can still report the failure. Restored episodes need fresh completed-candle evidence.

The deterministic **uncalibrated** continuation score is:

`score = 100 × sum(weight × available normalized component)`

| Component | Weight | Normalization |
| --- | ---: | --- |
| Tests | 0.15 | Test count divided by configured minimum, clipped to 0–1. |
| Rejection decay | 0.15 | Existing measured rejection-weakening component. |
| Compression | 0.15 | Existing measured range-compression component. |
| Velocity | 0.10 | Directional recent 5m movement in ATR units, clipped to 0–1. |
| Acceleration | 0.05 | Recent minus prior directional velocity, clipped to 0–1. |
| Spot volume | 0.15 | Clipped RVOL/2 × directional taker fraction × an acceleration multiplier from 0.75 to 1. |
| Relative strength | 0.10 | `clip(0.5 + direction × BTC-relative return × 100)`. |
| Distance | 0.10 | `clip(1 − distance_ATR / proximity_ATR)` on the setup side. |
| Market context | 0.05 | `clip(0.5 + direction × aligned BTC return × 50)`. |

`clip` bounds a value to 0–1; direction is +1 for LONG and −1 for SHORT. Velocity uses the last three 5m intervals relative to the preceding three. BTC context uses aligned completed 5m closes, up to seven observations. The spot-volume multiplier is `0.75 + 0.25 × clip(volume_acceleration + 0.5)`; its calculation uses a zero fallback for that multiplier when acceleration is unavailable, while the missing raw metric remains unavailable. Spot volume itself requires RVOL and taker evidence.

Missing component contributions remain `UNAVAILABLE`/`None`; their weights are not redistributed. Each alert stores contributions. **65/100 is not a 65% probability.** These initial weights and thresholds have not demonstrated predictive edge or been fitted to historical winners.

## Telegram on your phone

Telegram is optional. Create a bot with Telegram's BotFather, start a conversation with it, and obtain the intended chat ID. Set these two environment variables before launching the service:

```bash
export COCKPIT_TELEGRAM_BOT_TOKEN='your-bot-token'
export COCKPIT_TELEGRAM_CHAT_ID='your-chat-id'
```

On PowerShell, use `$env:COCKPIT_TELEGRAM_BOT_TOKEN = 'your-bot-token'` and `$env:COCKPIT_TELEGRAM_CHAT_ID = 'your-chat-id'` instead. Keep the actual values outside source control. A local shell launch does not automatically load a Compose `.env` file.

Send an explicit connectivity test:

```bash
expansion-hunter --config config/settings.json trade-alert-test
```

The page's **Telegram connectivity test** button invokes the same purpose through `POST /api/trade-alert/telegram/test`. This sends a clearly labelled service test, **not a fabricated market alert**. Without both credentials, delivery reports unavailable and scanning continues.

Real observed alerts use a persistent outbox, freshness checks, deduplication and bounded retries. Failures do not stop market ingestion. Telegram acknowledgement means Telegram accepted the message; it does not prove your handset displayed it. A crash between provider acceptance and local acknowledgement can still cause a retry duplicate.

## VPS / always-on deployment

Docker Compose includes an optional `trade-alert` service alongside the preserved `cockpit` service. On the host, copy the example environment file and replace its placeholder token with a long, randomly generated value:

```bash
cp .env.example .env
docker compose --profile trade-alert up -d --build trade-alert
docker compose logs -f trade-alert
```

Set `COCKPIT_API_TOKEN` in `.env`; add the two Telegram variables there if wanted. Do not commit `.env`. The service binds the host to **`127.0.0.1:8500`**, persists SQLite in the named `trade-alert-data` volume, and restarts after ordinary process exits. This service's database is separate from the original cockpit volume; the command does not copy or delete your old local database.

For remote phone/browser access, put an HTTPS reverse proxy in front of `127.0.0.1:8500`. For example, with a properly configured DNS name, a Caddy site can use:

```caddyfile
your-domain.example {
    reverse_proxy 127.0.0.1:8500
}
```

Then open `https://your-domain.example/trade-alert` and enter the dashboard token. Do not expose the backend publicly without authentication and TLS. Provider reachability, disk space, backups and actual endurance still need operational monitoring; `restart: unless-stopped` does not demonstrate 24/7 reliability.

Stop this service while preserving its data:

```bash
docker compose --profile trade-alert stop trade-alert
```

Operating costs can include the VPS, persistent disk, backups and bandwidth. No paid/private exchange API is required for this service; public provider rate and geographic restrictions still apply.

## Replay and research

Replay uses actual caller-supplied, point-in-time arrival data; it does not download or manufacture missing historical evidence:

```bash
expansion-hunter --config config/settings.json trade-alert-replay arrival-data.json --output trade-alert-replay-report.json
```

The input is a JSON object with a nonempty `observations` array, in arrival/decision order. Each observation contains:

| Field | Content |
| --- | --- |
| `as_of` | Decision timestamp with timezone. |
| `received_at` | Actual receipt timestamp, no later than `as_of`; defaults to `as_of` if omitted. |
| `asset` | Existing Asset object: `asset_id`, `symbol`, `name`, and preferably native `exchange_symbol`/`quote_asset`. |
| `candles` | Frame-to-candle-list mapping for `1m`, `5m`, `15m`, `1h`, `4h`. Incremental lists are allowed. |
| `btc_candles` | Optional BTC completed 5m candles for context. |
| `price`, `tick_size` | Optional genuinely observed price and native price increment. |

Every candle needs `open_time`, `close_time`, `available_at`, `open`, `high`, `low`, `close`, `volume` and `source`. `quote_volume` and `taker_buy_volume` are optional; missing values remain unknown. Do not label estimated buy volume as native. The engine ignores unavailable/future bars, preserves the first known closed candle, flags later revisions and rejects out-of-order arrivals or future outcome labels used as input.

Optional top-level `validation` accepts `train_end`, `holdout_start` and `embargo_seconds`. This makes chronological, purged partitions; it does not fit a model or prove a blind holdout result. Optional `evidence` labels dataset provenance. An empty/missing history produces insufficient evidence rather than synthetic alerts.

The background research worker evaluates matured 5m, 15m, 1h, 4h and 24h horizons. Whole-candle return/MFE/MAE measurements exclude unknown partial intervals; gaps are censored. The default 20-basis-point cost deduction is a **hypothetical** round-trip assumption, not realized execution cost or trade PnL. Outcomes remain separate from live features. Miss objects retain available prior observations and missing-data reasons; archives cannot reconstruct information never recorded.

## Current limits

- **500 is configured capacity, not proven simultaneous live coverage.** Native eligible pairs and freshness depend on the provider. Candle warmup is conservative under provider budgets; large cold starts can take time.
- This dedicated candle-only service has native candle taker volumes but does not add observed order books, CVD, futures or identified whale wallets. Their absence remains explicit.
- Historical MET/SAND/BEAM/AIN examples do not establish profitability. Authentic point-in-time datasets, matched non-winners and independent out-of-sample validation are still necessary; synthetic tests are software checks only.
- Live connectivity and uptime must be assessed from actual smoke evidence and forward observation. This guide makes no millisecond-latency, continuous 24/7 or calibrated-probability claim.
- Closed bars, alerts and first-decision rejection snapshots grow the database. At 500 continuously evaluated assets, one observation per closed minute can reach **720,000 observations/day**. Retention and actual disk growth are not yet measured; do not treat current storage as indefinitely bounded.
- Exact tests, smoke results and measured limitations belong in the current delivery report. This guide does not substitute for those results.
