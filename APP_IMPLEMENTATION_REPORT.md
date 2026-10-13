# Expansion Hunter — live app implementation report

Updated **5 October 2026, Europe/Amsterdam**. Exchange and archive timestamps remain stored in UTC. This report distinguishes executable functionality, real public-data observations and unvalidated research.

**The browser cockpit, public streaming feed, automatic discovery, fast-watch monitor and persistent signals now exist as executable code. The application remains in OBSERVATION mode and cannot place exchange orders. A personal watchlist is optional. Reliable coverage of every requested asset, 24/7 endurance and profitable expansion prediction have not been demonstrated.**

## What changed from the audited baseline

The original audit recorded 201 passing tests and a scanner/report/API foundation without a live cockpit. A later pre-addition checkpoint recorded 312 tests in [master-addon-baseline.json](reports/master-addon-baseline.json). Existing indicators, AVWAP, risk calculations, legacy CLI/API interfaces and historical SQLite evidence were preserved.

Additions include the live app, public Binance WebSocket adapter, bounded incremental feed state, independent fast evaluation, numeric research scores, discovery ranks, automatic promotion, liquidity objects, Fibonacci research overlays, persistent watchlists/events and separate intelligence workers.

The application now starts broad discovery without requiring a manually entered coin. The **Start scanner** button supervises the broad scanner and the live fast-monitoring worker. Broad discovery runs in a separate process; a slow broad scan does not become the timer for a 1m breakout evaluation.

The latest automatic app/browser acceptance repetition **FAILED** with an API timeout while broad discovery, live processing and regression tests ran concurrently. The clean final regression run passed **858 tests in 122.79 seconds**; a subsequent packaging compatibility check added two passing tests. The final clean suite passed 858 tests in 122.79 s. Ruff and compileall passed; no static type checker is configured. Actual automatic startup already received native streams for 13 automatically selected coins with an empty personal watchlist; the complete latest browser repetition did not pass and must not be presented as acceptance. Earlier browser/public-feed checks passed, as detailed below, and are not substituted for the latest acceptance test.

## Demonstrated functionality and limits

