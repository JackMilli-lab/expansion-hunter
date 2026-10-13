# War Room audit — implementation baseline

This audit describes executable code before the next additive upgrade. It does not
turn earlier prompts into implemented features. Full baseline: **860 tests passed
in 91.67 seconds** (`reports/war-room-baseline-tests.txt`). Ruff passed across
source, tests, scripts and benchmarks; Python compilation passed. No static type
checker is configured. File hashes are in `reports/war-room-baseline.json`.

| Component | Classification | Evidence and reuse |
| --- | --- | --- |
| Native public Binance WebSocket ingestion | WORKING | `providers/binance_ws.py`, `live_feed.py`, native aggregate trades, BBO, top-20 partial depth and candles; actual public smoke reports. Reuse the adapter and causal cache. |
| Live runtime at promoted-universe load | BROKEN / NEEDS ATTENTION | `live_runtime.py` is integrated, but the latest automatic empty-watchlist smoke timed out. Synchronous snapshots/SQLite publication and unbounded chart analysis cause delays. Single-asset success does not prove reliable multi-asset operation. |
| Broad discovery and automatic promotion | PARTIAL | `discovery.py`, ranking, scheduler and stored promotion history work. Configured target is 500; last real run discovered 491, lightly scanned 122 and deeply scanned 10. Provider coverage is visibly incomplete. No manual watchlist is required. |
| Cross-sectional rank and rank migration | WORKING | `discovery_ranking.py` compares compatible native cohorts, keeps historical ranks, velocity and acceleration; unit/integration tests. No optimized predictive edge has been demonstrated. |
| Operational SQLite history | WORKING | Existing observations, alerts, states and summaries are persisted in the configured cockpit database; original smoke evidence is preserved. |
| Archive storage efficiency | BROKEN / NEEDS ATTENTION | Read-only profiling found approximately 3.9 GB duplicated compressed snapshot payloads across cycles and observations. Introduce forward-compatible deduplication without deleting historical records. |
| Personal watchlist | WORKING | Persistent CRUD, notes, pinning and priority monitoring in `intelligence_store.py` and API. Membership does not increase scores. |
| Fast 5m/1m/5m/15m state machine | WORKING | `fast.py`: resistance is predefined, intraminute crossing is an attempt, a completed 1m close determines confirmation, supporting evidence determines quality. Sweep/failure/dedupe/restart tests exist. Preserve this contract. |
| Stateful structure and six-timeframe context | WORKING | `war_room.py` calculates confirmed pivots, HH/HL/LH/LL, BOS/CHoCH/MSS, sweeps/reclaims/failures/retests and alignment; runtime persists chronological transitions. Labels remain deterministic hypotheses. |
| Named geometric chart patterns and standard setup library | MISSING | Existing state detection is not a complete triangle/flag/H&S/wedge/rounded-pattern engine. Cycle archetypes are a separate research module. |
| Conditional expected paths and scenarios | WORKING | `war_room.py` uses currently known levels and produces primary/alternative/failure paths. Missing targets remain unavailable. No forecast probability is supplied. |
| Fibonacci and golden confluence | PARTIAL | Confirmed point-in-time anchors, levels and confluence are integrated through `live_analysis.py`. Predictive weighting is zero; no completed placebo/holdout validation demonstrates an edge. |
| Liquidity price map | PARTIAL | Native observed partial-book levels and structural levels are separate. Candle-derived profiles are estimates. An order disappearing is not evidence that it was consumed. Actual stop orders, whale identity and liquidation clusters are unavailable. |
| VWAP / AVWAP / Volume Profile | WORKING | Existing tested indicator and anchor code is reused. Session/weekly coverage limits and candle-profile estimates are explicit. |
| Stored Energy / Absorption / Trigger Sensitivity / Readiness | WORKING | Deterministic component scores and evidence coverage exist in `discovery_features.py`. They are uncalibrated research scores, not probabilities. |
| Multi-window ETH/sector strength and BTC beta | MISSING | Existing BTC/market discovery excess return is narrower; full rolling correlation, beta and upside/downside beta are not implemented. |
| Native order flow | PARTIAL | Aggregate-trade direction/delta/CVD exist with feed-gap guards. Futures flow and identified on-chain institutional wallets are not connected. A large exchange trade is not proof of a whale. |
| Explicit three-candle forward model | MISSING | Conditional next structure steps exist, but there is no validated future OHLC forecasting model. Do not fabricate three future candles. |
| Session context | PARTIAL | UTC session VWAP exists; an explicit session/timezone context presentation is missing. |
| Persistent alerts and latency | PARTIAL | State events and browser/local delivery exist. The runtime latency profiler currently excludes slow publication work. External mobile delivery is not verified. |
| Trade/risk/target/portfolio calculators | WORKING | LONG/SHORT and fee-aware sizing tests pass; liquidation is estimated or unavailable. Portfolio inputs are declared assumptions. No orders are sent. |
| Actual paper trade journal and realized analytics | MISSING | Signal outcome learning exists; it is not a persistent paper-position engine. Win rate, realized R and paper drawdown must not be fabricated. |
| Historical research, analogues and replay | PARTIAL | Point-in-time matching, outcome maturation, censoring and deterministic replay are implemented. SAND/BEAM/AIN matched-control studies and statistical calibration are not completed. |
| Compact browser cockpit | WORKING | Native six-timeframe candles, exact levels, watchlist, alerts, structure, scenarios and calculator render; desktop/mobile smoke evidence exists. All-asset automatic smoke still failed. |
| Central Dutch i18n | MISSING | Much of the existing cockpit is Dutch, but strings and internal event names are not centrally translated. |
| Native Windows launcher source | PARTIAL | Desktop/backend and Win32 extraction/build source plus tests exist. No executable Setup.exe or dependency payload has been built; no real Windows execution has been verified. |
| Legacy scanner/risk/API | WORKING | Retain existing scanner and risk interfaces. The in-memory chart cache is operational state, not a replacement for immutable event history. |
| Old build output | DUPLICATE | `build/lib` and the old distribution wheel are generated snapshots; reuse authoritative `src/` for new packaging. Do not delete user work. |
| Planned interfaces without runtime connection | UNUSED | Proposed unimplemented features in old documents are requirements, not operational modules. Experimental features must remain disconnected from production scoring. |

## Changes justified by the audit

1. Bound and isolate chart computation and SQLite publication; measure the whole
   processing path and expose backlog/failure. Preserve completed-candle priority.
2. Add immutable content-addressed snapshot storage for new records; hydrate old
   and new formats through existing readers. Keep old databases and exports.
3. Add causal pattern/setup modules, relative context and a paper journal through
   existing `war_room`, storage and API interfaces.
4. Integrate Dutch translations, compact decision output, honest flow/session
   context, conditional next steps and paper controls into the existing cockpit.
5. Build the real Windows payload/executable if official dependency downloads
   succeed. A cross-built executable is not Windows runtime verification.
6. Repeat full regressions, actual public-data and automatic-promotion smoke tests
   and storage/latency measurements. Report remaining restrictions explicitly.

No live order execution exists or is authorized. Observation mode remains the
default. Research data shortages are technical limits, not permission to invent
historical analogues, calibrated probabilities or profitable results.
