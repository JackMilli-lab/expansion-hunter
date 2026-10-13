"""Optional Telegram delivery, isolated from market ingestion and exchange APIs.

Telegram acceptance is recorded, not claimed as handset delivery. Persistence
deduplicates acknowledged events across restarts. A process crash after provider
acceptance but before the local commit can still duplicate a retry (at-least-once
delivery). Without explicit environment credentials, no HTTP request is made.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import httpx

from .fast_models import FastEvent, FastStage
from .i18n import translate
from .intelligence_store import IntelligenceStore
from .journal_runtime import _complete_thread_call
from .models import aware, utcnow
from .trade_alert_models import TradeAlertRecord


@dataclass(frozen=True)
class TelegramSettings:
    bot_token: str = field(default="", repr=False)
    chat_id: str = field(default="", repr=False)
    batch_size: int = 5
    max_attempts: int = 5
    max_age_seconds: float = 300
    retry_base_seconds: float = 5
    retry_cap_seconds: float = 300
    timeout_seconds: float = 8

    def __post_init__(self):
        if (not 1 <= self.batch_size <= 5 or not 1 <= self.max_attempts <= 20
                or any(not math.isfinite(v) or v <= 0 for v in
                       (self.max_age_seconds, self.retry_base_seconds, self.retry_cap_seconds, self.timeout_seconds))):
            raise ValueError("invalid notification delivery bounds")

    @classmethod
    def from_env(cls, environ=None):
        env = os.environ if environ is None else environ
        return cls(bot_token=env.get("COCKPIT_TELEGRAM_BOT_TOKEN", "").strip(),
                   chat_id=env.get("COCKPIT_TELEGRAM_CHAT_ID", "").strip())

    def readiness(self) -> tuple[bool, str]:
        if not self.bot_token or not self.chat_id:
            return False, "Telegram is optional; both bot token and chat ID must be configured"
        if (not re.fullmatch(r"[0-9]{5,}:[A-Za-z0-9_-]{20,}", self.bot_token)
                or not re.fullmatch(r"-?[0-9]+|@[A-Za-z][A-Za-z0-9_]{4,}", self.chat_id)):
            return False, "Telegram configuration is invalid"
        return True, "Configured"


class _SecretFilter(logging.Filter):
    def __init__(self, secrets):
        super().__init__()
        self.secrets = tuple(value for value in secrets if value)

    def filter(self, record):
        message = record.getMessage()
        for secret in self.secrets:
            message = message.replace(secret, "[REDACTED]")
        record.msg, record.args = message, ()
        return True


def _number(value) -> str:
    return format(value, ".10g") if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else "UNAVAILABLE"


def format_telegram_alert(event: FastEvent) -> str:
    """Only observed event values; no invented targets or probabilities."""
    level = event.support if event.bias == "SHORT" else event.resistance
    lines = [f"{event.symbol} — {translate(event.event_type)}", f"Bias: {event.bias}",
             f"Price: {_number(event.price)}",
             f"{'Support' if event.bias == 'SHORT' else 'Resistance'}: {_number(level.price if level else None)}",
             f"Pressure score: {_number(event.metrics.get('pressure_score'))} / uncalibrated",
             f"RVOL: {_number(event.metrics.get('relative_volume'))}",
             f"Regime: {translate(event.regime) if event.regime else 'UNAVAILABLE'}",
             "Data: " + (", ".join(f"{key}={value}" for key, value in sorted(event.data_quality.items())) or "UNAVAILABLE"),
             f"Market time: {event.market_event_timestamp.isoformat()}", event.reason[:500],
             "Decision support. No orders. Scores are not probabilities."]
    return "\n".join(lines)[:3900]


def format_trade_telegram_alert(event: TradeAlertRecord) -> str:
    """Human-readable decision support from the immutable observed payload."""
    def metric(*names):
        return next((event.metrics[name] for name in names if event.metrics.get(name) is not None), None)

    targets = ", ".join(_number(value) for value in event.targets) or "UNAVAILABLE"
    btc = json.dumps(event.btc_context, ensure_ascii=False, separators=(",", ":"))[:300] if event.btc_context else "UNAVAILABLE"
    components = ", ".join(f"{name}={_number(value.get('contribution') if isinstance(value, dict) else value)}"
                           for name, value in event.components.items())[:400] or "UNAVAILABLE"
    signal_state = getattr(event, "signal_state", None)
    header = f"TRADE ALERT — {signal_state}" if signal_state else event.phase
    lines = [f"{event.symbol} — {header}", f"Bias: {event.bias} | Setup: {event.setup_type}",
             f"Status: {event.status}", f"Price: {_number(event.price)}",
             f"Trigger: {_number(event.trigger_level)} | Distance: {_number(event.distance_pct)}%",
             f"Invalidation: {_number(event.invalidation)}", f"Targets: {targets}",
             f"Score: {_number(event.score)}/100 — UNCALIBRATED, not a probability",
             f"Components: {components}",
             f"RVOL: {_number(metric('relative_volume', 'relative_volume_5m', 'rvol_5m'))}",
             f"Volume acceleration: {_number(metric('volume_acceleration'))}",
             f"BTC context: {btc}",
             "Data: " + (", ".join(f"{key}={value}" for key, value in sorted(event.data_quality.items())) or "UNAVAILABLE"),
             f"Event UTC: {event.event_timestamp.isoformat()}",
             f"Detected UTC: {event.detected_at.isoformat()} | Expires UTC: {event.expires_at.isoformat()}",
             f"Detection latency: {_number(event.detection_latency_ms)} ms",
             event.reason[:450], f"Risk: {event.risk[:350]}",
             "Cancellation only; this setup is invalidated." if event.phase == "INVALIDATED"
             else "Conditional observation. No exchange orders are submitted."]
    return "\n".join(lines)[:3900]


def _provider_retry_after(response) -> float | None:
    if response.status_code != 429:
        return None
    try:
        value = response.json().get("parameters", {}).get("retry_after")
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None
    except (ValueError, TypeError, AttributeError):
        return None


async def send_telegram_test(client, settings: TelegramSettings, *, clock=utcnow) -> dict:
    """One explicitly requested connectivity message, never a fictitious signal.

    The caller may retry a reported transient/rate-limit failure after the
    returned delay. No hidden resend can duplicate an acknowledged test.
    """
    configured, reason = settings.readiness()
    if not configured:
        return {"status": "UNAVAILABLE", "channel": "telegram", "reason": reason,
                "http_requests": 0, "provider_accepted": False, "live_trading": False}
    filters = []
    secret_filter = _SecretFilter((settings.bot_token, settings.chat_id))
    for name in ("httpx", "httpcore", "httpcore.http11", "httpcore.http2", "httpcore.connection", "httpcore.proxy"):
        logger = logging.getLogger(name)
        logger.addFilter(secret_filter)
        filters.append(logger)
    started = time.perf_counter()
    base = {"channel": "telegram", "http_requests": 1, "provider_accepted": False,
            "handset_delivery_timestamp": None, "live_trading": False}
    try:
        response = await client.post(f"https://api.telegram.org/bot{settings.bot_token}/sendMessage",
            json={"chat_id": settings.chat_id, "text": "Expansion Hunter — Telegram connection test.\n"
                  "Notification transport only. This is not a market signal, trade recommendation, or order.",
                  "disable_web_page_preview": True}, timeout=settings.timeout_seconds)
        if response.status_code != 200:
            retryable = response.status_code == 429 or response.status_code >= 500
            retry_after = _provider_retry_after(response)
            delay = max(settings.retry_base_seconds, min(retry_after, 3600)) if retry_after is not None else settings.retry_base_seconds
            return {**base, "status": "RETRYING" if retryable else "FAILED",
                    "error": f"HTTP_{response.status_code}", "retryable": retryable,
                    "retry_after_seconds": delay if retryable else None}
        body = response.json()
        message_id = body.get("result", {}).get("message_id")
        if body.get("ok") is not True or not isinstance(message_id, int) or isinstance(message_id, bool):
            raise ValueError("invalid acknowledgement")
        return {**base, "status": "DELIVERED", "provider_accepted": True, "retryable": False,
                "provider_accepted_timestamp": aware(clock()).isoformat(), "telegram_message_id": message_id,
                "transport_latency_ms": (time.perf_counter() - started) * 1000,
                "delivery_semantics": "PROVIDER_ACCEPTED; handset delivery unobserved"}
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        return {**base, "status": "RETRYING", "error": "TRANSPORT_OR_RESPONSE_FAILURE",
                "retryable": True, "retry_after_seconds": settings.retry_base_seconds}
    finally:
        for logger in filters:
            logger.removeFilter(secret_filter)


class NotificationWorker:
    def __init__(self, store: IntelligenceStore, settings: TelegramSettings, client, *, clock=utcnow):
        self.store, self.settings, self.client, self.clock = store, settings, client, clock

    async def _result(self, row, at, *, error=None, retry_after=None, terminal=False, details=None):
        attempt = row["attempts"] + 1
        delay = min(self.settings.retry_cap_seconds, self.settings.retry_base_seconds * 2 ** (attempt - 1))
        if retry_after is not None:
            delay = max(delay, min(retry_after, 3600))
        terminal = terminal or attempt >= self.settings.max_attempts
        retry = at + timedelta(seconds=delay) if error and not terminal else None
        created = aware(datetime.fromisoformat(row["created_at"]))
        latency = max(0.0, (at - created).total_seconds() * 1000) if not error else None

        def save():
            self.store.notification_result(row["event_id"], row["channel"], at, error,
                                           next_attempt_at=retry, terminal=terminal, latency_ms=latency)
            self.store.put_state("notification_delivery", row["event_id"], at,
                                 {"event_id": row["event_id"], "channel": "telegram",
                                  "status": "FAILED" if error and terminal else "RETRYING" if error else "DELIVERED",
                                  "provider_accepted_timestamp": at.isoformat() if not error else None,
                                  "handset_delivery_timestamp": None, "notification_latency_ms": latency,
                                  "error": error, "next_attempt_at": retry.isoformat() if retry else None,
                                  **(details or {})})

        await _complete_thread_call(save)

    async def _deliver(self, row):
        now = aware(self.clock())
        if row["channel"] not in {"configured", "telegram"}:
            await self._result(row, now, error="UNSUPPORTED_CHANNEL", terminal=True)
            return "unsupported"
        existing = await _complete_thread_call(self.store.delivered_notification, row["event_id"])
        if existing:
            await self._result(row, aware(datetime.fromisoformat(existing)), details={"deduplicated": True})
            return "deduplicated"
        payload = await _complete_thread_call(self.store.object, "fast_event", row["event_id"], now)
        trade_alert = payload is None
        try:
            if trade_alert:
                payload = await _complete_thread_call(self.store.object, "trade_alert", row["event_id"], now)
                event = TradeAlertRecord.model_validate(payload)
            else:
                event = FastEvent.model_validate(payload)
                FastStage(event.event_type)
            if event.event_id != row["event_id"]:
                raise ValueError("event identity mismatch")
        except (TypeError, ValueError):
            await self._result(row, now, error="MISSING_OR_INVALID_FAST_EVENT", terminal=True)
            return "invalid"
        if trade_alert:
            stale = (event.event_timestamp > now or event.received_at > now or event.detected_at > now
                     or event.expires_at <= now or event.status in {"LATE", "MISSED"}
                     or (now - event.event_timestamp).total_seconds() > self.settings.max_age_seconds
                     or any(event.data_quality.get(key) != "AVAILABLE" for key in ("1m", "5m")))
            if event.phase == "BREAK_CONFIRMED" and event.trigger_discovered_at > event.event_timestamp:
                stale = True
            engine_state = await _complete_thread_call(self.store.get_state, "trade_alert_engine", "default")
            if engine_state is None:
                engine_state = await _complete_thread_call(self.store.get_state, "trade_alert_engine", "active")
            episodes = engine_state.get("episodes", {}) if isinstance(engine_state, dict) else {}
            episode = episodes.get(event.episode_id, {}) if isinstance(episodes, dict) else {}
            if not episode and isinstance(episodes, dict):
                # Production episodes are keyed by asset|bias|setup_type; the
                # immutable episode ID is stored in each value, not its key.
                episode = next((value for value in episodes.values() if isinstance(value, dict)
                                and value.get("episode_id") == event.episode_id), {})
            if isinstance(episode, dict):
                stale = stale or episode.get("restart_stale") is True
                invalidated = episode.get("phase") == "INVALIDATED" or episode.get("status") == "INVALIDATED"
                stale = stale or (invalidated and event.phase != "INVALIDATED")
            text = format_trade_telegram_alert(event)
        else:
            stale = (event.alert_created_timestamp > now or event.data_received_timestamp > now
                     or (now - event.market_event_timestamp).total_seconds() > self.settings.max_age_seconds
                     or any(event.data_quality.get(key) in {"STALE", "MISSING", "UNAVAILABLE"}
                            for key in ("1m", "5m", "spot_candles", "candles")))
            text = format_telegram_alert(event)
        if stale:
            await self._result(row, now, error="STALE_OR_UNAVAILABLE_CRITICAL_DATA", terminal=True)
            return "stale"
        started = time.perf_counter()
        try:
            response = await self.client.post(f"https://api.telegram.org/bot{self.settings.bot_token}/sendMessage",
                                              json={"chat_id": self.settings.chat_id, "text": text,
                                                    "disable_web_page_preview": True},
                                              timeout=self.settings.timeout_seconds)
            received = aware(self.clock())
            if response.status_code != 200:
                retry_after = _provider_retry_after(response)
                await self._result(row, received, error=f"HTTP_{response.status_code}", retry_after=retry_after,
                                   terminal=400 <= response.status_code < 500 and response.status_code != 429)
                return "failed"
            body = response.json()
            message_id = body.get("result", {}).get("message_id")
            if body.get("ok") is not True or not isinstance(message_id, int) or isinstance(message_id, bool):
                raise ValueError("invalid Telegram acknowledgement")
            await self._result(row, received, details={"telegram_message_id": message_id,
                                                     "transport_latency_ms": (time.perf_counter() - started) * 1000})
            return "delivered"
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            # Exception strings/response bodies may contain the token or chat ID.
            await self._result(row, aware(self.clock()), error="TRANSPORT_OR_RESPONSE_FAILURE")
            return "failed"

    async def run_once(self) -> dict:
        configured, reason = self.settings.readiness()
        now = aware(self.clock())
        if not configured:
            return {"status": "UNAVAILABLE", "channel": "telegram", "reason": reason, "http_requests": 0}
        expired = await _complete_thread_call(
            lambda: self.store.expire_notifications(now, max_age_seconds=self.settings.max_age_seconds,
                                                    max_attempts=self.settings.max_attempts))
        rows = await _complete_thread_call(
            lambda: self.store.pending_notifications(now, limit=self.settings.batch_size,
                                                     max_attempts=self.settings.max_attempts))
        results = {}
        for row in rows:
            pending = asyncio.create_task(self._deliver(row))
            try:
                outcome = await asyncio.shield(pending)
            except asyncio.CancelledError:
                await pending  # Drain provider acknowledgement + SQLite commit before shutdown.
                raise
            results[outcome] = results.get(outcome, 0) + 1
        counts = await _complete_thread_call(self.store.notification_counts)
        return {"status": "DEGRADED" if any(key in results for key in ("failed", "invalid", "unsupported", "stale")) else "AVAILABLE",
                "channel": "telegram", "updated_at": aware(self.clock()).isoformat(), "processed": len(rows),
                "results": results, "expired": expired, "outbox_counts": counts, "live_trading": False,
                "delivery_semantics": "PROVIDER_ACCEPTED_AT_LEAST_ONCE; handset delivery unobserved"}


async def run_notification_worker(app, *, interval_seconds: float = 2, settings: TelegramSettings | None = None,
                                  client_factory=httpx.AsyncClient, clock=utcnow):
    if not math.isfinite(interval_seconds) or interval_seconds <= 0:
        raise ValueError("notification interval must be positive")
    settings = settings or TelegramSettings.from_env()
    configured, reason = settings.readiness()
    if not configured:
        app.state.notification_health = {"status": "UNAVAILABLE", "channel": "telegram", "reason": reason,
                                         "http_requests": 0, "live_trading": False}
        return
    owned, filters = {}, []
    secret_filter = _SecretFilter((settings.bot_token, settings.chat_id))
    for name in ("httpx", "httpcore", "httpcore.http11", "httpcore.http2", "httpcore.connection", "httpcore.proxy"):
        logger = logging.getLogger(name)
        logger.addFilter(secret_filter)
        filters.append(logger)

    def open_store():
        owned["store"] = IntelligenceStore(app.state.settings.db_path)

    app.state.notification_health = {"status": "STARTING", "channel": "telegram", "live_trading": False}
    try:
        await _complete_thread_call(open_store)
        async with client_factory(timeout=settings.timeout_seconds, follow_redirects=False) as client:
            worker = NotificationWorker(owned["store"], settings, client, clock=clock)
            while True:
                try:
                    app.state.notification_health = await worker.run_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    app.state.notification_health = {"status": "FAILED", "channel": "telegram",
                                                     "reason": "Notification worker failed; market engine remains independent",
                                                     "updated_at": aware(clock()).isoformat(), "live_trading": False}
                await asyncio.sleep(interval_seconds)
    finally:
        if owned.get("store"):
            await _complete_thread_call(owned["store"].close)
        for logger in filters:
            logger.removeFilter(secret_filter)
        app.state.notification_health = {**getattr(app.state, "notification_health", {}), "status": "STOPPED"}
