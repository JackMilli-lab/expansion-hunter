"""Direction-neutral, causal pre-break alerts built on the existing Fast engine.

No price forecast or calibrated probability is produced. Native taker volume is
required by default for the main pressure alert; absent flow remains unknown.
"""
from __future__ import annotations

import copy
import hashlib
from datetime import datetime, timedelta

from .fast import detect_resistance, detect_support, pressure_metrics
from .fast_models import FastSettings
from .models import TIMEFRAMES, Asset, Candle, aware
from .trade_alert_models import TradeAlertRecord, TradeAlertSettings


def clip(value):
    return max(0.0, min(1.0, value))


def available_bars(bars, now, frame):
    """Never complete an open candle or accept a revised/future historical bar."""
    known = {}
    for bar in bars:
        if bar.available_at <= now and bar.close_time <= now and (
                bar.close_time - bar.open_time).total_seconds() == TIMEFRAMES[frame]:
            known.setdefault(bar.close_time, bar)
    return [known[key] for key in sorted(known)]


def contiguous(bars):
    return bool(bars) and all(a.close_time == b.open_time for a, b in zip(bars[:-1], bars[1:], strict=True))


def btc_context(five, benchmark, now):
    btc = {bar.close_time: bar for bar in available_bars(benchmark, now, "5m")}
    aligned = [(bar, btc[bar.close_time]) for bar in five if bar.close_time in btc][-7:]
    if len(aligned) < 4:
        return {"status": "UNAVAILABLE", "reason": "Insufficient aligned closed BTC spot candles"}
    coin = aligned[-1][0].close / aligned[0][0].close - 1
    market = aligned[-1][1].close / aligned[0][1].close - 1
    return {"status": "AVAILABLE", "asset_return": coin, "btc_return": market,
            "relative_return": coin - market, "window_start": aligned[0][0].close_time.isoformat(),
            "window_end": aligned[-1][0].close_time.isoformat(),
            "regime": "BTC_UP" if market > .001 else "BTC_DOWN" if market < -.001 else "BTC_RANGE",
            "source": "Aligned native Binance spot 5m closes; regime is a deterministic context label"}


def score_pressure(metrics, bias, context, settings):
    """Missing contributions remain None and lower score/coverage, not neutral."""
    short = bias == "SHORT"
    count = metrics.get("support_test_count" if short else "resistance_test_count", 0)
    native_ratio = metrics.get("live_taker_buy_ratio", metrics.get("taker_buy_ratio"))
    directed_flow = 1 - native_ratio if short and native_ratio is not None else native_ratio
    rvol = metrics.get("relative_volume")
    acceleration = metrics.get("volume_acceleration")
    spot = None
    if rvol is not None and directed_flow is not None:
        spot = clip(rvol / 2) * clip(directed_flow) * (
            .75 + .25 * clip((acceleration or 0) + .5))
    distance = metrics.get("distance_to_support_atr" if short else "distance_to_resistance_atr")
    direction = -1 if short else 1
    rs = context.get("relative_return")
    market = context.get("btc_return")
    normalized = {
        "tests": clip(count / max(settings.minimum_tests, 1)),
        "decay": metrics.get("rejection_weakening"),
        "compression": metrics.get("range_compression"),
        "velocity": clip(metrics["pressure_velocity"]) if metrics.get("pressure_velocity") is not None else None,
        "acceleration": clip(metrics["pressure_acceleration"]) if metrics.get("pressure_acceleration") is not None else None,
        "spot_volume": spot,
        "relative_strength": clip(.5 + direction * rs * 100) if rs is not None else None,
        "distance": clip(1 - max(0, distance) / settings.proximity_atr) if distance is not None and distance >= 0 else 0,
        "market_context": clip(.5 + direction * market * 50) if market is not None else None,
    }
    components = {key: {"normalized": value, "weight": settings.weights[key],
        "contribution": value * settings.weights[key] * 100 if value is not None else None,
        "status": "DERIVED" if value is not None else "UNAVAILABLE"} for key, value in normalized.items()}
    total = sum(c["contribution"] for c in components.values() if c["contribution"] is not None)
    return min(100.0, max(0.0, total)), components


