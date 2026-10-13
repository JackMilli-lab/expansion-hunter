"""Local fast ignition evaluation, independent of broad discovery/network I/O.

Five-minute resistance is fixed before a one-minute trigger. Closed price is
the confirmation condition; volume/flow only describe quality. Every candidate
level and its discovery time are archived. These rules are hypotheses, not a
probability model. The monitor performs no trading or notification delivery.
"""

from __future__ import annotations

import hashlib
import math
from datetime import datetime, timedelta
from statistics import median, pstdev

from ._numeric import float_mean as mean
from .fast_models import (
    FastEvaluation,
    FastEvent,
    FastResistance,
    FastSettings,
    FastStage,
    FastState,
)
from .models import TIMEFRAMES, Asset, AssetSnapshot, Candle, aware
from .technical import confirmed_swings

FROZEN = {
    FastStage.BREAKOUT_ATTEMPT,
    FastStage.BREAKOUT_CONFIRMED,
    FastStage.ACCEPTED,
    FastStage.EXPANSION_CONFIRMED,
    FastStage.BREAKDOWN_ATTEMPT,
    FastStage.BREAKDOWN_CONFIRMED,
    FastStage.ACCEPTED_DOWN,
    FastStage.EXPANSION_CONFIRMED_DOWN,
}
PREBREAK = {FastStage.WATCH, FastStage.PRE_IGNITION, FastStage.LOADED,
            FastStage.BREAKOUT_PRESSURE, FastStage.BREAKDOWN_PRESSURE}


def _ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator > 0 else None


def _clip(value: float) -> float:
    return max(0.0, min(1.0, value))


def _atr(bars: list[Candle], period: int) -> float | None:
    if len(bars) <= period:
        return None
    # Only the final period contributes. Computing every prior true range was
    # costly when structure analysis repeatedly queried confirmed-pivot ATRs.
    relevant = bars[-period - 1:] if period > 0 else bars
    values = [
        max(bar.high - bar.low, abs(bar.high - previous.close), abs(bar.low - previous.close))
        for previous, bar in zip(relevant[:-1], relevant[1:], strict=True)
    ]
    result = mean(values[-period:])
    return result if result > 0 else None


def _vwap(bars: list[Candle]) -> float | None:
    volume = sum(bar.volume for bar in bars)
    return sum((bar.high + bar.low + bar.close) / 3 * bar.volume for bar in bars) / volume if volume else None


def _metrics(bars: list[Candle], level: float, atr: float) -> dict[str, float | None]:
    last = bars[-1]
    baseline = mean(bar.volume for bar in bars[-21:-1]) if len(bars) >= 21 else None
    candle_range = last.high - last.low
    recent = bars[-6:]
    prior = bars[-18:-6]
    ranges = [bar.high - bar.low for bar in recent]
    baseline_ranges = [bar.high - bar.low for bar in prior]
    baseline_range = mean(baseline_ranges) if baseline_ranges else None
    prior_atr = _atr(bars[:-6], 14)
    returns = [(b.close / a.close) - 1 for a, b in zip(bars[:-1], bars[1:], strict=True)]
    recent_volatility = pstdev(returns[-6:]) if len(returns) >= 6 else None
    prior_volatility = pstdev(returns[-18:-6]) if len(returns) >= 18 else None
    recent_volume = mean(bar.volume for bar in bars[-3:])
    prior_volume = mean(bar.volume for bar in bars[-6:-3]) if len(bars) >= 6 else None
    vwap = _vwap([bar for bar in bars if bar.open_time.date() == last.open_time.date()])
    lows = [bar.low for bar in recent]
    return {
        "distance_to_resistance_pct": (level - last.close) / last.close * 100,
        "distance_to_resistance_atr": (level - last.close) / atr,
        "relative_volume": _ratio(last.volume, baseline) if baseline is not None else None,
        "volume_acceleration": (_ratio(recent_volume, prior_volume) - 1) if prior_volume else None,
        "range_compression": 1 - _clip(mean(ranges) / baseline_range)
        if baseline_range
        else None,
        "atr_compression": 1 - _clip(atr / prior_atr) if prior_atr else None,
        "volatility_compression": 1 - _clip(recent_volatility / prior_volatility)
        if prior_volatility and recent_volatility is not None
        else None,
        "higher_low_strength": _clip((mean(lows[-3:]) - mean(lows[:3])) / atr),
        "body_to_range_ratio": abs(last.close - last.open) / candle_range if candle_range else 0.0,
        "upper_wick_rejection": (last.high - max(last.open, last.close)) / candle_range
        if candle_range
        else 0.0,
        "close_location": (last.close - last.low) / candle_range if candle_range else 0.5,
        "price_velocity": (last.close / bars[-4].close - 1) / 3 if len(bars) >= 4 else None,
        "VWAP": vwap,
        "VWAP_distance_atr": (last.close - vwap) / atr if vwap is not None else None,
        "AVWAP_distance_atr": None,
        "taker_buy_ratio": last.taker_buy_volume / last.volume
        if last.taker_buy_volume is not None and last.volume
        else None,
    }


