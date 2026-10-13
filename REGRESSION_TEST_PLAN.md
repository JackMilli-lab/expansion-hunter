# PRE-EXPANSION DISCOVERY ENGINE v2 — regression and evidence plan

Status: **design only; historical SAND/BEAM study BLOCKED; v2 not implemented**.
Prepared on 2026-10-04 from the existing repository. This document changes no
production code, test, threshold, provider mapping or archived decision.

## 1. What existing validation does and does not establish

The existing `docs/VALIDATION.md` records 201 passing tests on 2026-10-03.
Those tests establish software behavior on fixtures, including causal replay,
outcome-blind control selection and censored outcomes. The research fixtures are
explicitly synthetic. They do not establish a market edge, real SAND/BEAM
detection, calibrated probabilities or full-1000 operational reliability.

Preserve the following safeguards while extending their coverage:

| Existing contract | Preserve | Limitation to address in a versioned extension |
| --- | --- | --- |
| `replay.py:13–53` causal cutoff | Closed candles and known metrics/catalysts only | A single-asset snapshot cannot reconstruct historical cross-sectional ranks or the actual scan scheduler |
| `replay.py:62–79` unknown historical regime | Missing regime must remain explicit | Standalone replay does not recover the original market regime or exact live decisions |
| `research.py:51–67` study observation | Pre-event data separate from outcomes | Only `1D`/`4h` future candles; UTC candle-open-aligned boundaries; cannot represent arbitrary T-15m checkpoints |
| `research.py:70–103` archived cohort | Historical universe and delisted members; consistent dates | Maximum 500 IDs, operator attestations rather than independently verified lineage |
| `research.py:219–272` eligibility | Missing provenance/data must not become invented features | Requires 220 valid daily and 4h spot bars, historical cap/turnover/depth, and a narrow pre-resistance setup |
| `research.py:26–47` matching/effects | Fixed scales and separate candidate explanatory features | No historical sector matching, full v2 feature set, rank migration or microtime analysis |
| `research.py:334–381` outcomes | Missing/gapped paths censored; accepted close sequence | Default 30-day accepted breakout plus 50% maximum-high move differs from v2 mover horizons |
| `research.py:384–421` split/effects | Overlap and late-label purging | One chronological split with paired means/signs is not rolling walk-forward validation or calibration |
| `storage.py:25–38` immutable storage | Existing snapshots and run states readable | Excluded assets have no observation row; no alert episodes, scheduler decisions or outcome ledger |

Existing CLI `replay` can accept timestamp arguments, but `research` uses the
restricted study schema above. Do not round a T-15m cutoff to a 4h boundary or
relabel future bars to fit it. Add a separate versioned research contract and
retain the v1 CLI/schema semantics.

## 2. Entry gates for the named historical cases

The request identifies symbols but supplies no expansion dates, exact token
identities, venues or original scan/alert records. Therefore both statements
“the old engine missed it” and “v2 would have caught it” are **unproved**.

The available `data/smoke.sqlite3` archive contains two runs on 2026-10-03
(one failed, one completed) and two stored asset observations, BTC and ETH.
The completed batch requested five assets and evaluated two. It has no SAND
or BEAM observations and no historical top-universe snapshots. Old daily
candles beginning 2025-08-29 were first available to this installation on
2026-10-03; their old event dates do not justify backdating availability.
`examples/study.template.json` is empty. This inventory cannot supply either
named case, historical controls or actual pre-move alert lead times.

Required before either substantive case can run:

1. Resolve immutable asset identity: provider ID, chain/contract where relevant,
   token migration history, venue instrument and quote currency valid at the date.
   BEAM requires explicit gaming-token versus privacy-token disambiguation;
   symbol matching alone is insufficient. Verify SAND identity too.
2. Identify the actual claimed expansion interval in UTC with source artifacts.
   Record the user's reference event separately from an objectively discovered
   event; do not invent a “recent” date or silently substitute another move.
