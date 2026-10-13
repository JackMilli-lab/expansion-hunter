"""Causal pre-ignition hypotheses; never probabilities or entry confirmations.

Seven documented evidence families consume existing native market observations.
Missing evidence keeps its original weight and contributes zero. Cross-sectional
migration and own-history anomalies prioritize changing, still-quiet candidates.
Future excursion is unknown live; the extension estimate is only a historical
baseline proxy, with an immutable boundary anchor and monotonic peak progress.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from datetime import datetime
from statistics import mean, pstdev

from pydantic import Field, model_validator

from .discovery_ranking import _midranks
from .models import TIMEFRAMES, Model, aware

FAMILY_WEIGHTS = {
    "acceleration": .25, "compression_structure": .20, "volume_anomaly": .15,
    "location": .15, "relative_market": .10, "native_order_flow": .10, "context": .05,
}


class PreIgnitionSettings(Model):
    enabled: bool = True
    version: str = "PRE_IGNITION_V1"
    hypothesis: str = "UNVALIDATED_PRE_MOVE_RESEARCH_HYPOTHESIS"
    weights: dict[str, float] = Field(default_factory=lambda: dict(FAMILY_WEIGHTS))
    radar_score: float = Field(default=25, ge=0, le=100)
    watch_score: float = Field(default=40, ge=0, le=100)
    ready_score: float = Field(default=65, ge=0, le=100)
    minimum_coverage: float = Field(default=.65, ge=0, le=1)
    minimum_independent_families: int = Field(default=4, ge=1, le=7)
    independent_family_floor: float = Field(default=.35, ge=0, le=1)
    proximity_atr: float = Field(default=.75, gt=0)
    minimum_tests: int = Field(default=2, ge=1)
    minimum_structure: float = Field(default=.10, ge=0, le=1)
    minimum_compression: float = Field(default=.20, ge=0, le=1)
    minimum_taker_pressure: float = Field(default=.52, ge=.5, le=1)
    require_native_flow: bool = True
    stale_seconds: float = Field(default=90, gt=0)
    maximum_dynamics_gap_seconds: float = Field(default=180, ge=60)
    history_samples: int = Field(default=120, ge=3, le=1440)
    score_velocity_scale: float = Field(default=5, gt=0)
    rank_velocity_scale: float = Field(default=5, gt=0)
    minimum_extension_samples: int = Field(default=20, ge=5)
    maximum_move_consumed_estimate_pct: float = Field(default=20, gt=0, le=100)
    fresh_boundary_atr: float = Field(default=.5, gt=0)
    fresh_boundary_minimum_seconds: float = Field(default=300, ge=60)

    @model_validator(mode="after")
    def valid(self):
        if set(self.weights) != set(FAMILY_WEIGHTS) or any(v < 0 for v in self.weights.values()):
            raise ValueError("weights must contain all seven nonnegative evidence families")
        if abs(sum(self.weights.values())-1) > 1e-9:
            raise ValueError("family weights must sum to one")
        if not self.radar_score <= self.watch_score <= self.ready_score:
            raise ValueError("pre-ignition thresholds must be ordered")
        return self


def _time(value):
    if isinstance(value, datetime):
        return aware(value)
    return aware(datetime.fromisoformat(value))


def _number(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def _clip(value):
    return min(1., max(0., value))


def _get(bar, key, default=None):
    return bar.get(key, default) if isinstance(bar, dict) else getattr(bar, key, default)


def _ratio(a, b):
    return a/b if a is not None and b is not None and b > 0 else None


def _z(value, baseline):
    if value is None or len(baseline) < 20:
        return None
    spread = pstdev(baseline)
    # A constant own-history baseline is meaningful: a changed value is anomalous,
    # but an arbitrary huge z-score is not manufactured from zero variance.
    return (value-mean(baseline))/spread if spread > 0 else None


def _percentile(values, fraction=.75):
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered)-1)*fraction
    lower = int(position)
    upper = min(lower+1, len(ordered)-1)
    return ordered[lower]+(ordered[upper]-ordered[lower])*(position-lower)


def _known(bars, frame, now):
    known = {}
    for bar in bars:
        try:
            opened, closed, received = (_time(_get(bar, key)) for key in ("open_time", "close_time", "available_at"))
            if closed <= now and received <= now and closed > opened and (closed-opened).total_seconds() == TIMEFRAMES[frame]:
                known.setdefault(closed, bar)
        except (TypeError, ValueError, KeyError):
            continue
    # Bound the cost: this consumes the cache; it does not request histories.
    return [known[key] for key in sorted(known)][-240:]


def causal_fast_features(candles, as_of, market_snapshot=None):
    """Native completed-cache features with own-history, point-in-time baselines.

    Dicts and existing Candle contracts are accepted; unavailable native trade
    counts remain None. Current or future candles cannot become historical data.
    Comparable 15m windows are disjoint and exclude the active/latest window.
    """
    now = _time(as_of)
    frames = candles if isinstance(candles, dict) else {"1m": candles}
    one, five = _known(frames.get("1m", []), "1m", now), _known(frames.get("5m", []), "5m", now)
    result = {"quality": "UNAVAILABLE", "source": "NATIVE_CLOSED_SPOT_CANDLE_CACHE",
        "evidence_timestamp": None, "available_at": None, "fingerprint": None,
        "baseline_samples": 0, "trade_count": None, "trade_count_acceleration": None,
        "trade_count_acceleration_zscore": None, "expected_expansion_long_pct": None,
        "expected_expansion_short_pct": None, "expansion_sample_count": 0,
        "expansion_baseline_source": "CAUSAL_PRIOR_COMPLETED_15M_WINDOW_EXCURSION_PERCENTILE",
        "baseline_definition": "Own symbol; trailing completed windows; active comparison windows excluded"}
    if not one:
        return result
    last = one[-1]
    stamp = _time(_get(last, "close_time"))
    result.update(evidence_timestamp=stamp.isoformat(), available_at=_time(_get(last, "available_at")).isoformat())
    if (now-stamp).total_seconds() > 90:
        result["quality"] = "STALE"
        return result
    if any(_time(_get(a, "close_time")) != _time(_get(b, "open_time")) for a, b in zip(one[:-1], one[1:], strict=True)):
        result["quality"] = "DATA_GAP"
        return result
    if len(one) < 24:
        return result
    close = [_get(b, "close") for b in one]
    volumes = [_get(b, "volume") for b in one]
    ranges = [_get(b, "high")-_get(b, "low") for b in one]
    returns = [b/a-1 for a, b in zip(close[:-1], close[1:], strict=True)]
    tr = [max(_get(b, "high")-_get(b, "low"), abs(_get(b, "high")-_get(a, "close")),
              abs(_get(b, "low")-_get(a, "close"))) for a, b in zip(one[:-1], one[1:], strict=True)]
    atr = mean(tr[-14:])
    baseline = volumes[-61:-1]
    recent_volume, prior_volume = mean(volumes[-3:]), mean(volumes[-6:-3])
    volume_acceleration = _ratio(recent_volume, prior_volume)
    volume_acceleration = volume_acceleration-1 if volume_acceleration is not None else None
    prior_accel = [_ratio(mean(volumes[i-3:i]), mean(volumes[i-6:i-3])) for i in range(6, len(volumes)-3)]
    prior_accel = [v-1 for v in prior_accel if v is not None]
    counts = [_number(_get(b, "trade_count")) for b in one]
    count_accel, count_z = None, None
    if all(v is not None for v in counts[-6:]):
        ratio = _ratio(mean(counts[-3:]), mean(counts[-6:-3]))
        count_accel = ratio-1 if ratio is not None else None
        count_baseline = []
        for i in range(6, len(counts)-3):
            if all(v is not None for v in counts[i-6:i]):
                ratio = _ratio(mean(counts[i-3:i]), mean(counts[i-6:i-3]))
                if ratio is not None:
                    count_baseline.append(ratio-1)
        count_z = _z(count_accel, count_baseline)
    recent_range, prior_range = mean(ranges[-6:]), mean(ranges[-18:-6])
    recent_vol, prior_vol = pstdev(returns[-6:]), pstdev(returns[-18:-6])
    prior_atr = mean(tr[-28:-14]) if len(tr) >= 28 else None
    velocity, before = (close[-1]/close[-4]-1)/3, (close[-4]/close[-7]-1)/3
    own_return_scale = pstdev(returns[-61:-6]) if len(returns) >= 26 else None
    baseline_volume = mean(baseline) if baseline else None
    ratio = _ratio(_get(last, "taker_buy_volume"), _get(last, "volume"))
    session = [b for b in one if _time(_get(b, "open_time")).date() == stamp.date()]
    session_volume = sum(_get(b, "volume") for b in session)
    vwap = sum((_get(b, "high")+_get(b, "low")+_get(b, "close"))/3*_get(b, "volume") for b in session)/session_volume if session_volume else None
    result.update(quality="AVAILABLE", baseline_samples=len(baseline),
        relative_volume=_ratio(volumes[-1], baseline_volume), volume_zscore=_z(volumes[-1], baseline),
        volume_acceleration=volume_acceleration, volume_acceleration_zscore=_z(volume_acceleration, prior_accel),
        trade_count=counts[-1], trade_count_acceleration=count_accel, trade_count_acceleration_zscore=count_z,
        range_compression=1-_clip(recent_range/prior_range) if prior_range else None,
        atr_compression=1-_clip(atr/prior_atr) if prior_atr else None,
        volatility_compression=1-_clip(recent_vol/prior_vol) if prior_vol else None,
        atr=atr if atr > 0 else None, atr_pct=atr/close[-1]*100 if atr > 0 else None,
        price_return_3m=close[-1]/close[-4]-1, price_velocity=velocity,
        extension_baseline_price=close[-16],
        extension_baseline_timestamp=_time(_get(one[-16], "close_time")).isoformat(),
        price_acceleration=velocity-before,
        price_acceleration_normalized=(velocity-before)/own_return_scale if own_return_scale else None,
        relative_return_scale=own_return_scale,
        taker_buy_ratio=ratio, flow_source="NATIVE_COMPLETED_1M_TAKER_VOLUME" if ratio is not None else "UNAVAILABLE",
        vwap=vwap, vwap_distance_atr=(close[-1]-vwap)/atr if vwap is not None and atr > 0 else None,
        vwap_scope="AVAILABLE_INTRADAY_CACHE_ONLY_NOT_FULL_SESSION",
        higher_low_strength=_clip((mean(_get(b, "low") for b in one[-3:])-mean(_get(b, "low") for b in one[-6:-3]))/atr) if atr > 0 else None,
        lower_high_strength=_clip((mean(_get(b, "high") for b in one[-6:-3])-mean(_get(b, "high") for b in one[-3:]))/atr) if atr > 0 else None)
    # Use native 5m candles for disjoint 15m historical excursion baselines. Exclude
    # the current final 15m region; a selected winner never determines the anchor.
    historical = five[:-3]
    expansions = {"LONG": [], "SHORT": []}
    for index in range(1, len(historical)-2, 3):
        window = historical[index:index+3]
        before = historical[index-1]
        if not all(_time(_get(a, "close_time")) == _time(_get(b, "open_time")) for a, b in zip([before]+window[:-1], window, strict=True)):
            continue
        base = _get(before, "close")
        expansions["LONG"].append(max(0., (max(_get(b, "high") for b in window)/base-1)*100))
        expansions["SHORT"].append(max(0., (1-min(_get(b, "low") for b in window)/base)*100))
    result.update(expected_expansion_long_pct=_percentile(expansions["LONG"]),
                  expected_expansion_short_pct=_percentile(expansions["SHORT"]),
                  expansion_sample_count=len(expansions["LONG"]))
    result["fingerprint"] = hashlib.sha256(json.dumps({"at": result["evidence_timestamp"], "close": close[-1],
        "volume": volumes[-1], "count": counts[-1], "baseline": len(baseline)}, sort_keys=True).encode()).hexdigest()
    if market_snapshot:
        try:
            if _time(market_snapshot["as_of"]) <= now:
                result["market_snapshot"] = copy.deepcopy(market_snapshot)
        except (KeyError, ValueError, TypeError):
            pass
    return result


class PreIgnitionEngine:
    def __init__(self, settings: PreIgnitionSettings | None = None):
        self.settings = settings or PreIgnitionSettings()
        self._samples = []
        self._states = {}
        self._anchors = {}
        self._restart = {}
        self._last_update = None

    def export_state(self, *, maximum_samples=None):
        limit = self.settings.history_samples if maximum_samples is None else maximum_samples
        if limit < 3:
            raise ValueError("at least three samples are required in a dynamics checkpoint")
        return copy.deepcopy({"version": 1, "settings": self.settings.model_dump(mode="json"),
            "samples": self._samples[-limit:], "states": self._states, "anchors": self._anchors,
            "last_update": self._last_update.isoformat() if self._last_update else None})

    def checkpoint_state(self):
        return self.export_state(maximum_samples=3)

    def restore_state(self, payload):
        if not payload or payload.get("version") != 1 or payload.get("settings") != self.settings.model_dump(mode="json"):
            return
        samples = copy.deepcopy(payload.get("samples", []))[-self.settings.history_samples:]
        prior = None
        for sample in samples:
            at = _time(sample["as_of"])
            if prior and int(at.timestamp()//60) <= int(prior.timestamp()//60):
                raise ValueError("checkpoint samples must be chronological and minute-distinct")
            prior = at
        self._samples = samples
        self._states = copy.deepcopy(payload.get("states", {}))
        self._anchors = copy.deepcopy(payload.get("anchors", {}))
        self._restart = {key: {"observed_at": value.get("observed_at"), "evidence": value.get("evidence")}
            for key, value in self._states.items() if value.get("observed_at")}
        self._last_update = _time(payload["last_update"]) if payload.get("last_update") else None

    def _row(self, asset_id, observation, bias, now):
        key = f"{asset_id}|{bias}"
        data = observation.get("directions", {}).get(bias) or {}
        metrics = copy.deepcopy(data.get("metrics", {}))
        fast = copy.deepcopy(observation.get("fast_features") or {})
        status, reason = "AVAILABLE", "Observed directional pre-ignition hypothesis; uncalibrated"
        try:
            observed = _time(observation["as_of"])
        except (KeyError, TypeError, ValueError):
            observed = None
        quality = copy.deepcopy(observation.get("data_quality", {}))
        if observed is None or observed > now:
            status, reason = "UNAVAILABLE", "Missing or future observation timestamp"
        elif key in self._restart and observed <= _time(self._restart[key]["observed_at"]):
            status, reason = "STALE", "Restored state requires a newer market observation"
        elif (now-observed).total_seconds() > self.settings.stale_seconds or quality.get("1m") == "STALE":
            status, reason = "STALE", "Critical market observation is stale"
        elif quality.get("1m") != "AVAILABLE":
            status, reason = "UNAVAILABLE", "Native fresh 1m observations are required"
        try:
            fast_time = _time(fast["evidence_timestamp"])
            fast_available = _time(fast.get("available_at", fast["evidence_timestamp"]))
            if fast.get("quality") in {"DATA_GAP", "STALE"}:
                status, reason = ("STALE" if fast["quality"] == "STALE" else "UNAVAILABLE"), "Critical native candle cache is stale or has a data gap"
            elif fast.get("quality") == "AVAILABLE" and (now-fast_time).total_seconds() > self.settings.stale_seconds:
                status, reason = "STALE", "Cached native fast features exceed their permitted age"
            if fast_time > now or fast_available > now or fast.get("quality") != "AVAILABLE":
                fast = {}
        except (KeyError, ValueError, TypeError):
            fast = {}
        if key in self._restart and fast.get("fingerprint") == self._restart[key].get("evidence"):
            status, reason = "STALE", "Restored cached evidence cannot be refreshed by advancing the evaluation clock"
        level, price = _number(data.get("level")), _number(observation.get("price"))
        discovered = data.get("level_discovered_at")
        try:
            if discovered and (_time(discovered) > now or observed and _time(discovered) > observed):
                level, discovered = None, None
        except (ValueError, TypeError):
            level, discovered = None, None
        if price is None or price <= 0:
            status, reason = "UNAVAILABLE", "Positive observed native price is required"
        return {"asset_id": asset_id, "symbol": observation.get("symbol", asset_id), "bias": bias,
            "as_of": now.isoformat(), "observation_timestamp": observed.isoformat() if observed else None,
            "status": status, "state": "NORMAL", "price": price, "trigger": level,
            "trigger_discovered_at": discovered, "distance_atr": _number(data.get("distance_atr")),
            "metrics": metrics, "fast_features": fast, "data_quality": quality, "reason": reason,
            "invalidation": None, "targets": [], "setup_type": "CONTINUATION",
            "calibration": "UNCALIBRATED_SCORE_NOT_PROBABILITY", "hypothesis": self.settings.hypothesis,
            "model_version": self.settings.version, "probability": None, "execution_enabled": False,
            "score_velocity": None, "score_acceleration": None,
            "score_velocity_unit": "score_points_per_minute", "score_acceleration_unit": "score_points_per_minute_squared",
            "rank": {"current_rank": None, "previous_rank": None, "rank_velocity": None, "rank_acceleration": None,
                "cohort_size": 0, "basis": "OBSERVED_DIRECTIONAL_RAW_FEATURE_SCORE_NOT_WHOLE_MARKET"},
            "rank_velocity": None, "rank_acceleration": None, "current_rank": None,
            "move_consumed_pct": None, "move_consumed_estimate_pct": None,
            "move_consumed_estimate_source": None, "move_consumed_estimate_sample_count": 0,
            "expected_expansion_pct": None, "move_remaining": "UNKNOWN", "extension_status": "UNAVAILABLE",
            "estimate_label": "HISTORICAL_BASELINE_EXTENSION_ESTIMATE_NOT_EVENTUAL_MOVE"}

    def _flow(self, row, observation, now):
        metrics, fast = row["metrics"], row["fast_features"]
        # Preserve the native pressure scorer's precedence and use it for READY too.
        ratio = _number(metrics.get("live_taker_buy_ratio"))
        source = "NATIVE_OBSERVED_AGGREGATE_TRADES_1M"
        if ratio is None:
            snapshot = observation.get("market_snapshot") or fast.get("market_snapshot") or {}
            try:
                snapshot_time = _time(snapshot["as_of"])
                snapshot_known = 0 <= (now-snapshot_time).total_seconds() <= self.settings.stale_seconds
            except (KeyError, TypeError, ValueError):
                snapshot_known = False
            flow = snapshot.get("order_flow_windows", {}).get("1m", {}) if snapshot_known else {}
            buy, sell = _number(flow.get("taker_buy_volume")), _number(flow.get("taker_sell_volume"))
            if flow.get("quality") == "AVAILABLE" and buy is not None and sell is not None and buy >= 0 and sell >= 0 and buy+sell > 0:
                ratio = buy/(buy+sell)
        if ratio is None and row["data_quality"].get("5m") == "AVAILABLE":
            ratio = _number(metrics.get("taker_buy_ratio"))
            source = "NATIVE_COMPLETED_5M_TAKER_VOLUME"
        if ratio is None:
            ratio = _number(fast.get("taker_buy_ratio"))
            source = "NATIVE_COMPLETED_1M_TAKER_VOLUME"
        if ratio is None or not 0 <= ratio <= 1:
            return None, "UNAVAILABLE"
        return 1-ratio if row["bias"] == "SHORT" else ratio, source

    def _families(self, row, observation, now, rank_values=None):
        metrics, fast = row["metrics"], row["fast_features"]
        sign = -1 if row["bias"] == "SHORT" else 1
        context = observation.get("btc_context", {})
        def take(name):
            value = _number(fast.get(name))
            return value if value is not None else _number(metrics.get(name))
        def positive(value, scale=1):
            return _clip(value/scale) if value is not None else None
        native, flow_source = self._flow(row, observation, now)
        row.update(directional_taker_ratio=native, native_flow_source=flow_source)
        volume_acceleration, volume_z = take("volume_acceleration"), take("volume_acceleration_zscore")
        volume_anomaly = take("relative_volume")
        trade_accel, trade_z = take("trade_count_acceleration"), take("trade_count_acceleration_zscore")
        rs = _number(context.get("relative_return")) if context.get("status") == "AVAILABLE" else None
        market = _number(context.get("btc_return")) if context.get("status") == "AVAILABLE" else None
        market_relative = _number(observation.get("relative_market_return"))
        own_scale = take("relative_return_scale")
        price_accel = take("price_acceleration_normalized")
        if price_accel is None:
            value = _number(metrics.get("pressure_acceleration"))
            price_accel = sign*value if value is not None else None
        prior = self._previous(row, now)
        relative_acceleration = None
        if prior and rs is not None and prior.get("relative_return") is not None:
            elapsed = (now-_time(prior["as_of"])).total_seconds()/60
            relative_acceleration = sign*(rs-prior["relative_return"])/elapsed/(own_scale if own_scale else .001)
        structure = take("lower_high_strength" if sign < 0 else "higher_low_strength")
        tests = take("support_test_count" if sign < 0 else "resistance_test_count")
        compressions = [take(name) for name in ("range_compression", "atr_compression", "volatility_compression")]
        compression = mean(v for v in compressions if v is not None) if any(v is not None for v in compressions) else None
        distance = row["distance_atr"]
        location = _clip(1-max(0, distance)/self.settings.proximity_atr) if distance is not None and distance >= 0 and row["trigger"] else None
        vwap = take("vwap_distance_atr")
        if vwap is None:
            vwap = take("VWAP_distance_atr")
        ranks = rank_values or {}
        families = {
            "acceleration": {
                "volume_acceleration": positive(volume_z, 3) if volume_z is not None else positive(volume_acceleration),
                "trade_count_acceleration": positive(trade_z, 3) if trade_z is not None else positive(trade_accel),
                "relative_strength_acceleration": positive(relative_acceleration, 2),
                "rank_velocity": positive(ranks.get("rank_velocity"), self.settings.rank_velocity_scale),
                "rank_acceleration": positive(ranks.get("rank_acceleration"), self.settings.rank_velocity_scale)},
            "compression_structure": {"compression": compression,
                "directional_structure": positive(structure, .5), "repeated_tests": positive(tests, 4),
                "rejection_weakening": take("rejection_weakening")},
            "volume_anomaly": {"own_relative_volume": positive(volume_anomaly-1, 2) if volume_anomaly is not None else None,
                "own_volume_zscore": positive(take("volume_zscore"), 3),
                "activity_before_price": positive(volume_acceleration)*(1-positive(abs(take("price_return_3m")), .01))
                if volume_acceleration is not None and take("price_return_3m") is not None else None},
            "location": {"unbroken_boundary_proximity": location,
                "vwap_relationship": _clip(.5+sign*vwap/2) if vwap is not None else None},
            "relative_market": {"versus_btc": _clip(.5+sign*rs/(own_scale if own_scale else .01)) if rs is not None else None,
                "versus_observed_market": _clip(.5+sign*market_relative/(own_scale if own_scale else .01)) if market_relative is not None else None},
            "native_order_flow": {"directional_native_taker_activity": _clip((native-.5)/.25) if native is not None else None},
            "context": {"btc_directional_context": _clip(.5+sign*market/(own_scale if own_scale else .02)) if market is not None else None},
        }
        components = {}
        for family, inputs in families.items():
            usable = {name: _clip(value) if value is not None else None for name, value in inputs.items()}
            count = sum(value is not None for value in usable.values())
            normalized = sum(value or 0 for value in usable.values())/len(usable) if count else None
            coverage = count/len(usable)
            weight = self.settings.weights[family]
            components[family] = {"normalized": normalized, "weight": weight,
                "contribution": (normalized or 0)*weight*100, "coverage": coverage,
                "status": "AVAILABLE" if coverage == 1 else "PARTIAL" if count else "UNAVAILABLE",
                "inputs": usable}
        row.update(components=components, family_decomposition=copy.deepcopy(components),
            pre_ignition_score=sum(part["contribution"] for part in components.values()),
            evidence_coverage=sum(part["coverage"]*part["weight"] for part in components.values()),
            available_family_count=sum(part["normalized"] is not None and part["normalized"] >= self.settings.independent_family_floor for part in components.values()),
            compression=compression, structural_pressure=structure, level_test_count=tests,
            relative_return=rs, price_acceleration_directional=sign*price_accel if price_accel is not None else None)
        row["score"] = row["pre_ignition_score"]

    def _previous(self, row, now):
        key = f"{row['asset_id']}|{row['bias']}"
        for sample in reversed(self._samples):
            at = _time(sample["as_of"])
            if int(at.timestamp()//60) < int(now.timestamp()//60) and 0 < (now-at).total_seconds() <= self.settings.maximum_dynamics_gap_seconds:
                value = sample.get("rows", {}).get(key)
                if value and value.get("evidence") != row["evidence_fingerprint"]:
                    return dict(value, as_of=sample["as_of"])
                return None
        return None

    def _dynamics(self, rows, now):
        for bias in ("LONG", "SHORT"):
            group = [row for row in rows if row["bias"] == bias and row["status"] == "AVAILABLE"]
            # Rank raw causal features before migration enters their acceleration
            # family. This explicit basis avoids circular self-amplifying ranks.
            ranks = _midranks({row["asset_id"]: row["score"] for row in group}) if len(group) >= 2 else {}
            cohort = hashlib.sha256(json.dumps(sorted(ranks)).encode()).hexdigest()
            for row in group:
                old = self._previous(row, now)
                current = ranks.get(row["asset_id"])
                rank = row["rank"]
                rank.update(current_rank=current, cohort_size=len(group))
                if old and current is not None and old.get("cohort") == cohort and old.get("rank") is not None:
                    dt = (now-_time(old["as_of"])).total_seconds()/60
                    velocity = (old["rank"]-current)/dt
                    rank.update(previous_rank=old["rank"], rank_velocity=velocity)
                    if old.get("rank_velocity") is not None:
                        rank["rank_acceleration"] = (velocity-old["rank_velocity"])/dt
                cached = self._states.get(f"{row['asset_id']}|{row['bias']}", {})
                same_sample = (cached.get("evidence") == row["evidence_fingerprint"]
                    and cached.get("dynamics_minute") == int(now.timestamp()//60)
                    and cached.get("cohort") == cohort)
                if same_sample:
                    rank.update(previous_rank=cached.get("previous_rank"), rank_velocity=cached.get("rank_velocity"),
                        rank_acceleration=cached.get("rank_acceleration"))
                self._families(row, row["_observation"], now, rank)
                if old:
                    dt = (now-_time(old["as_of"])).total_seconds()/60
                    row["score_velocity"] = (row["score"]-old["score"])/dt
                    if old.get("score_velocity") is not None:
                        row["score_acceleration"] = (row["score_velocity"]-old["score_velocity"])/dt
                if same_sample:
                    row.update(score_velocity=cached.get("score_velocity"), score_acceleration=cached.get("score_acceleration"))
                row.update(current_rank=current, rank_velocity=rank["rank_velocity"], rank_acceleration=rank["rank_acceleration"])
                row["cohort_signature"] = cohort
        for row in rows:
            if row["status"] != "AVAILABLE":
                row["early_detection_priority"] = 0.
                continue
            score_change = _clip(max(0., row["score_velocity"] or 0)/self.settings.score_velocity_scale)
            score_accel = _clip(max(0., row["score_acceleration"] or 0)/self.settings.score_velocity_scale)
            rank_change = _clip(max(0., row["rank_velocity"] or 0)/self.settings.rank_velocity_scale)
            change = max(score_change, rank_change, score_accel)
            row["early_detection_priority"] = 30*row["score"]/100+70*change

    def _extension(self, row, now):
        key = f"{row['asset_id']}|{row['bias']}"
        trigger, discovered, price = row["trigger"], row["trigger_discovered_at"], row["price"]
        if price is None or row["status"] != "AVAILABLE":
            return
        atr = _number(row["fast_features"].get("atr")) or _number(row["metrics"].get("atr"))
        if atr is None and trigger and row["distance_atr"] is not None and row["distance_atr"] != 0:
            atr = abs(trigger-price)/abs(row["distance_atr"])
        anchor = self._anchors.get(key)
        fresh = bool(anchor and trigger and discovered and atr and anchor.get("trigger")
            and abs(trigger-anchor["trigger"]) >= atr*self.settings.fresh_boundary_atr
            and _time(discovered) > _time(anchor["boundary_discovered_at"])
            and (now-_time(anchor["started_at"])).total_seconds() >= self.settings.fresh_boundary_minimum_seconds
            and int(_time(discovered).timestamp()//300) > int(_time(anchor["boundary_discovered_at"]).timestamp()//300))
        if anchor is None or fresh:
            baseline_price = _number(row["fast_features"].get("extension_baseline_price"))
            baseline_stamp = row["fast_features"].get("extension_baseline_timestamp")
            try:
                baseline_known = baseline_price is not None and baseline_price > 0 and _time(baseline_stamp) <= now
            except (TypeError, ValueError):
                baseline_known = False
            # A new process must not make a recently extended move look quiet.
            # Its first estimate includes the observable past 15m price change.
            anchor = {"price": baseline_price if baseline_known and not fresh else price,
                "started_at": baseline_stamp if baseline_known and not fresh else now.isoformat(), "trigger": trigger,
                "boundary_discovered_at": discovered or now.isoformat(), "maximum_progress_pct": 0.,
                "expected_expansion_pct": None, "sample_count": 0}
            self._anchors[key] = anchor
        sign = -1 if row["bias"] == "SHORT" else 1
        progress = max(0., sign*(price/anchor["price"]-1)*100)
        anchor["maximum_progress_pct"] = max(anchor["maximum_progress_pct"], progress)
        expected = _number(row["fast_features"].get("expected_expansion_short_pct" if sign < 0 else "expected_expansion_long_pct"))
        count = _number(row["fast_features"].get("expansion_sample_count"))
        if anchor["expected_expansion_pct"] is None and expected and count and count >= self.settings.minimum_extension_samples:
            anchor.update(expected_expansion_pct=expected, sample_count=int(count))
        row.update(candidate_anchor_price=anchor["price"], candidate_anchor_timestamp=anchor["started_at"],
            maximum_observed_progress_pct=anchor["maximum_progress_pct"], fresh_structure_reset=fresh)
        expected = anchor["expected_expansion_pct"]
        if expected is None:
            return
        estimate = min(100., anchor["maximum_progress_pct"]/expected*100)
        row.update(move_consumed_estimate_pct=estimate,
            move_consumed_estimate_source="CAUSAL_PRIOR_COMPLETED_15M_WINDOW_EXCURSION_PERCENTILE",
            move_consumed_estimate_sample_count=anchor["sample_count"], expected_expansion_pct=expected,
            move_remaining="HIGH" if estimate < 10 else "MEDIUM" if estimate <= 20 else "LOW",
            extension_status="WITHIN_BASELINE" if estimate <= self.settings.maximum_move_consumed_estimate_pct else "EXTENDED")

    def _lifecycle(self, row, episodes, now):
        key = f"{row['asset_id']}|{row['bias']}"
        candidates = []
        for episode in episodes.values():
            if not isinstance(episode, dict) or episode.get("asset_id") != row["asset_id"] or episode.get("bias") != row["bias"]:
                continue
            try:
                if _time(episode["discovered_at"]) <= now:
                    candidates.append(episode)
            except (KeyError, TypeError, ValueError):
                pass
        episode = max(candidates, key=lambda value: (_time(value["discovered_at"]), value.get("phase") == "BREAK_CONFIRMED"), default=None)
        if episode:
            row.update(episode_id=episode.get("episode_id"), setup_type=episode.get("setup_type", "CONTINUATION"),
                invalidation=_number(episode.get("invalidation")), targets=copy.deepcopy(episode.get("targets", [])))
        if row["status"] != "AVAILABLE" or not self.settings.enabled:
            return
        if episode and episode.get("restart_stale"):
            row.update(status="STALE", reason="Native restored episode awaits fresh completed candles")
            return
        if episode and episode.get("status") == "INVALIDATED":
            row.update(state="FAILED", reason="Native engine invalidated the structural setup")
            return
        native_late = row["_observation"].get("status") == "LATE/MISSED" or episode and episode.get("status") in {"LATE", "MISSED"}
        if native_late:
            row.update(state="EXHAUSTION", extension_status="LATE", reason="Native engine marks this move late/missed; no new early alert")
            return
        if episode and episode.get("phase") == "BREAK_CONFIRMED" and episode.get("status") == "ACTIVE":
            try:
                confirmed = _time(episode["confirmed_at"])
                last = _time(episode.get("last_1m", episode["confirmed_at"]))
                if confirmed > now or last > now:
                    row.update(status="UNAVAILABLE", reason="Future native confirmation is excluded")
                    return
                row.update(state="ACTIVE" if last > confirmed else "TRIGGER", trigger=_number(episode.get("trigger")),
                    trigger_discovered_at=episode["discovered_at"], reason="Authoritative native completed-1m confirmation; supporting evidence only changes quality")
                return
            except (KeyError, ValueError, TypeError):
                row.update(status="UNAVAILABLE", reason="Native confirmation lacks causal completed-candle timestamp")
                return
        if row["score"] >= self.settings.radar_score or (row["score_velocity"] or 0) >= self.settings.score_velocity_scale or (row["rank_velocity"] or 0) >= self.settings.rank_velocity_scale:
            row.update(state="RADAR", reason="Abnormal directional change observed; confirmation and known boundary are not yet required")
        if row["score"] >= self.settings.watch_score and row["available_family_count"] >= 3:
            row.update(state="WATCH", reason="Several independent pre-ignition evidence families converge")
        direction = -1 if row["bias"] == "SHORT" else 1
        trigger, price = row["trigger"], row["price"]
        native = row["directional_taker_ratio"]
        ready = bool(trigger and row["trigger_discovered_at"] and row["distance_atr"] is not None
            and 0 <= row["distance_atr"] <= self.settings.proximity_atr and direction*(trigger-price) >= 0
            and row["data_quality"].get("5m") == "AVAILABLE"
            and row["score"] >= self.settings.ready_score
            and row["evidence_coverage"] >= self.settings.minimum_coverage
            and row["available_family_count"] >= self.settings.minimum_independent_families
            and row["structural_pressure"] is not None and row["structural_pressure"] >= self.settings.minimum_structure
            and row["level_test_count"] is not None and row["level_test_count"] >= self.settings.minimum_tests
            and row["compression"] is not None and row["compression"] >= self.settings.minimum_compression
            and (native is not None and native >= self.settings.minimum_taker_pressure or not self.settings.require_native_flow))
        if ready and row["extension_status"] not in {"EXTENDED", "LATE"}:
            row.update(state="READY", reason="Predefined boundary remains unbroken; independent measured pressure families and native directional flow align; early warning, not entry confirmation")
        elif row["extension_status"] == "EXTENDED":
            row.update(state="EXHAUSTION", reason="Causal historical-baseline extension estimate exceeds configured early threshold; no fresh pre-ignition claim")
        prior = self._states.get(key)
        row["previous_state"] = prior["state"] if prior else None

    def update(self, observations, episodes, as_of):
        now = _time(as_of)
        if self._last_update and now < self._last_update:
            raise ValueError("pre-ignition updates must be chronological")
        episodes = episodes.get("episodes", {}) if "episodes" in episodes else episodes
        # Aligned peer baselines are descriptive cross-sectional context. An
        # unaligned or one-symbol universe provides no market-relative evidence.
        observations = copy.deepcopy(observations)
        aligned = {}
        for asset_id, observation in observations.items():
            fast = observation.get("fast_features", {}) if isinstance(observation, dict) else {}
            value = _number(fast.get("price_return_3m"))
            if value is not None and fast.get("quality") == "AVAILABLE":
                try:
                    stamp = _time(fast["evidence_timestamp"])
                    received = _time(fast.get("available_at", fast["evidence_timestamp"]))
                    if stamp <= now and received <= now and (now-stamp).total_seconds() <= self.settings.stale_seconds:
                        aligned.setdefault(stamp, {})[asset_id] = value
                except (KeyError, TypeError, ValueError):
                    pass
        for values in aligned.values():
            ordered = sorted((value, asset_id) for asset_id, value in values.items())
            for removed_index, (value, asset_id) in enumerate(ordered):
                if len(values) < 2:
                    continue
                # The leave-one-out median can be obtained from the sorted
                # cohort without rebuilding all peer vectors per symbol.
                left, right = (len(ordered)-2)//2, (len(ordered)-1)//2
                median_peer = (ordered[left+(removed_index <= left)][0]
                    + ordered[right+(removed_index <= right)][0])/2
                observations[asset_id].setdefault("relative_market_return", value-median_peer)
                observations[asset_id]["relative_market_source"] = "ALIGNED_OBSERVED_COHORT_LEAVE_ONE_OUT_3M_MEDIAN"
        rows = []
        for asset_id, observation in sorted(observations.items()):
            if not isinstance(observation, dict):
                continue
            for bias in ("LONG", "SHORT"):
                row = self._row(asset_id, observation, bias, now)
                row["_observation"] = observation
                row["evidence_fingerprint"] = row["fast_features"].get("fingerprint") or row["observation_timestamp"]
                self._families(row, observation, now)
                rows.append(row)
        self._dynamics(rows, now)
        for row in rows:
            self._extension(row, now)
            self._lifecycle(row, episodes, now)
            key = f"{row['asset_id']}|{row['bias']}"
            prior = self._states.get(key)
            changed = prior is None or prior["state"] != row["state"] or prior["status"] != row["status"]
            row.update(previous_state=prior["state"] if prior else None, state_changed=changed,
                state_changed_at=now.isoformat() if changed else prior["changed_at"])
            self._states[key] = {"state": row["state"], "status": row["status"], "changed_at": row["state_changed_at"],
                "observed_at": row["observation_timestamp"], "evidence": row["evidence_fingerprint"],
                "dynamics_minute": int(now.timestamp()//60), "cohort": row.get("cohort_signature"),
                "score_velocity": row["score_velocity"], "score_acceleration": row["score_acceleration"],
                "rank_velocity": row["rank_velocity"], "rank_acceleration": row["rank_acceleration"],
                "previous_rank": row["rank"]["previous_rank"]}
            if row["status"] == "AVAILABLE":
                self._restart.pop(key, None)
            row.pop("_observation")
        prior_minute = int(_time(self._samples[-1]["as_of"]).timestamp()//60) if self._samples else None
        minute = int(now.timestamp()//60)
        if prior_minute is None or minute > prior_minute:
            eligible = [row for row in rows if row["status"] == "AVAILABLE"]
            sample_rows = {}
            cohorts = {bias: hashlib.sha256(json.dumps(sorted(row["asset_id"] for row in eligible if row["bias"] == bias)).encode()).hexdigest() for bias in ("LONG", "SHORT")}
            for row in eligible:
                key = f"{row['asset_id']}|{row['bias']}"
                sample_rows[key] = {"score": row["score"], "rank": row["current_rank"],
                    "cohort": cohorts[row["bias"]],
                    "rank_velocity": row["rank_velocity"], "score_velocity": row["score_velocity"],
                    "relative_return": row["relative_return"], "evidence": row["evidence_fingerprint"]}
            if sample_rows and (not self._samples or any(self._samples[-1]["rows"].get(key, {}).get("evidence") != value["evidence"] for key, value in sample_rows.items())):
                self._samples.append({"as_of": now.isoformat(), "rows": sample_rows})
                self._samples = self._samples[-self.settings.history_samples:]
        self._last_update = now
        return rows
