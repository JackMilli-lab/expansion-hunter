# Expansion Hunter / Quant Trading War Room — verified scope

Updated 2026-10-06. This replaces the earlier status claims; the original audit is preserved in `reports/PROJECT_STATUS-before-final-product.md`.

The existing application is being extended in place. **Exchange order execution remains absent.** Production uses actual public observations; fixtures belong only to tests/replay. Missing data is explicitly unavailable, and scores are uncalibrated evidence summaries.

## Current verification boundary

The starting source checkpoint had **860 passing tests**. The latest complete run before the final reliability fixes had **1,700 passing and one failing test**. That failure expected a LONG Stored Energy value without providing a known direction; the fixture now provides explicit LONG context, and a separate UNKNOWN regression requires unavailable directional values. Its targeted runtime/API suite passed **32 tests**. The final aggregate check is pending and will replace this paragraph with its actual result.

The native public-data smoke observed **501 verified USDT spot pairs, with 500 selected and seeded**. This is selection by native quote liquidity, not a claim that all global top-500 market-cap assets trade on Binance. Binance sends changed pairs approximately once a second; every row retains its actual source/receipt time. Quiet pairs can be delayed/stale. A previous full browser run exposed CPU/feed-lag and a chart-renewal race; those failures are retained. Final live/browser verification remains pending after the fixes.

## What is built

