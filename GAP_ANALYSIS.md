# PRE-EXPANSION DISCOVERY ENGINE v2 — repository gap analysis

Inspection date: **2026-10-04 UTC**. Status: **review completed; v2 not implemented; named historical studies blocked by missing evidence**.

This review follows the requested implementation order: inspect the existing system, record its actual behavior, design an additive upgrade and present findings before major changes. No production code, settings, mappings, tests or market archives were changed. Proposed formulas and thresholds in the companion documents are research specifications, not established predictive results.

## 1. Findings that can be established now

The repository has useful data-integrity, technical-analysis, paper-risk and matched-control research components. It is not the requested market-wide discovery engine. The most consequential verified gaps are:

1. **Universe coverage stops at 500**, and the wired spot provider covers verified Binance USDT instruments, not every liquid asset in that universe.
2. **The decision path consumes 4h and daily structures**, despite collecting all seven configured timeframes. Existing microtimeframe pattern recognition is also disabled by a shared 21-calendar-day warmup that the configured short histories cannot satisfy.
3. **Every mapped asset receives expensive deep collection**, followed by a uniform full-scan sleep. There is no lightweight discovery layer or continuously monitored LOADED queue.
4. **Scores are fixed, largely binary structural rules**, with archetype associations. There are no cross-sectional percentiles, rank migration, graded Stored Energy/Trigger Sensitivity/Absorption scores or sector rotation.
5. **There is no discovery-state lifecycle, pre-move alert service or automated online miss/false-positive/lead-time ledger.** Existing snapshots and offline matched-control error statistics are useful foundations, not substitutes for these components.

These findings identify mechanisms by which movers could be missed. They do **not** prove which mechanism caused a particular SAND or BEAM miss.

Two corrections to assumptions are necessary. The explicit symbol map is **not an exclusive manual watchlist**: globally unique verified asset identities can be resolved automatically. Also, the current engine does **not literally require passing a QNT/ZEC/TAO/ONDO classifier before evaluation**. Archetype labels are associated with scoring rules; conservative COMPRESSION/ACCUMULATION paper decisions already exist. The limitation is fixed structural scoring and slow decision/monitoring paths, not a token-name admission rule.

## 2. Inspection scope and existing architecture

Reviewed the complete application/package structure, provider implementations, settings, examples, existing documentation, CI, tests and local data/report artifacts. Source references below are relative to `src/expansion_hunter/` unless another path is stated.

| Component | Current behavior | Evidence |
| --- | --- | --- |
| CLI/runtime | `scan`, uniform `watch`, replay, research and read-only API; fixed CoinGecko/Binance provider construction | `cli.py:43–112,197–222`; `runtime.py:11–24`; `api.py:44–73` |
| Universe | Current market-cap-ranked CoinGecko assets, capped at 500; missing/invalid rank rejected | `config.py:7`; `providers/coingecko.py:54–104` |
| Identity | Explicit ID/instrument mappings plus verified globally unique ticker resolution; ambiguous identities fail explicitly | `providers/binance.py:69–162`; `config/symbols.json` |
| Collection | Metadata, order-book/ticker liquidity, derivatives and all seven OHLCV histories for every mapped asset | `scanner.py:39–102,188–206` |
| Technical engine | Indicators, confirmed swings/ranges, price-volume proxies, AVWAP anchors and one approximate volume profile | `technical.py`; `avwap.py` |
| Macro regime | BTC/ETH structure, seven-day returns, capitalization estimates, breadth, derivatives coverage and shakeout restrictions | `regime.py`; `scanner.py:104–142,226–249` |
| Decision engine | Nineteen weighted structural/adoption observations, capped independent hybrid bonus, extension/leverage penalties | `rules.py:138–436` |
| Paper risk | Freshness/identity/liquidity/account checks, position sizing, costs, stop/target/RR, fail-closed entries | `risk.py`; `scanner.py:266–329` |
| Publication | SQLite run statuses and compressed immutable asset/global observations; only completed runs exposed | `storage.py:25–140` |
| Research | Causal replay, archived cohort attestations, outcome-blind matching, censored outcomes and a chronological holdout comparison | `replay.py`; `research.py`; `docs/MATCHED_RESEARCH.md` |