3. Lock a universe-wide event definition, onset rule, outcome horizons, liquidity
   policy and event deduplication before studying either named feature path.
4. Supply point-in-time universe/sector/cap/supply/liquidity/benchmark metadata,
   price observations and original availability evidence, including delistings.
5. Recover original completed/failed scan runs, configurations, symbol mappings,
   rejected-asset reasons, alert deliveries and polling/event timestamps.
   Without them, causal reconstruction may become possible, but actual miss
   attribution remains `UNKNOWN_ORIGINAL_SCAN_HISTORY`.

An illustrative manifest structure, deliberately containing no market facts:

```json
{
  "manifest_status": "BLOCKED_MISSING_EVIDENCE",
  "schema_version": "v2-proposed",
  "case_label": "SAND_OR_BEAM_PLACEHOLDER",
  "asset_id": null,
  "chain_contract_or_identity_artifact": null,
  "exchange_instrument": null,
  "quote_currency": null,
  "reference_event_interval_utc": null,
  "objective_expansion_onset_utc": null,
  "event_definition_hash": null,
  "historical_universe_artifact_hash": null,
  "original_availability_evidence": null,
  "original_engine_version_and_config_hash": null,
  "original_scan_and_alert_artifact_hashes": [],
  "source_artifact_hashes": [],
  "retrieved_at_utc": null,
  "declared_latency_model": null
}
```

This is a planning example, not an executable dataset accepted by the v1 schema.
Missing identifiers/dates/returns stay null and appear in the blockage report.

## 3. Outcomes and expansion onset must be fixed first

Maintain separate universe-wide label families: requested significant movers
(for example +15%/24h, +25%/72h, +40%/7d) and expansion-library thresholds
(+20%, +30%, +50%, +100%, +200% over configured horizons). These are research
definitions, not validated parameter choices. Hash the chosen configuration.

Use a fixed evaluation grid and close-based primary price returns. Define
reference price, exchange aggregation, quote-currency treatment, missing-bar
policy and minimum liquidity before labeling. Report maximum-high excursions
separately; a momentary wick is not proof of a sustained expansion or order fill.
For the requested probabilities, use a distinct close-based `WITHIN` target:
`Y(a,h)=1[max(eligible closes over (t,t+h])/close(t)-1>a]`. Register its observation
grid and require full horizon coverage/maturity. A return at exactly `t+h` is an
endpoint target, and a candle-high excursion is a wick target; neither can be
silently rendered as the same calibrated `within` probability.

Specify onset mathematically before case inspection. One permissible generic
rule is the first observed price `P(t) >= P(b) * (1 + onset_return)` after a fixed
grid baseline `b` for a qualifying horizon. Freeze `onset_return`, grid, price
type and event-clustering rule first. Other generic onset definitions need the
same preregistration; do not move onset later to make an alert count as early.
The labeler may inspect future prices; predictors and matching may not.

Merge overlapping qualifying windows into event IDs with a fixed deduplication
rule. Each alert receives its own future-horizon label from its alert timestamp;
event-case labeling and alert-episode labeling are different analyses. Record
outcome `available_at` only when its required observation horizon has matured.

## 4. Identical SAND and BEAM checkpoint protocol

Let `T0` be the frozen objective onset; every checkpoint uses an exact aware UTC
timestamp `T0 - offset`. No actual values can be populated until section 2 passes.

| Checkpoint | Offset | State of the requested market reconstruction now |
| --- | --- | --- |
| T-30d | 30 days | BLOCKED: identity/onset/archives not verified |
| T-14d | 14 days | BLOCKED |
| T-7d | 7 days | BLOCKED |
| T-3d | 3 days | BLOCKED |
| T-24h | 24 hours | BLOCKED |
| T-6h | 6 hours | BLOCKED |
| T-3h | 3 hours | BLOCKED |
| T-1h | 1 hour | BLOCKED |
| T-30m | 30 minutes | BLOCKED |
| T-15m | 15 minutes | BLOCKED |

For every checkpoint export:

