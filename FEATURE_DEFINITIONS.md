# Pre-Expansion Discovery Engine v2: feature definitions

Status: design proposal, 2026-10-04. These additions are **not implemented or empirically validated**. The existing engine remains unchanged. Numerical weights, sample gates and thresholds below are shared research hypotheses, not coefficients fitted to SAND, BEAM or other selected winners. See `GAP_ANALYSIS.md`, `ARCHITECTURE_V2.md` and `REGRESSION_TEST_PLAN.md` for evidence, implementation boundaries and validation gates.

## 1. Existing mathematics to retain

The following are verified implementations rather than proposed v2 features. References identify the inspected source, not historical trading results.

| Existing feature | Verified contract | Source |
| --- | --- | --- |
| EMA10/20/50/100/200 | SMA seed, then alpha = 2/(n+1); null before n observations | `src/expansion_hunter/technical.py:44` |
| RSI14, ATR14 | Wilder smoothing; flat RSI = 50 | `src/expansion_hunter/technical.py:62`, `:327` |
| MACD | EMA12 minus EMA26, EMA9 signal | `src/expansion_hunter/technical.py:338` |
| BB width | 4 times population SD20 divided by SMA20; fraction | `src/expansion_hunter/technical.py:349` |
| Realized volatility | Sample SD20 of log returns, annualized with sqrt(365*86400/bar_seconds) | `src/expansion_hunter/technical.py:360` |
| Own-history percentiles | Prior observations only, mid-rank ties, minimum 60 valid values | `src/expansion_hunter/technical.py:77` |
| RVOL | Latest base volume / preceding 20-bar mean; latest bar excluded | `src/expansion_hunter/technical.py:368` |
| OBV | Sum sign(close change) times base volume | `src/expansion_hunter/technical.py:379` |
| VWAP/AVWAP | Native quote/base volumes where present; declared typical-price approximation otherwise | `src/expansion_hunter/technical.py:381`, `src/expansion_hunter/avwap.py:29` |
| Swings | Three-bar right confirmation; discovery time includes delivery delays | `src/expansion_hunter/technical.py:85` |
| Breakout episode | Fixed previously observed resistance; two closes for acceptance | `src/expansion_hunter/technical.py:179` |
| Candle profile | Uniform high-low volume allocation, 48 bins, contiguous 70% value area; expressly approximate | `src/expansion_hunter/technical.py:215` |
| BTC relative return | Identical daily endpoints and seven-day duration | `src/expansion_hunter/regime.py:12` |

Existing calendar and granularity limitations matter. Structures require both 220 bars and 21 actual days (`technical.py:503`); the base finder also requires 21 days (`:125`). With 400 default bars (`config.py:9`), 1m/5m/15m/1h history cannot satisfy 21 days. The single profile uses at most 512 bars from a base or 20-bar fallback (`technical.py:435`, `:461`). EMA clustering includes all five EMAs (`:408`), rather than a dedicated 10/20/50 cluster. Current feature returns use percentage points, whereas `matched_returns()` uses fractions. Do not silently change these v1 meanings.

The tests already establish useful invariants: future/delayed candles cannot change a prefix (`tests/test_technical.py:37`), short history is not a multiweek base (`:85`), session VWAP needs midnight coverage (`:159`), zero-volume profiles remain unknown (`:219`), and AVWAP confirmation cannot invent earlier acceptance (`:271`, `tests/test_avwap_audit.py:40`). Add local structures under new versioned names; preserve these invariants.

## 2. Causal contract, units and naming

At decision time t, input records must satisfy event_time <= t and available_at <= t. A closed candle also requires close_time <= t. Revised observations need immutable vintages. Downloading old candles today establishes their availability today unless a separate, explicit historical publication contract is proved. Research under reconstructed publication assumptions must be labelled separately from an exact archived replay.

Every feature stores: asset_id, venue, instrument_kind, quote_currency, timeframe, as_of, effective_end, available_at, computed_at, lookback, formula_version, input_digest, value, unit, status, missing_reason and sample_count. Live derived available_at is no earlier than contributing input availability, structural recognition and computation. Historical reconstruction stores a separate knowledge_boundary inferred from documented original publication assumptions, plus actual computed_at and `mode=CAUSAL_RECONSTRUCTION`; it never backdates a live feature record. Liquidity, sector and supply metadata obey the same contract as prices.

- `*_return`, turnover, BB width and funding are fractions; `*_pct` is percentage points; `*_bps` is basis points. Explicit adapters translate preserved v1 percentage-point fields.
- Base volume has token units; quote volume has the quote currency's units. USD/USDT conversion requires its own observed FX record. Never add different tokens' base volumes.
- Prices use the instrument's quote currency. Distances use fractions or ATR units with a named field. Percentiles use [0,1]; scores use [0,100].
- Durations and acceleration use elapsed UTC time, not assumed equal scan intervals. Crypto daily sessions begin 00:00 UTC; weeks begin Monday 00:00 UTC.
- Missing is null, with `DATA_INCOMPLETE`, `STALE`, `UNRELIABLE` or `INSUFFICIENT_HISTORY`. Zero is reserved for an observed zero. Boolean unknown is null.
- Partial-bar observations use a distinct immutable stream and `is_final=false`; final OHLCV cannot overwrite earlier partial data. A closed 1m/5m candle can trigger before an enclosing 1h closes without partial-candle assumptions.

No feature estimates news that has not occurred. No OHLCV proxy proves accumulation, aggressor direction, resting liquidity, exchange-wide inventory or future expansion.

## 3. Percentiles, normalization and missing coverage

For a value x and n valid historical values strictly preceding its endpoint:

    H(x) = [count(history < x) + 0.5*count(history == x)] / n

Historical comparison uses the same formula, source class, interval and units. Windows and sample gates are configurable: candidate defaults are at least 60 prior valid observations and 80% requested temporal coverage. A 180-bar comparison is not a 180-day comparison. Intraday seasonal baselines need enough complete earlier sessions. Record effective duration and never extend a window with future observations.