The live sequence is: universe and identity resolution → full collection → analyze all timeframes → derive BTC/ETH/breadth regime → 4h/day rules → optional entry-candidate refresh → persist evaluated snapshots → sort by Expansion Score/confidence. Collection retains an explicit information boundary; source event time and receipt/availability time are separate.

Default settings request seven frames with **400 bars each**, concurrency 6, `top_n=500`, minimum 24h quote volume $5m, spread limit 30 bps and minimum configured depth $50k. Liquidity thresholds constrain paper entries, rather than forming a broad discovery-quality admission score. There is no exchange order router.

## 3. Historical evidence inventory

| Artifact inspected | Actual contents | What it can establish |
| --- | --- | --- |
| `data/smoke.sqlite3` | Tables `runs`, `observations`, `global_observations`; two runs, only two asset observations | Local smoke operation and recorded data provenance |
| Failed run `57719a40-fed6-4629-b715-e4f810c052a5` | Started 2026-10-03 07:32:02.798811 UTC; Binance exchange-info GET rejected | A recorded provider failure, not a SAND/BEAM miss |
| Completed run `94badbed-0a4f-476d-839b-eb2f2378287d` | Started 2026-10-03 07:36:39.240071 UTC; requested 5 assets, evaluated 2; BTC/ETH observations as of 07:36:49.806569 UTC | Coverage for that five-asset smoke attempt only |
| `reports/live-smoke.json` | BTC/ETH results; explicit tether exclusion and BNB/XRP mapping issues; provider issues including blocked futures access | Recorded coverage/errors, not historical pre-expansion ranks |
| Archived daily candles | 400 BTC/ETH bars starting 2025-08-29; first availability to this installation on 2026-10-03 | Historical prices retrieved at the smoke boundary; not proof of earlier system knowledge |
| `examples/study.template.json` | Empty `cohorts` and `cases` | No executable historical event study |
| Existing tests | Synthetic technical/provider/risk/pipeline/research fixtures | Software behavior; no real named-event performance evidence |

There are **no local SAND or BEAM observations, event manifests, historical universe/sector snapshots, original alert records or real matched-control studies**. A source search found no named regression case. No event date or unambiguous BEAM identity was supplied in the request.

The smoke report predates subsequent identity-provider fixes and explicit BNB/XRP mappings already present in the current code. Its old mapping failures must not be presented as necessarily reproducible defects of the current implementation.

The database was inspected using SQLite read-only immutable mode, without initializing `Store` or modifying the archive. To reproduce its basic inventory:

```python
from pathlib import Path
import sqlite3

path = Path("data/smoke.sqlite3").resolve()
db = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
print(db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall())
for table in ("runs", "observations", "global_observations"):
    print(table, db.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
db.close()
```

A read-only recheck after the repeated upgrade request found the same artifacts and no changes to the 45 original files recorded at the first review. The machine-readable [review evidence report](reports/v2-review-evidence.json) records current archive counts, identities, source/document hashes and the unresolved case-study prerequisites. It proves the repository evidence inventory and code references; it does not supply missing historical SAND/BEAM market observations. Existing test results are explicitly attributed to the previous review, rather than presented as a fresh run.

Downloading old OHLCV today does not establish its historical `available_at`. Independently documented original release times can support a causal reconstruction; a disclosed latency model can support a sensitivity study. Neither may be relabeled as exact replay of unrecorded live scans.

## 4. Explicit SAND and BEAM answers