def detect_resistance(bars: list[Candle], settings: FastSettings, as_of: datetime) -> FastResistance | None:
    """Use only completed, available 5m bars; exclude the latest bar from anchors."""
    usable = [bar for bar in bars if bar.close_time <= as_of and bar.available_at <= as_of]
    usable = sorted(usable, key=lambda bar: bar.close_time)[-settings.resistance_lookback :]
    if len(usable) < 20:
        return None
    atr = _atr(usable, settings.atr_period)
    if atr is None:
        return None
    prior = usable[:-1]
    swings = confirmed_swings(prior, settings.swing_left, settings.swing_right)
    candidates = [
        (swing["price"], prior[swing["index"]].close_time, "CONFIRMED_SWING_HIGH")
        for swing in swings
        if swing["kind"] == "high"
    ]
    range_bar = max(prior, key=lambda bar: bar.high)
    candidates.append((range_bar.high, range_bar.close_time, "PRIOR_RANGE_HIGH"))
    tolerance = atr * settings.zone_atr
    # Equal-high plateaus need not be strict pivots: cluster prices causally.
    groups: list[list[Candle]] = []
    for bar in sorted(prior, key=lambda value: value.high):
        if groups and abs(bar.high - mean(item.high for item in groups[-1])) <= tolerance:
            groups[-1].append(bar)
        else:
            groups.append([bar])
    for group in groups:
        if len(group) >= settings.minimum_tests:
            candidates.append(
                (
                    median(bar.high for bar in group),
                    max(bar.close_time for bar in group),
                    "REPEATED_LOCAL_HIGHS",
                )
            )
    price = usable[-1].close
    candidates = [
        item
        for item in candidates
        if item[0] >= price and (item[0] - price) / atr <= settings.proximity_atr * 3
    ]
    if not candidates:
        return None
    # Nearest valid overhead structure; test count breaks exact ties.
    level, event_at, kind = min(
        candidates,
        key=lambda item: (item[0] - price, -sum(abs(bar.high - item[0]) <= tolerance for bar in prior)),
    )
    tests = sum(abs(bar.high - level) <= tolerance and bar.close <= level for bar in prior)
    return FastResistance(
        price=level, lower=level - tolerance, upper=level + tolerance,
        event_timestamp=event_at, discovered_at=as_of, atr=atr, kind=kind, tests=tests,
        reason=f"{kind}: available 5m structure, latest bar excluded from anchor selection",
    )


def detect_support(bars: list[Candle], settings: FastSettings, as_of: datetime) -> FastResistance | None:
    """Symmetric support anchors, known before a later downward trigger.

    The generic level contract retains its legacy type name for compatibility;
    SHORT objects are stored as support, never described as resistance.
    """
    usable = sorted((bar for bar in bars if bar.close_time <= as_of and bar.available_at <= as_of),
                    key=lambda bar: bar.close_time)[-settings.resistance_lookback:]
    if len(usable) < 20:
        return None
    atr = _atr(usable, settings.atr_period)
    if atr is None:
        return None
    prior = usable[:-1]
    swings = confirmed_swings(prior, settings.swing_left, settings.swing_right)
    candidates = [(swing["price"], prior[swing["index"]].close_time, "CONFIRMED_SWING_LOW")
                  for swing in swings if swing["kind"] == "low"]
    range_bar = min(prior, key=lambda bar: bar.low)
    candidates.append((range_bar.low, range_bar.close_time, "PRIOR_RANGE_LOW"))
    tolerance = atr * settings.zone_atr
    groups: list[list[Candle]] = []
    for bar in sorted(prior, key=lambda value: value.low):
        if groups and abs(bar.low - mean(item.low for item in groups[-1])) <= tolerance:
            groups[-1].append(bar)
        else:
            groups.append([bar])
    for group in groups:
        if len(group) >= settings.minimum_tests:
            candidates.append((median(bar.low for bar in group), max(bar.close_time for bar in group),
                               "REPEATED_LOCAL_LOWS"))
    price = usable[-1].close
    candidates = [item for item in candidates
                  if item[0] <= price and (price - item[0]) / atr <= settings.proximity_atr * 3]
    if not candidates:
        return None
    level, event_at, kind = min(candidates, key=lambda item: (
        price - item[0], -sum(abs(bar.low - item[0]) <= tolerance for bar in prior)))
    tests = sum(abs(bar.low - level) <= tolerance and bar.close >= level for bar in prior)
    return FastResistance(price=level, lower=max(level - tolerance, level * 1e-9), upper=level + tolerance,
                          event_timestamp=event_at, discovered_at=as_of, atr=atr, kind=kind, tests=tests,
                          reason=f"{kind}: available 5m support, latest bar excluded from anchor selection")


