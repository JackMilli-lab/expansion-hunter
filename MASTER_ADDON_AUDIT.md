# Master add-on: Phase A baseline audit

Audit boundary: **2026-10-05 UTC**, before the market-cycle, macro and historical-analogue additions. This is a snapshot of inspected executable code, not a claim that every previous specification has been delivered. Work added after this boundary must be verified separately.

The repository has a preserved v1 scanner **and newly implemented discovery infrastructure**. It does **not** yet have the requested continuously streaming Fast Ignition Monitor, personal watchlist, live cockpit, macro calendar or historical-analogue service at this boundary. `PROJECT_STATUS.md` describes an earlier October 4 audit; its statements that discovery ranking and the new scores are absent are now outdated.

## Baseline verification

- Root agent ran the complete existing suite: **312 passed in 69.48 seconds**. This includes the original baseline and added discovery tests.
- Full Ruff inspection initially found two import-order errors in `benchmarks/phase1_benchmark.py` and `tests/test_discovery_pipeline.py`. Root corrected them and reported the rerun passed.
- No static type checker is configured in `pyproject.toml`; no type-check pass is claimed.
- No `.git` repository is available here, so a Git commit/checkpoint is unavailable. File hashes in `reports/master-addon-baseline.json` provide a local audit snapshot.
- This audit performed no external market-data requests and no notifications or orders. Tests use explicit synthetic fixtures unless an existing recorded artifact is cited.

## What executable code already does

| Component | Verified status and scope | Reusable interface |
| --- | --- | --- |
| Original scanner | ✅ Tested public-data collection, technical reports, market-regime assessment and advisory risk calculations; real 500-asset operational coverage is unproved | `Scanner.scan`, `AssetSnapshot`, `ScanBatch` |
| Broad/deep discovery | ✅ Tested two-tier orchestration, bounded deep promotion, cache reuse, per-asset dispositions and immutable cycle publication | `DiscoveryEngine.run_once`, `DiscoverySettings`, `DiscoveryCycle` |
| Universe capacity | 🟡 Configuration/provider ceiling raised to 10,000; discovery configuration requests 1,000, legacy configuration 500. Synthetic tests exercise 1,000; real coverage has not been demonstrated | `MarketProvider.universe`, `BinanceSpotProvider.resolve_assets` |
| Cross-sectional ranking | ✅ Tested aligned cohorts, self-history percentiles, rank horizons and comparable-cohort migration; scores are uncalibrated | `rank_observations`, `RankHistory`, `dark_horses` |
| Structural scores | ✅ Deterministic component-auditable Stored Energy, Absorption, Trigger Sensitivity, Expansion Readiness, Volume Awakening and VWAP Structure calculations exist | `light_features`, `deep_scores`, `DiscoveryScore` |
| Dynamic discovery stages | ✅ DORMANT/RADAR/WATCH/PRE_IGNITION/LOADED/IGNITION/INVALIDATED transitions, freshness gates and bounded scheduling are tested | `transition`, `DiscoveryScheduler` |
| Data quality | ✅ Features carry source/retrieval timestamps, age, provider, timeframe and freshness. Missing values remain null; stale critical evidence suppresses high-priority promotions | `DiscoveryFeature`, `Freshness`, `Metric.usable` |
| Indicators / AVWAP | ✅ Existing indicators and causal AVWAP anchor/reclaim implementation preserved | `analyze`, `confirmed_swings`, `anchored_vwap` |
| Volume Profile | 🟡 POC/VAH/VAL/HVN/LVN are candle-derived estimates; not actual resting orders, nor a complete rolling-profile migration system | `technical._volume_profile` |
| SQLite archive | ✅ WAL, original scan tables, compressed discovery cycles/observations, transitions, local events and monitoring state | `Store`, `DiscoveryStore` |
| Safe archive import | ✅ Idempotent conflict-detecting `import_legacy` exists; source database is opened read-only. Implementation is not evidence an import has already been run | `DiscoveryStore.import_legacy` |
| CLI/API | ✅ Legacy operations preserved; discover/watch/status/events/import commands and read-only discovery endpoints exist | `discovery_runtime`, `create_app` |
| Local discovery alerts | 🟡 Persisted stage-change events are implemented; no browser/mobile/external delivery or fast breakout lifecycle exists at this boundary | `DiscoveryEvent`, `DiscoveryStore.events` |
| Matched-control research | 🟡 Generic point-in-time feature matching, outcome calculation, censoring and optional train/holdout comparison exist; no actual verified named event cohort is provided | `StudyDataset`, `run_study`, `point_in_time` |
| Paper/live trading | ❌ No execution engine or paper-position accounting; existing risk functions produce advisory plans only. Live trading remains absent | `risk.py` calculations remain reusable |

The original scanner and new discovery engine are separate collection paths. Discovery uses cheap broad histories/native bulk market data, then selected deep histories/books/derivatives. Its deep work still occurs inside `run_once`; there is no independent streaming fast path at this baseline.

## Requested capabilities absent at the audit boundary

