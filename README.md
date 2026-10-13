# Expansion Hunter — Quant Trading War Room

A public-data market intelligence app. It discovers candidates, follows live charts and records conditional LONG/SHORT setups. It never sends exchange orders. Scores are research hypotheses, not win probabilities.

## Trade Alert V1

The optional **Trade Alert** service records native spot LONG/SHORT structure, pressure and completed 1m breaks without placing orders. Start it with `expansion-hunter --config config/settings.json trade-alert`, then open `http://127.0.0.1:8000/trade-alert`. See [TRADE_ALERT_V1.md](TRADE_ALERT_V1.md) for installation, visible coverage, Telegram setup, the uncalibrated score rules, replay and Docker deployment. The existing `app` command remains available.

## Windows: install, start and stop

1. Get **ExpansionHunter-Setup.exe** from the build's actual delivery location.
2. Double-click it. The installer includes Python and the app; you do not need to open a terminal or install Python. Microsoft WebView2 may need an internet connection during setup.
3. Open the **Expansion Hunter** desktop shortcut. The app opens its database, starts its local market service and shows the cockpit automatically.
4. Wait for received public data. The broad universe price table starts through its own native stream; selected/deep charts need actual candle history and may take longer to warm up. **LIVE**, **DELAYED** and **STALE** are shown per instrument, not inferred from an animated interface.
5. Close the app window to stop the local service. **Stop scanner** stops monitoring while leaving the interface open.

The executable is built from this repository for Windows x64. Its PE/payload integrity and controller behavior are checked in the development environment. **Actual Windows installation, WebView2 display and firewall behavior have not been verified on a Windows machine.** The installer is an app package; the older 4 GB ZIP is a separate source/history backup.

Existing user databases are kept. User files live under `%LOCALAPPDATA%\ExpansionHunter`, with logs in its `logs` folder. The desktop service binds only to a random loopback port and uses a session token; you do not enter a localhost address.

## Using the app

- **War Room:** search the native lightweight universe, or open an automatically promoted candidate. Its price table updates through a one-second server stream. Best LONG/SHORT panels only use available intensive evidence; a lightweight activity rank is not Expansion Readiness. Missing evidence may mean **NO EDGE / WAIT**.
- **Chart:** switch between 1m, 5m, 15m, 1h, 4h and 1D. Each timeframe calculates its own confirmed Auto Fib. The Golden Pocket is on by default. No valid confirmed swing means an explicit unavailable/invalidated state.
- **Rookie mode:** read bias, swing phase, setup quality, entry quality, entry zone, trigger, structural invalidation and targets. The next three steps are conditional structural states, not predicted future candles or invented OHLC.
- **Pro mode:** opens structure, liquidity, flow, sessions, market context and research detail from the same analysis.
- **Layers:** toggle overlays. Drag to pan, use zoom controls or Ctrl/Meta-wheel, and reset to the latest candles. Two-finger pinch is supported by the chart handlers.
- **Watchlist:** optional. Automatic discovery does not require personal coins. Opening a coin temporarily requests public-data monitoring without improving its score.
- **Alerts:** pressure is early warning; a completed 1m close beyond a predefined level is confirmation. A wick alone is an attempt/sweep. Volume changes quality, not the fact of the close.
- **Calculator:** choose LONG/SHORT, entry, stop, target, margin and leverage. Fees and declared funding are included. Structural invalidation and estimated liquidation are different prices.
- **Paper journal:** manually record simulations and observed outcomes. Unknown funding, missing market coverage and ambiguous candle ordering remain explicit.

## What is live and what is unavailable?

**Broad live prices:** an independent Binance `!miniTicker@arr` worker follows up to the configured **500 verified USDT spot pairs**, selected by native 24h quote liquidity. It needs no personal watchlist and does not request full candle histories or order books for all 500. Bootstrap uses public native instrument definitions and 24h tickers. CoinGecko remains a separate market-cap/metadata source; provider restrictions there do not prevent verified native spot instruments from entering lightweight discovery. This is a quote-liquidity universe, not a claim to reproduce the top 500 crypto assets by market capitalization.

**Freshness:** the browser receives `/api/live/universe/stream` snapshots every second. Binance's mini-ticker sends changed pairs only, so an unchanged coin keeps its true source timestamp and grows older even while the table updates. The UI reports selected, live, delayed, stale and excluded counts. A connected socket alone does not make all 500 rows live. Rolling 24h volume changes are labelled proxies, not 1m candle volume, real RVOL or CVD.

**Fast/deep inputs:** automatically promoted candidates and optional personal/chart interest receive native trades, best bid/ask, bounded depth and multi-timeframe candles in a **separately capped** worker. Fresh observed minute-price activity and BTC-relative anomalies can promote native candidates without waiting for a full CoinGecko/history cycle. Their actual 5m pressure and completed 1m confirmation still require proper candle evidence; promotion never invents a score or breakout. Existing historical broad discovery, ranks and stored transitions remain in use.

**Derived/estimated:** structure, swing phase, scenarios, Fib, VWAP/AVWAP and scores. Candle-derived Volume Profile is an estimate of traded volume, not resting orders. Observed partial-book prices are separate from inferred stop zones.

**Unavailable unless configured/covered:** identified on-chain wallets, futures when the provider rejects access, reliable liquidation clusters, sector baskets, calibrated probabilities and real macro consensus/release numbers. There is no production demo-price fallback.

**Stored evidence:** immutable minute universe snapshots retain prices, activity ranks/features, exchange/receipt timestamps, quality and coverage. Existing discovery observations, structure transitions, fast alerts and outcomes are archived separately for point-in-time research. Minute archives cannot reconstruct unrecorded ticks, and new archives do not create missing historical SAND/BEAM/AIN evidence.

