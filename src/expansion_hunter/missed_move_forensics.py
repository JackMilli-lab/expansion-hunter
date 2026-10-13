"""Offline, frozen-rule TOP MOVER EARLY DETECTION TEST on public Binance data.

This runner never downloads, trades, fits thresholds, or updates production.
Historical REST candles have exchange event times, not archived local receipts.
Replay therefore explicitly assumes availability at close plus a declared delay.
The original download receipt and source checksum remain in the evidence file.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from statistics import mean

from .fast import _atr, _metrics
from .live_analysis import fibonacci_research
from .market_context import session_context
from .models import TIMEFRAMES, Asset, Candle, aware
from .technical import analyze, confirmed_swings
from .top_mover_benchmark import (
    EARLY_STATES,
    ExpansionDefinition,
    evaluate_early_detection,
    label_control_window,
    label_expansions,
    replay_fast_monitor,
    replay_pre_move,
    walk_forward_splits,
)
from .trade_alert_models import TradeAlertSettings

TARGETS = ("KAIA", "BAT", "ZK", "STRK", "ATOM", "W", "DOT", "S", "PEAQ")
CONTROLS = ("ETH", "XRP", "ADA", "OP", "ARB", "NEAR")
OFFSETS = (60, 30, 15, 5, 3, 1)
RECEIPT_ASSUMPTION = "ESTIMATED_HISTORICAL_RECEIPT_NOT_ARCHIVED_LOCAL_RECEIPT"


def _time(value: str) -> datetime:
    return aware(datetime.fromisoformat(value.replace("Z", "+00:00")))


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _unwrap(value):
    while isinstance(value, dict) and "result" in value:
        value = value["result"]
    return value


def _finite(value):
    return value if isinstance(value, (int, float)) and math.isfinite(value) else None


def load_evidence(raw_dir: Path, *, receipt_delay_seconds: float = 0) -> dict:
    """Validate native kline intervals, preserve receipts, reject revisions.

    Exactly identical overlaps across REST pages are deduplicated explicitly.
    Changed OHLCV for the same interval are rejected instead of choosing a
    convenient revision. Missing intervals are recorded, never manufactured.
    """
    if not math.isfinite(receipt_delay_seconds) or receipt_delay_seconds < 0:
        raise ValueError("historical receipt delay must be finite and nonnegative")
    metadata_path = raw_dir / "exchange_info.json"
    metadata = json.loads(metadata_path.read_text())
    as_of = datetime.fromtimestamp(metadata["as_of_ms"] / 1000, UTC)
    markets, assets = {}, {}
    for record in metadata.get("records", []):
        for market in record.get("data", {}).get("symbols", []):
            markets[market["symbol"]] = market
            if market.get("quoteAsset") == "USDT":
                symbol = market["symbol"]
                assets[symbol] = Asset(asset_id=symbol, symbol=market["baseAsset"],
                    name=market["baseAsset"], exchange_symbol=symbol)
    histories, native, provenance, failures = {}, {}, [], []
    candidates = sorted(raw_dir.glob("*.json"))
    for path in candidates:
        if path == metadata_path:
            continue
        envelope = json.loads(path.read_text())
        provenance.append({"file": path.name, "sha256": _digest(path),
            "retrieved_at": envelope.get("retrieved_at"), "source": envelope.get("source"),
            "request": envelope.get("request"), "error": bool(envelope.get("error"))})
        request = envelope.get("request", {})
        if not request.get("interval"):
            continue
        if envelope.get("error"):
            failures.append({"file": path.name, "status": "UNAVAILABLE", "reason": "provider returned error"})
            continue
        symbol, frame = request["symbol"], request["interval"]
        frame = "1D" if frame == "1d" else frame
        if frame not in TIMEFRAMES or symbol not in assets:
            failures.append({"file": path.name, "status": "UNAVAILABLE", "reason": "unsupported frame/market metadata"})
            continue
        result = _unwrap(envelope.get("result"))
        if not isinstance(result, list):
            raise ValueError(f"unexpected real kline response envelope: {path.name}")
        destination = native.setdefault(symbol, {}).setdefault(frame, {})
        for row in result:
            if len(row) < 11:
                raise ValueError(f"incomplete Binance kline: {path.name}")
            opening = datetime.fromtimestamp(int(row[0]) / 1000, UTC)
            closing = opening + timedelta(seconds=TIMEFRAMES[frame])
            if int(row[6]) != int(closing.timestamp() * 1000) - 1:
                raise ValueError(f"invalid native kline boundary: {path.name}")
            values = tuple(row[:11])
            if opening in destination:
                if destination[opening][0] != values:
                    raise ValueError(f"conflicting historical candle revision: {symbol}/{frame}/{opening.isoformat()}")
                destination[opening][2] += 1
                continue
            candle = Candle(open_time=opening, close_time=closing,
                available_at=closing + timedelta(seconds=receipt_delay_seconds),
                open=float(row[1]), high=float(row[2]), low=float(row[3]), close=float(row[4]),
                volume=float(row[5]), quote_volume=float(row[7]), taker_buy_volume=float(row[9]), trade_count=row[8],
                source="binance/spot/historical-public-connector")
            destination[opening] = [values, candle, 0]
    coverage = {}
    for symbol, frames in native.items():
        histories[symbol], coverage[symbol] = {}, {}
        for frame, mapping in frames.items():
            bars = [entry[1] for _, entry in sorted(mapping.items())
                    if entry[1].close_time <= as_of and entry[1].available_at <= as_of]
            histories[symbol][frame] = bars
            gaps = [{"after": a.close_time.isoformat(), "before": b.open_time.isoformat(),
                     "missing_seconds": (b.open_time-a.close_time).total_seconds()}
                    for a, b in zip(bars[:-1], bars[1:], strict=True) if a.close_time != b.open_time]
            coverage[symbol][frame] = {"bars": len(bars), "start": bars[0].open_time.isoformat() if bars else None,
                "end": bars[-1].close_time.isoformat() if bars else None,
                "gap_count": len(gaps), "gaps": gaps,
                "identical_overlap_rows": sum(entry[2] for entry in mapping.values()),
                "source": "Binance Spot native exchange klines", "availability": RECEIPT_ASSUMPTION}
    assets = {symbol: asset for symbol, asset in assets.items() if symbol in histories}
    return {"as_of": as_of, "assets": assets, "histories": histories, "markets": markets,
        "coverage": coverage, "download_provenance": provenance, "failures": failures,
        "metadata_provenance": {"file": metadata_path.name, "sha256": _digest(metadata_path),
            "retrieved_at": metadata.get("retrieved_at"), "source": metadata.get("provenance"),
            "limitation": "Current exchange metadata cannot establish historical universe membership"},
        "receipt_assumption": RECEIPT_ASSUMPTION, "receipt_delay_seconds": receipt_delay_seconds}


def known_prefix(bars: list[Candle], at: datetime, limit: int | None = None) -> list[Candle]:
    """No future candle or delayed receipt can enter forensic feature creation."""
    rows = [bar for bar in bars if bar.close_time <= at and bar.available_at <= at]
    return rows[-limit:] if limit else rows


def _metric(value, *, source, timestamp=None, status=None, reason=None):
    return {"value": value, "source": source, "timestamp": timestamp,
        "status": status or ("DERIVED" if value is not None else "UNAVAILABLE"),
        "reason": reason, "availability_assumption": RECEIPT_ASSUMPTION}


def intrinsic_five_minute_metrics(bars: list[Candle], at: datetime) -> dict:
    """Existing candle-only features remain observable without a trade level.

    Fast's shared metric helper requires a reference price for other outputs.
    The latest close is supplied only to satisfy that helper, then every
    reference-level distance/test/score field is discarded. This function does
    not discover resistance/support, fabricate triggers, or score a setup.
    """
    known = known_prefix(bars, at)
    if len(known) < 36 or any(a.close_time != b.open_time for a, b in zip(known[:-1], known[1:], strict=True)):
        return {"status": "INSUFFICIENT_DATA", "metrics": {},
            "source_timestamp": known[-1].close_time.isoformat() if known else None}
    atr = _atr(known, 14)
    if atr is None or atr <= 0:
        return {"status": "INSUFFICIENT_DATA", "metrics": {}, "source_timestamp": known[-1].close_time.isoformat()}
    allowed = {"relative_volume", "volume_acceleration", "range_compression", "atr_compression",
        "volatility_compression", "higher_low_strength", "body_to_range_ratio", "upper_wick_rejection",
        "close_location", "price_velocity", "VWAP", "taker_buy_ratio"}
    calculated = _metrics(known, known[-1].close, atr)
    return {"status": "DERIVED", "metrics": {key: value for key, value in calculated.items() if key in allowed},
        "atr14": atr, "source_timestamp": known[-1].close_time.isoformat(),
        "source": "Existing Fast _atr/_metrics applied to available native 5m candles; no resistance requirement",
        "trigger": None, "resistance": None, "pressure_score": None}


def structural_price_sweeps(bars: list[Candle], at: datetime) -> dict:
    """Price crosses/reclaims an earlier confirmed pivot; no order inference."""
    known = known_prefix(bars, at)
    if not known:
        return _metric(None, source="Existing confirmed_swings; structural price only", reason="No completed minute history")
    latest = known[-1]
    swings = [swing for swing in confirmed_swings(known[:-1]) if _time(swing["confirmed_at"]) <= latest.open_time]
    events = []
    for kind, side in (("high", "BUY_SIDE"), ("low", "SELL_SIDE")):
        points = [swing for swing in swings if swing["kind"] == kind]
        if not points:
            continue
        pivot = points[-1]
        price = pivot["price"]
        swept = latest.high > price > latest.close if kind == "high" else latest.low < price < latest.close
        if swept:
            events.append({"side": side, "price": price, "pivot_confirmed_at": pivot["confirmed_at"],
                "candle_close": latest.close_time.isoformat(), "classification": "PRICE_PIVOT_CROSS_AND_RECLAIM",
                "observed_stop_orders": "UNAVAILABLE"})
    return _metric({"events": events, "reference_pivots": len(swings)} if swings else None,
        source="Existing confirmed_swings(left=3,right=3); prior pivots known before current candle open",
        timestamp=latest.close_time.isoformat(), status="DERIVED" if swings else "UNAVAILABLE",
        reason="Structural price evidence only; cannot establish consumed resting orders or actual stops")


def _derivatives(raw_dir: Path, symbol: str, at: datetime) -> dict:
    output = {}
    for kind in ("oi", "funding"):
        path = raw_dir / f"{symbol}_{kind}.json"
        result, retrieved, issue = [], None, "No real historical derivatives response archived"
        if path.exists():
            envelope = json.loads(path.read_text())
            retrieved = envelope.get("retrieved_at")
            rows = _unwrap(envelope.get("result"))
            if not envelope.get("error") and isinstance(rows, list):
                result = [row for row in rows if isinstance(row, dict) and
                    int(row.get("timestamp", row.get("fundingTime", 10**30))) <= int(at.timestamp()*1000)]
                issue = "No record existed before this decision in the downloaded history"
        if not result:
            output[kind] = _metric(None, source="Binance USD-M futures; separate market from Spot",
                reason=issue)
            continue
        result.sort(key=lambda row: int(row.get("timestamp", row.get("fundingTime", 0))))
        latest = result[-1]
        stamp = datetime.fromtimestamp(int(latest.get("timestamp", latest.get("fundingTime")))/1000, UTC)
        age = (at-stamp).total_seconds()
        stale = age > (600 if kind == "oi" else 9*3600)
        value = {"open_interest": float(latest["sumOpenInterest"]),
            "notional": float(latest["sumOpenInterestValue"]),
            "change_pct": (float(latest["sumOpenInterest"])/float(result[-2]["sumOpenInterest"])-1)*100
                if len(result) > 1 and float(result[-2]["sumOpenInterest"]) else None} if kind == "oi" else {
            "funding_rate": float(latest["fundingRate"])}
        output[kind] = {**_metric(value, source="Binance USD-M historical futures; zero production weight",
            timestamp=stamp.isoformat(), status="STALE" if stale else "AVAILABLE"),
            "retrieved_at": retrieved, "age_seconds": age,
            "warning": "Historical publication time approximates availability; actual installation receipt unknown"}
    return output


def feature_snapshot(evidence: dict, symbol: str, at: datetime, *, settings: TradeAlertSettings,
                     replay_snapshot: dict | None = None, raw_dir: Path | None = None) -> dict:
    """Extend a saved production observation with existing causal TA research."""
    histories = evidence["histories"].get(symbol, {})
    frames = {frame: known_prefix(bars, at, settings.history_bars) for frame, bars in histories.items()}
    one = frames.get("1m", [])
    observed = (replay_snapshot or {}).get("observations", {}).get(symbol, {})
    rows = [row for row in (replay_snapshot or {}).get("rows", []) if row.get("symbol") == symbol]
    long = next((row for row in rows if row.get("bias") == "LONG"), {})
    short = next((row for row in rows if row.get("bias") == "SHORT"), {})
    pressure = long.get("metrics", observed.get("directions", {}).get("LONG", {}).get("metrics", {}))
    intrinsic = intrinsic_five_minute_metrics(frames.get("5m", []), at)
    metrics = {**intrinsic["metrics"], **pressure}
    stamp = one[-1].close_time.isoformat() if one else None
    five_stamp = frames.get("5m", [])[-1].close_time.isoformat() if frames.get("5m") else None
    price = one[-1].close if one else None
    technical, structures, overlays = {}, {}, []
    for frame, bars in frames.items():
        if len(bars) < 20 or any(a.close_time != b.open_time for a, b in zip(bars[:-1], bars[1:], strict=True)):
            technical[frame] = {"status": "INSUFFICIENT_DATA", "bars": len(bars)}
            continue
        report = analyze(bars, frame, at)
        swings = confirmed_swings(bars)
        directional = {}
        for kind, positive, negative in (("high", "HH", "LH"), ("low", "HL", "LL")):
            points = [s for s in swings if s["kind"] == kind]
            directional[kind] = positive if len(points) >= 2 and points[-1]["price"] > points[-2]["price"] else (
                negative if len(points) >= 2 and points[-1]["price"] < points[-2]["price"] else "UNAVAILABLE")
        technical[frame] = {"status": "DERIVED", "metrics": report.metrics,
            "patterns": report.patterns, "issues": report.issues,
            "volume_profile": report.levels.get("volume_profile"),
            "volume_profile_quality": "ESTIMATED_CANDLE_DERIVED_NOT_LIVE_RESTING_ORDERS",
            "anchors": [anchor.model_dump(mode="json") for anchor in report.anchors]}
        structures[frame] = {"state": directional, "last_confirmed_swings": swings[-4:],
            "source_timestamp": bars[-1].close_time.isoformat(),
            "algorithm": "Existing confirmed_swings(left=3,right=3); confirmation candles available by decision"}
        for kind in ("support", "resistance"):
            for level in report.levels.get(kind, []):
                overlays.append({"kind": kind.upper(), "price": level, "timeframe": frame,
                    "discovered_at": bars[-1].available_at.isoformat(), "quality": "DERIVED"})
    atr = metrics.get("atr", technical.get("5m", {}).get("metrics", {}).get("atr14"))
    fib = fibonacci_research(frames, overlays, as_of=at, current_price=price, atr=atr)
    full_one = known_prefix(histories.get("1m", []), at)
    session_rows = [bar for bar in full_one if bar.open_time >= at.replace(hour=0, minute=0, second=0, microsecond=0)]
    session_complete = bool(session_rows) and session_rows[0].open_time == at.replace(hour=0, minute=0, second=0, microsecond=0)
    session_contiguous = all(a.close_time == b.open_time for a, b in zip(session_rows[:-1], session_rows[1:], strict=True))
    volume = sum(bar.volume for bar in session_rows)
    vwap = sum(bar.quote_volume for bar in session_rows)/volume if (session_complete and session_contiguous
        and volume and all(bar.quote_volume is not None for bar in session_rows)) else None
    features = {
        "compression": _metric(metrics.get("range_compression"), source="Existing 5m Fast pressure_metrics", timestamp=five_stamp),
        "volatility_contraction": _metric(metrics.get("atr_compression"), source="Existing 5m ATR compression", timestamp=five_stamp),
        "repeated_level_pressure": _metric({"tests": metrics.get("resistance_test_count"),
            "trigger": long.get("trigger"), "distance_atr": long.get("distance_atr"),
            "rejection_weakening": metrics.get("rejection_weakening")} if metrics else None,
            source="Existing deterministic 5m resistance/tests with last available 1m price distance", timestamp=stamp,
            status="DERIVED" if pressure else "UNAVAILABLE",
            reason=None if pressure else "No predefined level exists; intrinsic candle features do not invent one"),
        "HH_HL_LH_LL": _metric(structures or None, source="Existing causal confirmed_swings", timestamp=stamp),
        "micro_structure": _metric(structures.get("1m"), source="Completed native 1m candles; not a tick reconstruction", timestamp=stamp),
        "liquidity_proximity": _metric({"distance_major_resistance_pct": technical.get("5m", {}).get("metrics", {}).get("distance_major_resistance_pct"),
            "distance_support_pct": technical.get("5m", {}).get("metrics", {}).get("distance_support_pct")},
            source="Historical structural pivots; inferred stops are not observed orders", timestamp=stamp, status="ESTIMATED"),
        "liquidity_sweeps": structural_price_sweeps(one, at),
        "volume_acceleration": _metric(metrics.get("volume_acceleration"), source="Existing native closed 5m volume acceleration", timestamp=five_stamp),
        "RVOL": _metric(metrics.get("relative_volume"), source="Existing prior closed 5m volume baseline", timestamp=five_stamp),
        "relative_strength": _metric(observed.get("btc_context") or None, source="Existing aligned coin/BTC native 5m closes", timestamp=five_stamp),
        "rank_velocity": _metric(long.get("rank", {}).get("rank_velocity"), source="Replayed supplied cohort; NOT historical top-500 rank", timestamp=stamp),
        "rank_acceleration": _metric(long.get("rank", {}).get("rank_acceleration"), source="Replayed supplied cohort; NOT historical top-500 rank", timestamp=stamp),
        "VWAP": _metric({"session_vwap": vwap, "distance_pct": (price/vwap-1)*100 if price and vwap else None}
            if vwap else None, source="UTC-session native quote/base traded volume; complete closed minute coverage", timestamp=stamp,
            reason=None if vwap else "Complete UTC session coverage unavailable"),
        "Fib_location": _metric({"golden_pockets": fib["golden_pockets"], "anchors": fib["anchors"],
            "scores": fib["scores"]} if fib["anchors"] else None, source="Existing point-in-time fibonacci_research; no predictive weight", timestamp=stamp),
        "session_context": _metric(session_context(at), source="Existing IANA timezone/DST calendar rules; zero predictive weight", timestamp=at.isoformat()),
        "BTC_context": _metric(observed.get("btc_context") or None, source="Existing causal aligned BTC 5m returns", timestamp=five_stamp),
        "order_flow": _metric({"taker_buy_fraction_5m": metrics.get("taker_buy_ratio"),
            "taker_buy_volume_1m": one[-1].taker_buy_volume if one else None,
            "taker_sell_volume_1m": one[-1].volume-one[-1].taker_buy_volume if one and one[-1].taker_buy_volume is not None else None},
            source="Native Binance kline taker-buy base volume; not synthetic CVD or archived trade tape", timestamp=stamp,
            status="AVAILABLE" if one and one[-1].taker_buy_volume is not None else "UNAVAILABLE"),
        "order_book": _metric(None, source="Historical order-book archive", reason="No historical order-book snapshots were supplied"),
    }
    features["order_flow"]["component_timestamps"] = {"taker_buy_fraction_5m": five_stamp,
        "taker_buy_volume_1m": stamp, "taker_sell_volume_1m": stamp}
    derivatives = _derivatives(raw_dir, symbol, at) if raw_dir else {}
    for kind in ("oi", "funding"):
        features[kind] = derivatives.get(kind, _metric(None, source="Historical Binance USD-M data",
            reason="No historical derivatives archive supplied"))
    return {"symbol": symbol, "decision_time": at.isoformat(), "price": price,
        "long_state": long.get("state", "UNAVAILABLE"), "short_state": short.get("state", "UNAVAILABLE"),
        "long_score": long.get("score"), "short_score": short.get("score"),
        "long_rank": long.get("rank"), "short_rank": short.get("rank"),
        "long_pressure": long, "short_pressure": short, "features": features,
        "intrinsic_5m_metrics": intrinsic,
        "technical": technical, "available_at_max": max((bar.available_at for bars in frames.values() for bar in bars), default=at).isoformat(),
        "availability_assumption": RECEIPT_ASSUMPTION, "real_installation_receipt": "UNAVAILABLE"}


def _control_features(evidence, symbol, at):
    bars = known_prefix(evidence["histories"][symbol].get("1m", []), at, 61)
    if len(bars) < 31:
        return None
    returns = [math.log(b.close/a.close) for a, b in zip(bars[:-1], bars[1:], strict=True)]
    centre = mean(returns)
    volatility = math.sqrt(mean((value-centre)**2 for value in returns))
    turnover = sum(bar.quote_volume or 0 for bar in bars[-60:])
    btc = known_prefix(evidence["histories"].get("BTCUSDT", {}).get("1m", []), at, 31)
    market = (btc[-1].close/btc[0].close-1) if len(btc) >= 31 else None
    return {"volatility": volatility, "quote_volume_60m": turnover,
        "btc_regime": "UP" if market is not None and market > .001 else "DOWN" if market is not None and market < -.001 else "RANGE" if market is not None else "UNAVAILABLE",
        "computed_at": at.isoformat(), "latest_feature_available_at": bars[-1].available_at.isoformat()}


def make_controls(evidence: dict, start: datetime, end: datetime, definition: ExpansionDefinition) -> list[dict]:
    """Fixed clock/cohort windows are selected before reading their future."""
    output = []
    at = start
    while at < end:
        for symbol in sorted(evidence["histories"]):
            prefix = known_prefix(evidence["histories"][symbol].get("1m", []), at)
            if not prefix or prefix[-1].close_time != at:
                continue
            baseline = prefix[-1]
            future = [bar for bar in evidence["histories"][symbol].get("1m", []) if bar.open_time >= at]
            control = label_control_window(symbol, baseline, future, evidence["as_of"], definition)
            control.update(features=_control_features(evidence, symbol, at),
                selection_rule="Every fixed UTC hour for every supplied cohort asset; chosen independently of outcomes",
                preselected_at=at.isoformat())
            output.append(control)
        at += timedelta(hours=1)
    return output


def match_controls(evidence: dict, labels: list[dict], controls: list[dict]) -> list[dict]:
    """Nearest past-feature controls, without outcome-conditioned selection."""
    output = []
    for label in labels:
        at = _time(label["expansion_start"])
        features = _control_features(evidence, label["symbol"], at)
        candidates = [row for row in controls if row.get("features") and row["symbol"] != label["symbol"]
            and 0 <= (at-_time(row["start"])).total_seconds() <= 3600]
        aligned = [row for row in candidates if features and row["features"]["btc_regime"] == features["btc_regime"]]
        relaxation = None
        if not aligned:
            aligned, relaxation = candidates, "No comparable BTC-regime controls; regime restriction relaxed"
        def distance(row, features=features):
            if not features:
                return math.inf
            other = row["features"]
            return abs(math.log((other["volatility"]+1e-12)/(features["volatility"]+1e-12))) + abs(
                math.log((other["quote_volume_60m"]+1)/(features["quote_volume_60m"]+1)))
        chosen = sorted(aligned, key=lambda row: (distance(row), row["symbol"], row["start"]))[:5]
        output.append({"event_id": label["event_id"], "symbol": label["symbol"], "features": features,
            "matches": [{"symbol": row["symbol"], "start": row["start"], "feature_distance": distance(row),
                "features": row["features"], "outcome_status_only_after_matching": row["status"]} for row in chosen],
            "relaxation": relaxation, "selection": "Log-scaled past volatility/turnover, contemporaneous BTC regime; outcomes never enter distance",
            "limitations": ["Five controls maximum, not 50–100", "Market-cap bucket unavailable", "Same-cohort retrospective selection; independent holdout still required"]})
    return output


def _common_features(snapshots: list[dict], settings: TradeAlertSettings) -> dict:
    counts, denominators = Counter(), Counter()
    for row in snapshots:
        pressure = row.get("long_pressure", {})
        metrics = row.get("intrinsic_5m_metrics", {}).get("metrics", {})
        level_metrics = pressure.get("metrics", {})
        tests = {
            "5m_range_compression_at_least_configured_minimum": (metrics.get("range_compression"), lambda x: x >= settings.pre_move["minimum_compression"]),
            "5m_higher_low_strength_at_least_configured_minimum": (metrics.get("higher_low_strength"), lambda x: x >= settings.minimum_structure),
            "independent_resistance_tests_at_least_configured_minimum": (level_metrics.get("resistance_test_count"), lambda x: x >= settings.minimum_tests),
            "native_taker_buy_fraction_at_least_configured_minimum": (metrics.get("taker_buy_ratio"), lambda x: x >= settings.minimum_taker_pressure),
            "native_5m_volume_acceleration_positive": (metrics.get("volume_acceleration"), lambda x: x > 0),
            "RVOL_above_own_prior_average": (metrics.get("relative_volume"), lambda x: x > 1),
            "within_configured_trigger_proximity_ATR": (pressure.get("distance_atr"), lambda x: 0 <= x <= settings.proximity_atr),
            "replayed_cohort_rank_improving": ((row.get("long_rank") or {}).get("rank_velocity"), lambda x: x > 0),
        }
        for key, (value, qualifies) in tests.items():
            if _finite(value) is not None:
                denominators[key] += 1
                counts[key] += bool(qualifies(value))
    return {"snapshot_rule": "One T-5m snapshot per requested qualifying LONG episode",
        "sample_size": len(snapshots), "features": {key: {"present": counts[key], "available": total}
            for key, total in denominators.items()}, "features_in_at_least_5_moves": [key for key, count in counts.items() if count >= 5],
        "interpretation": "Descriptive selected-winner counts; no causal or predictive evidence without control/holdout contrast"}


def correct_pre_move_detection(label: dict, signals: list[dict]) -> dict:
    """Descriptive matching-direction time, without replacing benchmark failure.

    The benchmark still scores the first meaningful alert, including an earlier
    opposite-direction alert. This separate field must not relabel that failure
    as a success merely because a later same-direction alert looked useful.
    """
    start = _time(label["expansion_start"])
    begin = start-timedelta(minutes=label["definition"]["lookback_minutes"])
    candidates = [row for row in signals if row.get("symbol") == label["symbol"] and row.get("bias") == label["bias"]
        and row.get("state", row.get("phase")) in EARLY_STATES and row.get("status") in {"AVAILABLE", "ACTIVE"}
        and _finite(row.get("price")) is not None and row["price"] > 0
        and begin <= _time(row.get("detected_at", row.get("as_of"))) < start]
    row = min(candidates, key=lambda item: _time(item.get("detected_at", item.get("as_of"))), default=None)
    return {"earliest_detectable_pre_move_time": row.get("detected_at", row.get("as_of")) if row else None,
        "price_move_at_that_time_pct": (1 if label["bias"] == "LONG" else -1)*(row["price"]/label["baseline_price"]-1)*100 if row else None,
        "move_remaining_pct": max(0, (1 if label["bias"] == "LONG" else -1)*(label["peak_price"]/row["price"]-1)*100) if row else None,
        "expected_alert_state": row.get("state", row.get("phase")) if row else "UNAVAILABLE",
        "earliest_correct_pre_move_alert": row,
        "selection_warning": "Descriptive same-direction pre-onset field; conservative first-alert benchmark remains unchanged"}


def repair_report_detection_fields(report: dict, signals: list[dict]) -> dict:
    """Re-render case descriptions from saved replay without running it again."""
    for case in report["cases"]:
        if not case.get("label"):
            continue
        case.update(correct_pre_move_detection(case["label"], signals))
        case["first_meaningful_alert_conservative"] = case["benchmark"].get("first_alert")
    return report


def phase_detection_metrics(labels: list[dict], alerts: list[dict], transitions: list[dict], *,
                            controls: list[dict], as_of: datetime) -> dict:
    """Separate native warning phases from confirmation and recorded states.

    A lifecycle transition is not proof of a delivered notification. A local
    BREAK_CONFIRMED preceding a larger episode is not a PRE_IGNITION warning.
    Each phase reuses the same conservative, outcome-only benchmark.
    """
    groups = {
        "STRUCTURE_WATCH_notifications": [row for row in alerts if row.get("phase") == "STRUCTURE_WATCH"],
        "TRADE_ALERT_notifications": [row for row in alerts if row.get("phase") == "TRADE_ALERT"],
        "PRE_IGNITION_or_LOADED_transitions": [row for row in transitions if row.get("state") in {"PRE_IGNITION", "LOADED"}],
        "BREAK_CONFIRMED_notifications": [row for row in alerts if row.get("phase") == "BREAK_CONFIRMED"],
    }
    return {name: evaluate_early_detection(labels, rows, controls=controls, as_of=as_of)
            for name, rows in groups.items()}


def run_forensics(raw_dir: Path, day: date, output_dir: Path, *, config_path: Path,
                  receipt_delay_seconds: float = 0) -> dict:
    evidence = load_evidence(raw_dir, receipt_delay_seconds=receipt_delay_seconds)
    settings = TradeAlertSettings.model_validate_json(config_path.read_text())
    definition = ExpansionDefinition()
    day_start = datetime.combine(day, datetime.min.time(), UTC)
    previous_start = day_start-timedelta(days=1)
    labels = []
    for symbol, histories in evidence["histories"].items():
        labels.extend(label_expansions(symbol, histories.get("1m", []), evidence["as_of"], definition))
    labels = [label for label in labels if previous_start <= _time(label["expansion_start"]) < day_start+timedelta(days=1)]
    today = [label for label in labels if day_start <= _time(label["expansion_start"])]
    previous = [label for label in labels if _time(label["expansion_start"]) < day_start]
    selected = {}
    for base in TARGETS:
        candidates = [label for label in today if label["symbol"] == base+"USDT" and label["bias"] == "LONG"]
        if candidates:
            selected[base+"USDT"] = max(candidates, key=lambda label: label["total_move_pct"])
    requested_times = {_time(label["expansion_start"])-timedelta(minutes=minutes)
        for label in selected.values() for minutes in OFFSETS}
    boundaries = sorted({bar.available_at for histories in evidence["histories"].values()
        for bar in histories.get("1m", [])})
    snapshot_times = {max((when for when in boundaries if when <= at), default=at) for at in requested_times}
    replay = replay_pre_move(evidence["assets"], evidence["histories"], evidence["as_of"],
        decision_start=previous_start-timedelta(hours=1), settings=settings, snapshot_times=snapshot_times)
    observations = {entry["as_of"]: entry for entry in replay["snapshots"]}
    legacy = replay_fast_monitor(evidence["assets"], evidence["histories"], evidence["as_of"],
        decision_start=previous_start-timedelta(hours=1), snapshot_times=snapshot_times)
    control_windows = make_controls(evidence, previous_start, evidence["as_of"], definition)
    signals = [*replay["alerts"], *replay["state_transitions"]]
    metrics = evaluate_early_detection(today, signals, controls=[row for row in control_windows if _time(row["start"]) >= day_start], as_of=evidence["as_of"])
    previous_metrics = evaluate_early_detection(previous, signals, controls=[row for row in control_windows if _time(row["start"]) < day_start], as_of=evidence["as_of"])
    independent = [label for label in labels if label["symbol"] in {base+"USDT" for base in CONTROLS}]
    independent_metrics = evaluate_early_detection(independent, signals,
        controls=[row for row in control_windows if row["symbol"] in {base+"USDT" for base in CONTROLS}], as_of=evidence["as_of"])
    legacy_metrics = evaluate_early_detection(today, legacy["alerts"],
        controls=[row for row in control_windows if _time(row["start"]) >= day_start], as_of=evidence["as_of"])
    case_lookup = {row["event_id"]: row for row in metrics["cases"]}
    cases, five_minute = [], []
    for base in TARGETS:
        symbol = base+"USDT"
        label = selected.get(symbol)
        if not label:
            cases.append({"symbol": symbol, "status": "DATA_UNAVAILABLE" if symbol not in evidence["histories"] else "NO_QUALIFYING_EXPANSION",
                "reason": "No supported Binance Spot native history supplied" if symbol not in evidence["histories"] else
                    "No completed one-minute-close LONG episode met the predeclared +3%/60m criterion before the cutoff; not proof of no daily rally",
                "earliest_detectable_pre_move_time": None, "price_move_at_that_time_pct": None,
                "move_remaining_pct": None, "expected_alert_state": "UNAVAILABLE", "snapshots": []})
            continue
        snapshots = []
        for minutes in OFFSETS:
            at = _time(label["expansion_start"])-timedelta(minutes=minutes)
            prior = max((when for when in observations if _time(when) <= at), default=None)
            snapshot = feature_snapshot(evidence, symbol, at, settings=settings,
                replay_snapshot=observations.get(prior) if prior else None, raw_dir=raw_dir)
            snapshot.update(minutes_before=minutes, price_move_from_episode_baseline_pct=(snapshot["price"]/label["baseline_price"]-1)*100
                if snapshot["price"] else None)
            snapshots.append(snapshot)
            if minutes == 5:
                five_minute.append(snapshot)
        measured = case_lookup[label["event_id"]]
        alert = measured["first_alert"]
        last = snapshots[-1]
        long = last.get("long_pressure", {})
        blockers = []
        if long.get("status") != "AVAILABLE":
            blockers.append("No fresh eligible production directional observation/known level")
        if (long.get("score") or 0) < settings.pre_move["pre_ignition_score"]:
            blockers.append("Frozen directional pressure below PRE_IGNITION threshold")
        if (long.get("score") or 0) < settings.alert_score:
            blockers.append("Frozen directional pressure below main TRADE_ALERT threshold")
        distance = long.get("distance_atr")
        if distance is None or not 0 <= distance <= settings.proximity_atr:
            blockers.append("Predefined local 5m resistance absent, already crossed, or outside ATR proximity")
        cases.append({"symbol": symbol, "status": measured["status"], "label": label,
            "benchmark": measured, **correct_pre_move_detection(label, signals),
            "first_meaningful_alert_conservative": alert,
            "why_should_detect": "Frozen production replay generated a correct pre-onset alert" if measured["early_detection_pass"] else
                "Not demonstrated under frozen current rules; retrospective attractive precursors do not establish detectability",
            "why_current_system_missed": "ACTUAL_LIVE_MISS_CAUSE_UNVERIFIABLE: local archived coverage/receipt/alerts unavailable for this day; external/Render logs not inspected. Counterfactual replay diagnostics only.",
            "counterfactual_blockers_at_T_minus_1m": blockers,
            "snapshots": snapshots})
    source = {key: value for key, value in evidence.items() if key not in {"assets", "histories", "markets"}}
    source["as_of"] = evidence["as_of"].isoformat()
    folds = walk_forward_splits([{ "event_id": row["event_id"], "decision_time": row["expansion_start"],
        "label_end": row["horizon_end"], "label_available_at": row["label_available_at"],
        "outcome": row["total_move_pct"]} for row in sorted(labels, key=lambda item: item["expansion_start"])], [(day_start-timedelta(hours=1), day_start)],
        horizon_seconds=3600, embargo_seconds=0) if labels else []
    result = {"benchmark": "TOP MOVER EARLY DETECTION TEST", "date": day.isoformat(),
        "as_of": evidence["as_of"].isoformat(), "definition": definition.__dict__,
        "frozen_configuration": settings.model_dump(mode="json"), "config_sha256": _digest(config_path),
        "cases": cases, "today_metrics": metrics, "previous_day_validation": previous_metrics,
        "independent_fixed_asset_validation": independent_metrics, "walk_forward_splits": folds,
        "phase_detection_metrics": phase_detection_metrics(today, replay["alerts"], replay["state_transitions"],
            controls=[row for row in control_windows if _time(row["start"]) >= day_start], as_of=evidence["as_of"]),
        "legacy_fast_monitor_metrics": legacy_metrics,
        "legacy_fast_monitor_assumption": legacy["membership_assumption"],
        "controls": control_windows, "matched_controls": match_controls(evidence, list(selected.values()), control_windows),
        "common_pre_move_features": _common_features(five_minute, settings), "sources": source,
        "production_changes": "NONE; offline benchmark only", "execution_enabled": False,
        "replay_latency_provenance": "Latency fields in model outputs follow the explicitly assumed historical receipt delay. No actual notification delivery latency was measured.",
        "risk": "Uncalibrated frozen hypotheses; no probability, profitability or edge claim",
        "limitations": ["Today's date is UTC and observations end at the frozen cutoff, not the full day.",
            "Onset is an outcome-labelled close proxy; no independent tick-level first expansion timestamp.",
            "REST downloaded today does not prove contemporaneous local receipt or unchanged revisions.",
            "Ranks are replayed within this fixed small supplied cohort, not archived top-500 ranks.",
            "Local archived live alert/coverage/latency history does not prove why the installation missed a move; external/Render logs were not inspected.",
            "Selected winners are biased; independent fixed controls may contain winners and are retained.",
            "One previous day and six independent assets are a small diagnostic validation, not robust walk-forward edge evidence.",
            "Current exchange metadata cannot reconstruct historical listing/liquidity eligibility.",
            "No production weights/thresholds were changed using the nine requested symbols."]}
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "TOP_MOVER_EARLY_DETECTION_TEST.json").write_text(json.dumps(result, indent=2, allow_nan=False), encoding="utf-8")
    (output_dir / "replay_signals.json").write_text(json.dumps({"alerts": replay["alerts"],
        "state_transitions": replay["state_transitions"], "rank_scope": replay["rank_scope"],
        "legacy_alerts": legacy["alerts"], "legacy_membership_assumption": legacy["membership_assumption"],
        "replay_latency_provenance": result["replay_latency_provenance"]}, indent=2, allow_nan=False), encoding="utf-8")
    (output_dir / "TOP_MOVER_EARLY_DETECTION_TEST.md").write_text(render_markdown(result), encoding="utf-8")
    return result


def _format(value, suffix=""):
    return f"{value:.4f}{suffix}" if isinstance(value, (int, float)) else "UNAVAILABLE" if value is None else str(value)


def render_markdown(report: dict) -> str:
    lines = ["# TOP MOVER EARLY DETECTION TEST", "", f"UTC date: {report['date']}; frozen cutoff: {report['as_of']}.",
        "Real native Binance Spot historical klines; frozen existing production config. NO PRODUCTION RULE CHANGES.",
        "", "Outcome definition: completed 1m onset proxy: first >=0.15% directional close; >=3% within a fully observed 60m horizon. "
        "Overlapping 60m episodes excluded. Largest qualifying LONG episode per requested asset. All LONG/SHORT episodes retained for validation.",
        "", "Historical receipt assumption: close + declared delay. Actual live receipt/coverage/alerts are UNAVAILABLE. "
        "A replay alert proves a frozen-rule counterfactual, not that the live installation actually received or notified it.", ""]
    phase = report.get("phase_detection_metrics", {}).get("PRE_IGNITION_or_LOADED_transitions", {})
    if phase:
        lines.extend([f"**Primary pre-ignition objective: {phase['early_detection_count']} / {phase['expansion_count']} episodes passed.** "
            "Broad watch and local break-confirmation matches below do not count as validated PRE_IGNITION/LOADED warnings.", ""])
    for case in report["cases"]:
        lines.extend([f"## {case['symbol']}", "", f"Status: **{case['status']}**."])
        if not case.get("label"):
            lines.extend([case["reason"], "EARLIEST DETECTABLE PRE-MOVE TIME: UNAVAILABLE", ""])
            continue
        label, benchmark = case["label"], case["benchmark"]
        lines.extend([f"Expansion onset proxy: {label['expansion_start']}; +{label['total_move_pct']:.3f}% by {label['peak_time']}.",
            f"EARLIEST DETECTABLE PRE-MOVE TIME: {case['earliest_detectable_pre_move_time'] or 'NONE DEMONSTRATED'}",
            f"PRICE MOVE AT THAT TIME (vs onset baseline): {_format(case['price_move_at_that_time_pct'], '%')}",
            f"MOVE STILL REMAINING (alert to observed peak): {_format(case['move_remaining_pct'], '%')}",
            f"EXPECTED ALERT STATE: {case['expected_alert_state']}",
            f"Conservative first-alert benchmark: {benchmark['status']}; lead {_format(benchmark['lead_time_seconds'], 's')}; completed fraction {_format(benchmark['move_completed_fraction'])}.",
            f"WHY IT SHOULD HAVE BEEN DETECTED: {case['why_should_detect']}",
            f"WHY CURRENT SYSTEM MISSED IT: {case['why_current_system_missed']}",
            "Counterfactual rule blockers at T-1m: " + ("; ".join(case["counterfactual_blockers_at_T_minus_1m"]) or "None of the summarized gates; full native episode history is in replay_signals.json."),
            "", "| Before | UTC decision | Price | LONG state / score | SHORT state / score | Compression | RVOL | Volume acceleration | Rank velocity |", "|---|---|---:|---|---|---:|---:|---:|---:|"])
        for row in case["snapshots"]:
            features = row["features"]
            lines.append(f"| T-{row['minutes_before']}m | {row['decision_time']} | {_format(row['price'])} | {row['long_state']} / {_format(row['long_score'])} | "
                f"{row['short_state']} / {_format(row['short_score'])} | {_format(features['compression']['value'])} | {_format(features['RVOL']['value'])} | "
                f"{_format(features['volume_acceleration']['value'])} | {_format(features['rank_velocity']['value'])} |")
        lines.extend(["", "All requested features, source/availability tags, causal swing anchors, session/BTC/Fib/VWAP/profile details and optional futures context are in the JSON snapshots.", ""])
    lines.extend(["## Cross-symbol and separate validation", "", "All accuracy/rate values below are fractions from 0 to 1, not calibrated probabilities.", "",
        "| Cohort | Expansions | Early passes | Direction accuracy | Mean lead (s) | Non-move windows | False-positive windows / rate |", "|---|---:|---:|---:|---:|---:|---:|"])
    for title, key in (("Requested day / full supplied cohort", "today_metrics"), ("Previous UTC day / same frozen rules", "previous_day_validation"),
                       ("Fixed ETH/XRP/ADA/OP/ARB/NEAR cohort", "independent_fixed_asset_validation"),
                       ("Legacy app Fast monitor, all cohort assumed eligible", "legacy_fast_monitor_metrics")):
        value = report[key]
        lines.append(f"| {title} | {value['expansion_count']} | {value['early_detection_count']} | {_format(value['direction_accuracy'])} | "
            f"{_format(value['mean_alert_lead_time_seconds'])} | {value['non_move_control_count']} | {value['false_positive_control_windows']} / {_format(value['false_positive_rate'])} |")
    if report.get("phase_detection_metrics"):
        lines.extend(["", "### Native phases reported separately", "",
            "Recorded lifecycle states are not delivery evidence. BREAK_CONFIRMED before a larger episode may confirm an earlier local move; it is not PRE_IGNITION. "
            "The broad any-state control rate above must not be called a TRADE_ALERT notification failure rate.", "",
            "| Native phase | Early passes / episodes | Direction accuracy among calls | Non-move windows called / rate |",
            "|---|---:|---:|---:|"])
        for phase, value in report["phase_detection_metrics"].items():
            lines.append(f"| {phase} | {value['early_detection_count']} / {value['expansion_count']} | "
                f"{_format(value['direction_accuracy'])} | {value['false_positive_control_windows']} / {_format(value['false_positive_rate'])} |")
    common = report["common_pre_move_features"]
    lines.extend(["", f"COMMON PRE-MOVE FEATURES FOUND: T-5m diagnostic snapshots; n={common['sample_size']}. Counts below reflect selected winners, not an edge.", ""])
    for key, value in common["features"].items():
        lines.append(f"- {key}: {value['present']} / {value['available']} available snapshots.")
    lines.extend(["", "FEATURES PRESENT IN 5+ MOVES: " + (", ".join(common["features_in_at_least_5_moves"]) or "NONE under the declared rules/coverage."),
        "", "FALSE SIGNAL RISKS: repeated local tests, high RVOL or native taker-buy fraction also occur in non-moves; opposite-direction alerts and post-onset alerts count as failures. "
        "Hourly controls are selected before labels and other winners are retained. Control false-positive rate is window-based, not trade win rate or notification precision.",
        "", "REQUIRED ENGINE CHANGES: no new pressure rule has earned promotion. Generalized candidates for later independent tests are: multi-scale predetermined resistance/support; "
        "causal slower-ramp onset labels; independent native flow with complete receipt coverage; stable universe/rank history; volume/compression interactions rather than separate votes. "
        "Threshold lowering must be rejected if control false alarms rise. These are hypotheses, not changes tuned to these assets.",
        "", "## Evidence limits", ""])
    lines.extend(f"- {issue}" for issue in report["limitations"])
    lines.extend(["", f"Frozen config SHA256: `{report['config_sha256']}`.",
        "Native downloads, separate retrieval timestamps, gap/duplicate checks and SHA256 manifest are included in the JSON. "
        "No fake order book, OI, funding, historical rank, live latency or calibrated probabilities were constructed.", ""])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", type=Path, required=True)
    parser.add_argument("--date", type=date.fromisoformat, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=Path("config/trade_alert.json"))
    parser.add_argument("--receipt-delay-seconds", type=float, default=0,
        help="Explicit historical availability assumption; not a measured live receipt")
    options = parser.parse_args(argv)
    report = run_forensics(options.raw_dir, options.date, options.output_dir,
        config_path=options.config, receipt_delay_seconds=options.receipt_delay_seconds)
    print(json.dumps({"benchmark": report["benchmark"], "report": str(options.output_dir / "TOP_MOVER_EARLY_DETECTION_TEST.md"),
        "cases": {case["symbol"]: case["status"] for case in report["cases"]},
        "early_detection_count": report["today_metrics"]["early_detection_count"], "production_changed": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