- Identity, cutoff, universe membership/rank, venue coverage, missing fields and
  source/knowledge timestamps, dataset/config/code hashes.
- Frozen full-universe percentile vector, historical percentile vector, raw
  discovery rank, comparable-cohort rank velocity/acceleration and denominator.
- Stored Energy, Trigger Sensitivity, Absorption, Relative Strength and its
  acceleration, Volume Structure, VWAP Structure, Range Pressure, Volatility
  Awakening, Liquidity Quality and Catalyst Sensitivity with component coverage.
- Independent 5m/15m/1h/4h/1D/1W states; microtime 1m evidence; resistance,
  support, VWAP, known AVWAP anchors/confirmation times, POC/VAH/VAL and LVN above.
- Market/sector regime, spot-versus-futures provenance, early/late flag, proposed
  trigger/confirmation/invalidation, promotion/demotion reasons and alerts.

Report the first observable statistically unusual rank, RS improvement,
compression, acceptance change and volume awakening. A missing observation
means the first occurrence is interval-censored or unknown, not equal to the
next observed timestamp with invented precision.

Run three outputs separately: (a) actual archived v1 decisions if available,
(b) deterministic causal v1 reconstruction and (c) frozen v2 reconstruction.
Never translate a reconstructed v1 `COMPRESSION` into a historical `LOADED`
alert that v1 never emitted. Attribute a miss only through archived evidence:
universe exclusion, unavailable data, missing feature, weighting, threshold,
scan frequency or delivery failure. Otherwise state which attribution is unknown.

## 5. Point-in-time integrity and microtime tests

Availability means when the system could know the observation. Downloading a
historical OHLCV archive today does not establish historical `available_at`.
Store retrieval time separately. A vendor archive with independently attested
release times may support reconstruction; otherwise disclose an explicit
latency-assumption sensitivity study and do not call it exact live replay.

Required invariants:

- Appending or changing future bars, future market caps, catalysts, sector tags,
  benchmarks and outcomes cannot change any earlier feature, rank or match.
- At a cutoff inside an open hourly candle, never use its eventual high, low,
  close or completed volume. Independently closed 1m/5m/15m observations can
  support an early alert; a known partial bar must remain a provisional stream.
- Event-driven provisional updates carry observation/receipt sequence and state
  type. Confirmed and provisional alerts cannot be pooled without disclosure.
- Delayed/revised inputs appear only after their actual availability; original
  snapshots remain immutable. Missing/censored values never become zero scores.
- Swings, resistance zones, range origins and AVWAP anchors use only causally
  confirmed evidence. Discovery-bar wicks cannot retroactively confirm a level.
- Microtime structures use their own elapsed-history requirement. Do not inherit
  the v1 minimum 21 calendar days that nulls short 1m structural histories.
- Resistance rejection-decay comparisons use the same settled measurement
  horizon and versioned zone; unfinished rejection episodes are right-censored.
- POC migration uses a consistent fixed bin grid/schema, not moving extrema
  that create artificial migration. OHLCV profile is labeled an estimate.
- Native signed volume/CVD requires reliable trade-side data; close-direction
  volume is an OBV-style proxy, not CVD. Venue and base/quote units must match.
- Rank velocity uses elapsed time and comparable cohorts; changing universe,
  exclusions or sources triggers a coverage flag rather than false acceleration.

## 6. Controls: same pre-event information, labels revealed afterward

For every case/checkpoint select 20–100 nearest eligible controls (default 50)
from the same frozen universe and cutoff. Match historical sector, cap,
liquidity, volatility and baseline structure using fixed training-derived scales
and preregistered distance/caliper rules. No current sector/cap substitution.

Freeze candidate IDs/distances before reading outcomes. Keep other winners,
failed setups and censored observations. Report the non-expanding subset after
label revelation. Do not replace successful controls with worse matches to
manufacture 50 losers; report control and non-breakout shortfalls explicitly.
Avoid matching away an explanatory variable being tested; document that choice.