**Not proved:** profitable predictive edge, every selected pair receiving a fresh quote every second, complete intensive analysis of all 500, 24/7 endurance, calibrated probabilities, and real SAND/BEAM/AIN matched historical validation. See [PROJECT_STATUS.md](PROJECT_STATUS.md), [PRODUCT_ARCHITECTURE.md](PRODUCT_ARCHITECTURE.md) and the final implementation report for actual measured coverage and test results.

## Optional keys and notifications

No private exchange or trading keys are required.

- `COINGECKO_API_KEY` (Pro) or `COINGECKO_DEMO_API_KEY`: optional provider tier. Provider limits and pricing depend on your plan.
- `COCKPIT_API_TOKEN`: protect remotely exposed API data/control. The desktop creates a session token automatically. Use TLS and authentication for remote deployments.
- `COCKPIT_TELEGRAM_BOT_TOKEN`, `COCKPIT_TELEGRAM_CHAT_ID`: optional Telegram notification delivery through the app's independent background worker; both must be configured. It sends recent persisted fast events, records Telegram acknowledgement and measured latency, retries bounded failures and deduplicates acknowledged events across restarts. Without credentials it reports **UNAVAILABLE** and makes no HTTP request. Credentials remain outside source code; they are not private exchange keys.
- `ETHERSCAN_API_KEY`: optional read-only Etherscan V2 wallet observer.
- `COCKPIT_WHALE_CHAIN_ID`: monitored chain ID (default 1).
- `COCKPIT_WHALE_WALLETS`: comma-separated explicit public EVM addresses.
- `COCKPIT_WHALE_CONTRACTS`: optional comma-separated token contracts.
- `COCKPIT_WHALE_ASSET_MAP`: JSON mapping `chain_id:contract` to the canonical asset ID. Ticker symbols alone do not identify a token.
- `COCKPIT_WHALE_MAX_QUERIES`: bounded queries per polling cycle (default 25).

Browser notifications require permission and an open supported tab. Telegram's server acknowledgement means the provider accepted a message, **not** that a handset displayed it. A process crash between provider acceptance and local persistence can cause a duplicate retry; stale queued signals are suppressed. Mock-transport tests prove adapter behavior, not delivery to your phone. Unconfigured real external delivery remains unverified.

Without wallet configuration the UI says **UNAVAILABLE / API KEY REQUIRED**. Token transfers never prove an exchange sale, institutional identity or wallet purpose. Keys are not bundled, logged or required for spot charts.

Environment options and provider budgets are also defined in `config/settings.json`, `config/discovery.json`, and typed settings modules. The installer copies safe defaults; it does not delete a user's existing settings.

## Logs and troubleshooting

- **No live candles:** inspect Data Health. Verify internet access and that Binance public data is reachable in your country/network.
- **STALE/DISCONNECTED:** stop/start monitoring. Automatic reconnect and bounded native gap repair exist; the UI keeps stale status visible until fresh evidence arrives.
- **A coin is delayed/stale:** mini-ticker arrays include changed pairs, not every instrument every second. Check the row's actual observation age and feed health; another coin's update does not refresh this one.
- **Not all 500 coins have deep charts:** the broad lightweight universe and capped intensive watch are different layers. Actual native coverage depends on verified supported pairs; a selected coin may be warming up or stale. Unsupported identities and exclusions have recorded reasons, and CoinGecko market-cap coverage is reported separately.
- **No Fib lines:** the selected timeframe may lack a qualified confirmed swing or its origin may be invalidated. The engine does not select hindsight anchors to make the chart attractive.
- **No whale information:** configure a read-only provider key and explicit wallet/contract mapping; ownership remains unknown without validated provenance.
- **Desktop fails to open:** see `%LOCALAPPDATA%\ExpansionHunter\logs\desktop.log`, `backend.log`, and installer logs. Check Windows x64 and WebView2 installation. Windows runtime verification remains outstanding.
- **A ZIP link does not download:** a file path inside the development workspace is not a delivered download. Ask for a real transfer location; rebuilding the archive alone does not fix delivery.

## Developer installation and commands

The Windows packaged app does not require these commands. They are for developers/research operators using the existing source.

```bash
python -m pip install -e ".[dev]"
expansion-hunter --config config/settings.json app
python -m pytest -q
python -m ruff check src tests scripts benchmarks
python -m compileall -q src
```

The browser cockpit is served by the Python app; no separate npm frontend process is needed. No static type checker is configured; compilation is not static type checking.

Replay and research use explicit point-in-time datasets:

```bash
expansion-hunter war-room-replay --snapshot snapshot.json --at 2026-01-01T00:00:00Z --output replay.json
expansion-hunter research --dataset study.json --research-config config/research.json --output study-report.json
expansion-hunter --config config/settings.json live-smoke --symbol BTCUSDT --seconds 20 --output public-feed.json
```

These commands do not create missing historical information or imply successful calibration. Future outcomes remain separate from feature construction.

Windows packaging from the same source:

```bash
python scripts/build_windows_bundle.py --include-webview-bootstrapper
python scripts/build_windows_launcher.py --zig build/windows/toolchain/zig-x86_64-linux-0.15.2/zig --payload build/windows/payload.zip
```

Always-on Docker configuration is in `Dockerfile` and `compose.yaml`. They need an actual host, provider reachability, backups, monitoring and measured endurance before 24/7 reliability can be claimed.

Previous README/status versions are retained in `reports/README-before-final-product.md` and `reports/PROJECT_STATUS-before-final-product.md`.