def _directional_metrics(bars: list[Candle], level: float, atr: float, bias: str):
    metrics = _metrics(bars, level, atr)
    if bias == "SHORT":
        last = bars[-1]
        metrics.pop("distance_to_resistance_pct")
        metrics.pop("distance_to_resistance_atr")
        metrics.pop("higher_low_strength")
        metrics.pop("upper_wick_rejection")
        candle_range = last.high - last.low
        highs = [bar.high for bar in bars[-6:]]
        metrics.update({
            "distance_to_support_pct": (last.close - level) / last.close * 100,
            "distance_to_support_atr": (last.close - level) / atr,
            "lower_high_strength": _clip((mean(highs[:3]) - mean(highs[-3:])) / atr),
            "lower_wick_rejection": (min(last.open, last.close) - last.low) / candle_range
                                      if candle_range else 0.0,
            "taker_sell_ratio": 1 - metrics["taker_buy_ratio"]
                                if metrics["taker_buy_ratio"] is not None else None,
        })
    return metrics


def pressure_metrics(
    bars: list[Candle], resistance: FastResistance, settings: FastSettings, *, bias: str = "LONG"
) -> dict[str, float | str | bool | None]:
    metrics = _directional_metrics(bars, resistance.price, resistance.atr, bias)
    short = bias == "SHORT"
    tolerance = resistance.atr * settings.zone_atr
    tests = [
        index
        for index, bar in enumerate(bars[:-1])
        if abs((bar.low if short else bar.high) - resistance.price) <= tolerance
        and (bar.close >= resistance.price if short else bar.close <= resistance.price)
    ]
    # Consecutive candles at a level are one attack, not repeated independent tests.
    attacks = [
        index for position, index in enumerate(tests) if position == 0 or index > tests[position - 1] + 1
    ]
    rejections = []
    for index in attacks:
        later = bars[index + 1 : min(index + 5, len(bars))]
        if len(later) >= 2:
            rejection = max(bar.high for bar in later) - resistance.price if short else resistance.price - min(bar.low for bar in later)
            rejections.append(max(0, rejection) / resistance.atr)
    weakening = (
        max(0, min(1, 1 - rejections[-1] / rejections[0]))
        if len(rejections) >= 2 and rejections[0] > 0
        else None
    )
    metrics.update(
        {
            "support_test_count" if short else "resistance_test_count": len(attacks),
            "time_since_last_test_seconds": (bars[-1].close_time - bars[tests[-1]].close_time).total_seconds()
            if tests
            else None,
            "rejection_weakening": weakening,
            "volume_profile_context": "UNAVAILABLE",
            "liquidity_below_support" if short else "liquidity_above_resistance": None,
            "spread_bps": None,
            "book_depth": None,
            "book_imbalance": None,
        }
    )
    components = {
        "proximity": (0.25, 1 - _clip(metrics["distance_to_support_atr" if short else "distance_to_resistance_atr"] / settings.proximity_atr)),
        "tests": (0.20, _clip(len(attacks) / max(2, settings.minimum_tests))),
        "lower_highs" if short else "higher_lows": (0.15, metrics["lower_high_strength" if short else "higher_low_strength"]),
        "compression": (0.15, metrics["range_compression"]),
        "close_location": (0.10, 1 - metrics["close_location"] if short else metrics["close_location"]),
        "rejection_weakening": (0.10, weakening),
        "volume_awakening": (
            0.05,
            _clip(metrics["relative_volume"] / 2) if metrics["relative_volume"] is not None else None,
        ),
    }
    metrics["pressure_score"] = sum(
        weight * value * 100 for weight, value in components.values() if value is not None
    )
    metrics["pressure_coverage"] = sum(weight for weight, value in components.values() if value is not None)
    for name, (weight, value) in components.items():
        metrics[f"contribution_{name}"] = weight * value * 100 if value is not None else None
    return metrics