Report full-universe eligibility/exclusion rates and base rates, not just famous
cases. Tests must prove that swapping control future outcomes leaves selected
IDs/order/distances unchanged and that insufficient matches do not expand the
date window, sector policy or liquidity caliper silently.

## 7. Historical winner and failure libraries

Discover all qualifying matured events over the archived universe, including
unknown and later delisted assets. Apply the same labeling configuration to
every asset. Freeze pre-event observations for the same checkpoints and retain
non-events to estimate alert prevalence and base rates.

The failure library includes accepted breaks that reverse, failed volume spikes,
transient wick/pump reversals and no-follow-through alerts under fixed definitions.
“Pump-and-dump,” “leverage-only” and “news-driven” are mechanism labels requiring
reliable trade, spot/derivatives or timestamped news evidence. A reversal alone
does not prove manipulation; absent evidence means `MECHANISM_UNKNOWN`.

## 8. Misses, false positives, lead time and daily evaluation

A detected mover requires an observed RADAR/WATCH/PRE-IGNITION/LOADED episode
strictly before objective onset within a fixed useful-lead window. IGNITION
after onset and finding an asset after +25% do not satisfy pre-move recall.
Keep stage-specific detection counts, original timestamp, rank and feature data.

Every eligible matured event is either detected, missed or coverage-unknown.
Do not silently remove excluded/partially scanned universe members. Report
conditional recall on evaluable events plus all-universe coverage and unresolved
events; do not claim full-market recall from a survivor/data-complete subset.

For each first LOADED alert episode, evaluate each registered horizon from that
timestamp. Deduplicate repeated polls under a frozen episode/cooldown policy.
A false positive is a fully observed matured episode without the specified
expansion; immature horizons are pending and missing/delisted unknown paths
are censored. No hindsight deletion of inconvenient alerts or invalidations.

`ALERT_LEAD_TIME = objective_onset - first_observed_alert_time`; use UTC and
report delivery latency separately. Without original observations, actual lead
time is unknown. Simulated reconstruction lead time is labeled as such.

Daily reporting uses fixed event/eligibility rules and daily attribution, not the
day's profitable subset. TOP10/25/50 recall asks whether each eligible event
appeared in the corresponding frozen rank group before onset; disclose stage,
lead window, numerator/denominator, coverage and alert budget. Report precision,
recall, false-positive rate, false-discovery rate, lead-time distribution and
alerts per day. False-positive rate uses negatives, not all alerts.

Expected value and maximum drawdown require a separately frozen execution model
(entry/exit rules, fees, slippage, spread/depth, latency and intrabar ordering).
OHLC maximum favorable/adverse returns do not establish executable EV or drawdown.

## 9. Walk-forward validation, feature selection and calibration

SAND/BEAM are untouched regression holdouts, not examples for tuning. Repeatedly
inspecting their results contaminates the holdout; keep an additional unseen
chronological/universe holdout before any generalization claim.

Use rolling development, validation and future test folds. Fit matching scales,
feature transforms, weights, thresholds and calibrators only on matured training
labels available before each fold cutoff. Purge overlapping label horizons;
embargo boundary events under a frozen rule. No repeated event/asset episode or
shared future path can cross folds unnoticed. Fit preprocessing inside each fold.

For each candidate feature report cross-sectional/horizon-specific rank IC,
incremental precision/recall/PR-AUC at a fixed alert budget, false-positive effect,
lead-time effect, missing-data coverage and regime stability on later folds.
Account for multiple feature tests. Retain a feature only after preregistered
out-of-sample improvement; a compelling named-case chart is not evidence.

Confidence intervals must account for overlapping windows, reused controls,
correlated dates/assets/sectors and event clustering, for example via frozen
event/time-block clustered bootstrap with sector dependence checks. Do not treat
ten checkpoints of one move as ten independent winners.

