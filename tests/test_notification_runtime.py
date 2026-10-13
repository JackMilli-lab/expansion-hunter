"""Real outbox/HTTP-adapter paths with explicit local transports; no messages sent."""

import asyncio
import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from expansion_hunter.fast_models import FastEvent, FastResistance, FastStage
from expansion_hunter.intelligence_store import IntelligenceStore
from expansion_hunter.notification_runtime import (
    NotificationWorker,
    TelegramSettings,
    _SecretFilter,
    format_telegram_alert,
    run_notification_worker,
)

NOW = datetime(2026, 10, 4, 12, tzinfo=UTC)
TOKEN = "123456:EXPLICIT_TEST_FAKE_TOKEN_123456789"
CHAT = "-1234567890"


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


@pytest.fixture
def store(tmp_path):
    connection = IntelligenceStore(str(tmp_path / "notifications.sqlite3"))
    yield connection
    connection.close()


def event(identity="signal-1", *, short=False, at=NOW, quality=None):
    boundary = FastResistance(price=100, lower=99.9, upper=100.1,
                              event_timestamp=at - timedelta(minutes=10), discovered_at=at - timedelta(minutes=1),
                              atr=1, kind="SYNTHETIC_TEST_SUPPORT" if short else "SYNTHETIC_TEST_RESISTANCE",
                              tests=3, reason="Explicit test geometry")
    return FastEvent(event_id=identity, asset_id="unknown", symbol="UNKNOWNUSDT",
                     event_type=FastStage.BREAKDOWN_CONFIRMED if short else FastStage.BREAKOUT_CONFIRMED,
                     previous_stage=FastStage.BREAKDOWN_ATTEMPT if short else FastStage.BREAKOUT_ATTEMPT,
                     market_event_timestamp=at - timedelta(seconds=1), candle_close_timestamp=at - timedelta(seconds=1),
                     data_received_timestamp=at, evaluation_timestamp=at, alert_created_timestamp=at,
                     detection_latency_ms=1000, alert_latency_ms=1000, processing_latency_ms=0,
                     price=99.9 if short else 100.1, bias="SHORT" if short else "LONG",
                     support=boundary if short else None, resistance=None if short else boundary,
                     reason="Native closed-price condition in explicit fixture; no profitability claim",
                     metrics={"pressure_score": 75, "relative_volume": 2},
                     data_quality=quality or {"1m": "AVAILABLE", "5m": "AVAILABLE", "order_book": "UNAVAILABLE"})


def queue(store, signal=None, *, channel="configured"):
    signal = signal or event()
    store.append("fast_event", signal.event_id, signal.market_event_timestamp,
                 signal.alert_created_timestamp, signal, signal.asset_id)
    store.enqueue_notification(signal.event_id, channel, signal.alert_created_timestamp)
    return signal


def configured(**options):
    return TelegramSettings(bot_token=TOKEN, chat_id=CHAT, **options)


def app(store):
    return SimpleNamespace(state=SimpleNamespace(settings=SimpleNamespace(db_path=store.database_path)))


def test_environment_is_explicit_optional_and_secrets_have_no_repr():
    empty = TelegramSettings.from_env({})
    assert empty.readiness()[0] is False
    settings = TelegramSettings.from_env({"COCKPIT_TELEGRAM_BOT_TOKEN": TOKEN, "COCKPIT_TELEGRAM_CHAT_ID": CHAT})
    assert settings.readiness()[0] is True
    assert TOKEN not in repr(settings) and CHAT not in repr(settings)
    assert not TelegramSettings(bot_token="invalid", chat_id=CHAT).readiness()[0]
    with pytest.raises(ValueError):
        configured(batch_size=6)


def test_http_log_filter_redacts_token_and_chat_without_changing_delivery():
    record = logging.LogRecord("httpx", logging.INFO, "fixture", 1, "HTTP request %s chat=%s",
                               ("https://api.telegram.org/bot" + TOKEN + "/sendMessage", CHAT), None)
    assert _SecretFilter((TOKEN, CHAT)).filter(record)
    assert TOKEN not in record.getMessage() and CHAT not in record.getMessage()
    assert "[REDACTED]" in record.getMessage()