| Component | Status | Evidence and practical scope |
| --- | --- | --- |
| Existing scanner, indicators, AVWAP and risk/report logic | ✅ WORKING within the tested baseline | Preserved code and regression suite; no execution client introduced. |
| Automatic broad discovery | 🟡 PARTIALLY IMPLEMENTED operationally | Real request for 500 assets completed; 491 universe records, 122 lightweight histories and 76 eligible assets. Native provider coverage and identity exclusions remain visible. |
| Cross-sectional ranking and promotion | ✅ WORKING as deterministic research infrastructure | The real cycle produced one WATCH and ten RADAR promotions; no manually preferred winners. Native-provider metadata is not required to be present for every historical bar percentile channel. |
| Independent asynchronous public feed | ✅ WORKING in bounded real checks | 3,776 actual Binance messages in a 20-second smoke; live browser check also received changing native prices and candles. This is not an endurance test. |
| Reconnect, freshness, timestamps and health | ✅ WORKING within tested scope | Decoder/feed tests, stale-state checks and app stop/start smoke. Packet-loss recovery and multi-day uptime are not established. |
| Fast Ignition Monitor | ✅ WORKING as an observable state machine | Deterministic tests and real BTC 1m breakout, 5m acceptance and subsequent failed-breakout events archived. Neither historical edge nor guaranteed continuation is implied. |
| Mobile browser cockpit and live chart | ✅ WORKING in the recorded browser smoke | Desktop/mobile Chromium, six timeframe views, changing candles/prices, exact liquidity levels, indicator overlays and visible disconnect/reconnect. |
| Personal watchlist | ✅ WORKING; optional | Add/remove/pin API and persistent SQLite state. Monitoring priority does not increase the score. |
| Automatic empty-watchlist app startup | ⚠️ LATEST ACCEPTANCE FAILED | Production supervisor and UI changes exist; latest full acceptance result will be recorded separately. |
| Stored Energy, Absorption, Trigger Sensitivity, Expansion Readiness and Volume Awakening | ✅ WORKING as calculations | Explainable deterministic components, UNKNOWN when insufficient. All scores remain UNCALIBRATED and are not probabilities. |
| Live liquidity map | ✅ WORKING within public-data scope | Exact observed top-of-book levels and categorized structural/inferred objects. Removed orders are not automatically described as consumed. Partial book depth is not the full exchange book. |
| VWAP, AVWAP, Volume Profile | ✅ WORKING within supplied-history limits | Existing causal AVWAP preserved; live overlays added. Candle-derived Volume Profile remains explicitly ESTIMATED. Missing session coverage stays unavailable. |
| Fibonacci/Golden Ratio | 🟡 IMPLEMENTED RESEARCH HYPOTHESIS | Deterministic confirmed anchors, ratios, golden pockets and descriptive confluence exist. Production predictive weight is zero; empirical placebo/out-of-sample validation has not been performed. |
| Cycle/macro/analogue infrastructure | 🟡 PARTIALLY IMPLEMENTED | Point-in-time contracts, storage, calculations, APIs and tests exist. The observed cycle report was NEUTRAL_UNCERTAIN with weak evidence; reliable numeric macro releases/consensus are unavailable. |
| Signal history, outcome maturation and misses | 🟡 PARTIALLY IMPLEMENTED research archive | Actual events and subsequent candle observations are stored; six matured outcomes, nine censored outcomes and 25 pending outcomes were recorded in one limited check. This is not a broad validated winner/control library. |
| Notifications | 🟡 PARTIALLY IMPLEMENTED | Browser notifications for newly observed events while the tab is open, plus persistent local outbox. Telegram/background mobile push delivery is not implemented/configured or verified. |
| Named SAND/BEAM/AIN matched regressions | ❌ NOT IMPLEMENTED with real datasets | No valid dated event datasets and matched controls have been reconstructed. No predictive findings or tuned thresholds are claimed. |
| Astrology/numerology | ❌ NOT IMPLEMENTED | No production influence. An isolated statistically tested experimental implementation remains future work. |
| Paper trading and calibrated probabilities | ❌ NOT IMPLEMENTED | Conditional legacy paper plans are not a paper-account simulator. Scores do not become probability forecasts. |
| Live trading | ❌ ABSENT AND DISABLED | No signed trading client, order endpoint, private trading key or withdrawal capability. |

## Actual automatic discovery coverage

[real-500-discovery.json](reports/real-500-discovery.json) contains the real public-provider cycle completed on **5 October at 14:54:46 Amsterdam time** (12:54:46 UTC).

| Count | Actual |
| --- | ---: |
| Requested target | 500 |
| Universe assets returned | 491 |
| Lightweight histories scanned | 122 |
| Eligible assets | 76 |
| Deep-scanned assets | 10 |
| Partial-data assets | 76 |
| Excluded assets | 415 |
| Failed assets | 0 |
| WATCH promotions | 1 |
| RADAR promotions | 10 |

The cycle status is **DEGRADED**, not “500 fully covered.” Exclusions include identity ambiguity, unavailable exact spot instruments, configured asset policy and insufficient liquidity. Nine requested universe rows lacked a usable market-cap rank and were individually reported by the universe provider. All 76 eligible rows were partial because some advanced evidence was unavailable. “Zero failed” does not mean complete data.

The broad tier uses lightweight native market snapshots and bounded candle history; only selected candidates receive expensive multi-timeframe/deep work. Defaults in this recorded cycle were top 500, a 15m light timeframe with 128 bars, at most ten deep scans per cycle and a dynamic deep watchlist capacity of 50. The app obtains candidates from persisted promotions automatically.

The cockpit now displays the configured target separately from discovered, eligible, light-scanned, deep-scanned, partial, failed and excluded counts. Old archive observations are labelled as archived rather than presented as current market coverage.