| Required question | SAND | BEAM |
| --- | --- | --- |
| Why did the existing system miss it? | **Not determinable from supplied/local evidence.** No event or original scan record is available. | **Not determinable from supplied/local evidence.** Event, token identity and original scan record are unavailable. |
| Could it have been detected using only information available before expansion? | **Untested.** Requires causal historical inputs, full contemporaneous universe and frozen event definition. | **Untested.** Same requirements, plus gaming/privacy identity and historical ticker/migration resolution. |
| Which exact measured features raised its ranking? | **No measured case values exist here.** Candidate variables are specified below, not attributed to SAND. | **No measured case values exist here.** Candidate variables are specified below, not attributed to BEAM. |
| Would those variables create excessive false positives? | **Unknown.** Requires outcome-blind controls, universe base rates and out-of-sample alert-budget tests. | **Unknown.** The same validation is required; a second winner does not establish discrimination. |
| Can the requested proof be produced from this repository now? | **No: blocked by missing case/data lineage.** | **No: blocked by missing case/data lineage and identity.** |

The request's report of missed movers is retained as an issue to investigate. It is not converted into a fabricated causal finding. In particular, absence from `config/symbols.json` proves neither asset was excluded: automatic mapping may have admitted it.

### Miss-attribution hypotheses and evidence needed

| Failure category | Verified mechanism in current code | Evidence required to establish it caused an actual named miss |
| --- | --- | --- |
| Universe exclusion | Hard 500 cap; valid market-cap rank required | Archived membership/rank, configuration and scan dispositions at the event |
| Venue/identity exclusion | Only verified Binance USDT spot instruments wired; ticker collision/migration/identity failures are explicit | Historical instrument listings, identity responses/mappings and recorded rejection reason |
| Missing data/history | Primary evaluation requires 220 usable daily and 4h bars; stale/gapped data fails closed | Original candle availability/gaps, instrument listing history and missing-feature records |
| Missing features | No cross-sectional rank migration, graded absorption, sector rotation or short-window spot-before-futures signal | Pre-event measurable feature paths and same-time control distributions |
| Structural threshold | Multiweek bounded-base definitions constrain compression and ignition | Causal v1 intermediate values/failed rules and broad validation of a frozen alternative |
| Weighting | Fixed rule weights and binary thresholds lack demonstrated empirical calibration | Actual score decomposition and walk-forward incremental discrimination |
| Scan frequency | Full deep scan, then sleep; no per-stage monitoring queue | Original scan/poll timestamps, queue/rate-limit latency and event onset |
| Alert delivery | No dedicated RADAR/WATCH/LOADED alert service in this repository | External system/version identification if an alert was expected, plus actual emission/delivery logs |

Keep original missed-event attribution, deterministic v1 reconstruction and proposed v2 reconstruction separate. A reconstructed stage is not an alert that was historically sent.

## 5. Exact scanner limitations

### 5.1 Short-timeframe data is collected but cannot drive early decisions

`rules.py:51–87,106–108` uses usable 4h and daily data for stage/scoring. IGNITION requires 4h spot demand, RVOL ≥1.5, extension ≤2 ATR and a nearby bounded multiweek base. BREAKOUT requires two completed 4h closes above a frozen level plus 0.1 ATR. These are useful confirmation safeguards but do not implement fast pre-move discovery.

`technical.py:503–511` returns before structural patterns unless history has both ≥220 bars and ≥21 actual calendar days. With 400 configured bars:

| Frame | Elapsed history | Meets 21-calendar-day structural warmup? |
| --- | --- | --- |
| 1m | 6h 40m | No |
| 5m | 33h 20m | No |
| 15m | 100h | No |
| 1h | 16d 16h | No |
| 4h | 66d 16h | Yes, if otherwise valid |
| 1D | 400d | Yes, if otherwise valid |

Even the allowed 1,000-bar maximum cannot provide 21 days for 1m/5m/15m. Indicators/levels/anchors computed earlier can still exist; the issue is structural booleans and their downstream decision use. V2 needs feature-specific/timeframe-specific warmup and quality coverage, while retaining causal confirmation.

### 5.2 Stored-energy evidence is binary and narrowly conditioned