For a same-time comparison cohort U with n valid comparable observations:

    X(x) = [count(U < x) + 0.5*count(U == x)] / n

U contains the target once. Own-history comparison excludes its current point. Preliminary cross-sectional percentiles require at least 30 assets and 80% expected cohort coverage, otherwise null. Report denominator, requested universe, excluded assets, endpoint tolerances and cohort hash. Do not recompute the entire top-1000 reference from only the top-50 promoted assets. Deep feature percentiles restricted to promoted candidates must explicitly say `cohort=deep_watchlist`.

For beneficial direction d in {+1,-1}, use H_d=H or 1-H and X_d=X or 1-X. Define a candidate feature percentile P=0.5*H_d+0.5*X_d only when both exist. Expose H and X independently. A single available percentile produces a partial score, not a full two-axis observation. `clip(x)` means min(1,max(0,x)). Robust self-anomaly can additionally use z=(x-median(history))/(1.4826*MAD(history)); zero MAD gives null, not infinity. Winsorization boundaries come from training history only.

For normalized family inputs f_j in [0,1] and fixed positive weights w_j:

    coverage C = sum(observed w_j) / sum(all w_j)
    observed estimate S = 100 * sum(observed w_j*f_j) / sum(observed w_j)
    information interval = [C*S, C*S + 100*(1-C)]

No observations gives S=null, C=0 and no eligible score. The interval expresses what unknown bounded components could contribute; it is not a confidence or probability interval. Use the lower information bound for cross-sectional prioritization only alongside minimum family coverage; do not treat missing components as observed zeroes. Publish the estimate, interval and missing list together. Proposed discovery eligibility requires C>=0.7; LOADED requires >=0.8 and the mandatory families in section 12. Optional rich data can create a separately labelled score variant with its own calibration.

For nested composites, expand weights to distinct leaf observations: multiply weights along each path and aggregate missing leaf weights. A partially observed subscore cannot count as a fully observed parent component. If a leaf appears on multiple paths, disclose its effective summed weight or assign it to only one scoring path. Stage thresholds refer to the observed score estimates with explicit coverage gates; displayed ranking uses the comparable conservative bounds. Store both, so a score crossing caused by improved data availability can be explained separately from changed market evidence.

## 4. Lightweight broad-discovery features

Layer A operates on approximately 1000 historically eligible assets, without archetype or manual-watchlist requirements. These computations use cached aligned returns, spot volumes and rolling summaries, not full profiles or every anchor on every asset. Each feature is compared to itself and its valid broad cohort.

Let p_t be a completed price observation; r_w=log(p_t/p_(t-w)). Require aligned endpoints and contiguous coverage. Compute 15m/1h/4h/24h/3d/7d/30d returns when available. `PRICE_RATE_CONTRAST=(r_short/short_hours)-(r_long/long_hours)` has units log-return/hour and is not physical acceleration. True `PRICE_ACCELERATION=(rate_current_block-rate_prior_block)/elapsed_hours_between_block_midpoints`, with equal-duration nonoverlapping blocks and rate=block_log_return/block_hours, has units log-return/hour². Both are attention evidence, not permission to reward a vertical candle; the formula registry names them separately.

| Feature | Candidate definition | Direction / qualification |
| --- | --- | --- |
| Relative price change | Asset log return minus aligned market median log return | Higher, capped by overextension |
| RVOL | Current window quote volume / mean volume in prior nonoverlapping equal-duration windows | Higher; denominator strictly prior |
| Volume acceleration | log(V_current_block/V_prior_block)/block_hours | Higher; both positive and same-duration |
| Volume acceleration-of-acceleration | Difference of successive volume acceleration / elapsed hours | Higher; three comparable blocks |
| Turnover | Matched-period observed notional / contemporaneously available market cap | Fraction; missing cap means null |
| Turnover acceleration | Difference of log turnover between comparable windows / elapsed hours | Higher; no stale future supply values |
| Volatility compression | 1-H and 1-X of ATR14/close, BB width, realized volatility | Higher means compressed |
| Range compression | Median(high-low)/close over recent k bars divided by preceding k-bar value | Lower; normalize ratio by percentiles |
| Recent-high distance | max(high in fixed preceding window)/close - 1 | Small nonnegative distance plus range acceptance |
| BTC / sector RS | r_asset-r_benchmark on exact equal endpoints | Higher, sector coverage declared |
| Liquidity quality | Section 11 score and its component missing mask | Gate plus separate prioritization |

Use separate volume-unexpectedness and volume-demand fields. A high volume percentile with falling prices is an anomaly; it does not receive bullish demand points. Seasonal RVOL may replace the simple baseline only in a named variant: match UTC weekday/time bucket and elapsed bucket duration from earlier sessions. Partial-bar RVOL compares observed elapsed volume to prior elapsed-volume curves, never to eventual full-bar volume.

`ANOMALY_SCORE` combines equal-weight normalized families: own-history/cross-sectional volume change, compressed volatility, aligned RS improvement, range acceptance, and true price acceleration. A volume-only candidate may reach RADAR, while LOADED requires structure. Define the broad nonmigration base exactly as `LIGHT_DISCOVERY_SCORE=C_anomaly*ANOMALY_SCORE`, the section 3 lower information bound, with null when no observations exist and normal coverage eligibility. Rank broad members with valid comparable bases before calculating migration; **rank migration is excluded**. Liquidity and lateness govern promotion eligibility separately and remain explicit fields alongside archived raw scores/ranks. Top-250/top-100 eligible promotion limits are configurable; candidates can also enter on exceptional comparable base-rank velocity to avoid missing a rapidly improving absolute-score laggard. An eligibility-filtered display rank is a different field and cannot define migration q.