## Final data flow

```text
CoinGecko ranked universe and metadata
  → official Binance public REST lightweight observations
  → cross-sectional/history ranks, quality checks and automatic promotion
  → immutable discovery archive in operational SQLite
  → dynamic fast-watch membership, plus optional personal priorities

Public Binance WebSocket, independently of the broad scanner
  → incremental trades, bid/ask, partial depth and candles
  → predefined 5m resistance and pressure
  → intraminute attempt / completed 1m confirmation
  → subsequent 5m acceptance / 15m structural confirmation
  → persistent event, timestamps, quality and local notification outbox
  → browser API polling, live SVG chart, overlays and signal history

Separate intelligence/research worker
  → archived market context, macro vintages and historical feature vectors
  → causal cycle/analogue calculations and matured/censored outcomes
```

The browser does not request private exchange data. It refreshes the selected live cache approximately once per second and the overview every three seconds; it does not recompute full market histories on each browser refresh. Market streaming and the browser-to-API transport are separate layers.

Full archive reads were removed from repeated overview/membership hot paths in favour of thin cached summaries. The parent integration check measured cached membership at **0.066–0.314 ms**, compared with an earlier **0.867 s** full-read path. These are local function measurements, not public-network latency or end-to-end guarantees. Initial per-coin warmup is bounded and runs in the background so a newly added asset does not pause already monitored assets.

## Fast state machine and exact confirmation principle

Fast lifecycle states are:

```text
WATCH → PRE_IGNITION → LOADED → BREAKOUT_PRESSURE
  → BREAKOUT_ATTEMPT → 1M_BREAKOUT_CONFIRMED
  → 5M_ACCEPTED → 15M_EXPANSION_CONFIRMED

Alternative paths: LIQUIDITY_SWEEP, FAILED_BREAKOUT, COOLDOWN, DEMOTED
```

Resistance is selected from available, confirmed 5m structure; the latest candle is excluded from anchor selection. Its event time and discovery time are retained. Pressure uses volatility-adjusted proximity, separate resistance attacks, higher lows, compression, close location, rejection weakening and available volume evidence. The production thresholds are explicit in `FastSettings`.

The confirmation rule is a **completed 1m close above the already known resistance plus the configured ATR/tick buffer**. A live trade above resistance can produce BREAKOUT_ATTEMPT. A wick above with a close back below cannot become a confirmed breakout. Volume and order-flow support change quality, not whether the objective close occurred. No 5m/15m/higher-timeframe close delays the original 1m confirmation.

On the recorded real BTC stream, the archive contains:

| Event | Market timestamp, Amsterdam | Recorded detection latency |
| --- | --- | ---: |
| 1M_BREAKOUT_CONFIRMED | 5 Oct 14:15:00 | 94.689 ms |
| 5M_ACCEPTED | 5 Oct 14:20:00 | 150.847 ms |
| FAILED_BREAKOUT | 5 Oct 14:21:00 | 96.347 ms |

Evidence: [real-app-events.json](reports/real-app-events.json). The same archive includes PRE_IGNITION/LOADED events about 182 seconds after the referenced 5m candle during warmup; those are not evidence of a 182-second early warning. A breakout that later failed was correctly retained as a failure. No profitable result is inferred.

## Persistence and point-in-time integrity

The explicit operational database is `data/cockpit.sqlite3`. Legacy `data/smoke.sqlite3` is retained; the safe import copied its two runs, two observations and one global observation without deleting the source ([smoke-import.json](reports/smoke-import.json)).

Additive SQLite migrations preserve legacy tables and add discovery cycles/observations/events/transitions, immutable intelligence objects, signal context, operational state, personal watchlist and a notification outbox. WAL, locks, indexes, compressed archives and bounded current caches reduce contention. Raw ticks are not retained indefinitely.