`technical.py:125–147` defines a base using ≥21 days, limited endpoint displacement and ≤35% high/low width over an approximately 60-day search. Compression uses low BB-width/realized-volatility percentiles, a five-EMA cluster and a bounded range (`technical.py:527–538`). Absorption and shallow-pullback proxies exist, but no graded rejection-decay/time-to-return study, upper-range residence score or causal awakening trajectory.

The 19 rules total 95 points: size 4; base 8; turnover 4; volume contraction 5; volatility 8; EMA cluster 4; RSI 3; resistance tests 6; higher lows 5; shallower pullbacks 4; near resistance 6; BTC RS 6; absorption 8; AVWAP 4; ignition 5; quantitative adoption 5; scarcity 3; RWA adoption 4; institutional inflows 3. The independent hybrid bonus is capped at 5. Extension, weak advance and leverage warnings reduce scores (`rules.py:391–422`). This already discourages chasing; it does not rank statistical unusualness across the universe.

`confidence` measures evidence coverage (`rules.py:427–436`), **not expansion probability**. It must not be relabeled as probability.

### 5.3 Full-scan scheduling is incompatible with equal one-minute coverage

`cli.py:53–62,78` runs a complete scan and then sleeps, default 300 seconds. Revisit time equals collection/analysis duration **plus** that interval. `scanner.py:266–329` refreshes a limited selection of IGNITION/BREAKOUT/RETEST candidates only when a portfolio is supplied; this protects paper-entry freshness, not WATCH/LOADED monitoring.

The existing limiter budgets 20 Binance request-weight units/second (`providers/http.py:54–56,96–104`). A 1,000-level depth request costs 50 weight and a 24h ticker costs 2 (`providers/binance.py:254–255`); seven one-page histories add 14. For an illustrative 500 mapped assets, depth alone consumes roughly 1,250 seconds of limiter budget; the 66-weight spot collection is roughly 1,650 seconds before extra pages/retries/network/CPU. This is a code-based planning estimate, **not a measured 500-asset benchmark**. Just raising the universe cap would worsen the problem.

Broad discovery should use cached/incremental/batched lightweight observations and reserve deep histories/books for promoted candidates. A measurable request budget, freshness SLA, backpressure and starvation policy are required; configuration cannot promise unavailable provider capacity.

### 5.4 Ranking, states and audit are not discovery lifecycles

`Asset.rank` is market-cap rank, not discovery rank (`models.py:117–123`). Results are sorted by score, confidence and asset ID (`scanner.py:357–359`), without percentiles, historical rank velocity or stable-cohort comparison.

Current stages are INSUFFICIENT_DATA, COMPRESSION, ACCUMULATION, IGNITION, BREAKOUT, RETEST, EXPANSION and NONE (`models.py:245–253`). They are snapshot classifications, not persisted transition episodes. Trade action WATCH is a different concept, not the requested asset-state WATCH.

Legacy EXPANSION is assigned by the overextension/≥3-ATR guard **before** breakout-acceptance checks (`rules.py:55–57`). It is an anti-chase classification, not proof of a confirmed expansion episode. Preserve that meaning; never migrate it into a historical v2 CONFIRMED_EXPANSION success.

Storage retains successful evaluated snapshots/results and top-level errors for excluded/failed assets (`storage.py:25–65`; `scanner.py:252–264,334–363`). There is no normalized per-requested-asset disposition/coverage row, feature/config lineage, promotion reason, transition history, alert/outcome episode or rank cohort. Preserve immutable completed-run publication and add comprehensive audit tables rather than overwriting archives.

## 6. Requirement coverage and additive scope