class TradeAlertEngine:
    def __init__(self, settings: TradeAlertSettings | None = None):
        self.settings = settings or TradeAlertSettings()
        self.fast_settings = FastSettings(history_bars=self.settings.history_bars,
            proximity_atr=self.settings.proximity_atr, minimum_tests=self.settings.minimum_tests)
        self.episodes: dict[str, dict] = {}
        self.observations: dict[str, dict] = {}
        self._prepared: dict[str, dict] = {}

    def export_state(self):
        return copy.deepcopy({"version": 1, "episodes": self.episodes})

    def restore_state(self, payload):
        if not payload or payload.get("version") != 1:
            return
        self.episodes = copy.deepcopy(payload.get("episodes", {}))
        for episode in self.episodes.values():
            episode["restart_stale"] = True

    def _quality(self, candles, now):
        quality = {"order_book": "UNAVAILABLE", "CVD": "UNAVAILABLE", "futures": "UNAVAILABLE"}
        for frame in ("1m", "5m", "15m", "1h", "4h"):
            bars = candles.get(frame, [])
            max_age = self.settings.candle_stale_seconds if frame == "1m" else (
                self.settings.five_minute_stale_seconds if frame == "5m" else TIMEFRAMES[frame] + 90)
            quality[frame] = "UNAVAILABLE" if not contiguous(bars) else (
                "STALE" if (now - bars[-1].close_time).total_seconds() > max_age else "AVAILABLE")
        return quality

    def _targets(self, candles, bias, trigger):
        prices = set()
        for frame in ("5m", "15m", "1h", "4h"):
            bars = candles.get(frame, [])
            for i in range(2, len(bars) - 2):
                bar = bars[i]
                neighbours = bars[i-2:i] + bars[i+1:i+3]
                if bias == "LONG" and bar.high > trigger and all(bar.high > b.high for b in neighbours):
                    prices.add(bar.high)
                if bias == "SHORT" and bar.low < trigger and all(bar.low < b.low for b in neighbours):
                    prices.add(bar.low)
        return sorted(prices, reverse=bias == "SHORT")[:2]

    def _record(self, asset, episode, phase, price, now, received_at, event_at, quality, reason, status="ACTIVE"):
        token = f"{episode['episode_id']}|{phase}|{status}"
        if token in episode.setdefault("emitted", []):
            return None
        episode["emitted"].append(token)
        episode["phase"] = phase
        episode["status"] = status
        direction = -1 if episode["bias"] == "SHORT" else 1
        trigger = episode["trigger"]
        return TradeAlertRecord(event_id=hashlib.sha256(token.encode()).hexdigest()[:32],
            episode_id=episode["episode_id"], asset_id=asset.asset_id,
            symbol=asset.exchange_symbol or asset.symbol, phase=phase, bias=episode["bias"],
            setup_type=episode["setup_type"], event_timestamp=event_at, received_at=received_at,
            detected_at=now, expires_at=now + timedelta(seconds=self.settings.alert_max_age_seconds),
            price=price, trigger_level=trigger,
            trigger_discovered_at=datetime.fromisoformat(episode["discovered_at"]),
            distance_pct=direction * (trigger - price) / price * 100,
            invalidation=episode.get("invalidation"), targets=episode.get("targets", []),
            score=episode["score"], components=copy.deepcopy(episode["components"]),
            metrics=copy.deepcopy(episode["metrics"]), data_quality=dict(quality),
            btc_context=copy.deepcopy(episode["btc_context"]), reason=reason, status=status,
            detection_latency_ms=max(0, (now - event_at).total_seconds() * 1000),
            alert_latency_ms=max(0, (now - event_at).total_seconds() * 1000),
            processing_latency_ms=max(0, (now - received_at).total_seconds() * 1000))

    def evaluate(self, asset: Asset, candles: dict[str, list[Candle]], now: datetime,
                 received_at: datetime, price: float | None, btc_candles: list[Candle],
                 tick_size: float | None = None, *, market_snapshot: dict | None = None) -> list[TradeAlertRecord]:
        now, received_at = aware(now), aware(received_at)
        if received_at > now:
            raise ValueError("Future receipt cannot enter live analysis")
        if tick_size is not None and tick_size <= 0:
            raise ValueError("tick size must be positive")
        bars = {frame: available_bars(values, now, frame)[-self.settings.history_bars:]
                for frame, values in candles.items() if frame in TIMEFRAMES}
        quality = self._quality(bars, now)
        market_snapshot = market_snapshot or {}
        snapshot_time = market_snapshot.get("as_of")
        if snapshot_time and datetime.fromisoformat(snapshot_time) > received_at:
            raise ValueError("Future microstructure snapshot cannot enter analysis")
        quality.update({key: value for key, value in market_snapshot.get("data_quality", {}).items()
                        if key not in TIMEFRAMES})
        self.observations[asset.asset_id] = {"symbol": asset.exchange_symbol or asset.symbol,
            "data_quality": quality, "as_of": now.isoformat(), "price": price,
            "directions": {}, "status": "INSUFFICIENT_DATA"}
        if quality["1m"] != "AVAILABLE" or quality["5m"] != "AVAILABLE":
            return []
        one, five = bars["1m"], bars["5m"]
        if len(five) < 36 or one[-1].available_at > received_at or five[-1].available_at > received_at:
            return []
        price = one[-1].close if price is None else price
        if price <= 0:
            raise ValueError("price must be positive")
        self.observations[asset.asset_id]["price"] = price
        context = btc_context(five, btc_candles, now)
        self.observations[asset.asset_id].update(status="EVALUATED", btc_context=context)
        events = []
        # Evaluate previously discovered levels before discovering new ones.
        episode_keys = [f"{asset.asset_id}|{side}|{kind}" for side, kind in (
            ("LONG", "CONTINUATION"), ("SHORT", "CONTINUATION"),
            ("LONG", "LONG_REVERSAL"), ("SHORT", "SHORT_REVERSAL"))]
        for episode in [self.episodes[key] for key in episode_keys if key in self.episodes]:
            if episode["asset_id"] != asset.asset_id or episode["status"] in {"INVALIDATED", "LATE", "MISSED"}:
                continue
            last = one[-1]
            if last.close_time.isoformat() == episode.get("last_1m"):
                continue
            episode["last_1m"] = last.close_time.isoformat()
            discovery = datetime.fromisoformat(episode["discovered_at"])
            if last.open_time < discovery or last.close_time <= discovery:
                continue
            episode["restart_stale"] = False
            direction = -1 if episode["bias"] == "SHORT" else 1
            buffer = max(episode["atr"] * self.settings.breakout_buffer_atr,
                         (tick_size or 0) * self.settings.breakout_buffer_ticks)
            displacement = direction * (last.close - episode["trigger"])
            overextended = displacement / episode["atr"] > self.settings.late_atr or (
                displacement / episode["trigger"] * 100 > self.settings.late_pct)
            invalid = episode.get("invalidation")
            invalidated = invalid is not None and direction * (last.close - invalid) < 0
            lost_break = episode["phase"] == "BREAK_CONFIRMED" and displacement < -buffer
            if invalidated or lost_break or (now - discovery).total_seconds() > self.settings.episode_seconds:
                e = self._record(asset, episode, "INVALIDATED", price, now, received_at, last.close_time,
                    quality, "Structural invalidation, failed acceptance, or expired setup", "INVALIDATED")
                episode["cooldown_until"] = (now + timedelta(seconds=self.settings.cooldown_seconds)).isoformat()
                if e:
                    events.append(e)
            elif displacement > buffer and episode["phase"] != "BREAK_CONFIRMED":
                e = self._record(asset, episode, "BREAK_CONFIRMED", price, now, received_at, last.close_time,
                    quality, "Completed 1m close beyond the previously known trigger plus ATR/tick buffer; volume changes quality",
                    "LATE" if overextended else "ACTIVE")
                episode["confirmed_at"] = last.close_time.isoformat()
                if e:
                    events.append(e)
            elif ((last.high > episode["trigger"] and episode["bias"] == "LONG" or
                   last.low < episode["trigger"] and episode["bias"] == "SHORT") and displacement <= 0):
                episode["last_sweep"] = {"at": last.close_time.isoformat(), "status": "LIQUIDITY_SWEEP",
                    "reason": "Wick crossed trigger; completed close returned inside. Not a confirmed break."}

        revision = (len(five), five[0].close_time, five[-1].close_time, five[-1].available_at,
                    five[-1].close, five[-1].volume, five[-1].taker_buy_volume)
        cached = self._prepared.get(asset.asset_id)
        if not cached or cached["revision"] != revision:
            resistance = detect_resistance(five, self.fast_settings, now)
            support = detect_support(five, self.fast_settings, now)
            cached = {"revision": revision, "LONG": resistance, "SHORT": support, "metrics": {}}
            for side, zone in (("LONG", resistance), ("SHORT", support)):
                if zone:
                    cached["metrics"][side] = pressure_metrics(five, zone, self.fast_settings, bias=side)
            self._prepared[asset.asset_id] = cached
        resistance, support = cached["LONG"], cached["SHORT"]
        if abs(one[-1].close / one[-1].open - 1) * 100 > self.settings.late_pct:
            self.observations[asset.asset_id]["status"] = "LATE/MISSED"
            return events  # Archive prior confirmations; do not create a new chasing setup.
        for bias, level in (("LONG", resistance), ("SHORT", support)):
            if not level:
                continue
            short = bias == "SHORT"
            metrics = dict(cached["metrics"][bias])
            direction = -1 if short else 1
            recent = direction * (five[-1].close - five[-4].close) / (3 * level.atr)
            prior = direction * (five[-4].close - five[-7].close) / (3 * level.atr)
            metrics.update(pressure_velocity=recent, pressure_acceleration=recent-prior,
                metric_timeframe="5m", spot_volume_source=five[-1].source)
            live_flow = market_snapshot.get("order_flow_windows", {}).get("1m", {})
            if live_flow.get("quality") == "AVAILABLE":
                buy, sell = live_flow.get("taker_buy_volume"), live_flow.get("taker_sell_volume")
                if isinstance(buy, (int, float)) and isinstance(sell, (int, float)) and buy + sell > 0:
                    metrics["live_taker_buy_ratio"] = buy / (buy + sell)
                metrics["observed_trade_delta_1m"] = live_flow.get("delta")
                metrics["observed_trade_count_1m"] = live_flow.get("aggregate_trade_count")
                metrics["flow_scope"] = live_flow.get("aggregation")
                metrics["flow_source"] = "NATIVE_OBSERVED_AGGREGATE_TRADES"
            metrics["spread_bps"] = None
            ticker = market_snapshot.get("book_ticker")
            if ticker and quality.get("best_bid_ask") == "AVAILABLE":
                bid, ask = ticker.get("bid", {}).get("price"), ticker.get("ask", {}).get("price")
                if bid and ask and 0 < bid <= ask:
                    metrics["spread_bps"] = (ask-bid) / ((ask+bid)/2) * 10000
            distance = direction * (level.price - price) / level.atr
            metrics["distance_to_support_atr" if short else "distance_to_resistance_atr"] = distance
            score, components = score_pressure(metrics, bias, context, self.settings)
            self.observations[asset.asset_id]["directions"][bias] = {
                "score": score, "components": components, "metrics": dict(metrics),
                "level": level.price, "level_discovered_at": level.discovered_at.isoformat(),
                "distance_atr": distance,
                "eligible_proximity": 0 <= distance <= self.settings.proximity_atr,
                "above_watch_threshold": score >= self.settings.watch_score}
            quality["spot_flow"] = "AVAILABLE" if metrics.get("taker_buy_ratio") is not None else "UNAVAILABLE"
            key = f"{asset.asset_id}|{bias}|CONTINUATION"
            existing = self.episodes.get(key)
            if existing and existing.get("phase") == "BREAK_CONFIRMED" and existing["status"] == "ACTIVE":
                continue
            if existing and existing.get("cooldown_until") and datetime.fromisoformat(existing["cooldown_until"]) > now:
                continue
            if distance < 0 or distance > self.settings.proximity_atr or score < self.settings.watch_score:
                continue
            if existing and existing["status"] == "ACTIVE" and abs(existing["trigger"] - level.price) <= level.atr * .5:
                episode = existing
            else:
                episode = {"asset_id": asset.asset_id, "bias": bias, "setup_type": "CONTINUATION",
                    "episode_id": hashlib.sha256(f"{key}|{now.isoformat()}|{level.price}".encode()).hexdigest()[:32],
                    "trigger": level.price, "discovered_at": now.isoformat(), "atr": level.atr,
                    "phase": "STRUCTURE_WATCH", "status": "ACTIVE", "restart_stale": False,
                    "invalidation": max(b.high for b in five[-10:]) if short else min(b.low for b in five[-10:]),
                    "targets": self._targets(bars, bias, level.price)}
                self.episodes[key] = episode
            episode.update(score=score, components=components, metrics=metrics, btc_context=context)
            native_ratio = metrics.get("live_taker_buy_ratio", metrics.get("taker_buy_ratio"))
            directed_flow = (1-native_ratio if short else native_ratio) if native_ratio is not None else None
            flow = directed_flow is not None and directed_flow >= self.settings.minimum_taker_pressure
            if not self.settings.require_native_flow and directed_flow is None:
                flow = True  # Explicit config relaxation; evidence stays unavailable.
            tests = metrics.get("support_test_count" if short else "resistance_test_count", 0)
            structure = metrics.get("lower_high_strength" if short else "higher_low_strength", 0)
            phase = "TRADE_ALERT" if (score >= self.settings.alert_score and tests >= self.settings.minimum_tests
                and (structure or 0) >= self.settings.minimum_structure and flow) else "STRUCTURE_WATCH"
            if episode["phase"] == "TRADE_ALERT" and phase == "STRUCTURE_WATCH":
                continue
            e = self._record(asset, episode, phase, price, now, received_at, one[-1].close_time, quality,
                f"Measured {'support' if short else 'resistance'} pressure: tests={tests}, "
                f"{'lower_high' if short else 'higher_low'}_strength={structure}; "
                f"directional native taker fraction={directed_flow if directed_flow is not None else 'UNAVAILABLE'}")
            if e:
                events.append(e)

        # Separate reversal hypotheses, never relabel an ordinary rejection as continuation.
        for bias, level in (("LONG", support), ("SHORT", resistance)):
            if not level or len(one) < 3:
                continue
            short = bias == "SHORT"
            last = five[-1]
            ratio = last.taker_buy_volume / last.volume if last.taker_buy_volume is not None and last.volume else None
            swept = last.high > level.upper and last.close < level.price if short else (
                last.low < level.lower and last.close > level.price)
            flow = ratio is not None and (1-ratio if short else ratio) >= self.settings.minimum_taker_pressure
            if not swept or not flow:
                continue
            setup_type = "SHORT_REVERSAL" if short else "LONG_REVERSAL"
            key = f"{asset.asset_id}|{bias}|{setup_type}"
            if key in self.episodes and self.episodes[key]["status"] == "ACTIVE":
                continue
            trigger = min(b.low for b in one[-3:-1]) if short else max(b.high for b in one[-3:-1])
            if (-1 if short else 1) * (trigger - price) <= 0:
                continue
            metrics = {"reversal_level": level.price, "reversal_evidence": "SWEPT_AND_RECLAIMED",
                "taker_buy_ratio": ratio, "metric_timeframe": "5m", "pressure_velocity": None,
                "pressure_acceleration": None, "relative_volume": None}
            episode = {"asset_id": asset.asset_id, "bias": bias, "setup_type": setup_type,
                "episode_id": hashlib.sha256(f"{key}|{last.close_time}|{now}".encode()).hexdigest()[:32],
                "trigger": trigger, "discovered_at": now.isoformat(), "atr": level.atr,
                "phase": "STRUCTURE_WATCH", "status": "ACTIVE", "restart_stale": False,
                "invalidation": last.high if short else last.low, "targets": self._targets(bars, bias, trigger),
                "score": None, "components": {}, "metrics": metrics, "btc_context": context}
            self.episodes[key] = episode
            e = self._record(asset, episode, "STRUCTURE_WATCH", price, now, received_at, last.close_time,
                quality, "Potential reversal after sweep and native opposing taker activity; not continuation pressure")
            if e:
                events.append(e)
        return events