Completed and open candles remain separate. Source, receipt, evaluation and alert times are explicit. Confirmed pivots cannot be used before their confirmation time. Macro initial releases, consensus snapshots and revisions are immutable separate records. Historical analogue matching excludes unavailable/future vectors and does not inspect outcomes while selecting matches. Future outcomes live in the research archive and do not become live ranking inputs.

Restored fast state starts stale until current data supports evaluation. The database can recover state and deduplication identifiers; this is different from guaranteeing the whole service resumes after every host/network failure. Historic replay requires genuinely recorded availability, not a historical date pasted onto data downloaded today.

## Tests and measured checks

| Check | Result and scope |
| --- | --- |
| Original audited baseline | 201 passed; old audit retained in PROJECT_STATUS.md. |
| Pre-addition checkpoint | 312 passed in 69.48 s; [master-addon-baseline.json](reports/master-addon-baseline.json). |
| Latest completed regression suite after startup/performance fixes | **650 passed in 128.63 s**, parent integration execution. Further supervised-restart tests are still being added, so the final total is pending. |
| Executable cockpit JavaScript contract tests | Four passed; target/current coverage, automatic discovery without a personal list, archive labelling and exact native chart/liquidity values. Fixtures are explicitly test-only. |
| JavaScript syntax / cockpit test lint | `node --check` and Ruff passed for the new UI contract tests. |
| Latest lint / bytecode validation | Full Ruff check and `compileall` passed in the parent integration run. A final rerun after supervised-restart changes is pending. No static type-checker configuration was present in the baseline; compileall is not a static type check. |
| Real public WebSocket smoke | 3,776 messages in 20 s, no malformed/rejected messages, no synthetic fallback. |
| Real public-data browser smoke | Changing candles and price without refresh, six timeframes, 50 native liquidity levels, 387 overlays, mobile overflow false, no browser errors, visible stop/start recovery. |
| Latest automatic app smoke with empty personal watchlist | **PENDING**; earlier browser smoke must not be substituted for this acceptance check. |

Real browser evidence: [live-app-smoke.json](reports/live-app-smoke.json), [desktop screenshot](reports/cockpit-desktop.png), [mobile screenshot](reports/cockpit-mobile.png), [disconnected screenshot](reports/cockpit-disconnected.png). The earlier screenshot/test captured the previous button wording; the latest UI now uses “Start scanner.”

Performance measurements:

| Measurement | Result | Limitation |
| --- | ---: | --- |
| Real 500-target broad lightweight phase | 16.937 s | 491 returned, 122 histories, 76 eligible; not all 500 supported. |
| Real selected deep phase | 37.465 s | Ten assets; derivatives remained unavailable. |
| Real complete discovery elapsed | 58.401 s | One recorded cycle, no multi-day stability claim. |
| Real HTTP requests / recorded weight | 97 / 758 | Recorded cycle only. |
| Measured rate-limit waiting | 9.051 s | Venue/client budget waits in this run. |
| Synthetic million-event decode/apply throughput | 10,787 events/s | No network, SQLite, dashboard clients or notification delivery in this benchmark. |
| Synthetic million-event processing latency p50/p95/p99 | 46.971 / 142.312 / 538.245 µs | Native decoder plus incremental state; compute time only. |
| Synthetic stress peak RSS | 80.36 MB | Whole process high-water mark; about 8.26 MB growth. |

[million-events.json](reports/million-events.json) accounts for all one million attempts: 974,982 accepted, 24,213 state rejections and 805 malformed decoder rejections. The fast-monitor stress replay additionally passed 9,001 invariant checks. These are stress observations, not one million independent tests or evidence of predictive value.

The earlier [phase1-benchmark.json](reports/phase1-benchmark.json) contains 50/100/250/500/1000-asset **synthetic** engine/SQLite capacity runs. Its zero promotion thresholds and fake provider I/O were for infrastructure measurement; those timings are not described as real-provider coverage.

## How to use it

From the existing project directory, with Python 3.11 or newer:

```bash
python -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/expansion-hunter --config config/settings.json app --host 127.0.0.1 --port 8000
```