| Requested v2 capability | Current status | Additive upgrade needed |
| --- | --- | --- |
| Top-1000/meaningful-liquidity universe, redundancy | Missing; modular protocols exist, wired universe capped at 500 | PIT/versioned universe/identity registry, venue capabilities, explicit partial coverage and source disagreements |
| Lightweight discovery → dynamic deep watch | Missing | Broad feature cache, anomaly admission, configurable capacity tiers and priority scheduler |
| Historical + cross-sectional percentiles, rank velocity/acceleration | Missing, except individual technical historical percentiles | Frozen cohort ranks, tie/missing rules, elapsed-time common-cohort migration |
| Stored Energy, Trigger Sensitivity, Absorption, Range Pressure | Partial constituent proxies only | Continuous causal features and coverage-aware component scores; validate weights |
| Volume/turnover/CVD acceleration and spot-before-futures | Native volume available; matched 24h spot/futures ratio exists | Comparable 1m/5m/15m flows; reliable trade-side capability gates; proxy labels |
| Rolling six-horizon volume profiles and POC migration | One explicitly approximate range profile exists | Causal horizon profiles, stable bins, provenance-aware migration/overhead proxies |
| Daily/weekly/session VWAP; AVWAP upgrade | Useful AVWAP anchors/reclaims/support already exist | Calendar VWAP variants, failed-break anchors, confluence score and causal microtime acceptance |
| RS vs BTC/ETH/TOTAL3/sector/universe; rotation | Seven-day daily BTC comparison only | Aligned multiwindow RS/acceleration, PIT sector baskets/breadth and dynamic rotation |
| BTC/regime conditioning and liquidity quality | Useful conservative regime and entry gates; incomplete sources explicit | Discovery-quality score, live macro freshness, disagreement/coverage warnings and broader benchmarks |
| Catalyst sensitivity separate from setup/news | Verified quantitative adoption rules exist | Structural sensitivity independent of news; timestamped adoption/catalysts used only after discovery |
| Independent multiframe states + asset state | Missing lifecycle; 4h primary stage only | Explicit transitions, expiration/invalidation/failure, hysteresis and per-frame readiness |
| Before-move RADAR/WATCH/LOADED/IGNITION alerts | Missing dedicated service | Objective payloads, thresholds, dedup, delivery audit and exact trigger/confirmation/invalidation |
| Dynamic sampling/event-driven early warning | Missing | State priority, fast microtime closed bars/provisional stream distinction, budgets and latency audit |
| Calibrated probabilities | Missing; existing confidence is coverage | Matured labels, fold-specific calibration/Brier/reliability/PR-AUC; withhold unsupported outputs |
| Miss/false-positive/daily recall/lead-time evaluation | Offline controls/error statistics exist; online ledger absent | Objective events, matured alert episodes, censored paths, fixed recall denominators and original lead time |
| SAND/BEAM, controls, winner/failure library | Framework partial; actual data missing | Verified manifests, identical causal checkpoints, outcome-blind controls and broad event discovery |
| Feature selection and anti-overfitting | Fixed scales and one holdout comparison exist | Purged rolling walk-forward, fold-local transformations, multiple-testing controls and untouched holdouts |
| Complete audit and no silent failures | Batch errors/provider issues present; per-excluded evidence incomplete | Every requested asset accounted for with status, missing reason and immutable source/config lineage |
| Score/state/miss/FP/PIT integration tests | 201 v1 fixture tests | Preserve v1 suite; add meaningful v2 contract, lifecycle, fault/load and real-source validation |

## 7. Measurable hypotheses for future ranking research

These variables could raise an **unknown** asset before expansion if its actual pre-event values differ from its history/peers. No row asserts a measured SAND/BEAM property. Full definitions, units, causal windows and missing-data behavior are in `FEATURE_DEFINITIONS.md`.