@pytest.mark.parametrize("short", [False, True])
def test_alert_format_preserves_actual_side_boundary_and_no_probabilities(short):
    text = format_telegram_alert(event(short=short))
    assert "Bias: SHORT" in text if short else "Bias: LONG" in text
    assert "Support: 100" in text if short else "Resistance: 100" in text
    assert "Pressure score: 75" in text and "RVOL: 2" in text
    assert "order_book=UNAVAILABLE" in text
    assert "not probabilities" in text and "No orders" in text
    assert "%" not in text and "winrate" not in text.lower()


def test_pending_retry_expiry_and_delivery_use_actual_persisted_clocks(store):
    queue(store)
    assert len(store.pending_notifications(NOW)) == 1
    retry = NOW + timedelta(seconds=5)
    store.notification_result("signal-1", "configured", NOW, "HTTP_429", next_attempt_at=retry)
    assert store.pending_notifications(NOW + timedelta(seconds=4)) == []
    assert store.pending_notifications(retry)[0]["attempts"] == 1
    store.notification_result("signal-1", "configured", retry, latency_ms=5000)
    store.notification_result("signal-1", "configured", retry + timedelta(seconds=1), "late failure")
    assert store.pending_notifications(retry + timedelta(seconds=1)) == []
    assert store.delivered_notification("signal-1") == retry.isoformat()
    assert store.notification_counts() == {"DELIVERED": 1}


def test_stale_backlog_is_suppressed_in_bulk_without_fake_delivery(store):
    for index in range(9):
        store.enqueue_notification(str(index), "configured", NOW - timedelta(minutes=10))
    store.enqueue_notification("new", "configured", NOW)
    assert store.expire_notifications(NOW) == {"stale": 9, "retry_exhausted": 0}
    assert [row["event_id"] for row in store.pending_notifications(NOW)] == ["new"]
    assert store.delivered_notification("0") is None
    assert store.notification_counts() == {"QUEUED": 1, "STALE": 9}


def test_future_outbox_entries_are_not_available_yet(store):
    store.enqueue_notification("future", "configured", NOW + timedelta(seconds=1))
    assert store.pending_notifications(NOW) == []


def test_safe_migration_preserves_existing_outbox_rows_and_original_delivery(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE notification_outbox(event_id TEXT NOT NULL,channel TEXT NOT NULL,"
                       "created_at TEXT NOT NULL,delivered_at TEXT,attempts INTEGER NOT NULL DEFAULT 0,"
                       "last_error TEXT,PRIMARY KEY(event_id,channel))")
    connection.execute("INSERT INTO notification_outbox VALUES(?,?,?,?,?,?)",
                       ("old", "telegram", NOW.isoformat(), NOW.isoformat(), 2, None))
    connection.commit()
    connection.close()
    migrated = IntelligenceStore(str(path))
    try:
        assert migrated.delivered_notification("old") == NOW.isoformat()
        assert migrated.notification_counts() == {"DELIVERED": 1}
        assert migrated.pending_notifications(NOW) == []
    finally:
        migrated.close()


async def test_unconfigured_runtime_makes_no_client_or_database_request():
    calls = []

    def forbidden_client(**options):
        calls.append(options)
        raise AssertionError("unconfigured delivery must not create HTTP client")

    application = SimpleNamespace(state=SimpleNamespace())
    await run_notification_worker(application, settings=TelegramSettings(), client_factory=forbidden_client)
    assert application.state.notification_health["status"] == "UNAVAILABLE"
    assert application.state.notification_health["http_requests"] == 0
    assert calls == []


async def test_native_http_acknowledgement_persists_receipt_latency_and_restart_dedupe(store):
    clock, calls = Clock(), []
    queue(store, event(short=True))

    def transport(request):
        calls.append(request)
        assert request.url.scheme == "https" and request.url.host == "api.telegram.org"
        assert request.method == "POST" and request.url.path.endswith("/sendMessage")
        body = json.loads(request.content)
        assert body["chat_id"] == CHAT
        assert "Bias: SHORT" in body["text"] and "Support: 100" in body["text"]
        clock.now += timedelta(milliseconds=125)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        worker = NotificationWorker(store, configured(), client, clock=clock)
        result = await worker.run_once()
        assert result["results"] == {"delivered": 1}
        assert store.delivered_notification("signal-1") == clock.now.isoformat()
        receipt = store.get_state("notification_delivery", "signal-1")
        assert receipt["telegram_message_id"] == 42
        assert receipt["notification_latency_ms"] == 125
        assert receipt["handset_delivery_timestamp"] is None
        restarted = NotificationWorker(store, configured(), client, clock=clock)
        assert (await restarted.run_once())["processed"] == 0
    assert len(calls) == 1


async def test_configured_and_explicit_telegram_channels_do_not_duplicate_delivery(store):
    queue(store)
    store.enqueue_notification("signal-1", "telegram", NOW)
    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 2}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        result = await NotificationWorker(store, configured(), client, clock=lambda: NOW).run_once()
    assert result["results"] == {"delivered": 1, "deduplicated": 1}
    assert len(calls) == 1
    assert store.notification_counts() == {"DELIVERED": 2}


