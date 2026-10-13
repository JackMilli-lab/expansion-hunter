"""Gap recovery uses synthetic native-protocol fixtures, never live claims."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from expansion_hunter.fast_models import FastState
from expansion_hunter.live_feed import LiveFeedState
from expansion_hunter.live_repair import detect_repair_frames, fetch_repair
from expansion_hunter.models import TIMEFRAMES, Asset, Candle

START = datetime(2026, 10, 5, 12, tzinfo=UTC)
ASSET = Asset(asset_id="bitcoin", symbol="BTC", name="Bitcoin", exchange_symbol="BTCUSDT")


def bars(frame="1m", count=30, *, source=None):
    duration = TIMEFRAMES[frame]
    return [Candle(open_time=START + timedelta(seconds=index * duration),
                   close_time=START + timedelta(seconds=(index + 1) * duration),
                   available_at=START + timedelta(seconds=(index + 1) * duration),
                   open=100, high=101, low=99, close=100, volume=100,
                   source=source or f"binance/spot/BTCUSDT/{frame}") for index in range(count)]


def caches(source, *, frame="1m", feed_remove=(), monitor_remove=(), point=None):
    point = point or source[-1].close_time + timedelta(seconds=5)
    feed = LiveFeedState()
    feed.seed_candles("BTCUSDT", frame, [bar for index, bar in enumerate(source) if index not in feed_remove], point)
    monitor = FastState(asset=ASSET, candles={frame: [bar for index, bar in enumerate(source) if index not in monitor_remove]})
    return feed, monitor, point


def plans_for(source, *, frame="1m", feed_remove=(), monitor_remove=(), point=None):
    feed, monitor, point = caches(source, frame=frame, feed_remove=feed_remove, monitor_remove=monitor_remove, point=point)
    plans = detect_repair_frames(ASSET, feed, monitor, point)
    return feed, monitor, point, next((plan for plan in plans if plan.timeframe == frame), None)


class Provider:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response or [], error, []

    async def candles(self, asset, frame, limit, as_of):
        self.calls.append((asset.asset_id, frame, limit, as_of))
        if self.error:
            raise self.error
        return self.response


def test_internal_gap_and_monitor_gap_detected_without_full_warmup():
    source = bars()
    feed, monitor, point, plan = plans_for(source, feed_remove=(24,), monitor_remove=(24,))
    assert plan is not None
    assert plan.timeframe == "1m"
    assert plan.missing_open_times == [source[24].open_time]
    assert plan.remote_missing_open_times == [source[24].open_time]
    assert 1 <= plan.requested_limit <= 8
    assert plan.historical_only is True
    assert plan.needs_monitor_merge is True
    # Planning never mutates either finalized cache or artificially clears STALE.
    assert len(feed.candles("BTCUSDT", "1m")) == 29
    assert len(monitor.candles["1m"]) == 29
    assert monitor.stale is True
    assert point == plan.planned_at


def test_contiguous_current_frame_is_not_repaired_each_minute():
    source = bars()
    _, _, _, plan = plans_for(source)
    assert plan is None


def test_exact_close_boundary_waits_for_native_message_grace():
    source = bars()
    feed, monitor, _ = caches(source[:-1], point=source[-1].close_time)
    boundary = source[-1].close_time
    assert not any(plan.timeframe == "1m" for plan in detect_repair_frames(ASSET, feed, monitor, boundary))
    assert not any(plan.timeframe == "1m" for plan in detect_repair_frames(ASSET, feed, monitor, boundary + timedelta(seconds=4.999)))
    plan = next(plan for plan in detect_repair_frames(ASSET, feed, monitor, boundary + timedelta(seconds=5)) if plan.timeframe == "1m")
    assert plan.missing_open_times == [source[-1].open_time]


@pytest.mark.parametrize("frame", ["1m", "5m", "15m"])
def test_large_disconnect_is_bounded_to_120_recent_bars(frame):
    source = bars(frame=frame, count=2)
    point = START + timedelta(seconds=TIMEFRAMES[frame] * 300 + 5)
    _, _, _, plan = plans_for(source, frame=frame, point=point)
    assert plan is not None
    assert plan.requested_limit == 120
    assert len(plan.remote_missing_open_times) == 120
    assert plan.remote_missing_open_times[-1] + timedelta(seconds=TIMEFRAMES[frame]) == plan.latest_expected_close


def test_monitor_gap_is_recovered_even_when_feed_is_contiguous():
    source = bars()
    _, _, _, plan = plans_for(source, monitor_remove=(24,))
    assert plan is not None
    assert plan.requested_limit == 0
    assert plan.needs_monitor_merge is True
    assert plan.remote_missing_open_times == []


@pytest.mark.asyncio
async def test_native_gap_fills_at_actual_receipt_and_existing_bars_win():
    source = bars()
    feed, monitor, point, plan = plans_for(source, feed_remove=(24,), monitor_remove=(24,))
    existing = feed.candles("BTCUSDT", "1m")
    receipt = point + timedelta(seconds=1)
    # A REST revision to already finalized bar 25 must not replace it.
    changed = source[25].model_copy(update={"high": 999})
    provider = Provider([source[24], changed])
    result = await fetch_repair(provider, ASSET, plan, clock=lambda: receipt)
    assert result.status == "REPAIRED"
    assert [bar.open_time for bar in result.bars] == [source[24].open_time]
    assert result.bars[0].available_at == receipt
    assert result.historical_only is True
    feed.seed_candles("BTCUSDT", "1m", result.bars, result.received_at)
    repaired = feed.candles("BTCUSDT", "1m", receipt)
    assert len(repaired) == 30
    assert all(right.open_time == left.close_time for left, right in zip(repaired, repaired[1:], strict=False))
    assert repaired[25].high == 101
    assert [bar for bar in repaired if bar.open_time != source[24].open_time] == existing
    assert monitor.stale is True  # caller still must merge/evaluate current data


@pytest.mark.asyncio
async def test_known_monitor_bar_fills_feed_without_any_provider_call():
    source = bars()
    feed, _, point, plan = plans_for(source, feed_remove=(24,))
    assert plan.requested_limit == 0
    provider = Provider(error=AssertionError("should not request native history"))
    result = await fetch_repair(provider, ASSET, plan, clock=lambda: point)
    assert result.status == "REPAIRED"
    assert len(result.bars) == 1
    assert result.bars[0].open_time == source[24].open_time
    assert provider.calls == []
    feed.seed_candles("BTCUSDT", "1m", result.bars, point)
    assert len(feed.candles("BTCUSDT", "1m")) == 30


@pytest.mark.asyncio
async def test_feed_known_bar_needs_monitor_merge_but_no_rest_or_revisions():
    source = bars()
    _, _, point, plan = plans_for(source, monitor_remove=(24,))
    provider = Provider(error=AssertionError("should not request native history"))
    result = await fetch_repair(provider, ASSET, plan, clock=lambda: point)
    assert result.status == "REPAIRED"
    assert result.bars == []
    assert provider.calls == []


@pytest.mark.asyncio
async def test_new_native_close_received_during_rest_is_not_overwritten_on_seed():
    source = bars()
    feed, _, point, plan = plans_for(source, feed_remove=(24,), monitor_remove=(24,))
    result = await fetch_repair(Provider([source[24]]), ASSET, plan, clock=lambda: point)
    authoritative = source[24].model_copy(update={"high": 101.25, "source": "binance-public-ws"})
    feed.seed_candles("BTCUSDT", "1m", [authoritative], point)
    feed.seed_candles("BTCUSDT", "1m", result.bars, point)
    final = next(bar for bar in feed.candles("BTCUSDT", "1m") if bar.open_time == authoritative.open_time)
    assert final == authoritative


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["foreign", "future", "late_receipt", "duration", "duplicate", "incomplete"])
async def test_bad_provider_data_cannot_silently_rehabilitate_history(bad):
    source = bars()
    _, _, point, plan = plans_for(source, feed_remove=(24,), monitor_remove=(24,))
    candidate = source[24]
    if bad == "foreign":
        response = [candidate.model_copy(update={"source": "foreign_exchange"})]
    elif bad == "future":
        response = [candidate.model_copy(update={"open_time": point, "close_time": point + timedelta(minutes=1), "available_at": point + timedelta(minutes=1)})]
    elif bad == "late_receipt":
        response = [candidate.model_copy(update={"available_at": point + timedelta(hours=1)})]
    elif bad == "duration":
        response = [candidate.model_copy(update={"close_time": candidate.close_time + timedelta(seconds=1)})]
    elif bad == "duplicate":
        response = [candidate, candidate]
    else:
        response = []
    result = await fetch_repair(Provider(response), ASSET, plan, clock=lambda: point)
    assert result.status == "PARTIAL"
    assert result.issues
    if bad != "duplicate":
        assert result.remaining_missing_open_times


@pytest.mark.asyncio
async def test_provider_failure_visible_and_no_fake_zero_candles():
    source = bars()
    _, _, point, plan = plans_for(source, feed_remove=(24,), monitor_remove=(24,))
    result = await fetch_repair(Provider(error=RuntimeError("public host unavailable")), ASSET, plan, clock=lambda: point)
    assert result.status == "UNAVAILABLE"
    assert result.bars == []
    assert "public host unavailable" in result.issues[0]


@pytest.mark.asyncio
async def test_provider_timeout_does_not_hang_market_worker():
    source = bars()
    _, _, point, plan = plans_for(source, feed_remove=(24,), monitor_remove=(24,))

    class SlowProvider:
        async def candles(self, *_):
            await asyncio.sleep(3600)

    result = await fetch_repair(SlowProvider(), ASSET, plan, clock=lambda: point, timeout_seconds=0.001)
    assert result.status == "UNAVAILABLE"
    assert "TimeoutError" in result.issues[0]


@pytest.mark.asyncio
async def test_wrong_asset_future_plan_and_clock_regression_rejected():
    source = bars()
    _, _, point, plan = plans_for(source, feed_remove=(24,), monitor_remove=(24,))
    wrong = ASSET.model_copy(update={"exchange_symbol": "ETHUSDT"})
    with pytest.raises(ValueError, match="different instrument"):
        await fetch_repair(Provider(), wrong, plan, clock=lambda: point)
    with pytest.raises(ValueError, match="future plan"):
        await fetch_repair(Provider(), ASSET, plan, clock=lambda: point - timedelta(seconds=1))
    readings = iter([point + timedelta(seconds=1), point])
    result = await fetch_repair(Provider([source[24]]), ASSET, plan, clock=lambda: next(readings))
    assert result.status == "UNAVAILABLE"
    assert result.issues == ["repair_clock_regressed"]


def test_missing_asset_mapping_and_config_guards():
    assert detect_repair_frames(ASSET.model_copy(update={"exchange_symbol": None}), LiveFeedState(), None, START) == []
    for limit in (0, 121, True):
        with pytest.raises(ValueError, match="max_bars"):
            detect_repair_frames(ASSET, LiveFeedState(), None, START, max_bars=limit)
    with pytest.raises(ValueError, match="boundary grace"):
        detect_repair_frames(ASSET, LiveFeedState(), None, START, boundary_grace_seconds=float("nan"))