Persist distinct broad base, deep base and final deep-priority rank series. All broad members retain their nonmigration light score whether promoted or not. Comparing one asset's deep score to another asset's light score creates a computation-selection advantage, so it is prohibited. Deep ranking references the same deep formula cohort; migration across promotion boundaries uses the unchanged broad **base** series, not mixed scores or final priority. Expensive profile/AVWAP evidence refines deep priority after broad discovery. Final priority may incorporate broad base-rank migration only in a second pass; it never feeds back into this scan's base ranks or subsequent base-rank migration history.

## 5. Rank migration

For the migration series, rank 1 is the highest **nonmigration base score**; ties use equal mid-ranks, with asset_id only for deterministic display order. For n>1, normalized base-rank quality q=(n-rank)/(n-1), in [0,1]. Store the raw published base rank, n, raw leaf features, frozen own-history percentiles H, leaf coverage mask, score version, source class/units and cohort hash each scan. Final deep priority is a different rank field and never defines q, avoiding circular rank-velocity scoring.

    RANK_VELOCITY = 100*(q_t-q_previous)/delta_hours
    RANK_ACCELERATION = (velocity_t-velocity_previous) /
                        elapsed_hours_between_velocity_midpoints

Units are percentile points/hour and percentile points/hour². At least three valid timestamps are required for acceleration. Do not divide by a fixed sampling interval when WATCH scans once/minute and DORMANT scans once/15 minutes. Use a fixed 1h/3h/6h historical comparison or a time-weighted regression as a separate migration view; missing scans never become repeated zero-change observations.

Changing membership can improve rank without asset improvement. Stored scores alone are insufficient: their cross-sectional X values may already depend on different universes. At current t, construct the assets present at both historical endpoints, require >=80% overlap and the percentile sample gates, and recompute **both** timestamps' X values and nonmigration base scores on that same intersection from immutable raw vectors and each timestamp's frozen H. Apply identical formula version, units, source class and per-asset leaf coverage mask; if these are incompatible or either raw vector is absent, withhold comparable migration. Do not refill old missing leaves with later data. For acceleration use one common intersection across all three timestamps and recompute all three bases together; velocities from different pairwise cohorts cannot be differentiated.

Keep the originally published base scores/ranks immutable. Store reconstructed comparable scores/ranks separately with computation time, historical knowledge boundaries and comparison-cohort hash. Only comparable q enters migration bonuses. If requirements fail, mark `UNIVERSE_CHANGED`/`INCOMPARABLE_RANK_HISTORY` and withhold the bonus rather than zeroing velocity. Delistings and listing entries remain in universe audit records. Separately expose comparable base-score velocity to reveal whether asset evidence improved or peers deteriorated.

## 6. Compression, local ranges and range pressure

Keep the existing multiweek-base detector as a daily/4h research feature. Add independent local-box features on 1m/5m/15m/1h. Suggested local fitting horizons are 60 closed bars with at least 40 observed bars; ATR/BB warmups retain their own requirements. The 1W structure uses its available bar count, not a required daily 200-EMA gate. Exact horizons are configuration, frozen before holdout evaluation.

A local box is registered at its first causal recognition. Use only bars preceding the test candle to obtain L and R; retain `zone_id`, `recognized_at`, support, resistance and tolerance for that episode. Candidate local-box conditions are `(R-L)/median(ATR14)<=8` and endpoint displacement <=0.5*(R-L). Register overlapping alternatives as distinct hypotheses, not retrospectively corrected lines. A flat zero-range box has unknown normalized range position.

    range_position_i = clip((close_i-L)/(R-L))
    upper_share = mean(range_position >= 0.75)
    midpoint_retention = mean(range_position >= 0.50)
    close_location = (2*close-high-low)/(high-low), null for zero range
    distance_resistance_atr = (R-close)/ATR14

Use the registered box for current-vs-prior equal-duration occupancy windows. `RANGE_PRESSURE_SCORE` is 100 times the equal-weight mean of upper_share, midpoint_retention, normalized upper-share improvement, normalized rising-confirmed-low slope, and `clip(1-max(0,distance_resistance_atr)/2)`. Once price is accepted above R, stop treating this as pre-breakout resistance pressure; transition to an episode above the old zone.

For price acceptance, define upper_improvement=upper_share_current-upper_share_prior and midpoint_improvement=midpoint_retention_current-midpoint_retention_prior using nonoverlapping equal-duration windows on the same frozen box. Normalize each as P from section 3, favorable direction positive. The acceptance family is the mean of upper_share_current, midpoint_retention_current, P(upper_improvement) and P(midpoint_improvement). Incomplete prior windows leave improvement null; a rebuilt box cannot manufacture improvement against a different old box.

Confirmed-low slope is the least-squares slope of the last >=3 causally confirmed lows against elapsed hours, divided by ATR frozen at box recognition. Map it to [0,1] by H/X; report low timestamps and their actual confirmation times. Rejection decay and return time are defined separately below so occupying resistance does not manufacture repeated tests.

Trend compression uses `(max(EMA10,20,50)-min(EMA10,20,50))/close`; smaller is more compressed. EMA slope is log(EMA_t/EMA_(t-k))/elapsed_hours. Slope improvement compares equal-duration blocks. EMA100/200 reclaim is optional long-history context; absent EMA200 cannot suppress all short-timeframe discovery. Volume contraction is recent mean quote volume divided by prior nonoverlapping baseline mean; turnover contraction uses contemporaneous cap vintages.

## 7. Resistance episodes and absorption

Resistance is a causally registered zone [R-epsilon,R+epsilon], with epsilon=max(0.25*ATR_at_recognition, price*tick_or_spread_tolerance). Inputs contain its supporting confirmed pivots and recognition time. Future highs cannot move it. A newly discovered zone can describe old candles, but must not create earlier alert eligibility or fictitious earlier tests.

A test episode starts when price returns from below to the zone after leaving it. Consecutive bars inside the zone form one episode. Define departure below `R-2*epsilon`; require at least two closed bars outside before rearming. Timestamp the first interaction; retain tests-per-day and time between tests. A close above is a potential break, not automatically another absorption test.

