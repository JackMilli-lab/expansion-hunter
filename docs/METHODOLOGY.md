# Trading Cockpit — Expansion Hunter: methodology and limitations

## What a score means

Expansion Hunter ranks observable conditions associated with compression, supply absorption, and early expansion. QNT, ZEC, TAO, and ONDO are names for research hypotheses, not trained historical event labels. The engine does not claim that a high-scoring asset will repeat any of those assets' returns. A score is an uncalibrated weighted sum of rules; confidence measures usable evidence and coverage, not the probability of profit. Hybrid matches share evidence and are therefore correlated. Scores must not be interpreted as independent confirmations or multiplied into a probability.

A watchlist is a decision aid. It is not an exchange execution system, a guarantee of entry fills, or a verified profitable strategy. Thresholds are explicit heuristics that require out-of-sample study before capital allocation. The preferred sequence is compression → accumulation/absorption → ignition → breakout → retest → expansion. Classification describes the latest observable state; it does not assert that an asset visited every earlier state in order.

## Point-in-time contract

Every evaluation has one UTC `as_of` cut-off. Bars can be used only after their close and only if their `available_at` is at or before this cut-off. The unfinished latest candle is excluded. A live scan requests candle history through its scan-start cut-off, then finalizes its knowledge cut-off after collection so fresh metadata can be used. These two times are disclosed; slower scans can leave fast-timeframe candles stale and must not imply a simultaneous market snapshot. Numeric data carries an observation time and an availability time; observations from the future, values not yet available, stale values, and unreliable values are unusable. Missing data remains missing with a reason. An unavailable field is never replaced by a fabricated price, zero funding, neutral positioning, or positive narrative evidence.

An event timestamp says when something occurred. An availability timestamp says when the system could first know it. These are different, especially for macro statistics, catalysts, revised supply, market capitalization, and API data collected after the fact. Historical evaluation must replay persisted point-in-time snapshots. Downloading today's metadata and attaching it to older candles is prohibited. Backdated hand-written availability dates do not establish historical provenance.

Exchange candle archives usually disclose a close time but not the original publication latency or subsequent revisions. Their close times allow causal closed-bar calculations, but an archive downloaded today is not proof of precisely what an operator saw at the original close. Results from archive-only studies must disclose this limitation. Persist ingestion timestamps and revisions when collecting a forward archive.

## Causal technical calculations

Repeated live scans preserve the original availability of an unchanged candle from the latest completed archive. Corrected values receive a new timestamp; the original version remains in its immutable snapshot. This allows known anchors to accept later closes without repeatedly resetting discovery to the latest download time. A first download still cannot manufacture acceptance that occurred before ingestion.

Indicators use trailing or recursively accumulated observations only. Warm-up periods stay unavailable until their minimum history exists. Multi-timeframe confirmation uses each timeframe's latest *closed* bar at the common cut-off, never a partially formed daily or weekly candle. No centered rolling windows, backward filling from future observations, or full-sample normalization may affect earlier output.

A swing is confirmed only after its configured number of right-side bars have closed. Its origin can be in the past, but the swing cannot contribute to a signal before confirmation. Support/resistance is constructed from previously known levels; breakout tests must compare with a boundary established before the tested breakout candle. An all-time high is a true lifetime high only when trustworthy full listing history is available. A maximum inside the downloaded window must be labeled as such. A 52-week reference requires a complete calendar-length history, not 52 arbitrary bars.

Relative volume compares the current closed bar with a trailing baseline that excludes that bar. Compression and volume-contraction rules use prior windows to avoid normalizing away the observation being tested. Realized volatility has a declared timeframe and annualization convention; crypto trades continuously, so calendar conventions matter. EMA clustering and ATR-normalized distance are scale-free proxies, not evidence of participant intent.

## Anchored VWAP

Automatic anchors include confirmed major swing highs/lows, known breakout origins, completed capitulation or exceptional-volume candles, and range origins. Every anchor has an origin time and a discovery/confirmation time. An anchor cannot influence a historical decision before discovery, even though its cumulative VWAP subsequently starts from the earlier origin.

AVWAP is computed from available completed candles and their volume. Native quote/base volume yields an exchange traded-volume average when supplied for the interval; otherwise typical-price OHLCV is an approximation. Each anchor reports whether its inputs are native, estimated, or mixed. Zero cumulative volume leaves VWAP unavailable. Reclaim requires a configured sequence of closed-bar acceptance; a wick alone is insufficient. Support, resistance, rejection, and confluence are heuristic distance/acceptance tests. They do not identify an actual participant's cost basis.

## Price, volume, and microstructure

Volume is venue-specific. A higher close with stronger spot volume is a demand proxy; a weak advance on declining volume is a warning. Repeated resistance tests with higher lows and smaller pullbacks can indicate supply absorption. Flat price with aggressive selling that fails to drive lower is an absorption proxy when valid taker-side measurements exist. OHLCV alone does not establish who accumulated inventory, whether the trades were organic, or whether sell-side liquidity was exhausted.

Taker-buy base volume must use the same units and bar interval as total base volume. It estimates aggressive buying, not all buying, net capital inflows, or holder accumulation. Futures turnover exceeding spot turnover is a leverage warning only when intervals, quote units, and market coverage are comparable. A USD OI increase partly reflects price changes; native quantity OI better separates changing exposure from mark-price effects. Neither rising OI nor funding establishes imminent liquidation by itself.