Open **http://127.0.0.1:8000/cockpit**. Click **Start scanner**. The app discovers candidates automatically; adding symbols is optional. The configured target is 500, and actual exclusions/coverage remain visible. Leave the backend running to collect observations. Closing a browser is not an exchange order action.

Useful commands:

```bash
# Full software checks
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests benchmarks --no-cache

# Separate current observation/research processing
.venv/bin/expansion-hunter --config config/settings.json intelligence --watch --interval 60

# Bounded real public streaming check
.venv/bin/expansion-hunter --config config/settings.json live-smoke --symbol BTCUSDT --seconds 20 --output reports/live-smoke.json

# Read the actual operational archive
.venv/bin/expansion-hunter --config config/settings.json discovery-status
.venv/bin/expansion-hunter --config config/settings.json discovery-events --limit 100

# Causal replay: use an actual saved snapshot and original available timestamp
.venv/bin/expansion-hunter replay --snapshot /path/to/actual-snapshot.json --at '<actual-ISO8601-decision-time>' --output reports/replay.json

# Matched-control research: requires a genuine point-in-time dataset
.venv/bin/expansion-hunter research --dataset /path/to/verified-study.json --research-config config/research.json --output reports/matched-study.json

# Container option; requires configured dashboard token
docker compose up --build -d
```

There is no separate frontend build or Node/CDN requirement to use the app: HTML, CSS and chart JavaScript are shipped with the Python package. Node is optional for the executable frontend contract tests. `/docs` exposes API documentation. `/api/live/health` reports feed/worker/archive state; `/ready` rejects an app without a running worker, fresh feed and fresh fast evaluation.

## Configuration, security and costs

`config/settings.json` controls the operational database, public provider hosts and the app's configured `top_n`. `config/discovery.json` controls discovery budgets and thresholds; the app-owned scanner sets its universe size from the app's `top_n`, currently 500. `LiveFeedSettings`, `FastSettings` and live-analysis settings make stream capacity, freshness, history and pressure/confirmation thresholds explicit.

| Environment variable | Purpose |
| --- | --- |
| `COCKPIT_API_TOKEN` | Bearer protection for data endpoints and remote app controls. The local UI holds the entered token only in tab memory. Required by the supplied Compose configuration. |
| `COINGECKO_DEMO_API_KEY` | Optional provider demo key. |
| `COINGECKO_API_KEY` | Optional provider API key; a Pro key also needs the correct provider base URL. |
| Standard HTTP/proxy/CA environment | Respected by the data clients when configured. This changes connectivity, not data quality or timestamps. |

No private Binance trading key is required. Public data is used. Local hosting uses your machine; a continuously running VPS/cloud machine can create hosting, storage and bandwidth costs. Paid CoinGecko/other future data subscriptions cost money only if you choose them. No paid service was purchased by this implementation.

The included Docker/Compose files are deployment configuration, not a verified deployed 24/7 service. Use authentication and HTTPS before exposing the app outside localhost. The current browser-notification path requires permission and a supported browser/security context and does not deliver while the browser tab is closed.

## Remaining limitations

1. Current providers do not cover all requested 500 assets. Strict identity verification excludes many ambiguous tickers; missing market-cap ranks still affect the CoinGecko-ranked universe. Additional verified mappings/providers are needed for broader trustworthy coverage.
2. Derivatives endpoints can remain blocked. OI, funding, liquidations and historical CVD are not fabricated or treated as neutral. Spot discovery continues.
3. Observed book data is bounded partial depth. It cannot identify actual stop orders or prove that a removed wall was traded rather than cancelled.
4. Scores and cycle labels are hypotheses. Fibonacci placebo tests, calibrated probabilities, independent winner/failure cohorts and SAND/BEAM/AIN point-in-time studies remain unperformed.
5. Macro storage/surprise/revision logic is implemented; reliable live consensus and initial numeric release adapters are unavailable. A missing calendar is not evidence that no release is scheduled.
6. The current history is too short and sparse for reliable statistical performance estimates. Outcome censorship is explicit; zero recorded misses is not zero real-world misses.
7. Browser notifications/local outbox are not a completed external delivery service. Telegram, background mobile push and delivery acceptance remain unfinished.
8. Multi-day uptime, realistic 20–50 concurrent real symbols, many browser clients, complete end-to-end CPU/memory load and durable hosting have not been demonstrated.
9. No trading, PnL, profit forecast or optimized production weights were enabled. Experimental astrology/numerology are absent and cannot influence alerts.