For episode j with touch time tau_j and a fixed reaction horizon H:

    rejection_j = max(0, R - min(low after touch through tau_j+H)) / R
    rejection_atr_j = max(0, R - min(low ...)) / ATR_at_recognition
    rejection_volume_j = sum(native sell quote volume in that reaction interval)
    return_time_j = first subsequent zone return - departure_time
    low_j = minimum low in the settled reaction interval

Proposed reaction horizon: 12 bars on the episode's timeframe, with a versioned wall-clock duration. Compare only settled episodes with the same duration and nonoverlapping reaction windows. An unfinished episode is right-censored; its currently small rejection must not earn exhaustion points. If no return occurs before H, return_time is censored at H, not zero. Aggregate different timeframes separately. Missing native sell notional does not become total volume.

For >=3 comparable settled tests, fit log(rejection_atr_j + tick_size/ATR_at_recognition) against test index; rejection_decay=negative fitted slope. Estimate analogous return-time improvement from uncensored, same-horizon records and rising-lows slope from confirmed lows. Record counts and censoring. A high-volume rejection that preserves the frozen support is a separate conditional feature; high volume with a support break is adverse evidence.

`ABSORPTION_SCORE` combines six equally weighted normalized components: repeated-test intensity, decreasing rejection size, faster zone return, rising settled lows, maintained upper-range acceptance, and observed aggressor selling with muted downside. The first five are explicitly `STRUCTURAL_ABSORPTION_PROXY`; the sixth requires native aggressor evidence. OHLCV-only variant has a distinct formula version and cannot claim observed sell absorption. Candidate structural absorption can still enter WATCH without CVD.

Aggressor sell-absorption measure for a completed interval: native sell share S/V, increase of S per elapsed hour, and downside efficiency `max(0,-log(close_end/close_start))/(S/notional_scale)`. Use only positive real sell notional and a named normalization scale. Reward unusually large S with unusually small downside efficiency **and** preserved support; a flat illiquid tape without reliable selling is insufficient. These are measurable hypotheses, not proof of ownership accumulation.

## 8. Price/volume microstructure and volatility awakening

Compute independent features on 1m/5m/15m/1h/4h/1D with sufficient actual history. Relative volume, acceleration, second derivative and OBV retain their names and units. `volume_per_atr_move = quote_volume/(abs(log(close/previous_close))/atr_fraction + eps)` uses eps fixed from training/tick resolution and declares saturation. Large values can mean absorption or churn; direction and structural retention are required.

Native aggressor base volume B_buy and total V produce:

    delta_base = B_buy - (V-B_buy) = 2*B_buy-V
    CVD_base(t; origin) = sum(delta_base from named origin to t)
    delta_share = sum(delta_base)/sum(V)

Quote CVD requires native taker-buy quote volume Q_buy: delta_quote=2*Q_buy-Q_total. Multiplying base delta by close or VWAP is a labelled quote-CVD estimate, never exact quote CVD. Candle color, close-change signs and tick-rule classification are separately named proxies. Current `Candle` supports base taker buying (`models.py:91`) but no taker-buy quote field. A provider lacking aggressor fields returns null CVD. Reset origins and missing intervals are recorded; no gap is treated as balanced flow.

Spot/futures comparisons require the same underlying, same quote-converted notional, exact equal intervals and reliable instrument metadata. A contract-volume series must be converted with its documented contract multiplier; volume and OI are different quantities. Define divergence as log(spot_RVOL)-log(futures_RVOL), with both positive, and first-threshold-crossing lead time from archived scans. CVD acceleration means change in normalized delta-share slope, not comparison of incompatible cumulative origins. Spot evidence preceding futures is stronger demand context; high futures RVOL with weak spot is a leverage warning.

Conditional tags, each with inspectable numerical inputs:

- `POSSIBLE_ABSORPTION`: elevated native selling, small downside response, support intact.
- `HEALTHY_DEMAND`: positive returns, increasing spot baseline volume, supportive close location.
- `WEAK_ADVANCE`: positive returns, sharply falling spot baseline volume.
- `STORED_ENERGY`: resistance proximity, contracted spot baseline, shrinking settled rejections.
- `EARLY_IGNITION`: abnormal spot volume acceleration and micro acceptance, without an extended candle.
- `LEVERAGE_DRIVEN`: futures acceleration exceeds spot with adverse OI/funding context.

Define `VOLUME_STRUCTURE_SCORE` for checkpoint reporting as 100 times the equal-weight normalized mean of four families: native baseline-volume contraction, healthy spot-demand improvement, observed sell-absorption efficiency, and aligned spot-before-futures divergence. Contraction uses P(volume_contraction, direction=-1). Healthy demand uses the mean of P(spot_quote_volume_acceleration) and P(delta_share_change) gated by positive return and preserved support; absent native delta_share leaves that leaf null. Sell absorption uses P(native_sell_share) and favorable P(downside_efficiency, direction=-1), gated by preserved support and nondeclining native sell notional. Spot-before-futures uses P(log(spot_RVOL)-log(futures_RVOL)); a positive recorded spot lead adds explanatory evidence only, not an unregistered extra score. Observed failed gates score zero for the respective bullish component; unavailable gate inputs are null. The section 3 missing-leaf and coverage contract applies. Publish contracted and awakening components separately: volume drying up and early demand can occupy different windows, and raw high volume is never sufficient. OHLCV-only variants have separate names and cannot claim observed selling or CVD.

For awakening, compute own-history percentiles of ATR fraction, BB width and RVOL at each closed endpoint. For the last k=6 bars:

    low_vol = min(mean(ATR_percentile, BB_percentile) in previous k bars)
    delta_vol = current_mean_vol_percentile - low_vol
    delta_volume = current_volume_percentile - median(prior k volume percentiles)
    VOLATILITY_AWAKENING_SCORE = 100*clip((0.5*clip(delta_vol/0.25)
                                 +0.5*clip(delta_volume/0.40))
                                 *clip((0.40-low_vol)/0.40))