| Component | Classification | Exact implemented boundary |
|---|---|---|
| Existing scanner, indicators, risk and read-only API | WORKING within tested baseline | Preserved; no exchange order client or private trading credential requirement. |
| Native broad price universe | WORKING source and real connection; final browser check pending | Independent public all-market mini-ticker stream, configured 500 target, light cross-sectional activity ranks, source age and minute archive. |
| Full advanced analysis for all 500 every second | NOT IMPLEMENTED / deliberately separated | Heavy histories/books run only on bounded promoted/chart/personal candidates. Every-second price updates are distinct from completed-candle analysis. |
| CoinGecko market-cap discovery | PARTIAL | Existing ranked metadata discovery continues; unsupported/missing/ambiguous exchange mappings and provider limits are visible. |
| Dynamic promotion and scheduling | WORKING deterministic infrastructure | Native observed-minute anomalies and existing historical discovery promote without a personal list; finite monitoring capacity and expiry apply. Priority never adds a research score. |
| Streaming trades, candles, bid/ask and top-20 depth | WORKING adapter; operational scale still measured | Public WebSocket reconnect, heartbeat, native timestamps, gaps and stale checks. No full order-book reconstruction or guaranteed delivery. |
| Fast LONG and SHORT ignition | WORKING deterministic infrastructure | Frozen 5m resistance/support; intraminute attempt; completed 1m close plus configured buffer; later 5m acceptance and 15m structure. Supporting evidence changes quality. |
| Structure and multi-timeframe state | WORKING deterministic infrastructure | Six frames, confirmed pivots, sequence memory, BOS/CHoCH/MSS, sweeps, retests/failures and persistent history. Labels are measurable hypotheses. |
| Swing phases and conditional scenarios | WORKING deterministic infrastructure | Symmetric per-frame phases, separate setup/entry quality, actual trigger/invalidation/available targets and three conditional next states. No future OHLC or probabilities. |
| Fibonacci / Golden Pocket | WORKING deterministic infrastructure; edge UNVALIDATED | Each of six frames owns confirmed causal swing anchors; origin loss invalidates active levels. Contextual Fib quality contribution is explicitly uncalibrated and bounded; discovery/Fast weights remain zero. |
| VWAP / AVWAP / Volume Profile | WORKING within evidence scope | Existing anchors preserved, session/weekly VWAP where covered, POC/VAH/VAL/HVN/LVN and rolling profiles where history permits. Candle-derived profile is estimated, not true trade-at-price. |
| Liquidity price map | PARTIAL | Exact observed top-20 resting quotes plus separately labelled inferred structural levels. Disappearance is not proof of consumption/spoofing; actual stops/liquidation clusters are unavailable. |
| Stored Energy / Absorption / Trigger Sensitivity / Readiness | PARTIAL as validated quantitative models | Explainable deterministic components; original LONG directional formulas are not falsely mirrored into SHORT. Unavailable/UNKNOWN direction retains null values. No calibration or profitability claim. |
| Native order flow | WORKING within retained connection window | Aggregate taker flow and CVD since connection/gap; candle native taker fields where supplied. Not a full historical exchange CVD. |
| BTC/ETH dependency and relative strength | PARTIAL | Exact-time aligned returns, correlation and beta with adequate variance/history. Sector baskets and institutional cross-asset coverage remain unavailable where not supplied. |
| Personal watchlist | WORKING persistence | Add/remove/pin/notes survive restart; optional and unnecessary for broad discovery. |
| English Rookie/Pro live cockpit | WORKING source; final browser QA pending | Responsive navigation, six-frame chart, pan/zoom/crosshair, default own-frame Fib, conditional decisions, exact levels, calculators and real health. No production sample-data fallback. |
| Incremental chart updates | WORKING tested API/client | Initial native history once, then selected-frame native open/last-two closed bars, changed-analysis cursor and bounded real gap backfill. Other frame history is preserved. |
| Alerts / dedupe / outbox | WORKING deterministic infrastructure | Immutable events, cooldown/state transitions, timing and persisted notifications. Actual alerts require actual fresh evidence. |
| Telegram delivery | PARTIAL operationally | Independent configured adapter, retry/expiry/dedupe and actual provider acknowledgement. No bot/chat credential was supplied; no external message/handset receipt demonstrated. |
| Sessions | WORKING deterministic candle context | Actual OHLCV/VWAP/coverage and session structural events; IANA Tokyo/London/New York time zones and DST. Session patterns remain hypotheses. |
| Wallet / whale layer | PARTIAL operationally | Optional read-only Etherscan V2 integration with explicit chain/contract/asset mapping and confirmation checks. No supplied key/wallet configuration, no live wallet test, no guaranteed identity/intent. Production weight zero. |
| Market cycle / macro / analogues | PARTIAL research infrastructure | Point-in-time states, initial/revised release separation, causal matching, outcomes and independent workers. BLS calendar supplies schedules, not consensus or numeric release feed. |
| Journal / calculators / replay / misses | WORKING within declared simulation/research scope | LONG/SHORT fee-aware calculators, manual paper entries, coverage-aware outcomes, directional misses and chronological causal replay. Unknown funding/net results stay unknown. |
| SAND / BEAM / AIN matched point-in-time validation | NOT DEMONSTRATED | No genuine specified event checkpoints/cohorts, blind holdout or validated predictive edge is supplied. Never tuned to named winners. |
| Windows desktop bundle | PARTIAL | Real embedded-Python native installer build exists; final payload must be rebuilt after these source changes. Actual Windows installation/GUI/WebView2 remains unverified on Linux. |
| 24/7 endurance and full market predictive validation | NOT DEMONSTRATED | Short live tests and synthetic throughput are not multi-day reliability or investment performance. |
| Astrology / numerology | NOT IMPLEMENTED; zero influence | No production decision uses these hypotheses. |
| Live orders / withdrawal | NOT IMPLEMENTED | Observation, decision support and declared paper research only. |

## Preservation and data

`data/cockpit.sqlite3` is the explicit operational database. The old smoke database and historical evidence remain. Additive indexed SQLite/WAL migrations retain existing records; no production database was reset or copied over. Native broad snapshots are compressed at a minute cadence; raw tick arrays are not archived indefinitely. Full legacy archives need a separately agreed long-term retention/storage policy.

The source checkpoint and exact changed-file/hash inventory are in `reports/product-source-checkpoint.json` and `reports/product-source-changes.md`. This workspace has no Git repository, so there is no invented commit history.

## Keys and costs

Public Binance spot data requires no private trading key. CoinGecko plan/access limits may require its own data key. Optional Telegram and Etherscan require only their respective configured credentials; they are never hard-coded. Always-on hosting, disk/archive growth, bandwidth, optional paid provider plans and WebView2 download can create costs. No paid plan was purchased.

## Remaining acceptance work

Complete the final full suite, live 500-universe/API and mobile/tablet/desktop chart/reconnect checks, measure the new incremental path and rebuild/inspect the final Windows artifact. Independent out-of-sample validation, real external notification/wallet credentials and actual Windows-device verification remain separate evidence requirements.