async def test_rate_limit_backoff_survives_worker_restart_and_retry_limit(store):
    queue(store)
    clock, calls = Clock(), []

    def transport(request):
        calls.append(request)
        return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 20}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        worker = NotificationWorker(store, configured(max_attempts=2), client, clock=clock)
        assert (await worker.run_once())["results"] == {"failed": 1}
        clock.now += timedelta(seconds=19)
        assert (await NotificationWorker(store, configured(max_attempts=2), client, clock=clock).run_once())["processed"] == 0
        clock.now += timedelta(seconds=1)
        assert (await worker.run_once())["results"] == {"failed": 1}
        clock.now += timedelta(seconds=40)
        assert (await worker.run_once())["processed"] == 0
    assert len(calls) == 2
    assert store.notification_counts() == {"FAILED": 1}


@pytest.mark.parametrize("status", [401, 403])
async def test_permanent_provider_failure_does_not_retry_or_expose_secrets(store, status):
    queue(store)
    transport = httpx.MockTransport(lambda _: httpx.Response(status, json={"description": TOKEN + CHAT}))
    async with httpx.AsyncClient(transport=transport) as client:
        result = await NotificationWorker(store, configured(), client, clock=lambda: NOW).run_once()
    assert result["status"] == "DEGRADED"
    state = store.get_state("notification_delivery", "signal-1")
    assert state["error"] == f"HTTP_{status}"
    assert TOKEN not in json.dumps(state) and CHAT not in json.dumps(state)
    assert store.notification_counts() == {"FAILED": 1}


@pytest.mark.parametrize("case", ["unknown_channel", "missing_event", "stale_critical_data", "old_market_event"])
async def test_invalid_or_stale_evidence_does_not_make_an_http_request(store, case):
    if case == "missing_event":
        store.enqueue_notification("signal-1", "configured", NOW)
    elif case == "stale_critical_data":
        queue(store, event(quality={"1m": "STALE", "5m": "AVAILABLE"}))
    elif case == "old_market_event":
        queue(store, event().model_copy(update={"market_event_timestamp": NOW - timedelta(minutes=10)}))
    else:
        queue(store, channel="unknown")

    def forbidden(request):
        raise AssertionError("invalid evidence cannot reach provider")

    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        result = await NotificationWorker(store, configured(), client, clock=lambda: NOW).run_once()
    assert result["status"] == "DEGRADED"
    assert store.notification_counts() == {"FAILED": 1}


async def test_worker_cancellation_drains_inflight_provider_and_local_commit(store):
    queue(store)
    started, finish = asyncio.Event(), asyncio.Event()

    async def transport(request):
        started.set()
        await finish.wait()
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 5}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        worker = NotificationWorker(store, configured(), client, clock=lambda: NOW)
        task = asyncio.create_task(worker.run_once())
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert store.delivered_notification("signal-1") == NOW.isoformat()


async def test_actual_runtime_owns_connection_and_reports_delivery_without_blocking_market(store):
    queue(store)
    application = app(store)
    received = asyncio.Event()

    def client_factory(**options):
        assert options["follow_redirects"] is False

        def transport(request):
            received.set()
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 8}})

        return httpx.AsyncClient(transport=httpx.MockTransport(transport), **options)

    task = asyncio.create_task(run_notification_worker(application, settings=configured(),
                                                      client_factory=client_factory, clock=lambda: NOW))
    await asyncio.wait_for(received.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert application.state.notification_health["status"] == "STOPPED"
    assert store.delivered_notification("signal-1") == NOW.isoformat()
    # Independent original store remains usable after the worker closes its own.
    store.put_state("test", "still-open", NOW, {"available": True})
    assert store.get_state("test", "still-open")["available"] is True