This is a candidate compression-to-awakening formula, not a breakout probability. Require positive micro return or accepted VWAP structure for bullish ignition; declining prices can also awaken volatility. The extension gate in section 11 prevents a volume climax from ranking as early ignition.

## 9. Rolling volume profiles

Compute separate rolling 24h/3d/7d/14d/30d/90d profiles using exact available trade prices where obtainable. Native trades contribute executed base volume to their price bin; quote-weighted profiles are a separate methodology. The fallback distributes each candle's base volume uniformly over high-low intersections, preserving the existing documented approximation. Store methodology, venue coverage, timeframe, complete requested interval, last event time and bin registry.

Full rolling windows require coverage from t-window through t; truncated history produces a separately labelled partial profile with no full-window feature eligibility. Do not obtain a 90d profile from a 20-bar intraday fallback. Closed aggregate bars must fit entirely inside the interval unless finer immutable data provides the clipped contribution. Profiles from different precision/source classes are not mixed silently.

For bin masses v_b: POC is the center of the highest-volume bin, with a deterministic tie rule. Begin value area at POC and add the larger adjacent bin until >=70% total mass; VAH/VAL are outer bin edges. HVN/LVN are interior local maxima/minima above the 80th/below the 20th mass percentile, requiring sufficient populated neighbors. Store the full mass vector, not only node prices. Empty profiles give null levels.

For migration, use a fixed grid registered at episode start, e.g. price_ref*exp(k*delta_log_price), with delta fixed by configured resolution and observed tick size. Extend this grid by integer bins when needed; never resize old bins to new observed extrema. Re-binned profiles have a new grid_id; cross-grid POC migration is unknown until both windows are recomputed causally on one declared grid.

    POC_migration = log(POC_t/POC_previous)/elapsed_hours
    acceptance_above_POC = fraction of recent closed bars above current known POC
    VAH_pressure = upper-zone occupancy relative to causally known VAH
    overhead_mass = sum(profile volume in (resistance, resistance+2*ATR]) / total
    LVN_gap_atr = consecutive low-mass bin width immediately above resistance / ATR

Require the overhead band to fall inside observed profile support; bins beyond the highest historically traded price are `UNOBSERVED`, not proven empty supply. Rolling-window expiration can move POC without new buying; expose separate new-volume and expired-volume contributions and an expanding fixed-origin profile as a control. Thin historical executed volume is an overhead-volume hypothesis, never measured resting ask depth. An LVN above resistance can raise a trigger proxy; direct order-book depth remains separate.

## 10. Session VWAP and anchored structure

For a fully observed period, VWAP=sum(native quote volume)/sum(base volume). Otherwise the explicitly labelled estimate is sum(typical_price*base_volume)/sum(base_volume), typical_price=(high+low+close)/3. Mixed native/estimated inputs carry that method label. Zero total base volume gives null. Daily UTC session VWAP and session VWAP are identical unless a venue-specific session is explicitly declared; do not double-count them. Weekly VWAP starts Monday 00:00 UTC and requires intraday coverage from that start.

Retain existing major swing-low/high, range-start, capitulation, abnormal high-volume and fixed breakout-origin anchors. Add `highest_volume_window` as a separate anchor: maximum observed native volume in a declared trailing interval, recognized at the current evaluation. Never backdate its eligibility just because the winning candle is older. A record-setting volume anchor is a different causal variant. Deduplicate origin timestamps before confluence counting.

Add `failed_breakout_origin`: an already observed fixed-zone breakout followed by two closed bars below its frozen acceptance boundary. Anchor at that episode's originating candle; recognize only after failure is known. A later failure cannot create this anchor in an earlier snapshot. Store origin, recognition, episode_id, value curve and methodology.

Reclaim requires a previously observed below-side, followed by >=2 consecutive eligible closes above AVWAP+buffer. Candidate buffer=max(0.0005*AVWAP,0.1*ATR), preserved v1 buffer remains unchanged in v1 reports. Touch/wick evidence must occur after recognition, as established by existing AVWAP audit tests. Support requires previous accepted above-side, a later observable touch, and close acceptance. Rejection is a post-recognition touch from below followed by a close below, not a reclaim wick.

Confluence means >=2 distinct origins whose values fall within 0.5 ATR of each other, with current price accepted above the group. AVWAP compression alone is not bullish. `VWAP_STRUCTURE_SCORE` is the equal-weight normalized mean of current session/weekly acceptance, accepted important-AVWAP reclaim, observed AVWAP support, supportive confluence, and improving distance without overextension. Importance is declared at recognition from excursion/volume evidence; it cannot be selected by subsequent returns.

## 11. Relative strength, rotation, liquidity and lateness

For windows {15m,1h,4h,24h,3d,7d,30d}, compare log returns on exactly identical endpoints:

    RS(asset, benchmark, w) = log(p_t/p_(t-w)) - log(b_t/b_(t-w))

Benchmark families are BTC, ETH, reliable TOTAL3, point-in-time sector basket and broad alt universe. Use a common completed endpoint; persist its age. A current asset quote and stale daily benchmark do not provide valid 15m RS. Missing TOTAL3 cannot be filled with the current exchange's selected alt basket; publish that basket under its own name. Equal-weight sector/universe baskets use prior-period known eligible membership, lagged rebalances, observed exits/delistings and declared incomplete-member coverage. Exclude the asset from its own comparison basket.

`RELATIVE_STRENGTH_SCORE` uses equal weight across available benchmark families, then across windows within each family, so seven BTC windows cannot outweigh one ETH family by repetition. Normalize RS via H/X. `RELATIVE_STRENGTH_ACCELERATION` is change of this comparable score per elapsed hour, in points/hour; source, coverage or membership changes withhold the bonus. Also measure BTC-downside resilience and recovery: asset minus BTC return during a registered BTC drawdown episode, then time to recover each own frozen pre-drawdown reference level. Incomplete recoveries are censored.