## Changed files and evidence references

The implementation is additive. Main executable additions/reused integration points include:

- `providers/binance_ws.py`, `live_models.py`, `live_feed.py`, `live_runtime.py`: public streams, native data contracts, incremental state and independent worker.
- `fast.py`, `fast_models.py`, `live_analysis.py`: objective resistance/pressure/confirmation, liquidity/indicator/Fibonacci context.
- `discovery.py`, `discovery_models.py`, `discovery_features.py`, `discovery_ranking.py`, `discovery_scheduler.py`, `discovery_store.py`, `app_discovery.py`: two-tier discovery, ranks, promotion, archives and app-owned broad process.
- `intelligence_store.py`, `intelligence_api.py`, `api.py`, `cli.py`: persistent app state, protected APIs, startup controls and operator commands.
- `cycle.py`, `cycle_patterns.py`, `macro.py`, `macro_models.py`, `providers/macro.py`, `analog.py`, `intelligence.py`, `event_learning.py`: separately executed causal context/research infrastructure.
- `static/cockpit.html`: responsive Dutch app, live candlesticks, exact levels, score/health panels, automatic-scan coverage and optional watchlist.
- Corresponding unit/integration tests, `tests/test_cockpit_browser.py`, benchmark scripts, `Dockerfile`, `compose.yaml`, package/static configuration and preserved operational settings.

[implementation-files.json](reports/implementation-files.json) is the path-by-path baseline comparison; its final refresh is pending the parent integration pass. No Git checkpoint was available in the inspected repository. The unchanged original audit is retained below the current status in PROJECT_STATUS.md rather than erased.


## War Room addition and current export checkpoint

- `war_room.py` adds bounded causal state memory on 1m/5m/15m/1h/4h/1D. It records pivot confirmation/discovery times, structure transitions, alignment, conditional bullish/range/bearish scenarios, actual known destinations and separate entry-quality evidence. It runs in background analysis, preserving the existing immediate 1m FastMonitor rules. New transitions are immutable `structure_transition` records; `war_room_state` is restart memory.
- `trade_calculator.py` and `calculator_api.py` add manual LONG/SHORT, maximum-risk, target-profit and supplied portfolio calculations. Costs/funding are inputs, unknowns stay unknown, liquidation is only an isolated estimate when maintenance assumptions exist, and no order/trade storage/execution occurs.
- The cockpit adds collapsed details for structure/scenarios/calculators and an optional conditional-price overlay. Missing levels are not replaced with Fibonacci or arbitrary targets. Fibonacci research now includes all six live timeframes and labels 0.705 as a trading convention; research production weight remains zero.
- Personal watchlist remains optional and empty in the automatic test. Desktop session credentials are held in memory, never in URLs or browser storage. Native Windows build source and bundled-runtime builder exist, but no actual Windows installation has been verified.
- Actual known limitations: latest automatic live browser attempt timed out; current operational archive grew to about 4.2 GB; no real SAND/BEAM/AIN matched point-in-time validation, calibrated edge, paper account, external mobile delivery, all named discretionary patterns or independently proven 24/7 performance.
- Source change inventory: `reports/app-changed-files.json`. Existing `smoke.sqlite3` and operational archives are preserved. No Git checkout is available for a commit; the export is a file checkpoint.

The source export contains a consistent SQLite backup fixed at 2026-10-05 17:25:43 UTC. This is an explicitly dated historical snapshot, not a claim that later logs/research results were present in that database at that timestamp.