Train probability outputs separately for >10% within 24h, >20% within 72h and
>30% within 7d using the registered close-based threshold-hit labels above.
Report Brier score, reliability curves with bin counts/uncertainty, precision,
recall and PR-AUC on untouched chronological folds versus base-rate baselines.
Freeze minimum total/positive/negative and per-bin effective sample requirements;
if absent, unsupported or drifted, output `PROBABILITY_UNAVAILABLE` with reasons.
Rule scores are not percentages of success. Report major provider disagreement
and rerun affected feature/label sensitivity rather than averaging it away.

## 10. Software test and operational evidence matrix

| Area | Required evidence/test beyond existing v1 checks |
| --- | --- |
| Scores | Bounds, monotonic controlled changes, component coverage, null propagation, no pumped-candle reward |
| Absorption/range pressure | Fixed-zone settled rejection decay, return time, rising lows, upper-quartile residence; unsettled tests censored |
| VWAP/AVWAP/profile | Exact native-volume tests and separately labeled approximations; prefix invariance, failed-break anchor latency, fixed-grid POC migration/LVN |
| Anomaly/rank | Own-history versus frozen cross-section percentiles, ties/missingness, common-cohort elapsed-time migration |
| State transitions | Explicit rules plus LOADED→INVALIDATED, IGNITION→FAILED_BREAKOUT, WATCH→DORMANT; independent timeframe disagreement |
| Alerts | Before-breakout thresholds, exact trigger/invalidation, dedup episodes, delivery failure and provisional/confirmed separation |
| Dynamic scheduling | Unknown asset auto-promotion without manual list, priority changes, starvation prevention, 1m monitoring during open 1h bar |
| Top-1000 operations | 1000-ID synthetic orchestration plus separate real-source coverage/latency/rate-limit/partial-failure runs |
| Audit | One attempt row per requested ID, errors and policy rejections preserved, immutable feature/rank/config lineage |
| Outcomes | Matured horizons only, gaps/delistings censored, onset-before-alert miss, repeated-poll dedup, fixed recall denominator |
| End-to-end | Broad discovery→promotion→deep/microtime scoring→states→alerts→audit→matured evaluation without archetype admission gate |

Fixture-based load tests establish capacity behavior, not coverage of 1000 real
tradeable assets. Native/secondary price, metadata and derivatives disagreements
must be audited with units, timestamps and tolerance rules in live-source runs.
Provider unavailability is an explicit outcome, never a passing reliability test.

## 11. Acceptance status and next evidence gates

Status below refers to this inspection stage, not future completion promises.

| User criterion | Current status | Gate required to pass |
| --- | --- | --- |
| A: reliable top-1000 | NOT RUN / NOT IMPLEMENTED | Versioned universe plus observed real-source throughput/coverage/recovery |
| B: every asset audit | PARTIAL v1 / v2 NOT RUN | All requested IDs, including exclusions/failures, have immutable attempt evidence |
| C: SAND/BEAM PIT reconstruction | BLOCKED | Verified identities, objective UTC events and knowledge-time archives |
| D: unusual before expansion | BLOCKED | Frozen checkpoint outputs and controls before objective onset |
| E: comparable non-pumping controls | BLOCKED | Outcome-blind same-time matching, later full label disclosure |
| F: no look-ahead | v1 FIXTURE TESTED / real study BLOCKED | Audited source lineage plus replay/prefix invariants on actual cases |
| G: measured false positives | BLOCKED | Complete matured LOADED episodes and valid negative/censored denominators |
| H: measured lead time | BLOCKED | Original alert/scan timestamps and locked onset; reconstruction labeled separately |
| I: unknown assets auto-promoted | v2 NOT RUN | Unknown-ID end-to-end promotion with observed coverage |
| J: no manual watchlist dependency | v2 NOT RUN | Broad-ranked admission independent of archetype/name/manual list |

First review `GAP_ANALYSIS.md`, `ARCHITECTURE_V2.md` and
`FEATURE_DEFINITIONS.md` with this plan. This stage stops before major
architecture/production changes, as requested. Missing historical evidence is
an explicit blocker for empirical claims, not permission to manufacture dates,
inputs, scores, successful detections or validation results.
