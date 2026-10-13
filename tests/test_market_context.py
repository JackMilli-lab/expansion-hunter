from datetime import UTC, datetime, timedelta

import pytest

from expansion_hunter.live_models import LiveMarketEvent
from expansion_hunter.market_context import FlowSettings, LargeTradeTracker, session_context

NOW = datetime(2026, 10, 5, 14, tzinfo=UTC)


def trade(sequence=1, price=100, quantity=1000, maker=False, when=NOW, epoch=1, gap=False):
    return LiveMarketEvent(symbol="TESTUSDT", kind="TRADE", price=price, quantity=quantity,
                           buyer_is_maker=maker, sequence_id=sequence, stream_epoch=epoch,
                           sequence_gap=gap, exchange_timestamp=when, received_timestamp=when,
                           processed_timestamp=when)


def test_native_large_trades_are_not_whale_identities():
    tracker = LargeTradeTracker()
    tracker.observe(trade())
    tracker.observe(trade(sequence=2, quantity=2000, maker=True))
    result = tracker.snapshot("TESTUSDT", NOW)
    assert result["quality"] == "AVAILABLE" and result["large_trade_count"] == 2
    assert result["delta_quote_notional"] == -100000
    assert result["whale_identity"] == "UNAVAILABLE"
    assert result["institutional_identity"] == "UNAVAILABLE" and result["research_weight"] == 0


def test_no_feed_is_missing_but_observed_small_trade_means_zero_large_trades():
    tracker = LargeTradeTracker()
    assert tracker.snapshot("TESTUSDT", NOW)["large_trade_count"] is None
    tracker.observe(trade(quantity=1))
    assert tracker.snapshot("TESTUSDT", NOW)["large_trade_count"] == 0


def test_duplicates_epoch_and_gap_do_not_double_count():
    tracker = LargeTradeTracker()
    assert tracker.observe(trade())
    assert not tracker.observe(trade())
    assert not tracker.observe(trade(epoch=0, sequence=99))
    tracker.observe(trade(epoch=2))
    assert tracker.snapshot("TESTUSDT", NOW)["large_trade_count"] == 1
    tracker.observe(trade(epoch=2, sequence=3, gap=True))
    assert tracker.snapshot("TESTUSDT", NOW)["continuous"] is False
    assert tracker.snapshot("TESTUSDT", NOW)["large_trade_count"] == 1


def test_retention_staleness_and_future_guard():
    tracker = LargeTradeTracker(FlowSettings(retained_large_trades=2))
    for sequence in range(3):
        tracker.observe(trade(sequence=sequence, when=NOW + timedelta(seconds=sequence)))
    with pytest.raises(ValueError, match="earlier decision"):
        tracker.snapshot("TESTUSDT", NOW)
    report = tracker.snapshot("TESTUSDT", NOW + timedelta(seconds=40))
    assert report["quality"] == "STALE" and report["retention_truncated"]
    assert report["large_trade_count"] == 2
    assert tracker.snapshot("TESTUSDT", NOW + timedelta(minutes=6))["large_trade_count"] == 0


def test_unknown_time_does_not_become_native_evidence():
    tracker = LargeTradeTracker()
    event = trade().model_copy(update={"exchange_timestamp": None})
    assert not tracker.observe(event)
    assert tracker.snapshot("TESTUSDT", NOW)["quality"] == "UNAVAILABLE"


def test_unknown_provider_and_overflow_cannot_become_native_evidence():
    tracker = LargeTradeTracker()
    assert not tracker.observe(trade().model_copy(update={"source": "unknown-price-proxy"}))
    assert not tracker.observe(trade(price=1e300, quantity=1e300))
    assert tracker.snapshot("TESTUSDT", NOW)["quality"] == "UNAVAILABLE"


def test_session_context_dst_and_no_directional_weight():
    winter = session_context(datetime(2026, 1, 5, 14, 0, tzinfo=UTC))
    summer = session_context(datetime(2026, 7, 6, 14, 0, tzinfo=UTC))
    assert "NEW_YORK" not in winter["active_windows"]
    assert "NEW_YORK" in summer["active_windows"]
    assert summer["crypto_market"] == "24_7" and summer["research_weight"] == 0
    weekend = session_context(datetime(2026, 7, 5, 14, tzinfo=UTC))
    assert weekend["active_windows"] == []
    with pytest.raises(ValueError, match="timezone"):
        session_context(datetime(2026, 1, 5))