Sector rotation requires point-in-time asset-to-sector metadata, not tickers, current narrative labels or social hype. Assets may have weighted sector memberships; weights sum to one and are vintage-stamped. For each sufficiently covered sector compute median returns, native quote-volume growth, median turnover, positive-return breadth, shares above EMA20/50, and aligned relative return vs TOTAL3 or the explicitly named substitute basket. Candidate `SECTOR_ROTATION_SCORE` is the equal-weight H/X mean of these families; a sector signal requires >=5 eligible constituents and >=80% declared coverage. Sectors are configured metadata categories, while their relative leadership is discovered dynamically. Statistical clustering is a separately tested research feature and must not invent semantic sectors.

`LIQUIDITY_QUALITY_SCORE` uses observed spread, ask/bid depth at configured ±0.5%/1%/2% bands, expected order-size slippage, reliable native notional, exchange coverage, and cross-source agreement. Component normalization is monotone: spread/slippage lower is better; adequate depth/coverage higher is better. Order-book imbalance=(bid_depth-ask_depth)/(bid_depth+ask_depth) on the same price band, with both positive total depth and fresh snapshots. Trade-size distribution requires raw reliable trades and venue identifiers; OHLCV cannot reconstruct it.

For a fresh spot book let mid=(best_ask+best_bid)/2, spread_bps=10000*(best_ask-best_bid)/mid, and D=min(bid_USD_depth,ask_USD_depth) inside the declared ±1% band. Simulate both sides of a fixed configured probe order on observed levels: buy_slippage_bps=10000*(buy_VWAP/mid-1), sell_slippage_bps=10000*(1-sell_VWAP/mid), slippage_bps=max(buy,sell). An insufficient observed book yields an explicit insufficient-depth result; do not extrapolate unobserved levels. Native Q_24h is observed matched-interval USD-converted spot notional. Cross-source price dispersion is the median absolute log deviation from the aligned venue median, multiplied by 10000; at least two independently sourced, correctly mapped prices are required.

Candidate normalized components are s_spread=clip(1-spread_bps/max_spread_bps); s_depth=clip(D/(2*min_depth_usd)); s_slippage=clip(1-slippage_bps/max_probe_slippage_bps); s_notional=clip(Q_24h/(2*min_quote_volume_24h)); s_coverage=clip(reliable_venue_count/target_venue_count); s_agreement=clip(1-dispersion_bps/max_disagreement_bps). All denominators are positive versioned configuration. A provisional target_venue_count=3 is a research hypothesis. `LIQUIDITY_QUALITY_SCORE=100*mean(s_spread,s_depth,s_slippage,s_notional,s_coverage,s_agreement)` with equal component weights and section 3 missing coverage, not an unqualified mean over arbitrary available inputs. Also publish H/X ranks of these raw metrics; the absolute eligibility transforms deliberately prevent a uniformly poor universe from scoring as execution-quality merely because one asset is relatively better. Native notional hygiene/reliability is a precondition for s_notional; self-reported exchange volume cannot stand in for it.

Use configurable hard eligibility gates before ranking pre-expansion candidates: freshness, minimum native notional, maximum spread, minimum observed depth when required, and adequate history. Proposed starting values retain the current $5m native daily volume, 30 bps spread and $50k observed depth for the execution-quality variant (`config.py:11`); a partial discovery-only variant is separately labelled. Suspicious self-reported volume, venue disagreement or implausible trades are quality flags, not invented manipulation diagnoses. Low turnover or low float alone earns no liquidity-quality bonus.

`TOO_LATE` is a separate timing flag paired with stage `OVEREXTENDED`. Candidate conditions: one completed candle return >=30%; positive EMA20 distance >=3 ATR; very high RSI with AVWAP displacement; or volume climax plus parabolic OI/extreme normalized funding. An isolated high RSI is context, not sufficient proof. Signed funding is normalized to an explicit settlement interval; reliable derivatives are optional context. Preserve early discovery records even when the asset later becomes too late. Missing derivatives means unknown leverage risk, not safe leverage.

## 12. Composite scores: explicit candidate formulas

Normalize component values to [0,1] with declared H/X direction or the deterministic transforms above. Collapse correlated members into families before weighting. Do not count the same resistance tests separately as absorption, pressure and an archetype bonus. The following weights are fixed provisional research settings and require walk-forward evidence before production alerts.

| Stored Energy family | Weight | Inputs / causal qualification |
| --- | ---: | --- |
| Volatility compression | .20 | ATR fraction, BB width, RV, candle ranges; mean of four favorable percentiles |
| Volume contraction | .10 | Spot baseline contraction, turnover contraction, observed sell-volume decay |
| Structural pressure/absorption | .20 | Mean favorable percentile of settled rejection decay, return-time improvement, repeated-test intensity and rising settled lows |
| Price acceptance | .10 | Mean upper occupancy, midpoint retention and favorable percentile of their equal-window improvement |
| Trend compression | .10 | EMA10/20/50 convergence and slope improvement; 100/200 reclaim optional variant |
| Relative strength | .10 | BTC/ETH/sector/universe RS family mean; unavailable benchmark mask |
| VWAP structure | .10 | Section 10, daily/session deduplicated |
| Profile structure | .10 | Grid-valid POC migration, VAH acceptance, measured overhead mass |

`STORED_ENERGY_SCORE=100*weighted_mean(families)` with the section 3 coverage/interval contract. The standalone absorption and range-pressure scores remain explanatory summaries; their raw leaves feed the table once, rather than adding both summaries as extra points. Compression plus positive acceptance can score before any abnormal spot volume; ignition is a subsequent observation. Mandatory LOADED families are valid volatility compression, known local/multitimeframe structure, reliable spot price-volume and valid liquidity eligibility. No archetype or catalyst is mandatory.

Trigger sensitivity separates structural proxies from observed order-book information:

