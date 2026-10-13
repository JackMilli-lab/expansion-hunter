"""Persistent notification integration using explicit local HTTP transports."""

import json
import logging
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from expansion_hunter.intelligence_store import IntelligenceStore
from expansion_hunter.notification_runtime import (
    NotificationWorker,
    TelegramSettings,
    format_trade_telegram_alert,
    send_telegram_test,
)
from expansion_hunter.trade_alert_models import TradeAlertRecord

NOW = datetime(2026, 10, 8, 12, tzinfo=UTC)
TOKEN = "123456:EXPLICIT_TEST_FAKE_TOKEN_123456789"
CHAT = "-1234567890"


@pytest.fixture
def store(tmp_path):
    connection = IntelligenceStore(str(tmp_path / "trade-notifications.sqlite3"))
    yield connection
    connection.close()


def settings(**overrides):
    return TelegramSettings(bot_token=TOKEN, chat_id=CHAT, **overrides)


def alert(**changes):
    fields = dict(event_id="trade-signal", episode_id="episode", asset_id="unknown", symbol="UNKNOWNUSDT",
        phase="TRADE_ALERT", bias="LONG", event_timestamp=NOW - timedelta(seconds=1), received_at=NOW,
        detected_at=NOW, expires_at=NOW + timedelta(seconds=120), price=99.9, trigger_level=100,
        trigger_discovered_at=NOW - timedelta(minutes=1), distance_pct=.1, invalidation=98,
        targets=[102, 103.5], score=75, components={"compression": {"contribution": 11.25}},
        metrics={"relative_volume": 1.5, "volume_acceleration": .4},
        data_quality={"1m": "AVAILABLE", "5m": "AVAILABLE", "order_book": "UNAVAILABLE"},
        btc_context={"regime": "NEUTRAL"}, reason="Explicit artificial test geometry, no predictive claim",
        detection_latency_ms=1000, alert_latency_ms=1000)
    fields.update(changes)
    return TradeAlertRecord(**fields)


def queue(store, signal):
    payload = signal.model_dump(mode="json") if isinstance(signal, TradeAlertRecord) else signal
    store.append("trade_alert", payload["event_id"], NOW - timedelta(seconds=1), NOW, payload, "unknown")
    store.enqueue_notification(payload["event_id"], "configured", NOW)


async def deliver(store, *, clock=lambda: NOW, response=None, requests=None):
    requests = requests if requests is not None else []

    def transport(request):
        requests.append(request)
        return response or httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        return await NotificationWorker(store, settings(), client, clock=clock).run_once()


@pytest.mark.parametrize("bias", ["LONG", "SHORT"])
def test_format_contains_observed_fields_status_and_risk_without_probability(bias):
    record = alert(bias=bias, setup_type="SHORT_REVERSAL" if bias == "SHORT" else "LONG_REVERSAL",
                   invalidation=102 if bias == "SHORT" else 98, targets=[98, 96] if bias == "SHORT" else [102, 103.5])
    text = format_trade_telegram_alert(record)
    assert f"Bias: {bias}" in text
    assert f"Setup: {bias}_REVERSAL" in text
    assert "Status: ACTIVE" in text and "Trigger: 100" in text
    assert "Invalidation:" in text and "Targets:" in text
    assert "75/100" in text and "not a probability" in text
    assert "BTC context:" in text and "order_book=UNAVAILABLE" in text
    assert "Risk:" in text and "Expires UTC:" in text
    assert "No exchange orders" in text
    assert "75%" not in text


def test_missing_optional_evidence_is_unavailable_and_real_zero_is_preserved():
    record = alert(invalidation=None, targets=[], btc_context={}, metrics={"relative_volume": 0})
    text = format_trade_telegram_alert(record)
    assert "Invalidation: UNAVAILABLE" in text
    assert "Targets: UNAVAILABLE" in text
    assert "BTC context: UNAVAILABLE" in text
    assert "RVOL: 0" in text
    assert "Volume acceleration: UNAVAILABLE" in text


def test_unknown_reversal_score_is_not_fabricated_as_zero():
    text = format_trade_telegram_alert(alert(score=None, setup_type="LONG_REVERSAL"))
    assert "Score: UNAVAILABLE/100" in text
    assert "Score: 0/100" not in text