| Candidate measurement | Discovery hypothesis | Main false-positive/confounding risk |
| --- | --- | --- |
| Own-history ATR/price, BB width, realized-volatility and candle-range percentiles | Low volatility with unusually persistent compression implies stored energy | Dormant illiquid assets, normal low-volatility drift |
| Settled fixed-zone rejection depth/return-time decay, rising intervening lows | Supply response weakens as resistance is repeatedly tested | No eventual demand, changing resistance zones, unfinished rejection episodes |
| Upper-quartile residence, midpoint retention, close-location trend | Price increasingly accepts upper range levels | Extended trends or range estimation artifacts |
| Matched-window RS and RS slope/acceleration vs BTC/ETH/sector/universe | Strength/fast recovery appears before broad repricing | Sector-wide beta, quote effects, asynchronous observations |
| RVOL, baseline spot-volume slope and acceleration with limited price displacement | Demand is awakening before a vertical candle | Wash volume, news spikes, leverage-only activity |
| Turnover contraction then acceleration, with usable cap/supply evidence | Quiet float begins receiving disproportionately stronger demand | Stale denominator, genuine lack of interest, unreliable reported volume |
| Accepted AVWAP reclaim/support and confluence under resistance | Higher levels hold while structure tightens | Intrabar wick false reclaims, anchors chosen after the outcome |
| Fixed-grid POC migration, VAH pressure and low estimated overhead profile mass | Acceptance rises and overhead traded-volume resistance may be limited | OHLCV allocation artifacts; past traded volume is not current resting supply |
| Comparable-cohort rank velocity and acceleration | An asset merits early attention even below an absolute threshold | Membership churn, source changes and uneven scan age |
| Native book depth/imbalance and reliable signed-flow divergence | New spot demand could move price with limited opposing depth | Spoofing, stale books, high spread/slippage and invalid microcap liquidity |

Treat thin overhead as potential responsiveness **only after liquidity quality**, not permission to promote an untradeable microcap. Avoid counting correlated volatility/range variables as independent confirmations. Controls must include non-expanding structures and other winners selected before outcomes are read.

No false-positive claim is possible from two selected winners. Evaluate precision/recall/PR-AUC at a frozen alert budget, false-positive and false-discovery rates, lead-time distributions, coverage and regime stability on later folds. Execution EV/drawdown requires a separate costs/latency/entry-exit model.

## 8. Preserve, extend and verify

Preserve provider protocols; explicit unavailable/stale values; timezone-aware event/availability times; candle/unit validation; native quote/taker-buy volumes; identity collision protections; causal replay and confirmed AVWAP discovery/acceptance; existing indicators and honestly labeled profile estimates; immutable completed-run SQLite publication; per-asset fault isolation; read-only API/auth; and fail-closed paper-risk/account/anti-chase rules. Discovery eligibility and promotion must be independent of the stricter paper-entry permission path.

Preserve outcome-blind control matching, missing-path censoring, overlap/late-label safeguards and the v1 research interface. Extend through a separate versioned microtime/cohort contract; the current research schema accepts only 1D/4h future paths and aligned UTC boundaries, with at most 500 members (`research.py:51–103`). It cannot simply be reused for exact T-15m studies or a top-1000 cohort.

Verification rerun on 2026-10-04:

- Existing suite: **201 passed in 15.38 seconds** with bytecode/cache writes disabled.
- Ruff: **all checks passed**, no fixes applied.
- Preservation check: SHA-256 comparison of **45 existing source/config/test/documentation/data/report artifacts found zero changed or missing files**.
- Real SAND/BEAM replay, control-group study, historical libraries, calibrated probabilities and top-1000 live reliability: **not run / missing required evidence**.
- Existing application/config/test/data artifacts are retained; companion files are design/review documents only.

## 9. Review deliverables and empirical gate

Read `ARCHITECTURE_V2.md` for the additive discovery/monitoring/storage design, `FEATURE_DEFINITIONS.md` for mathematical contracts and `REGRESSION_TEST_PLAN.md` for named cases, controls, walk-forward and acceptance evidence.

Before historical conclusions can be established, obtain each requested event's UTC date/interval, exact exchange/pair and immutable asset identity (especially BEAM), independently supported pre-event data/universe metadata and original scan/alert records where actual miss attribution is required. Without original scan history, a valid causal reconstruction can still be reported separately; actual historical alert lead time remains unknown.

The review identifies concrete upgrade targets. **It does not claim the v2 acceptance criteria have passed, either named mover would have been caught, or any asset is guaranteed to expand.** No major architectural change has been made.