No executable implementation was found for public WebSocket ingestion/reconnect/heartbeat, event-driven 1m breakout confirmation, post-break 5m acceptance, 15m confirmation, persistent personal watchlists, exact live liquidity objects and wall consumption, Fibonacci/Golden Ratio research, browser charts/dashboard, notification delivery, operational readiness, macro providers/releases/revisions/reactions, dedicated market-cycle intelligence, historical analogues, cycle-pattern archetypes, or isolated astrology/numerology experiments.

The existing `IGNITION` stage is a micro-timeframe volume-awakening heuristic. It does **not** satisfy the requested Fast Ignition confirmation rule: a completed 1m close above a previously discovered 5m resistance plus a buffer.

## Current adapters and preserved data

`runtime.py` constructs CoinGecko, Binance public spot and Binance USD-M adapters through the existing asynchronous HTTP client. This client makes public GET requests, provides rate-limit diagnostics and bounded retries. Spot native-host fallback is additive; it does not replace provider identity checking or evade rate-limit responses.

- `config/settings.json`: legacy universe 500; `https://data-api.binance.vision` primary spot host; `https://api.binance.com` fallback; operational database `data/cockpit.sqlite3`.
- `config/discovery.json`: discovery universe 1,000; maximum 10 deep assets per cycle; 50-member deep-watch shortlist; configured 300-second broad interval and 15-second polling interval.
- `config/smoke.json` now points to the operational database. The original smoke archive remains separate evidence.
- At read-only inspection, **only `data/smoke.sqlite3` exists**: 266,240 bytes; two legacy runs, two asset observations and one global observation. SHA-256: `639ecb892df59a66af96df330fa1d6beefb261373d1a95aae18e44197185ddf6`.
- Original real-source evidence is dated October 3: usable BTC/ETH data in a five-asset request, default Binance/futures host restrictions. It does not establish present live connectivity, 500/1,000 coverage or 24/7 operation.
- No verified SAND/BEAM/AIN/AKT historical cohort or matched controls are supplied. The empty study template and synthetic tests must not be presented as real named regressions.

## Issues identified before additions

1. **Discovery market regime lacks primary structure in its current call.** `discovery.py` invokes `classify_regime` with empty BTC/ETH technical maps. `regime.py` therefore marks primary BTC evidence incomplete and returns low-coverage NEUTRAL with zero risk multiplier. A nonzero coverage field can subsequently map to neutral readiness credit; this must be corrected or explicitly unknown. The original scanner correctly constructs technical maps.
2. **Missing market-cap rank still excludes at the provider universe boundary.** `CoinGeckoProvider.universe` skips invalid/missing `market_cap_rank`; there is no numeric market-cap gate later in DiscoveryEngine. Missing-cap universal discovery therefore requires a separate verified liquidity-based admission path; the existing ranked universe alone does not meet that requirement.
3. **Cold broad initialization still requires one lightweight history request per supported asset.** Bulk prices/books are cheap, but bootstrap latency and provider budgets at 500/1,000 require measurements. Subsequent unchanged histories are cached; synthetic timing is not exchange timing.
4. **Discovery and selected deep evaluation share a cycle.** Adding macro, analogues or streaming must not turn that cycle into the required 1m hot path. Independent ingestion/evaluation workers are needed to avoid a slow universe scan delaying a breakout.
5. **Health is process liveness only.** `/health` currently returns static OK; it proves neither a connected feed nor fresh observations. Discovery status reports archived freshness and report age, which is useful but not a live readiness assessment.
6. **Configuration does not itself make a service 24/7.** Discover-watch has bounded error backoff, but no deployment supervisor or measured reconnect/endurance evidence exists at this baseline.
7. **History provenance remains essential.** Candle closure, retrieval times, confirmed swings and AVWAP timestamps have safeguards; supplied historical provenance still needs independent validation. Newly downloaded old candles cannot be backdated into original knowledge.
8. **Storage/CPU growth needs measurement.** Full source snapshots are archived in compressed cycles and per-asset rows. Thin rank-history queries reduce reconstruction memory, but do not establish a retention policy or sustainable sustained-write rate.
9. **No predictive validity established.** Deterministic weights and new cycle/macro/archetype labels remain hypotheses. Software test passes do not prove recall, lead time, false-positive control or profitability.

These observations were sent to the implementing root agent. Later corrections must be verified with tests rather than silently rewriting the baseline finding.

## Safe integration boundaries

Use existing `Model`, `Metric`, `AssetSnapshot`, aware timestamps, provider protocols and immutable SQLite publication patterns. Preserve original tables, API response contracts, AVWAP, no-lookahead checks and advisory risk functions. Add cycle/macro/analog entities independently; keep initial macro releases and later revisions separate, and preserve publication timestamps in every point-in-time query.

Market context can enrich a signal explanation, but must not delay or redefine an objectively completed 1m breakout close. Analogue matching and research outcomes belong outside production feature generation. Personal monitoring priority must not add score credit. No private exchange credentials or order endpoints are needed.

This file completes the requested baseline audit only. It does not substitute for implementing and testing the subsequent additions.