@pytest.mark.parametrize("phase,signal_state", [("STRUCTURE_WATCH", "PRE_IGNITION"), ("TRADE_ALERT", "LOADED")])
def test_early_transition_header_shows_signal_state_and_preserves_observed_fields(phase, signal_state):
    text = format_trade_telegram_alert(alert(phase=phase, signal_state=signal_state))
    assert text.splitlines()[0] == f"UNKNOWNUSDT — TRADE ALERT — {signal_state}"
    assert "Trigger: 100" in text and "Invalidation: 98" in text
    assert "not a probability" in text and "Data:" in text and "Expires UTC:" in text
    assert format_trade_telegram_alert(alert()).splitlines()[0] == "UNKNOWNUSDT — TRADE_ALERT"


@pytest.mark.parametrize("bias", ["LONG", "SHORT"])
async def test_trade_alert_acknowledgement_persistence_and_restart_deduplication(store, bias):
    queue(store, alert(bias=bias))
    requests = []
    result = await deliver(store, requests=requests)
    assert result["results"] == {"delivered": 1}
    assert len(requests) == 1
    body = json.loads(requests[0].content)
    assert body["chat_id"] == CHAT and f"Bias: {bias}" in body["text"]
    assert requests[0].extensions["timeout"]["read"] == 8
    state = store.get_state("notification_delivery", "trade-signal")
    assert state["telegram_message_id"] == 42
    assert state["provider_accepted_timestamp"] == NOW.isoformat()
    assert state["handset_delivery_timestamp"] is None
    assert state["notification_latency_ms"] == 0
    assert (await deliver(store, requests=requests))["processed"] == 0
    assert len(requests) == 1


@pytest.mark.parametrize("changes", [
    {"status": "LATE"}, {"status": "MISSED"},
    {"expires_at": NOW}, {"detected_at": NOW + timedelta(seconds=1)},
    {"received_at": NOW + timedelta(seconds=1), "detected_at": NOW + timedelta(seconds=1)},
    {"event_timestamp": NOW + timedelta(seconds=1), "received_at": NOW + timedelta(seconds=1),
     "detected_at": NOW + timedelta(seconds=1)},
    {"data_quality": {"5m": "AVAILABLE"}},
    {"data_quality": {"1m": "AVAILABLE", "5m": "STALE"}},
    {"data_quality": {"1m": "UNAVAILABLE", "5m": "AVAILABLE"}},
    {"phase": "BREAK_CONFIRMED", "trigger_discovered_at": NOW},
])
async def test_expired_late_future_or_incomplete_trade_alert_cannot_reach_telegram(store, changes):
    payload = alert().model_dump(mode="json")
    for key, value in changes.items():
        payload[key] = value.isoformat() if isinstance(value, datetime) else value
    queue(store, payload)
    requests = []
    result = await deliver(store, requests=requests)
    assert requests == []
    assert result["status"] == "DEGRADED"
    assert store.notification_counts() == {"FAILED": 1}


@pytest.mark.parametrize("episode", [{"restart_stale": True}, {"phase": "INVALIDATED"}])
async def test_restored_stale_or_already_invalidated_episode_suppresses_old_active_alert(store, episode):
    queue(store, alert())
    store.put_state("trade_alert_engine", "default", NOW, {"version": 1, "episodes": {"episode": episode}})
    requests = []
    assert (await deliver(store, requests=requests))["results"] == {"stale": 1}
    assert requests == []


@pytest.mark.parametrize("episode", [{"restart_stale": True}, {"phase": "INVALIDATED"}, {"status": "INVALIDATED"}])
@pytest.mark.parametrize("state_key", ["default", "active"])
async def test_real_composite_engine_key_enforces_restart_and_invalidation_guard(store, episode, state_key):
    queue(store, alert())
    native = {"episode_id": "episode", "asset_id": "unknown", "bias": "LONG",
              "setup_type": "CONTINUATION", **episode}
    store.put_state("trade_alert_engine", state_key, NOW,
                    {"version": 1, "episodes": {"unknown|LONG|CONTINUATION": native}})
    requests = []
    assert (await deliver(store, requests=requests))["results"] == {"stale": 1}
    assert requests == []


async def test_early_transition_records_deliver_without_overriding_native_episode_phase(store):
    queue(store, alert(event_id="pre-ignition", phase="STRUCTURE_WATCH", signal_state="PRE_IGNITION", score=55))
    queue(store, alert(event_id="loaded", phase="TRADE_ALERT", signal_state="LOADED", score=65))
    native = {"episode_id": "episode", "asset_id": "unknown", "bias": "LONG", "setup_type": "CONTINUATION",
              "phase": "TRADE_ALERT", "status": "ACTIVE", "restart_stale": False}
    store.put_state("trade_alert_engine", "default", NOW,
                    {"version": 1, "episodes": {"unknown|LONG|CONTINUATION": native}})
    requests = []
    assert (await deliver(store, requests=requests))["results"] == {"delivered": 2}
    headers = {json.loads(request.content)["text"].splitlines()[0] for request in requests}
    assert headers == {"UNKNOWNUSDT — TRADE ALERT — PRE_IGNITION", "UNKNOWNUSDT — TRADE ALERT — LOADED"}
    assert store.get_state("trade_alert_engine", "default")["episodes"]["unknown|LONG|CONTINUATION"] == native