class FastIgnitionMonitor:
    """A bounded local state machine; persist ``export_states`` after evaluation.

    Restored state is STALE until fresh candles/trades arrive. The method makes
    no awaits or remote calls and is safe to run in an independent event worker.
    """

    def __init__(self, settings: FastSettings | None = None, states: list[FastState] | None = None):
        self.settings = settings or FastSettings()
        self.states: dict[str, FastState] = {}
        self.restore(states or [])

    def restore(self, states: list[FastState]) -> None:
        for state in states:
            restored = state.model_copy(deep=True)
            restored.stale = True
            restored.data_quality["spot_candles"] = "STALE"
            self.states[restored.asset.asset_id] = restored

    def export_states(self) -> list[FastState]:
        return [state.model_copy(deep=True) for state in self.states.values()]

    def promote(self, asset: Asset, now: datetime) -> FastState:
        """Explicit candidate membership starts a fresh episode after demotion.

        Old observations remain in the external immutable archive. Obsolete
        candles, cooldowns, frozen levels and dedupe tokens cannot contaminate
        this new live episode. Already active episodes retain their exact level.
        """
        now = aware(now)
        previous = self.states.get(asset.asset_id)
        if previous is None or previous.stage == FastStage.DEMOTED:
            if previous and previous.demoted_at and now < previous.demoted_at:
                raise ValueError("promotion cannot precede its known demotion")
            self.states[asset.asset_id] = FastState(asset=asset, promoted_at=now,
                bias_reason="Fresh monitoring episode; direction requires completed 5m structural evidence")
        return self.states[asset.asset_id]

    def _state(self, asset: Asset | str, now: datetime) -> FastState:
        if isinstance(asset, Asset):
            return self.promote(asset, now)
        asset = asset if isinstance(asset, Asset) else Asset(asset_id=asset, symbol=asset, name=asset)
        if asset.asset_id not in self.states:
            self.states[asset.asset_id] = FastState(asset=asset, promoted_at=now)
        return self.states[asset.asset_id]

    def _emit(
        self,
        state: FastState,
        stage: FastStage | str,
        price: float,
        event_at: datetime,
        received_at: datetime,
        now: datetime,
        reason: str,
        *,
        candle_close: datetime | None = None,
        quality: str = "UNCALIBRATED",
        metrics: dict | None = None,
    ) -> FastEvent | None:
        boundary = state.trigger_level
        level = f"{boundary.price:.12g}" if boundary else "none"
        # Transition identity survives process restart; no wall-clock random IDs.
        key = f"{state.asset.asset_id}|{stage}|{level}|{boundary.discovered_at.isoformat() if boundary else 'none'}"
        if state.bias == "SHORT":
            key += "|SHORT"
        if key in state.dedupe_keys:
            return None
        previous = state.stage
        if isinstance(stage, FastStage):
            state.stage = stage
        state.dedupe_keys = (state.dedupe_keys + [key])[-256:]
        return FastEvent(
            event_id=hashlib.sha256(key.encode()).hexdigest(),
            asset_id=state.asset.asset_id,
            symbol=state.asset.symbol,
            event_type=str(stage),
            previous_stage=previous,
            market_event_timestamp=event_at,
            candle_close_timestamp=candle_close,
            data_received_timestamp=received_at,
            evaluation_timestamp=now,
            alert_created_timestamp=now,
            detection_latency_ms=(now - event_at).total_seconds() * 1000,
            alert_latency_ms=(now - event_at).total_seconds() * 1000,
            processing_latency_ms=(now - received_at).total_seconds() * 1000,
            price=price,
            resistance=state.resistance.model_copy(deep=True) if state.resistance else None,
            support=state.support.model_copy(deep=True) if state.support else None,
            bias=state.bias,
            reason=reason,
            quality=quality,
            metrics=metrics or {},
            data_quality=dict(state.data_quality),
            regime=state.regime,
            macro_context=dict(state.macro_context),
        )

    def _append(self, events: list[FastEvent], event: FastEvent | None) -> None:
        if event:
            events.append(event)

    def _merge(self, state: FastState, snapshot: AssetSnapshot, now: datetime) -> list[str]:
        issues = []
        for frame in ("1m", "5m", "15m"):
            existing = {bar.close_time: bar for bar in state.candles.get(frame, [])}
            for bar in snapshot.candles.get(frame, []):
                if bar.close_time > now or bar.available_at > now:
                    issues.append(f"{frame}: future/unavailable candle excluded")
                    continue
                if (bar.close_time - bar.open_time).total_seconds() != TIMEFRAMES[frame]:
                    issues.append(f"{frame}: invalid candle duration")
                    continue
                previous = existing.get(bar.close_time)
                if previous and previous.model_dump(exclude={"available_at"}) != bar.model_dump(
                    exclude={"available_at"}
                ):
                    issues.append(f"{frame}: revision ignored for already archived closed candle")
                    continue
                if not previous:
                    existing[bar.close_time] = bar
            bars = sorted(existing.values(), key=lambda value: value.close_time)[
                -self.settings.history_bars :
            ]
            if any(
                (right.open_time != left.close_time) for left, right in zip(bars[:-1], bars[1:], strict=True)
            ):
                issues.append(f"{frame}: candle gap; corresponding evaluation suspended")
                state.data_quality[frame] = "UNAVAILABLE"
            else:
                state.data_quality[frame] = "AVAILABLE" if bars else "UNAVAILABLE"
            state.candles[frame] = bars
        return issues

    def evaluate(
        self,
        asset: Asset | str,
        snapshot: AssetSnapshot,
        *,
        evaluation_at: datetime | None = None,
        tick_size: float | None = None,
        regime: str | None = None,
        macro_context: dict | None = None,
    ) -> FastEvaluation:
        now = aware(evaluation_at or snapshot.as_of)
        if snapshot.as_of > now:
            raise ValueError("snapshot unavailable at evaluation")
        if tick_size is not None and (not math.isfinite(tick_size) or tick_size <= 0):
            raise ValueError("tick size must be finite and positive")
        state = self._state(asset, now)
        if state.last_evaluation_at and now < state.last_evaluation_at:
            return FastEvaluation(
                state=state.model_copy(deep=True), issues=["out-of-order evaluation ignored"]
            )
        issues = list(snapshot.issues) + self._merge(state, snapshot, now)
        state.last_evaluation_at = now
        state.regime = regime if regime is not None else state.regime
        state.macro_context = macro_context if macro_context is not None else state.macro_context
        state.data_quality.update(
            {
                "best_bid_ask": "AVAILABLE"
                if snapshot.market.get("spread_bps") and snapshot.market["spread_bps"].usable(now, 15)
                else "UNAVAILABLE",
                "order_book": "AVAILABLE"
                if snapshot.market.get("observed_depth_quote") and snapshot.market["observed_depth_quote"].usable(now, 15)
                else "UNAVAILABLE",
                "futures": "AVAILABLE"
                if any(value.usable(now, 300) for value in snapshot.derivatives.values())
                else "UNAVAILABLE",
                "CVD": "UNAVAILABLE",
            }
        )
        events: list[FastEvent] = []
        one = state.candles.get("1m", [])
        five = state.candles.get("5m", [])
        candle_fresh = bool(
            one
            and (now - one[-1].close_time).total_seconds() <= self.settings.candle_stale_seconds
            and state.data_quality.get("1m") == "AVAILABLE"
        )
        pressure_fresh = bool(
            five
            and (now - five[-1].close_time).total_seconds() <= self.settings.five_minute_stale_seconds
            and state.data_quality.get("5m") == "AVAILABLE"
        )
        state.stale = not candle_fresh
        state.data_quality["spot_candles"] = "AVAILABLE" if candle_fresh else "STALE"
        if state.bias == "SHORT" and not self.settings.short_enabled:
            state.stale = True
            return FastEvaluation(state=state.model_copy(deep=True), issues=issues + ["SHORT monitoring disabled"])
        if not candle_fresh:
            if one:
                self._append(
                    events,
                    self._emit(
                        state,
                        "DATA_STALE",
                        one[-1].close,
                        one[-1].close_time,
                        one[-1].available_at,
                        now,
                        "No fresh closed 1m candle; trigger evaluation suspended",
                    ),
                )
            return FastEvaluation(
                state=state.model_copy(deep=True),
                events=events,
                issues=issues + ["fresh 1m candle unavailable"],
            )
        state.price = one[-1].close
        state.last_received_at = max(bar.available_at for bar in one[-1:])
        if state.cooldown_until and now < state.cooldown_until:
            return FastEvaluation(state=state.model_copy(deep=True), issues=issues + ["cooldown active"])
        if state.stage in {FastStage.FAILED_BREAKOUT, FastStage.FAILED_BREAKDOWN,
                           FastStage.LIQUIDITY_SWEEP, FastStage.COOLDOWN}:
            state.stage = FastStage.WATCH
            state.resistance = None
            state.support = None
            state.confirmed_at = state.accepted_at = state.structural_at = state.attempt_at = None
            state.accepted_closes = state.structural_closes = 0
        # Process 1m against the PREVIOUS level before discovering any new 5m level.
        if state.trigger_level:
            for bar in one:
                if state.last_processed.get("1m") and bar.close_time <= state.last_processed["1m"]:
                    continue
                state.last_processed["1m"] = bar.close_time
                observed_causal_attempt = bool(
                    state.attempt_at and state.trigger_level.discovered_at <= state.attempt_at
                    and bar.open_time <= state.attempt_at < bar.close_time
                )
                if bar.open_time < state.trigger_level.discovered_at and not observed_causal_attempt:
                    continue
                if (now - bar.close_time).total_seconds() > self.settings.late_candle_seconds:
                    issues.append("1m: late historical close archived without real-time alert")
                    continue
                self._trigger(state, bar, now, tick_size, events)
        else:
            state.last_processed["1m"] = one[-1].close_time
        self._acceptance(state, "5m", now, events)
        self._acceptance(state, "15m", now, events)
        if pressure_fresh and state.stage in PREBREAK and len(five) >= 20:
            previous_five = state.last_processed.get("pressure")
            if previous_five is None or five[-1].close_time > previous_five:
                state.last_processed["pressure"] = five[-1].close_time
                candidate = detect_resistance(five, self.settings, now)
                long_metrics = pressure_metrics(five, candidate, self.settings) if candidate else None
                support = detect_support(five, self.settings, now) if self.settings.short_enabled else None
                short_metrics = pressure_metrics(five, support, self.settings, bias="SHORT") if support else None
                bias = "LONG"
                if (short_metrics and short_metrics["lower_high_strength"] >= self.settings.direction_min_structure_atr
                        and short_metrics["lower_high_strength"] > 0
                        and 0 <= short_metrics["distance_to_support_atr"] <= self.settings.proximity_atr
                        and short_metrics["support_test_count"] >= self.settings.minimum_tests
                        and short_metrics["pressure_score"] >= (
                            long_metrics["pressure_score"] + self.settings.direction_score_margin if long_metrics else 0
                        )):
                    candidate, bias = support, "SHORT"
                if candidate and state.bias != bias:
                    state.bias = bias
                    state.stage = FastStage.WATCH
                    state.resistance = state.support = None
                if candidate:
                    state.bias_reason = (
                        "Completed 5m lower highs, repeated support tests and stronger downside pressure"
                        if bias == "SHORT" else "Completed 5m overhead resistance context"
                    )
                boundary = state.trigger_level
                if candidate and (
                    boundary is None
                    or abs(candidate.price - boundary.price)
                    > boundary.atr * self.settings.resistance_change_atr
                ):
                    if state.bias == "SHORT":
                        state.support = candidate
                    else:
                        state.resistance = candidate
                if state.trigger_level:
                    short = state.bias == "SHORT"
                    metrics = pressure_metrics(five, state.trigger_level, self.settings, bias=state.bias)
                    state.pressure_metrics = metrics
                    distance = metrics["distance_to_support_atr" if short else "distance_to_resistance_atr"]
                    tests = metrics["support_test_count" if short else "resistance_test_count"]
                    score = metrics["pressure_score"]
                    near = 0 <= distance <= self.settings.proximity_atr
                    for stage, threshold in (
                        (FastStage.PRE_IGNITION, self.settings.pre_ignition_score),
                        (FastStage.LOADED, self.settings.loaded_score),
                        (FastStage.BREAKDOWN_PRESSURE if short else FastStage.BREAKOUT_PRESSURE, self.settings.pressure_score),
                    ):
                        if near and tests >= self.settings.minimum_tests and score >= threshold:
                            self._append(
                                events,
                                self._emit(
                                    state,
                                    stage,
                                    five[-1].close,
                                    five[-1].close_time,
                                    five[-1].available_at,
                                    now,
                                    "Price above predefined 5m support with measurable downside pressure" if short
                                    else "Price below predefined 5m resistance with measurable pressure",
                                    candle_close=five[-1].close_time,
                                    metrics=metrics,
                                ),
                            )
        return FastEvaluation(state=state.model_copy(deep=True), events=events, issues=issues)

    def _trigger(
        self, state: FastState, bar: Candle, now: datetime, tick_size: float | None, events: list[FastEvent]
    ) -> None:
        boundary = state.trigger_level
        level = boundary.price
        short = state.bias == "SHORT"
        buffer = max(
            boundary.atr * self.settings.breakout_buffer_atr,
            (tick_size or 0) * self.settings.breakout_buffer_ticks,
        )
        metrics = _directional_metrics(
            state.candles["1m"][: state.candles["1m"].index(bar) + 1], level, boundary.atr, state.bias
        )
        metrics["confirmation_buffer"] = buffer
        metrics["tick_size"] = tick_size
        metrics["time_below_level_seconds" if short else "time_above_level_seconds"] = (
            (bar.close_time - state.confirmed_at).total_seconds() if state.confirmed_at else None
        )
        state.trigger_metrics = metrics
        if state.confirmed_at:
            excursion = (1 - bar.low / level) * 100 if short else (bar.high / level - 1) * 100
            state.maximum_excursion_pct = max(state.maximum_excursion_pct or 0, excursion)
            failed = (bar.close > level + self.settings.failure_buffer_atr * boundary.atr) if short else (
                bar.close < level - self.settings.failure_buffer_atr * boundary.atr
            )
            if failed:
                self._append(
                    events,
                    self._emit(
                        state,
                        FastStage.FAILED_BREAKDOWN if short else FastStage.FAILED_BREAKOUT,
                        bar.close,
                        bar.close_time,
                        bar.available_at,
                        now,
                        "Previously confirmed 1m breakdown reclaimed the frozen support" if short
                        else "Previously confirmed 1m breakout lost the frozen resistance",
                        candle_close=bar.close_time,
                        metrics=metrics,
                    ),
                )
                state.cooldown_until = now + timedelta(seconds=self.settings.cooldown_seconds)
            return
        if state.stage not in PREBREAK | {FastStage.BREAKOUT_ATTEMPT, FastStage.BREAKDOWN_ATTEMPT}:
            return
        confirmed = (bar.close < level - buffer) if short else (bar.close > level + buffer)
        if confirmed:
            rvol = metrics["relative_volume"]
            body = metrics["body_to_range_ratio"]
            location = metrics["close_location"]
            strong = (
                rvol is not None
                and rvol >= self.settings.quality_rvol_strong
                and body >= self.settings.quality_body_ratio
                and (1 - location if short else location) >= self.settings.quality_close_location
            )
            moderate = rvol is not None and rvol >= self.settings.quality_rvol_moderate
            quality = "STRONG" if strong else "MODERATE" if moderate else "WEAK"
            self._append(
                events,
                self._emit(
                    state,
                    FastStage.BREAKDOWN_CONFIRMED if short else FastStage.BREAKOUT_CONFIRMED,
                    bar.close,
                    bar.close_time,
                    bar.available_at,
                    now,
                    "Completed 1m close falls below predefined 5m support minus ATR/tick buffer" if short
                    else "Completed 1m close exceeds predefined 5m resistance plus ATR/tick buffer",
                    candle_close=bar.close_time,
                    quality=quality,
                    metrics=metrics,
                ),
            )
            state.confirmed_at = bar.close_time
            state.maximum_excursion_pct = (1 - bar.low / level) * 100 if short else (bar.high / level - 1) * 100
        elif (bar.low < level and bar.close >= level) if short else (bar.high > level and bar.close <= level):
            self._append(
                events,
                self._emit(
                    state,
                    FastStage.LIQUIDITY_SWEEP,
                    bar.close,
                    bar.close_time,
                    bar.available_at,
                    now,
                    "1m wick crossed support but close returned above: no confirmed breakdown" if short
                    else "1m wick crossed resistance but close returned below: no confirmed breakout",
                    candle_close=bar.close_time,
                    metrics=metrics,
                ),
            )
            state.cooldown_until = now + timedelta(seconds=self.settings.cooldown_seconds)

    def _acceptance(self, state: FastState, frame: str, now: datetime, events: list[FastEvent]) -> None:
        boundary = state.trigger_level
        short = state.bias == "SHORT"
        if not state.confirmed_at or not boundary or state.stage in {FastStage.FAILED_BREAKOUT, FastStage.FAILED_BREAKDOWN}:
            return
        if frame == "15m" and not state.accepted_at:
            return
        if state.data_quality.get(frame) != "AVAILABLE":
            return
        for bar in state.candles.get(frame, []):
            if state.last_processed.get(frame) and bar.close_time <= state.last_processed[frame]:
                continue
            state.last_processed[frame] = bar.close_time
            # First later completed higher-timeframe candle may contain the
            # trigger; its closing acceptance is secondary and never delays 1m.
            confirmation_at = state.confirmed_at if frame == "5m" else state.accepted_at
            if bar.close_time <= confirmation_at or (now - bar.close_time).total_seconds() > TIMEFRAMES[frame]:
                continue
            if (bar.close < boundary.price) if short else (bar.close > boundary.price):
                if frame == "5m":
                    state.accepted_closes += 1
                    if state.accepted_closes >= self.settings.acceptance_closes and not state.accepted_at:
                        self._append(
                            events,
                            self._emit(
                                state,
                                FastStage.ACCEPTED_DOWN if short else FastStage.ACCEPTED,
                                bar.close,
                                bar.close_time,
                                bar.available_at,
                                now,
                                "Subsequent completed 5m candle accepts below the confirmed breakdown level" if short
                                else "Subsequent completed 5m candle accepts above the confirmed breakout level",
                                candle_close=bar.close_time,
                            ),
                        )
                        state.accepted_at = bar.close_time
                else:
                    state.structural_closes += 1
                    if state.structural_closes >= self.settings.structural_closes and not state.structural_at:
                        self._append(
                            events,
                            self._emit(
                                state,
                                FastStage.EXPANSION_CONFIRMED_DOWN if short else FastStage.EXPANSION_CONFIRMED,
                                bar.close,
                                bar.close_time,
                                bar.available_at,
                                now,
                                "Subsequent completed 15m candle holds structure below accepted breakdown" if short
                                else "Subsequent completed 15m candle holds structure above accepted breakout",
                                candle_close=bar.close_time,
                            ),
                        )
                        state.structural_at = bar.close_time
            else:
                self._append(
                    events,
                    self._emit(
                        state,
                        FastStage.FAILED_BREAKDOWN if short else FastStage.FAILED_BREAKOUT,
                        bar.close,
                        bar.close_time,
                        bar.available_at,
                        now,
                        f"Subsequent {frame} candle failed acceptance below frozen support" if short
                        else f"Subsequent {frame} candle failed acceptance above frozen resistance",
                        candle_close=bar.close_time,
                    ),
                )
                state.cooldown_until = now + timedelta(seconds=self.settings.cooldown_seconds)
                break

    def observe_trade(
        self,
        asset_id: str,
        price: float,
        *,
        market_event_at: datetime,
        received_at: datetime,
        evaluation_at: datetime | None = None,
        source: str = "public_trade",
    ) -> FastEvaluation:
        now = aware(evaluation_at or received_at)
        event_at, received_at = aware(market_event_at), aware(received_at)
        if not math.isfinite(price) or price <= 0 or not event_at <= received_at <= now:
            raise ValueError("invalid trade price/timestamp ordering")
        state = self._state(asset_id, now)
        if state.last_trade_at and event_at <= state.last_trade_at:
            return FastEvaluation(
                state=state.model_copy(deep=True), issues=["duplicate/out-of-order trade ignored"]
            )
        state.last_trade_at = event_at
        state.last_received_at = received_at
        state.last_evaluation_at = now
        state.price = price
        if state.bias == "SHORT" and not self.settings.short_enabled:
            state.stale = True
            return FastEvaluation(state=state.model_copy(deep=True), issues=["SHORT monitoring disabled"])
        if (now - event_at).total_seconds() > self.settings.trade_stale_seconds:
            state.stale = True
            return FastEvaluation(
                state=state.model_copy(deep=True), issues=["stale trade; attempt evaluation suspended"]
            )
        state.data_quality["live_trade"] = "AVAILABLE"
        boundary = state.trigger_level
        short = state.bias == "SHORT"
        events = []
        latest_closed = state.candles.get("1m", [])
        if (
            not latest_closed
            or (now - latest_closed[-1].close_time).total_seconds() > self.settings.candle_stale_seconds
        ):
            state.stale = True
        # Fresh trade does not rehabilitate stale candle history after restart.
        if (
            not state.stale
            and boundary
            and state.stage == (FastStage.BREAKDOWN_PRESSURE if short else FastStage.BREAKOUT_PRESSURE)
            and event_at >= boundary.discovered_at
            and ((price < boundary.price) if short else (price > boundary.price))
        ):
            self._append(
                events,
                self._emit(
                    state,
                    FastStage.BREAKDOWN_ATTEMPT if short else FastStage.BREAKOUT_ATTEMPT,
                    price,
                    event_at,
                    received_at,
                    now,
                    "Live price crossed frozen 5m support; 1m candle remains unconfirmed" if short
                    else "Live price crossed frozen 5m resistance; 1m candle remains unconfirmed",
                    metrics={"source": source},
                ),
            )
            state.attempt_at = event_at
        return FastEvaluation(state=state.model_copy(deep=True), events=events)