Candle volume profiles distribute volume across a candle's observed range by a stated approximation. Their POC, VAH, VAL, HVN, and LVN levels are useful coarse references, not a reconstruction of executed trade volume at each price. A claimed liquidity gap above resistance is at best a historical traded-volume proxy. Executable overhead supply requires fresh order-book data and remains sensitive to cancellation and spoofing.

## Market and derivatives data

The initial exchange adapter covers supported USDT markets on its configured venue. USD market-cap metadata and USDT execution quotes are only approximate comparables; material quote-asset depegs require an additional conversion/risk gate before live use. Account equity and liquidity limits must use a consistent quote currency; USD labels must not silently convert USDT balances at an assumed peg. Market-cap rank comes from a separate provider. Tokens lacking a reliable asset-to-exchange mapping are not silently matched by ticker. Coin symbols collide and can change. Wrapped assets, stablecoins, rebases, migrated contracts, quote currencies, and corporate token events require deliberate universe policy.

Spread and depth are a current snapshot of one venue and do not measure consolidated global liquidity. A tight displayed spread does not establish affordable execution at the planned size. Order books can be cancelled, API snapshots can be delayed, and fees vary by account. Volume participation and price-impact caps are conservative screening assumptions; actual fills, slippage, queue position, exchange limits, and stop behavior are not simulated by OHLCV.

Funding cadence varies by instrument and can change. Settled funding, a current predicted funding rate, and an annualized rate must not be interchanged. OI comparisons require explicit cadence and units. Positioning ratios are samples of a provider's population, not market-wide net long/short exposure. Liquidations reported by a public stream may be incomplete; liquidation clusters require a dedicated reliable source. If no such source exists, the cluster input remains unavailable.

BTC dominance and total market capitalization inherit constituent and supply-estimation errors. TOTAL2/TOTAL3 derived from a provider are labeled provider-defined approximations. ETH/BTC should use aligned observations and preferably a native cross pair. Stablecoin market-cap growth is not automatically deployable exchange liquidity. Missing regime inputs lower confidence and constrain risk; absence of reported leverage stress cannot prove low stress.

## Narrative and structural catalysts

TAO- and ONDO-style structural evidence comes from timestamped, attributable sources on actual usage, adoption, supply, product growth, capital inflows, infrastructure, or institutional activity. A social post or marketing announcement alone contributes no verified-adoption score. A partnership does not establish live integration, material revenue, or token value capture.

Manual catalyst entries need source URLs, event and availability timestamps, verification status, and measurable evidence where required by a rule. The engine checks the declared fields; it does not independently audit their truth. Historical replay must use the original public disclosure and original availability, not a later article explaining a past rally. Missing qualitative data does not become a neutral-positive narrative score.

## Regime and risk interpretation

BTC trend, volatility, spot demand, VWAP structure, derivatives stress, dominance, ETH/BTC, aggregate capitalization, and alt breadth can disagree. A regime label is a rule-based summary with explicit missing inputs, not a macro forecast. Risk-off and possible shakeout states justify smaller risk or no new entry. Relative strength during weakness can improve the watchlist score but never bypass liquidity, data quality, stop-distance, cost, or portfolio constraints.

A theoretical entry is available only after observable acceptance and risk gates. Stops are invalidation references, not guaranteed execution prices. Reward/risk must account for estimated fees and slippage and use a defensible target rather than manufacturing upside to pass a threshold. Suggested position size is bounded by account risk, exposure, liquidity participation, and reported portfolio/daily-loss limits. A fresh native order-book execution reference must agree with the closed-bar setup within the permitted ATR displacement; a move since the last candle close can block entry even when the old candle still looked compressed. Without accurate account state those controls cannot protect a real portfolio.

The engine does not route orders, manage exchange credentials, reconcile positions, or implement an exchange-native kill switch. Portfolio exposure and daily loss must come from trusted account state before live use. It is unsafe to treat default or absent account state as proof of spare risk capacity.

## Research standards and production boundaries

No-look-ahead behavior must be checked through prefix-invariance tests: evaluating a snapshot at a past cut-off must produce the same historical result after future candles or future-unavailable metadata are appended. Tests should separately cover bar closure, swing confirmation, anchor confirmation, stale/missing data, provider failures, same-symbol collisions, and risk blocks.

A strategy study needs a historical point-in-time universe that includes delisted assets; today's top 500 creates survivorship and selection bias. Evaluate untouched chronological holdouts or walk-forward folds, purge overlapping labels when applicable, include fees and conservative slippage, and disclose parameter searches. Close-bar signals cannot fill retroactively at that bar's close without a justified execution model. Intrabar stop/target ordering is unknown in OHLCV and must use a conservative policy or finer data. Do not report edge, hit rate, or expected return before conducting such a study.

The delivered application provides typed contracts, modular providers, auditable rules, persistence, API/CLI access, and tested causal calculation paths. These are production-oriented foundations. They do not establish operational readiness for unattended capital deployment. That requires validated strategy performance, deployment-specific load/rate-limit testing, source continuity, monitoring and alerting, hardened access control where exposed, account reconciliation, redundant recovery, and exchange execution controls. Each unavailable capability should be reported directly, with no invented market data or implied historical validation.