@pytest.mark.parametrize("episode_key", ["episode", "unknown|LONG|CONTINUATION"])
async def test_invalidation_is_delivered_as_cancellation_after_episode_was_invalidated(store, episode_key):
    queue(store, alert(phase="INVALIDATED", status="INVALIDATED"))
    store.put_state("trade_alert_engine", "default", NOW,
                    {"version": 1, "episodes": {episode_key: {"episode_id": "episode", "phase": "INVALIDATED", "restart_stale": False}}})
    requests = []
    assert (await deliver(store, requests=requests))["results"] == {"delivered": 1}
    text = json.loads(requests[0].content)["text"]
    assert "Cancellation only" in text and "Status: INVALIDATED" in text
    assert "No exchange orders are submitted" not in text


async def test_expiry_boundary_is_exclusive(store):
    queue(store, alert())
    requests = []
    result = await deliver(store, clock=lambda: NOW + timedelta(seconds=120), requests=requests)
    assert result["results"] == {"stale": 1} and requests == []


async def test_invalid_payload_does_not_bypass_the_typed_contract(store):
    payload = alert().model_dump(mode="json")
    payload["phase"] = "MADE_UP_PHASE"
    queue(store, payload)
    requests = []
    assert (await deliver(store, requests=requests))["results"] == {"invalid": 1}
    assert requests == []


async def test_connectivity_test_does_not_make_request_without_credentials():
    class ForbiddenClient:
        async def post(self, *args, **kwargs):
            raise AssertionError("No credentials means no network request")

    result = await send_telegram_test(ForbiddenClient(), TelegramSettings())
    assert result["status"] == "UNAVAILABLE"
    assert result["http_requests"] == 0 and result["provider_accepted"] is False


async def test_connectivity_test_sends_transport_message_without_fictitious_market_data(caplog):
    requests = []

    def transport(request):
        requests.append(request)
        text = json.loads(request.content)["text"]
        assert "connection test" in text and "not a market signal" in text
        assert "price" not in text.lower() and "TRADE_ALERT" not in text
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})

    with caplog.at_level(logging.INFO, logger="httpx"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            result = await send_telegram_test(client, settings(), clock=lambda: NOW)
    assert result["status"] == "DELIVERED" and result["provider_accepted"] is True
    assert result["provider_accepted_timestamp"] == NOW.isoformat()
    assert result["handset_delivery_timestamp"] is None
    assert result["telegram_message_id"] == 7
    assert result["transport_latency_ms"] >= 0 and len(requests) == 1
    assert TOKEN not in caplog.text and CHAT not in caplog.text


@pytest.mark.parametrize("status,retryable", [(401, False), (403, False), (429, True), (500, True), (503, True)])
async def test_connectivity_provider_errors_are_safe_and_report_retry_policy(status, retryable):
    requests = []

    def transport(request):
        requests.append(request)
        return httpx.Response(status, json={"ok": False, "description": TOKEN + CHAT,
                                           "parameters": {"retry_after": 20}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        result = await send_telegram_test(client, settings())
    assert result["retryable"] is retryable
    assert result["status"] == ("RETRYING" if retryable else "FAILED")
    assert result["retry_after_seconds"] == (20 if status == 429 else 5 if retryable else None)
    assert TOKEN not in json.dumps(result) and CHAT not in json.dumps(result)
    assert result["provider_accepted"] is False and len(requests) == 1


@pytest.mark.parametrize("body", [{"ok": True}, {"ok": False}, {"ok": True, "result": {"message_id": True}}, []])
async def test_connectivity_invalid_acknowledgement_is_not_reported_as_delivered(body):
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=body))
    async with httpx.AsyncClient(transport=transport) as client:
        result = await send_telegram_test(client, settings())
    assert result["status"] == "RETRYING" and result["provider_accepted"] is False
    assert result["error"] == "TRANSPORT_OR_RESPONSE_FAILURE"


async def test_connectivity_transport_exception_does_not_expose_token():
    def transport(request):
        raise httpx.ConnectError(TOKEN + CHAT, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        result = await send_telegram_test(client, settings())
    assert result["status"] == "RETRYING"
    assert TOKEN not in json.dumps(result) and CHAT not in json.dumps(result)