| Trigger family | Weight | Inputs |
| --- | ---: | --- |
| Resistance fragility | .25 | Settled rejection decay, test intensity, small positive distance to frozen resistance |
| Overhead volume | .20 | Covered LVN width, low overhead mass, absence of immediate HVN within observed range |
| Supply pressure | .15 | Observed sell decay, shallow settled lows, turnover contraction with preserved liquidity |
| VWAP structure | .10 | Support/reclaim beneath resistance, limited overhead AVWAP rejection |
| Relative demand | .10 | Increasing spot demand efficiency and RS; no large vertical-candle reward |
| Observed book mobility | .10 | Fresh ask notional to +1% / typical incoming spot notional, native depth/slippage |
| Derivative asymmetry | .10 | Reliable short positioning, OI structure, nonextreme/negative funding; no assumed shorts |

`TRIGGER_SENSITIVITY_SCORE` obeys the same coverage contract. A missing book or derivatives leaves a broad information interval and explicitly `proxy_only=true`; display the conservative structural core separately. Thin historical profile volume must never populate observed book mobility. Small cap/float is contextual, not a free score boost; dated issuance/unlock records may add a separately validated supply-risk feature. A thin book below acceptable execution liquidity is rejected rather than rewarded.

For observed book mobility, define mobility_ratio=current fresh ask USD notional from best ask through mid*1.01 divided by the median native USD-converted spot quote notional of the **20 preceding completed 5m candles** from the same venue/instrument. Exclude the current candle, require positive denominator, all 20 actual observations, valid FX and the liquidity-quality gate. Normalize by favorable P(mobility_ratio,direction=-1): relatively little observed ask depth compared with normal five-minute demand indicates greater potential mobility. It is a bounded-book snapshot proxy, not proof orders will remain. A failed liquidity gate makes the asset ineligible, rather than awarding points for vanishing liquidity.

The derivative-asymmetry family is **inactive/null in the initial v2 formula**; its .10 table weight is a reserved, explicitly missing leaf weight, not earned points. Funding and OI remain observed risk/explanation fields. A future active variant requires a preregistered definition of the observed trader/venue cohort, verified native position units and coverage, exact windows and normalization/weights, then independent validation. OI cannot reveal net market shorts because every matched contract has two sides; account long/short ratios do not establish position-notional imbalance. No inferred global-short or squeeze score enters the initial composite. Activating this family changes formula_version and invalidates old/new rank comparison until compatible recomputation exists.

`CATALYST_SENSITIVITY_SCORE` is a clearly labelled structural derivative: 0.5*Stored Energy + 0.5*Trigger Sensitivity when both are eligible. It supplies explanatory context only and adds no additional independent points to discovery. Verified dated adoption/inflows/catalysts remain external observations from the existing engine, not requirements and not predictions of future news.

Use two sequential score passes. `DEEP_BASE_SCORE` combines Stored Energy, Trigger Sensitivity, Relative Strength and Anomaly in proportions .30:.25:.20:.15, normalized to total weight .90, and excludes migration. Its cohort and coverage are labelled separately from the broad base. Then `DISCOVERY_SCORE`, the final deep-priority estimate, is the candidate weighted mean .30 Stored Energy, .25 Trigger Sensitivity, .20 Relative Strength, .15 Anomaly, .10 comparable **broad nonmigration base-rank** Migration. Rank Migration is the mean favorable percentile of velocity and acceleration computed using section 5; it is null until comparable raw history exists. Apply the missing-leaf contract, rather than requiring a bootstrapping asset to already have a migration bonus. Final priority cannot define the base ranks that generate its own migration component. Reject or separately report liquidity-ineligible candidates. **No undefined numeric late penalty applies:** preserve raw scores/ranks, but `TOO_LATE=true` hard-excludes the asset from pre-move promotion, LOADED eligibility and pre-move alert lists, while retaining `OVEREXTENDED` audit/post-move monitoring. Record this timing rejection alongside the raw broad base, so changed eligibility cannot masquerade as a changed feature score. Do not manufacture predictive probability from the discovery score. Archetype labels run afterwards and contribute zero to this ranking version.

`EXPANSION_SCORE_V2` is an explanatory alias of `DISCOVERY_SCORE`, not an additional score or term. Preserve the legacy `expansion_score` field unchanged. The candidate discovery formula intentionally shares some families with Stored Energy and Trigger Sensitivity; its leaf-weight registry must publish those effective combined weights. Do not claim these correlated composite votes constitute independent confirmations. Independent-family gates use source family IDs, not the number of scores above threshold.

All nested scores propagate component missing masks. Publish deep-feature cohort constraints. A watchlist-only percentile cannot claim top-1000 percentile status. A high LOADED score with dangerous BTC context remains a structural observation with an explicit regime warning; automatic order eligibility stays separate.

## 13. Independent timeframe states and stage eligibility

Maintain independent 5m/15m/1h/4h/1D/1W states plus 1m micro-event monitoring. Each requires only the inputs for its local rule; no universal 21-day or 200-EMA prerequisite. Global stage and per-frame stages share versioned evidence, but higher timeframe alignment is context rather than unanimity.

Proposed generic score thresholds: RADAR anomaly>=60 or migration percentile>=90%; WATCH discovery>=65 and >=2 independent families>=60; PRE-IGNITION stored>=65 and pressure>=60; LOADED discovery>=75, stored>=75, trigger>=70, >=3 independent families>=65 and >=80% core coverage. These are research defaults, not validated alert settings. No positive future return or SAND/BEAM identity may enter these conditions.

LOADED additionally requires a causally registered resistance zone, `0<=distance_resistance_atr<=2`, no accepted breakout and `TOO_LATE=false`. An otherwise strong structure farther from resistance remains WATCH/PRE-IGNITION under this initial variant. Do not force historical controls into this condition by choosing their future breakout level.

