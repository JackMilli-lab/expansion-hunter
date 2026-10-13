"""Causal anchors and acceptance of an OHLCV-derived anchored VWAP.

VWAP uses native quote/base volume when supplied and a typical-price OHLCV
estimate otherwise. Each report declares its method. A pivot anchor becomes usable at its confirmation,
never at the pivot itself. Breakout thresholds are fixed when an episode starts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite

from .models import AnchorReport, Candle

_LOOKBACK = 20
_TOUCH_TOLERANCE = 0.003
_ACCEPTANCE_TOLERANCE = 0.0005


@dataclass(frozen=True)
class _Anchor:
    kind: str
    index: int
    confirmed_index: int
    confirmed_at: datetime


def anchored_vwap(candles: list[Candle], anchor_index: int) -> list[float | None]:
    """Return a running AVWAP aligned to the input, with unavailable values None.

    Zero-volume bars carry an already established VWAP; an all-zero-volume
    anchor has no price estimate. Every element uses only that bar and earlier
    observations. An anchor outside the supplied history is an input error.
    """
    if (
        not isinstance(anchor_index, int)
        or isinstance(anchor_index, bool)
        or not 0 <= anchor_index < len(candles)
    ):
        raise ValueError("anchor_index must refer to an input candle")
    values: list[float | None] = [None] * anchor_index
    volume = 0.0
    weighted_price = 0.0
    for candle in candles[anchor_index:]:
        volume += candle.volume
        weighted_price += (
            candle.quote_volume
            if candle.quote_volume is not None
            else ((candle.high + candle.low + candle.close) / 3) * candle.volume
        )
        values.append(weighted_price / volume if volume > 0 else None)
    return values


def _true_range(candles: list[Candle], index: int) -> float:
    candle = candles[index]
    if not index:
        return candle.high - candle.low
    previous_close = candles[index - 1].close
    return max(candle.high - candle.low, abs(candle.high - previous_close), abs(candle.low - previous_close))


def _baseline(candles: list[Candle], index: int) -> tuple[float, float] | None:
    """Mean prior true range and volume; the candidate bar is excluded."""
    if index < _LOOKBACK:
        return None
    first = index - _LOOKBACK
    return (
        sum(_true_range(candles, j) for j in range(first, index)) / _LOOKBACK,
        sum(c.volume for c in candles[first:index]) / _LOOKBACK,
    )


def _discovery_times(candles: list[Candle]) -> list[datetime]:
    """When each historical prefix was completely available, including delays."""
    result: list[datetime] = []
    for candle in candles:
        result.append(max(result[-1], candle.available_at) if result else candle.available_at)
    return result


def _supplied_confirmation(swing: dict) -> datetime | None:
    raw = swing.get("confirmed_at")
    if isinstance(raw, str):
        try:
            raw = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(raw, datetime) and raw.tzinfo is not None and raw.utcoffset() is not None:
        return raw.astimezone(UTC)
    return None


def _swing_anchors(
    candles: list[Candle],
    swings: list[dict],
    discovery: list[datetime],
    known_at: datetime,
) -> list[_Anchor]:
    latest: dict[str, _Anchor] = {}
    for swing in swings:
        index, confirmation = swing.get("index"), swing.get("confirmed_index")
        kind, price = swing.get("kind"), swing.get("price")
        if (
            not isinstance(index, int)
            or isinstance(index, bool)
            or not isinstance(confirmation, int)
            or isinstance(confirmation, bool)
            or not 0 <= index <= confirmation < len(candles)
            or kind not in {"high", "low"}
            or not isinstance(price, (int, float))
            or isinstance(price, bool)
            or not isfinite(price)
            or price <= 0
        ):
            continue
        supplied = _supplied_confirmation(swing)
        confirmed_at = max(discovery[confirmation], supplied) if supplied else discovery[confirmation]
        if confirmed_at > known_at:
            continue
        # Significance uses only observations available at pivot confirmation.
        # A one-TR excursion filters minor pivots; no future extremum is used.
        first = max(0, index - _LOOKBACK)
        before = candles[first:index]
        if not before:
            continue
        baseline_tr = sum(_true_range(candles, j) for j in range(first, index)) / len(before)
        excursion = (
            max(c.high for c in before) - price if kind == "low" else price - min(c.low for c in before)
        )
        if baseline_tr <= 0 or excursion < baseline_tr:
            continue
        name = "major_swing_low" if kind == "low" else "major_swing_high"
        existing = latest.get(name)
        if existing is None or index > existing.index:
            latest[name] = _Anchor(name, index, confirmation, confirmed_at)
    return list(latest.values())


def _event_anchors(candles: list[Candle], discovery: list[datetime]) -> list[_Anchor]:
    latest: dict[str, _Anchor] = {}
    fixed_breakout_level: float | None = None
    failed_closes = 0
    for index in range(_LOOKBACK, len(candles)):
        candle = candles[index]
        confirmed_at = discovery[index]
        baseline = _baseline(candles, index)
        assert baseline is not None
        prior_range, prior_volume = baseline
        if prior_volume > 0:
            if candle.volume >= 3 * prior_volume:
                latest["high_volume"] = _Anchor("high_volume", index, index, confirmed_at)
            width = candle.high - candle.low
            if (
                prior_range > 0
                and candle.close < candle.open
                and _true_range(candles, index) >= 2 * prior_range
                and candle.volume >= 2 * prior_volume
                and width > 0
                and (candle.close - candle.low) / width <= 0.35
            ):
                latest["capitulation"] = _Anchor("capitulation", index, index, confirmed_at)

        if fixed_breakout_level is not None:
            # Continue the original episode, retaining its original resistance.
            # Two closes back below it re-arm detection on the next bar.
            failed_closes = failed_closes + 1 if candle.close <= fixed_breakout_level else 0
            if failed_closes >= 2:
                fixed_breakout_level = None
                failed_closes = 0
            continue
        prior_high = max(c.high for c in candles[index - _LOOKBACK : index])
        if candles[index - 1].close <= prior_high < candle.close:
            fixed_breakout_level = prior_high
            latest["breakout_origin"] = _Anchor("breakout_origin", index, index, confirmed_at)
    return list(latest.values())


def _side(close: float, vwap: float) -> int:
    if close > vwap * (1 + _ACCEPTANCE_TOLERANCE):
        return 1
    if close < vwap * (1 - _ACCEPTANCE_TOLERANCE):
        return -1
    return 0


def _report(candles: list[Candle], anchor: _Anchor) -> AnchorReport:
    curve = anchored_vwap(candles, anchor.index)
    value = curve[-1]
    report = AnchorReport(
        kind=anchor.kind,
        anchor_time=candles[anchor.index].open_time,
        confirmed_at=anchor.confirmed_at,
        value=value,
        distance_pct=100 * (candles[-1].close / value - 1) if value else None,
        method=(
            "exchange quote/base traded volume"
            if all(c.quote_volume is not None for c in candles[anchor.index :])
            else "OHLCV typical-price estimate"
            if all(c.quote_volume is None for c in candles[anchor.index :])
            else "mixed native quote-volume / OHLCV estimate"
        ),
    )
    if value is None:
        return report

    # Discovery can occur after the confirmation candle closed (late delivery).
    # Earlier closes can describe the state *at* discovery, but acceptance must
    # consist of consecutive actual candle closes on or after discovery.
    first = max(anchor.index, anchor.confirmed_index)
    indexes = list(range(first, len(candles)))

    def eligible(index: int) -> bool:
        return bool(
            candles[index].close_time >= anchor.confirmed_at
            and candles[index].available_at >= anchor.confirmed_at
            and curve[index] is not None
        )

    if not indexes or not eligible(indexes[-1]):
        return report
    sides = [
        _side(candles[index].close, curve[index]) if curve[index] is not None else 0 for index in indexes
    ]
    consecutive_above = 0
    for index, side in zip(reversed(indexes), reversed(sides), strict=True):
        if not eligible(index) or side != 1:
            break
        consecutive_above += 1
    report.accepted_closes = consecutive_above
    # An anchor's first close above does not create a fictitious reclaim.
    # Neutral closes may lie between the last below and the first above.
    prior_sides = sides[:-consecutive_above] if consecutive_above else sides
    prior_direction = next((side for side in reversed(prior_sides) if side), 0)
    report.reclaim = consecutive_above >= 2 and prior_direction == -1

    # OHLCV cannot establish whether a wick occurred before a mid-bar discovery.
    # Only bars opened after the anchor was knowable can supply wick evidence;
    # a later close remains eligible for acceptance under the rule above.
    recent = [
        index for index in indexes[-3:] if eligible(index) and candles[index].open_time >= anchor.confirmed_at
    ]
    touched_from_above = any(
        index > anchor.confirmed_index
        and curve[index - 1] is not None
        and _side(candles[index - 1].close, curve[index - 1]) == 1
        and candles[index].low <= curve[index] * (1 + _TOUCH_TOLERANCE)
        and candles[index].close > curve[index] * (1 + _ACCEPTANCE_TOLERANCE)
        for index in recent
    )
    touched_from_below = any(
        index > anchor.confirmed_index
        and curve[index - 1] is not None
        and _side(candles[index - 1].close, curve[index - 1]) == -1
        and candles[index].high >= curve[index] * (1 - _TOUCH_TOLERANCE)
        and candles[index].close < curve[index] * (1 - _ACCEPTANCE_TOLERANCE)
        for index in recent
    )
    report.support = consecutive_above >= 2 and touched_from_above
    report.resistance = (
        len(sides) >= 2
        and all(eligible(index) for index in indexes[-2:])
        and sides[-2:] == [-1, -1]
        and touched_from_below
    )
    # Wick-through followed by a close below is rejection, never reclaim.
    report.rejection = (
        candles[-1].open_time >= anchor.confirmed_at
        and candles[-1].high >= value * (1 - _TOUCH_TOLERANCE)
        and candles[-1].close < value * (1 - _ACCEPTANCE_TOLERANCE)
    )
    return report


def analyze_anchors(
    candles: list[Candle],
    swings: list[dict],
    range_start_index: int | None,
    range_high: float | None,
    *,
    range_confirmed_index: int | None = None,
) -> list[AnchorReport]:
    """Analyze the most recent known anchor of each of six anchor types.

    Input must contain chronological, closed, contiguous candles filtered to a
    single point-in-time evaluation. ``swings`` supplies causally confirmed
    ``index``, ``kind``, ``price``, and ``confirmed_index`` values. A supplied
    range-start anchor uses its supplied causal recognition index. Callers
    without that provenance conservatively confirm it at this evaluation.

    ``range_high`` describes the current range for callers; breakout detection
    deliberately uses its own fixed prior-20-bar threshold, rather than fitting
    a current range retrospectively to old breakouts. Empty input returns [].
    """
    if not candles:
        return []
    if any(left.open_time >= right.open_time for left, right in zip(candles[:-1], candles[1:], strict=True)):
        raise ValueError("AVWAP candles must be in strictly increasing time order")
    discovery = _discovery_times(candles)
    known_at = discovery[-1]
    anchors = _swing_anchors(candles, swings, discovery, known_at) + _event_anchors(candles, discovery)
    if range_start_index is not None:
        if (
            not isinstance(range_start_index, int)
            or isinstance(range_start_index, bool)
            or not 0 <= range_start_index < len(candles)
        ):
            raise ValueError("range_start_index must refer to an input candle")
        if range_confirmed_index is None:
            confirmation = len(candles) - 1
        else:
            if (
                not isinstance(range_confirmed_index, int)
                or isinstance(range_confirmed_index, bool)
                or not range_start_index <= range_confirmed_index < len(candles)
            ):
                raise ValueError("range_confirmed_index must be at or after the range start")
            confirmation = range_confirmed_index
        anchors.append(_Anchor("range_start", range_start_index, confirmation, discovery[confirmation]))
    order = {
        kind: index
        for index, kind in enumerate(
            (
                "major_swing_low",
                "major_swing_high",
                "breakout_origin",
                "capitulation",
                "high_volume",
                "range_start",
            )
        )
    }
    return [_report(candles, anchor) for anchor in sorted(anchors, key=lambda item: order[item.kind])]
