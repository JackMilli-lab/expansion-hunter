"""Native count protocol fixtures; no synthetic production trade-count fallback."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from expansion_hunter.config import Settings
from expansion_hunter.live_feed import LiveFeedState
from expansion_hunter.live_models import LiveCandle
from expansion_hunter.models import Asset, Candle
from expansion_hunter.providers import BinanceSpotProvider, FetchedJSON, ProviderError
from expansion_hunter.providers.binance_ws import decode_message

OPEN = datetime(2026, 10, 9, 12, tzinfo=UTC)
CLOSE = OPEN + timedelta(minutes=1)
RECEIVED = CLOSE + timedelta(milliseconds=40)


def historical_bar(**updates):
    return Candle(
        open_time=OPEN, close_time=CLOSE, available_at=RECEIVED,
        open=100, high=102, low=99, close=101, volume=10,
        quote_volume=1000, taker_buy_volume=6, source="test-native-spot", **updates,
    )


def rest_row(count=37, opened=OPEN):
    boundary = opened + timedelta(minutes=1)
    return [int(opened.timestamp() * 1000), "100", "102", "99", "101", "10",
            int(boundary.timestamp() * 1000) - 1, "1000", count, "6", "600", "0"]


class NativeResponseHTTP:
    def __init__(self, rows):
        self.rows = rows

    async def get_json(self, _url, **_kwargs):
        return FetchedJSON(data=self.rows, retrieved_at=RECEIVED)


async def fetch(rows, as_of=RECEIVED):
    asset = Asset(asset_id="bitcoin", symbol="BTC", name="Bitcoin", exchange_symbol="BTCUSDT")
    return await BinanceSpotProvider(NativeResponseHTTP(rows), Settings()).candles(asset, "1m", 1, as_of)


def ws_payload(count=37, *, complete=True):
    return {"stream": "btcusdt@kline_1m", "data": {
        "e": "kline", "s": "BTCUSDT", "E": int(CLOSE.timestamp() * 1000),
        "k": {"t": int(OPEN.timestamp() * 1000), "T": int(CLOSE.timestamp() * 1000) - 1,
              "i": "1m", "o": "100", "h": "102", "l": "99", "c": "101", "v": "10",
              "q": "1000", "V": "6", "Q": "600", "n": count, "x": complete},
    }}


@pytest.mark.parametrize("count", [0, 37])
async def test_native_rest_count_uses_kline_index_eight_without_estimation(count):
    result = await fetch([rest_row(count)])
    assert result[0].trade_count == count
    assert result[0].volume == 10
    assert result[0].available_at == RECEIVED
    assert result[0].model_dump()["trade_count"] == count


@pytest.mark.parametrize("count", [-1, 1.5, True, "37"])
async def test_invalid_native_rest_counts_are_provider_errors(count):
    with pytest.raises(ProviderError, match="invalid candle"):
        await fetch([rest_row(count)])


@pytest.mark.parametrize("count", [-1, 1.5, True, "37"])
def test_candle_rejects_noninteger_or_negative_native_counts(count):
    with pytest.raises(ValidationError):
        historical_bar(trade_count=count)


def test_old_archive_keeps_missing_count_unavailable():
    archived = historical_bar().model_dump(mode="json")
    del archived["trade_count"]
    restored = Candle.model_validate(archived)
    assert restored.trade_count is None
    live = LiveCandle(timeframe="1m", complete=True,
                      **{key: archived[key] for key in (
                          "open_time", "close_time", "open", "high", "low", "close", "volume")})
    assert live.as_closed(RECEIVED).trade_count is None


def test_closed_live_native_count_survives_conversion_and_archive_roundtrip():
    event = decode_message(ws_payload(), RECEIVED, RECEIVED + timedelta(milliseconds=1))
    feed = LiveFeedState()
    assert feed.apply(event)
    bar = feed.candles("BTCUSDT", "1m", RECEIVED)[0]
    assert bar.trade_count == 37
    assert Candle.model_validate_json(bar.model_dump_json()).trade_count == 37
    assert bar.available_at == RECEIVED
    assert feed.candles("BTCUSDT", "1m", CLOSE) == []


@pytest.mark.parametrize("count", [-1, 1.5, True, "37"])
def test_live_count_decoder_does_not_coerce_bad_native_counts(count):
    with pytest.raises(ValidationError):
        decode_message(ws_payload(count), RECEIVED, RECEIVED)


def test_current_candle_native_count_cannot_enter_closed_history():
    payload = ws_payload(20, complete=False)
    observed = OPEN + timedelta(seconds=30)
    payload["data"]["E"] = int(observed.timestamp() * 1000)
    feed = LiveFeedState()
    assert feed.apply(decode_message(payload, observed, observed))
    assert feed.open_candle("BTCUSDT", "1m").trade_count == 20
    assert feed.candles("BTCUSDT", "1m", observed) == []


async def test_later_downloaded_count_does_not_rewrite_historical_availability():
    bar = (await fetch([rest_row()], as_of=CLOSE))[0]
    assert bar.trade_count == 37
    assert bar.available_at > CLOSE
    feed = LiveFeedState()
    with pytest.raises(ValueError, match="future or unavailable"):
        feed.seed_candles("BTCUSDT", "1m", [bar], CLOSE)


async def test_forming_rest_bar_is_not_used_even_with_native_count():
    bars = await fetch([rest_row(37), rest_row(99, opened=CLOSE)])
    assert len(bars) == 1
    assert bars[0].trade_count == 37