The canonical proposed IGNITION predicate in `ARCHITECTURE_V2.md` is native closed-5m RVOL>=2 AND (VOLATILITY_AWAKENING_SCORE>=65 OR an accepted micro AVWAP/VWAP reclaim or resistance break) AND EMA20_extension_atr=(close-EMA20)/ATR14<=2, with valid liquidity/data and no TOO_LATE condition. The volume gate cannot be bypassed by the OR. If the micro branch is unavailable, that branch is unknown rather than true. Other 1m/15m or event-driven ignition predicates are separate research variants with their own formula versions and validation; they do not silently alter the canonical rule. BREAKOUT requires two closed 15m accepts above the episode's frozen resistance+0.1 ATR buffer as specified in the architecture, RETEST an observable return to that known zone with acceptance. CONFIRMED_EXPANSION is subsequently measured follow-through; OVEREXTENDED takes precedence over claiming an early setup. FAILED_BREAKOUT requires an observed break and subsequent loss; INVALIDATED requires a frozen support/structural invalidation. Other assets remain DORMANT. `DATA_INCOMPLETE` is separate status, not fabricated DORMANT evidence.

The explicit transition, expiration, hysteresis and alert rule matrix belongs to `ARCHITECTURE_V2.md`. Store both rule changes and all counterevidence; no stage is permanent. Score crossing can promote an unknown asset automatically. Rank improvement alone can justify RADAR, but cannot create a LOADED trade.

## 14. Outcomes, calibration and candidate-feature retention

Prediction inputs are frozen at t. The requested **within-horizon** probability target is `Y_within_(a,h)=1[max(eligible observed closes in (t,t+h])/close_t-1>a]`, on a preregistered close-sampling interval, with complete actual coverage and full horizon maturity. It uses completed observed closes, not candle-high wicks. An early positive threshold hit still remains pending for the calibration dataset until the entire horizon matures, preserving identical observation gates for positive and negative cases. Sampling interval, availability rules and reference-price method are part of label_version and the displayed probability description.

Keep `Y_endpoint_(a,h)=1[close_(t+h)/close_t-1>a]` as a **different** outcome using aligned actual endpoint prices. Endpoint returns remain the default information-coefficient target. Path-high excursion is a third differently labelled target and must not be substituted for observed-close-within outcomes. Incomplete horizons are censored, never negative. Research additionally records first threshold hit, MAE, MFE, costs, and episode drawdown with declared sampling; label data cannot enter earlier feature snapshots.

Candidate calibrated outputs are P(observed-close expansion >10% **within** 24h), >20% within 72h and >30% within 7d, each with its own label/calibration version and sampling disclosure. Separate significant-mover labels use configurable +15%/24h, +25%/72h and +40%/7d, with endpoint/within/path rules specified independently. Misses, false positives and lead times use the same preregistered earliest generic event onset and immutable pre-event alerts; an alert after the vertical onset but before threshold crossing is not a pre-move success.

Train candidate probability mapping only on chronologically prior observations, with purged overlap between maximum seven-day labels, embargo, asset/event grouping and untouched later validation. Calibration mapping and sample cutoffs are model-versioned. Proposed minimum display gate: >=1000 matured out-of-sample predictions, >=100 positive and >=100 negative labels, >=3 disjoint temporal validation blocks, and >=50 observations per displayed reliability bin. These are minimum evidence hypotheses, not proof of calibration. Report effective independent event count and clustered uncertainty; correlated minute alerts cannot fill the gate.

Only show probabilities after the gate and predeclared stability checks pass. Otherwise probability=null with `UNCALIBRATED`. Evaluate Brier score `mean((p-y)^2)`, reliability-bin predicted versus observed rate with intervals, precision/recall at frozen alert thresholds, PR-AUC, prevalence and cost-aware expected value. A 0-100 heuristic score divided by 100 is never a probability. Publish metrics by regime, liquidity tier and timeframe; unsupported subgroups cannot inherit certainty from a pooled model.

Provisional display-quality requirements are positive out-of-sample Brier skill against the lagged-prevalence baseline with a clustered 95% interval excluding zero, reliability-bin interval width <=0.20, and no material deterioration across the supported validation blocks. Freeze the actual reliability criterion, bins and bootstrap procedure before validation. These gates may legitimately withhold every probability; enough rows alone never establishes calibrated forecasts.

Candidate-feature retention requires purged walk-forward improvement against the frozen baseline and matched non-expansion controls: Spearman information coefficient on future endpoint returns, delta precision, delta recall, false-positive rate, lead time, expected value and drawdown. Confidence intervals cluster asset/event/day dependencies; apply a predeclared multiple-testing correction to feature discovery. Require stability across supported regimes and venues; do not retain a feature because it explains a famous example. SAND/BEAM remain holdout cases, with no asset-specific rules or weight adjustments.

Additional candidates include compression duration, rejection-test frequency, volume/turnover slope, POC migration, AVWAP distance, observed book imbalance, aggressor CVD, native trade-size distribution, lagged market-cap percentile, sector breadth and rolling BTC-return correlation change. Correlation change uses matched return arrays from separate completed windows; constant-series variance yields null. These candidates remain research-only until source availability and out-of-sample improvement are demonstrated.

## 15. Required formula-level checks

Tests must verify known mathematical examples and economically meaningful counterexamples, not merely mirror implementation. Keep future-prefix invariance, warmup/zero-denominator behavior, chronology checks and all AVWAP discovery-time cases. Add equal-score ties, changing-universe rank migration, irregular scan cadence, unequal benchmark endpoints, absent sector/TOTAL3, CVD unit mismatches, missing aggressor fields, partial-bar finalization, censored rejection episodes, frozen resistance, rolling-profile expiration, fixed-grid POC migration and empty/unobserved overhead bands.

Integration must demonstrate an unknown asset promoted by broad evidence without archetypes or manual inclusion; a vertical mover penalized as late; data-incomplete assets visible in coverage; and every scan/transition/alert reproducible from immutable inputs and formula versions. Synthetic fixtures validate mathematics only. Neither these tests nor this document prove that SAND or BEAM was detectable before an unspecified historical move.
