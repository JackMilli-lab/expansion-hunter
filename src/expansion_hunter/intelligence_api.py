"""Authenticated read-only intelligence views and an observation cockpit."""

from __future__ import annotations

import asyncio
import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import Field

from .intelligence import context_at, load_macro
from .models import Model, utcnow


def _age_seconds(timestamp, now):
    """Invalid or future cache timestamps cannot rehabilitate market evidence."""
    if timestamp is None:
        return None
    try:
        stamp = timestamp if isinstance(timestamp, datetime) else datetime.fromisoformat(timestamp)
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        return None
    return (now - stamp).total_seconds()


def _fresh_fast_state(state, now):
    age = _age_seconds(state.get("last_evaluation_at"), now)
    return bool(
        not state.get("stale", True)
        and age is not None
        and 0 <= age <= 90
        and state.get("data_quality", {}).get("spot_candles") == "AVAILABLE"
        and state.get("stage") != "DEMOTED"
    )


def _live_status(record, now):
    age = _age_seconds(record.get("received_timestamp") or record.get("last_received_timestamp"), now)
    status = "UNAVAILABLE" if age is None or age < 0 else "STALE" if age > 30 else "AVAILABLE"
    if status == "AVAILABLE" and record.get("price_quality") in {"STALE", "UNAVAILABLE"}:
        status = record["price_quality"]
    return status, age


def _runtime_status(app, now):
    task = getattr(app.state, "live_task", None)
    in_process = bool(task and not task.done())
    heartbeats = app.state.store.states("live_runtime")
    recent = []
    for record in heartbeats:
        age = _age_seconds(record.get("updated_at"), now)
        if record.get("running") and age is not None and 0 <= age <= 10:
            recent.append((age, record))
    discovery = _discovery_runtime(app)
    return {
        "running": in_process or bool(recent) or discovery["running"],
        "fast_running": in_process or bool(recent),
        "in_process": in_process,
        "heartbeat_age_seconds": min((age for age, _ in recent), default=None),
        "evidence": "IN_PROCESS_TASK"
        if in_process
        else "RECENT_WORKER_HEARTBEAT"
        if recent
        else "DISCOVERY_SUBPROCESS"
        if discovery["running"]
        else "UNAVAILABLE",
    }


def _discovery_runtime(app):
    service = getattr(app.state, "discovery_service", None)
    if service is None:
        return {
            "running": False,
            "status": "UNAVAILABLE",
            "configured_assets": 500,
            "configured_universe_size": 500,
        }
    report = dict(service.status())
    report.setdefault("configured_assets", 500)
    report.setdefault("configured_universe_size", report["configured_assets"])
    report.setdefault("running", False)
    return report


def _native_monitoring_intent_available(app, now, *, cycle=None):
    """A verified native promotion/chart request can start an empty fast worker.

    Current native instrument identity is preferred over CoinGecko coverage.
    Persisted monitoring intent is not a price observation or a score bonus;
    the live worker still validates membership and waits for fresh candles.
    """
    from .models import Asset

    universe = getattr(app.state, "universe_worker", None)
    legacy = {observation.asset.exchange_symbol: observation.asset
              for observation in cycle.observations} if cycle else {}
    for kind in ("universe_promotion", "chart_interest"):
        for row in app.state.store.states(kind):
            try:
                symbol = row["symbol"]
                if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9]{5,24}", symbol):
                    continue
                created = datetime.fromisoformat(row["created_at"])
                expires = datetime.fromisoformat(row["expires_at"])
                if (not created.tzinfo or not expires.tzinfo
                        or not 0 < (expires - created).total_seconds() <= 900
                        or not created <= now < expires):
                    continue
                asset = Asset.model_validate(row["asset"])
                mapped = universe.asset(symbol) if universe is not None else None
                mapped = mapped or legacy.get(symbol)
                if (mapped is None or asset.asset_id != row["asset_id"]
                        or asset.asset_id != mapped.asset_id
                        or asset.exchange_symbol != symbol or mapped.exchange_symbol != symbol
                        or asset.symbol.upper() != mapped.symbol.upper()
                        or asset.quote_asset != mapped.quote_asset):
                    continue
                if kind == "universe_promotion":
                    source = datetime.fromisoformat(row["source_timestamp"])
                    if (not source.tzinfo or not source <= created
                            or (created - source).total_seconds() > 15
                            or row.get("native_instrument_verified") is not True
                            or row.get("source") != "NATIVE_UNIVERSE_DISCOVERY"
                            or row.get("bias") not in {"LONG", "SHORT"}
                            or asset.quote_asset != "USDT"
                            or symbol != asset.symbol.upper() + "USDT"):
                        continue
                elif (row.get("purpose") != "TEMPORARY_CHART_OBSERVATION"
                      or row.get("score_bonus") != 0):
                    continue
                return True
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
    return False


def _automatic_membership_available(app):
    if app.state.store.watchlist():
        return True
    now = utcnow()
    # Fresh native installations may have no CoinGecko/discovery cycle at all.
    # Check the cheap native monitoring intents before reading that archive.
    if _native_monitoring_intent_available(app, now):
        return True
    cycle = app.state.store.latest_summary()
    return bool(
        cycle
        and (any(
            observation.eligible
            and observation.asset.exchange_symbol
            and observation.stage.value in {"RADAR", "WATCH", "PRE_IGNITION", "LOADED", "IGNITION"}
            for observation in cycle.observations
        ) or _native_monitoring_intent_available(app, now, cycle=cycle))
    )


async def _start_discovery_service(app):
    try:
        await app.state.discovery_service.start()
    except Exception as exc:
        await asyncio.to_thread(app.state.store.put_state,
            "provider_error", "discovery_service", utcnow(), {"status": "FAILED", "error": str(exc)}
        )
        raise HTTPException(503, "Automatic discovery could not start; inspect provider health") from exc


async def _start_fast_worker(app):
    """Read persisted promotions; never perform broad collection in the API process."""
    task = getattr(app.state, "live_task", None)
    if task and not task.done():
        return True
    if not await asyncio.to_thread(_automatic_membership_available, app):
        return False
    from .live_runtime import LiveObservationWorker

    def prepare():
        worker = LiveObservationWorker(app.state.settings)
        try:
            membership = worker.membership()
        except Exception:
            worker.store.close()
            raise
        return worker, membership

    worker, membership = await asyncio.to_thread(prepare)
    if not membership:
        worker.store.close()
        return False
    app.state.live_worker = worker
    app.state.live_worker_error = None

    async def run_worker():
        try:
            await worker.run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Publish failure immediately; archival writes may be queued behind
            # other SQLite work. Health must not briefly report a healthy worker.
            app.state.live_worker_error = {"status": "FAILED", "error": str(exc),
                                           "timestamp": utcnow().isoformat(), "component": "live_worker"}
            await asyncio.to_thread(app.state.store.put_state,
                "provider_error", "live_worker", utcnow(), app.state.live_worker_error
            )

    app.state.live_task = asyncio.create_task(run_worker())
    return True


async def _supervise_fast_watch(app):
    """Promotions published by the independent broad process become fast watch."""
    while getattr(app.state, "live_requested", False):
        try:
            await _start_fast_worker(app)
        except Exception as exc:
            await asyncio.to_thread(app.state.store.put_state,
                "provider_error", "fast_supervisor", utcnow(), {"status": "FAILED", "error": str(exc)}
            )
        await asyncio.sleep(2)


async def resume_automatic_observation(app):
    """Restore an explicitly requested scanner after a process restart.

    This restores monitoring intent, not feed freshness. Fast state restoration
    and readiness checks still require new public observations.
    """
    controls = app.state.store.states("app_control")
    if not any(row.get("automatic_observation_requested") is True for row in controls):
        return False
    await _start_discovery_service(app)
    app.state.live_requested = True
    await _start_fast_worker(app)
    app.state.live_supervisor_task = asyncio.create_task(_supervise_fast_watch(app))
    return True


async def stop_application_workers(app):
    """Stop owned tasks/processes without deleting any archived observations."""
    app.state.live_requested = False
    supervisor = getattr(app.state, "live_supervisor_task", None)
    if supervisor and not supervisor.done():
        supervisor.cancel()
        await asyncio.gather(supervisor, return_exceptions=True)
    worker = getattr(app.state, "live_worker", None)
    task = getattr(app.state, "live_task", None)
    if worker:
        worker.stop.set()
        if worker.stream:
            worker.stream.stop()
    if task and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    service = getattr(app.state, "discovery_service", None)
    errors = []
    if service:
        try:
            await service.stop()
        except Exception as exc:
            errors.append(str(exc))
            await asyncio.to_thread(app.state.store.put_state,
                "provider_error", "discovery_service", utcnow(), {"status": "FAILED", "error": str(exc)}
            )
    return errors


class WatchlistInput(Model):
    asset_id: str
    symbol: str
    priority: int = Field(default=0, ge=0, le=100)
    pinned: bool = False
    notes: str = Field(default="", max_length=2000)


async def local_or_authenticated(request: Request):
    """Personal settings control only, never exchange actions. Protect localhost from CSRF."""
    import os

    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc != request.headers.get("host"):
        raise HTTPException(403, "Cross-origin app changes are not allowed")
    if (
        not os.getenv("COCKPIT_API_TOKEN")
        and request.client
        and request.client.host not in {"127.0.0.1", "::1", "localhost", "testclient"}
    ):
        raise HTTPException(403, "Configure COCKPIT_API_TOKEN for remote app controls")


def install_routes(app, authorize):
    @app.get("/api/i18n/{locale}")
    def translations(locale: str):
        from .i18n import catalog

        try:
            return catalog(locale)
        except ValueError as exc:
            raise HTTPException(404, "Language unavailable") from exc

    def cutoff(before):
        if before and before.tzinfo is None:
            raise HTTPException(422, "timestamp must include UTC offset")
        return before or utcnow()

    @app.get("/cockpit", response_class=HTMLResponse)
    async def cockpit():
        if getattr(app.state, "trade_alert_mode", False):
            return files("expansion_hunter").joinpath("static/trade_alert.html").read_text()
        # HTML contains no data or secrets. Data requests use the existing bearer guard.
        return files("expansion_hunter").joinpath("static/cockpit.html").read_text()

    @app.get("/cockpit-assets/{asset_name}")
    async def cockpit_asset(asset_name: str):
        # Only public interface assets; no API observations or private files.
        assets = {
            "chart-engine.js": ("chart_engine.js", "application/javascript"),
            "candidate-ui.js": ("candidate_ui.js", "application/javascript"),
            "manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
            "sw.js": ("sw.js", "application/javascript"),
            "icon.svg": ("icon.svg", "image/svg+xml"),
        }
        if asset_name not in assets:
            raise HTTPException(404, "Interface asset unavailable")
        name, media_type = assets[asset_name]
        headers = {"Cache-Control": "no-cache"}
        if asset_name == "sw.js":
            headers["Service-Worker-Allowed"] = "/"
        return Response(files("expansion_hunter").joinpath("static", name).read_bytes(),
                        media_type=media_type, headers=headers)

    @app.get("/", response_class=HTMLResponse)
    async def home():
        return await cockpit()

    @app.get("/api/cockpit/overview", dependencies=[Depends(authorize)])
    def overview():
        now = utcnow()
        discovery = app.state.store.latest_summary()
        personal = app.state.store.watchlist()
        live_records = app.state.store.live_overview_records()
        fast_records = app.state.store.states_projected(
            "fast_state", ("asset", "stage", "stale", "last_evaluation_at", "data_quality",
                           "pressure_metrics", "trigger_metrics", "resistance", "support", "bias", "bias_reason", "promoted_at")
        )
        structures = app.state.store.states_projected(
            "war_room_state", ("symbol", "as_of", "bias", "setup_quality", "entry_quality", "alignment", "expected_path", "swing_phases")
        )
        # The market table needs decisions, not all six structure histories and
        # component trees. Full evidence stays in the selected coin endpoint.
        structures_by_symbol = {}
        for record in structures:
            compact = dict(record)
            compact["swing_phases"] = {}
            for frame, swing in (record.get("swing_phases") or {}).items():
                decision = swing.get("decision", {})
                visible = {key: decision.get(key) for key in (
                    "side", "entry_status", "trigger", "invalidation", "target_1", "target_2", "entry_zone")}
                for key in ("setup_quality", "entry_quality"):
                    quality = decision.get(key)
                    visible[key] = ({name: quality.get(name) for name in ("value", "status", "calibration")}
                                    if isinstance(quality, dict) else quality)
                compact["swing_phases"][frame] = {"current_phase": swing.get("current_phase"),
                    "side": swing.get("side"), "timeframe": frame, "decision": visible}
            structures_by_symbol[record.get("symbol")] = compact
        live_by_asset = {record.get("asset_id"): record for record in live_records}
        live_by_symbol = {record.get("symbol"): record for record in live_records}
        fast_by_asset = {record.get("asset", {}).get("asset_id"): record for record in fast_records}
        rows = []
        if discovery:
            for o in discovery.observations:
                rows.append(
                    {
                        "asset_id": o.asset.asset_id,
                        "symbol": o.asset.symbol,
                        "exchange_symbol": o.asset.exchange_symbol,
                        "price": o.features.get("price").value if o.features.get("price") else None,
                        "state": o.stage.value,
                        "bias": o.bias,
                        "ranking_mode": o.ranking_mode,
                        "rank": o.rank.model_dump(),
                        "scores": {
                            k: {"value": v.value, "coverage": v.coverage} for k, v in o.scores.items()
                        },
                        "levels": o.levels,
                        "as_of": o.as_of,
                        "freshness": "STALE" if (now - o.as_of).total_seconds() > 300 else o.freshness.value,
                        "reasons": o.reasons,
                        "eligible": o.eligible,
                    }
                )
        else:
            legacy = app.state.store.latest()
            for r in legacy.results if legacy else []:
                rows.append(
                    {
                        "asset_id": r.asset.asset_id,
                        "symbol": r.asset.symbol,
                        "exchange_symbol": r.asset.exchange_symbol,
                        "price": r.technical.get("4h").metrics.get("close")
                        if r.technical.get("4h")
                        else None,
                        "state": r.stage.value,
                        "rank": None,
                        "scores": {"expansion_score": {"value": r.expansion_score, "coverage": r.confidence}},
                        "levels": {},
                        "as_of": r.as_of,
                        "freshness": "STALE",
                        "reasons": r.warnings,
                        "eligible": False,
                        "source": "ARCHIVED_LEGACY_REPORT",
                    }
                )
        by_asset = {row["asset_id"]: row for row in rows}
        for item in personal:
            row = by_asset.get(item["asset_id"])
            if row is None:
                row = {
                    "asset_id": item["asset_id"],
                    "symbol": item["symbol"],
                    "exchange_symbol": item["symbol"],
                    "price": None,
                    "state": "DORMANT",
                    "rank": None,
                    "scores": {},
                    "levels": {},
                    "as_of": None,
                    "freshness": "MISSING",
                    "eligible": False,
                    "reasons": ["Personal monitoring priority; no discovery score bonus"],
                    "source": "PERSONAL_WATCHLIST",
                }
                rows.append(row)
                by_asset[item["asset_id"]] = row
            row["personal_watchlist"] = True
            row["monitoring_priority"] = item["priority"]
        for asset_id, fast in fast_by_asset.items():
            live = live_by_asset.get(asset_id)
            if asset_id and asset_id not in by_asset and live and fast.get("stage") != "DEMOTED":
                row = {
                    "asset_id": asset_id,
                    "symbol": fast["asset"].get("symbol"),
                    "exchange_symbol": live.get("symbol"),
                    "price": None,
                    "state": "DORMANT",
                    "rank": None,
                    "scores": {},
                    "levels": {},
                    "as_of": None,
                    "freshness": "MISSING",
                    "eligible": False,
                    "reasons": [],
                    "source": "LIVE_OBSERVATION",
                }
                rows.append(row)
                by_asset[asset_id] = row
        for row in rows:
            row["war_room"] = structures_by_symbol.get(row.get("exchange_symbol"))
            live = live_by_asset.get(row["asset_id"]) or live_by_symbol.get(row.get("exchange_symbol"))
            fast = fast_by_asset.get(row["asset_id"])
            if live:
                status, age = _live_status(live, now)
                # Keep stale native values visible with their timestamp; never invent a fresh price.
                if live.get("price") is not None:
                    row["price"] = live["price"]
                    row["price_as_of"] = (live.get("price_source_timestamp") or
                                           live.get("last_exchange_timestamp") or live.get("received_timestamp"))
                    row["price_source"] = live.get("price_source") or "NATIVE_LIVE_CACHE"
                row["freshness"] = {"AVAILABLE": "FRESH", "STALE": "STALE", "UNAVAILABLE": "MISSING"}[status]
                row["live_status"] = status
                row["live_age_seconds"] = age
                row["data_quality"] = live.get("data_quality", {})
                row["liquidity_map"] = live.get("liquidity_map", [])
                row["as_of"] = live.get("as_of") or live.get("received_timestamp")
            if fast:
                row["state"] = fast.get("stage", row["state"])
                row["fast_bias"] = fast.get("bias", "LONG") if (fast.get("support") or fast.get("resistance")) else "UNAVAILABLE"
                row["bias_reason"] = fast.get("bias_reason")
                row["fast_state_stale"] = not _fresh_fast_state(fast, now)
                row["pressure"] = fast.get("pressure_metrics", {})
                row["trigger"] = fast.get("trigger_metrics", {})
                resistance = fast.get("resistance")
                if resistance:
                    row["levels"] = {**row["levels"], "resistance": resistance.get("price")}
                    row["resistance_discovered_at"] = resistance.get("discovered_at")
                support = fast.get("support")
                if support:
                    row["levels"] = {**row["levels"], "support": support.get("price")}
                    row["support_discovered_at"] = support.get("discovered_at")
                frozen_level = support if row["fast_bias"] == "SHORT" else resistance
                row["trigger_level"] = frozen_level.get("price") if frozen_level else None
        discovery_status = {
            "status": discovery.status if discovery else "NO_DISCOVERY_CYCLE",
            "counts": discovery.counts if discovery else {},
            "completed_at": discovery.completed_at if discovery else None,
            "provider_issues": discovery.provider_issues if discovery else [],
        }
        return {
            "assets": rows,
            "watchlist": personal,
            "market": context_at(app.state.store, now),
            "mode": "OBSERVATION",
            "discovery_status": discovery_status,
            "discovery_runtime": _discovery_runtime(app),
            "as_of": now,
            "live_trading": False,
            "feed_states": app.state.store.states("feed_health"),
        }

    @app.get("/api/intelligence/cycle", dependencies=[Depends(authorize)])
    def cycle(before: datetime | None = None):
        when = cutoff(before)
        records = app.state.store.objects("cycle_state", when, limit=1)
        if not records:
            return {
                "status": "UNAVAILABLE",
                "reason": "Run intelligence worker after discovery",
                "cycle": None,
            }
        report = records[0]
        observed = report.get("as_of")
        age = (when - datetime.fromisoformat(observed)).total_seconds() if observed else None
        return {
            "status": "STALE" if age is None or age > 86400 else "AVAILABLE",
            "age_seconds": age,
            "cycle": report,
            "scores_are_probabilities": False,
        }

    @app.get("/api/intelligence/macro/calendar", dependencies=[Depends(authorize)])
    def calendar(days: int = Query(default=7, ge=1, le=31), before: datetime | None = None):
        when = cutoff(before)
        engine = load_macro(app.state.store, when)
        start = when.replace(hour=0, minute=0, second=0, microsecond=0)
        events = engine.calendar(when, start, when + timedelta(days=days))
        providers = app.state.store.objects("macro_provider", when, limit=1)
        return {
            "as_of": when,
            "provider": providers[0] if providers else None,
            "status": "AVAILABLE" if events else "UNAVAILABLE",
            "events": [engine.event_view(event.event_id, when) for event in events],
        }

    @app.get("/api/intelligence/macro/context", dependencies=[Depends(authorize)])
    def macro_context(before: datetime | None = None):
        return load_macro(app.state.store, cutoff(before)).context(cutoff(before))

    @app.get("/api/intelligence/macro/events/{event_id}", dependencies=[Depends(authorize)])
    def macro_event(event_id: str, before: datetime | None = None):
        when = cutoff(before)
        view = load_macro(app.state.store, when).event_view(event_id, when)
        if view.get("event") is None:
            raise HTTPException(404, "Macro event unavailable at this timestamp")
        return {
            "event": view,
            "reactions": [
                r for r in app.state.store.objects("macro_reaction", when) if r.get("event_id") == event_id
            ],
        }

    @app.get("/api/intelligence/analogs/{asset_id}", dependencies=[Depends(authorize)])
    def analogs(asset_id: str, before: datetime | None = None):
        when = cutoff(before)
        return {
            "label": "HISTORICAL RESEARCH — NOT FORECAST",
            "queries": app.state.store.objects("analog_query", when, asset_id=asset_id, limit=10),
            "outcomes": app.state.store.objects("research_outcome", when, asset_id=asset_id, limit=100),
            "patterns": app.state.store.objects("cycle_patterns", when, asset_id=asset_id, limit=10),
        }

    @app.get("/api/intelligence/events", dependencies=[Depends(authorize)])
    def research_events(limit: int = Query(default=100, ge=1, le=1000), before: datetime | None = None):
        when = cutoff(before)
        return {
            "events": app.state.store.objects("research_event", when, limit=limit),
            "misses": app.state.store.objects("miss_event", when, limit=limit),
        }

    @app.get("/api/watchlist", dependencies=[Depends(authorize)])
    def watchlist():
        return {"watchlist": app.state.store.watchlist(), "monitoring_priority_is_score_boost": False}

    @app.put("/api/watchlist", dependencies=[Depends(authorize), Depends(local_or_authenticated)])
    def add_watchlist(body: WatchlistInput):
        symbol = body.symbol.strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{5,24}", symbol):
            raise HTTPException(422, "Use a public exchange pair such as XRPUSDT")
        asset_id = body.asset_id.strip()
        if not asset_id or len(asset_id) > 150:
            raise HTTPException(422, "Invalid asset identity")
        # Reuse verified existing exchange mapping where present.
        latest = app.state.store.latest_summary()
        if latest:
            mapped = next((o.asset for o in latest.observations if o.asset.exchange_symbol == symbol), None)
            if mapped:
                asset_id = mapped.asset_id
        app.state.store.watchlist_add(
            asset_id, symbol, priority=body.priority, pinned=body.pinned, notes=body.notes
        )
        return {"watchlist": app.state.store.watchlist(), "exchange_access": "PUBLIC_READ_ONLY"}

    @app.delete(
        "/api/watchlist/{asset_id:path}", dependencies=[Depends(authorize), Depends(local_or_authenticated)]
    )
    def remove_watchlist(asset_id: str):
        app.state.store.watchlist_remove(asset_id)
        return {"watchlist": app.state.store.watchlist()}

    @app.post("/api/live/start", dependencies=[Depends(authorize), Depends(local_or_authenticated)])
    async def start_live():
        await _start_discovery_service(app)
        app.state.live_requested = True
        await asyncio.to_thread(app.state.store.put_state, "app_control", "app", utcnow(),
            {"automatic_observation_requested": True, "mode": "OBSERVATION", "live_trading": False})
        task = getattr(app.state, "live_task", None)
        already_running = bool(task and not task.done())
        try:
            has_fast_watch = await _start_fast_worker(app)
        except Exception as exc:
            has_fast_watch = False
            await asyncio.to_thread(app.state.store.put_state,
                "provider_error", "fast_start", utcnow(), {"status": "FAILED", "error": str(exc)}
            )
        supervisor = getattr(app.state, "live_supervisor_task", None)
        if not supervisor or supervisor.done():
            app.state.live_supervisor_task = asyncio.create_task(_supervise_fast_watch(app))
        return {
            "status": "RUNNING" if already_running else "STARTING" if has_fast_watch else "DISCOVERING",
            "mode": "OBSERVATION",
            "private_exchange_keys_required": False,
            "discovery_runtime": _discovery_runtime(app),
            "manual_watchlist_required": False,
            "message": "Automatic broad discovery is running; promoted candidates receive fast monitoring.",
        }

    @app.post("/api/live/stop", dependencies=[Depends(authorize), Depends(local_or_authenticated)])
    async def stop_live():
        await asyncio.to_thread(app.state.store.put_state, "app_control", "app", utcnow(),
            {"automatic_observation_requested": False, "mode": "OBSERVATION", "live_trading": False})
        errors = await stop_application_workers(app)
        if errors:
            raise HTTPException(503, "Automatic discovery shutdown failed; inspect provider health")
        return {"status": "STOPPED", "mode": "OBSERVATION"}

    @app.post("/api/discovery/start", dependencies=[Depends(authorize), Depends(local_or_authenticated)])
    async def start_discovery():
        await _start_discovery_service(app)
        return {"mode": "OBSERVATION", "discovery_runtime": _discovery_runtime(app)}

    @app.post("/api/discovery/stop", dependencies=[Depends(authorize), Depends(local_or_authenticated)])
    async def stop_discovery():
        await app.state.discovery_service.stop()
        return {"mode": "OBSERVATION", "discovery_runtime": _discovery_runtime(app)}

    @app.get("/api/discovery/health", dependencies=[Depends(authorize)])
    def discovery_health():
        cycle = app.state.store.latest_summary()
        return {
            "discovery_runtime": _discovery_runtime(app),
            "counts": cycle.counts if cycle else {},
            "completed_at": cycle.completed_at if cycle else None,
            "provider_issues": cycle.provider_issues if cycle else [],
            "mode": "OBSERVATION",
        }

    @app.get("/api/live/health", dependencies=[Depends(authorize)])
    def live_health():
        now = datetime.now(UTC)
        worker = getattr(app.state, "live_worker", None)
        task = getattr(app.state, "live_task", None)
        if (worker is not None and task is not None and not task.done()
                and getattr(getattr(worker, "state", None), "events_accepted", 0) > 0):
            # During live operation health must not queue behind large archive
            # reads. These are actual in-process observations, not saved prices
            # promoted to LIVE. Staleness is recomputed at the request boundary.
            feed = worker.stream.health.model_dump(mode="json")
            age = _age_seconds(feed.get("last_message_timestamp"), now)
            fresh = bool(feed.get("connection") == "CONNECTED" and not feed.get("stale", True)
                         and age is not None and 0 <= age <= 30)
            states = tuple(worker.monitor.states.copy().values())
            fresh_states = sum(_fresh_fast_state({"stage": state.stage.value, "stale": state.stale,
                "last_evaluation_at": state.last_evaluation_at, "data_quality": state.data_quality}, now) for state in states)
            metrics = worker.metrics()
            discovery_runtime = _discovery_runtime(app)
            coverage = dict(getattr(worker, "discovery_coverage", {}))
            context = dict(getattr(worker, "_context", {}))
            error = getattr(app.state, "live_worker_error", None)
            persistence = metrics.get("persistence", {}).get("status", "UNAVAILABLE")
            return {"database": "AVAILABLE" if persistence == "AVAILABLE" else "DEGRADED",
                "database_path": app.state.store.database_path, "feed_health": [feed], "feed_fresh": fresh,
                "feed_age_seconds": age, "fast_assets": len(states), "fresh_fast_assets": fresh_states,
                "runtime": {"running": True, "fast_running": True, "in_process": True, "evidence": "NATIVE_IN_PROCESS_EVENTS"},
                "live_worker_running": True, "discovery_runtime": discovery_runtime,
                "broad_running": discovery_runtime["running"], "broad_counts": coverage.get("counts", {}),
                "broad_completed_at": coverage.get("completed_at"),
                "broad_report_age_seconds": _age_seconds(coverage.get("completed_at"), now),
                "broad_freshness_counts": coverage.get("freshness_counts", {}),
                "broad_counts_scope": "Cached actual cycle coverage; no archive work in health hot path",
                "paper_journal": getattr(app.state, "paper_journal_health", {"status": "UNAVAILABLE"}),
                "intelligence_worker": getattr(app.state, "intelligence_health", {"status": "UNAVAILABLE"}),
                "macro_calendar_worker": getattr(app.state, "macro_calendar_health", {"status": "UNAVAILABLE"}),
                "whale_worker": getattr(app.state, "whale_health", {"status": "UNAVAILABLE"}),
                "notification_worker": getattr(app.state, "notification_health", {"status": "UNAVAILABLE"}),
                "universe_worker": getattr(app.state, "universe_health", {"status": "UNAVAILABLE"}),
                "cycle_macro": context, "mode": "OBSERVATION", "provider_errors": [error] if error else [],
                "metrics": metrics, "live_trading": False,
                "ready": fresh and bool(fresh_states) and persistence == "AVAILABLE"}
        health = app.state.store.states("feed_health")
        latest = app.state.store.latest_summary()
        states = app.state.store.states_projected(
            "fast_state", ("stage", "stale", "last_evaluation_at", "data_quality")
        )
        ages = [
            _age_seconds(h["last_message_timestamp"], now)
            for h in health
            if h.get("last_message_timestamp")
            and h.get("connection") == "CONNECTED"
            and not h.get("stale", True)
        ]
        age = min((value for value in ages if value is not None and value >= 0), default=None)
        fresh = bool(age is not None and 0 <= age <= 30)
        fresh_states = [state for state in states if _fresh_fast_state(state, now)]
        runtime = _runtime_status(app, now)
        discovery_runtime = _discovery_runtime(app)
        metrics = app.state.store.get_state("live_metrics", "worker") or {}
        provider_errors = app.state.store.states("provider_error")
        worker_error = getattr(app.state, "live_worker_error", None)
        if worker_error and worker_error not in provider_errors:
            provider_errors.append(worker_error)
        return {
            "database": "AVAILABLE",
            "database_path": app.state.store.database_path,
            "paper_journal": getattr(app.state, "paper_journal_health", {"status": "UNAVAILABLE"}),
            "intelligence_worker": getattr(app.state, "intelligence_health", {"status": "UNAVAILABLE"}),
            "macro_calendar_worker": getattr(app.state, "macro_calendar_health", {"status": "UNAVAILABLE"}),
            "whale_worker": getattr(app.state, "whale_health", {"status": "UNAVAILABLE"}),
            "notification_worker": getattr(app.state, "notification_health", {"status": "UNAVAILABLE"}),
            "universe_worker": getattr(app.state, "universe_health", {"status": "UNAVAILABLE"}),
            "feed_health": health,
            "feed_fresh": fresh,
            "feed_age_seconds": age,
            "fast_assets": len(states),
            "fresh_fast_assets": len(fresh_states),
            "runtime": runtime,
            "live_worker_running": runtime["fast_running"],
            "discovery_runtime": discovery_runtime,
            "broad_running": discovery_runtime["running"],
            "broad_counts": latest.counts if latest else {},
            "broad_report_age_seconds": _age_seconds(latest.completed_at, now) if latest else None,
            "broad_freshness_counts": dict(
                Counter(observation.freshness.value for observation in latest.observations)
            )
            if latest
            else {},
            "broad_completed_at": latest.completed_at if latest else None,
            "cycle_macro": context_at(app.state.store, now),
            "mode": "OBSERVATION",
            "provider_errors": provider_errors,
            "metrics": metrics,
            "live_trading": False,
            "ready": (fresh and bool(fresh_states) and runtime["fast_running"]
                      and metrics.get("persistence", {}).get("status", "AVAILABLE") == "AVAILABLE"),
        }

    @app.get("/ready", dependencies=[Depends(authorize)])
    def ready():
        if getattr(app.state, "trade_alert_mode", False):
            from fastapi.responses import JSONResponse

            worker = getattr(app.state, "trade_alert_worker", None)
            report = worker.snapshot() if worker else {}
            task = getattr(app.state, "trade_alert_task", None)
            counts = report.get("counts", {})
            if not (task and not task.done() and report.get("running") and counts.get("ready", 0)
                    and not counts.get("persistence_failed", 0)):
                return JSONResponse({"status": "NOT_READY", "counts": counts,
                                     "reason": "Running scanner, fresh critical candles and working persistence required"},
                                    status_code=503)
            return {"status": "READY", "counts": counts, "scope": "Measured fresh assets; not configured universe size"}
        report = live_health()
        if not report["ready"]:
            from fastapi.responses import JSONResponse

            return JSONResponse(
                {
                    "status": "NOT_READY",
                    "reason": "running worker, fresh feed and fresh fast-watch evaluation required",
                },
                status_code=503,
            )
        return {"status": "READY"}

    @app.get("/api/live/updates/{symbol}", dependencies=[Depends(authorize)])
    async def live_updates(symbol: str, timeframe: str = "1m", analysis_after: datetime | None = None):
        """Native incremental chart updates; no history download or SQLite read.

        Initial history remains on the existing asset endpoint. Each subsequent
        update contains at most two finalized bars for the selected timeframe.
        The receipt time is the source receipt, never the HTTP response time.
        """
        from .live_analysis import order_book_analysis

        if timeframe not in {"1m", "5m", "15m", "1h", "4h", "1D"}:
            raise HTTPException(422, "Unsupported chart timeframe")
        now = utcnow()
        if analysis_after is not None and (analysis_after.tzinfo is None or analysis_after > now):
            raise HTTPException(422, "Analysis cursor must be an available aware timestamp")
        symbol = symbol.upper()
        worker = getattr(app.state, "live_worker", None)
        task = getattr(app.state, "live_task", None)
        if (worker is None or task is None or task.done() or worker.stop.is_set()
                or symbol not in worker.assets):
            return {"incremental": True, "monitoring": "UNAVAILABLE", "status": "UNAVAILABLE",
                    "live": None, "fast": [], "age_seconds": None,
                    "reason": "No active public-data monitoring for this pair"}
        live = worker.state.snapshot(symbol, now, include_history=False)
        live["asset_id"] = worker.assets[symbol].asset_id
        live["received_timestamp"] = live.get("last_received_timestamp")
        live["quote_asset"] = worker.assets[symbol].quote_asset
        live["large_trade_flow"] = worker.large_trade_tracker.snapshot(
            symbol, now, quote_currency=worker.assets[symbol].quote_asset)
        from .market_context import session_context
        live["session_context"] = session_context(now)
        live["closed_candles"] = {timeframe: [bar.model_dump(mode="json") for bar in
            worker.state.candles(symbol, timeframe, now, limit=2)]}
        live["open_candles"] = {frame: bar for frame, bar in live.get("open_candles", {}).items()
                                if frame == timeframe}
        analysis = worker._analysis.get(symbol, {})
        analysis_at = analysis.get("as_of")
        analysis_age = _age_seconds(analysis_at, now)
        available = analysis_age is not None and analysis_age >= 0
        live["analysis_at"] = analysis_at if available else None
        live["analysis_quality"] = analysis.get("data_quality", {}) if available else {}
        if available and (analysis_after is None or datetime.fromisoformat(analysis_at) > analysis_after):
            for key in ("indicators", "overlays", "volume_profiles", "weekly_vwap", "fibonacci",
                        "active_fibonacci", "sessions", "scores", "score_levels", "transport_provenance",
                        "war_room", "relative_context", "issues", "market_regime", "cycle_context",
                        "macro_context", "context_available_at", "score_bias"):
                if key in analysis:
                    live[key] = analysis[key]
        atr = analysis.get("indicators", {}).get("5m", {}).get("metrics", {}).get("atr14") if available else None
        book = order_book_analysis(worker.state.latest_event(symbol, "DEPTH"), now,
                                   price=live.get("price"), atr=atr,
                                   prior_levels=worker._book_levels.get(symbol, []))
        structural = [level for level in analysis.get("liquidity_map", [])
                      if level.get("category") != "OBSERVED_ORDER_BOOK"] if available else []
        price = live.get("price")
        structural.sort(key=lambda level: abs(level.get("price", price) - price)
                        if price and level.get("price") is not None else float("inf"))
        live["order_book"] = book
        live["liquidity_map"] = structural[:16] + book["levels"]
        live["liquidity_map_scope"] = "NEAREST_16_STRUCTURAL_PLUS_OBSERVED_TOP_20"
        fast = worker.monitor.states.get(worker.assets[symbol].asset_id)
        status, age = _live_status(live, now)
        return {"incremental": True, "monitoring": "ACTIVE", "status": status,
                "age_seconds": age, "live": live,
                "fast": [fast.model_dump(mode="json", exclude={"candles", "dedupe_keys"})] if fast else []}

    @app.get("/api/live/assets/{symbol}", dependencies=[Depends(authorize)])
    def live_asset(symbol: str):
        symbol = symbol.upper()
        record = app.state.store.get_state("live_asset", symbol)
        if not record:
            raise HTTPException(404, "No observed live state; start automatic discovery and wait for fast-watch promotion")
        fast = app.state.store.get_state("fast_state", record.get("asset_id"))
        status, age = _live_status(record, utcnow())
        return {
            "status": status,
            "age_seconds": age,
            "live": record,
            "fast": [fast] if fast else [],
        }

    @app.post("/api/live/chart-interest/{symbol}", dependencies=[Depends(authorize), Depends(local_or_authenticated)])
    def chart_interest(symbol: str):
        """Temporary public-data monitoring; viewing a chart never boosts rank."""
        symbol = symbol.strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{5,24}", symbol):
            raise HTTPException(422, "Use a supported public exchange pair")
        universe = getattr(app.state, "universe_worker", None)
        asset = universe.asset(symbol) if universe is not None else None
        if asset is None:
            latest = app.state.store.latest_summary()
            asset = next((o.asset for o in latest.observations if o.asset.exchange_symbol == symbol), None) if latest else None
        if asset is None:
            raise HTTPException(404, "Pair is not mapped in the discovered public-data universe")
        # Timestamp and commit order must be serialized together. Otherwise a
        # slow renewal can be overtaken and try to move runtime state backwards.
        # Native pair lookup remains outside this existing reentrant store lock.
        with app.state.store._archive_lock:
            now = utcnow()
            interest = {"asset_id": asset.asset_id, "symbol": symbol, "asset": asset.model_dump(mode="json"),
                        "created_at": now.isoformat(), "expires_at": (now + timedelta(seconds=900)).isoformat(),
                        "score_bonus": 0, "purpose": "TEMPORARY_CHART_OBSERVATION"}
            app.state.store.put_state("chart_interest", asset.asset_id, now, interest)
        return {"status": "MONITORING_REQUESTED", "interest": interest,
                "data_status": "PENDING_NATIVE_DATA", "monitoring_priority_is_score_boost": False}

    def universe_snapshot():
        worker = getattr(app.state, "universe_worker", None)
        if worker is not None:
            return worker.snapshot()
        return {"status": "UNAVAILABLE", "reason": "Live universe worker has not started", "assets": [],
                "counts": {"target": app.state.settings.top_n, "live": 0}, "as_of": utcnow().isoformat(),
                "scan_scope": "LIGHTWEIGHT_PUBLIC_PRICE_DISCOVERY", "live_trading": False}

    @app.get("/api/live/universe", dependencies=[Depends(authorize)])
    async def live_universe():
        # The native cache has one async owner. A threadpool read could race a
        # quote update and drop a pair whose receipt is after its captured time.
        return universe_snapshot()

    @app.get("/api/live/universe/stream", dependencies=[Depends(authorize)])
    async def universe_stream(request: Request):
        import json

        from fastapi.responses import StreamingResponse

        async def frames():
            while not await request.is_disconnected():
                yield "event: universe\ndata: " + json.dumps(universe_snapshot(), separators=(",", ":"), allow_nan=False) + "\n\n"
                await asyncio.sleep(1)
        return StreamingResponse(frames(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/whales", dependencies=[Depends(authorize)])
    def whales(asset_id: str | None = None, before: datetime | None = None):
        from .whale_flow import WhaleSettings
        from .whale_runtime import whale_asset_context, whale_events
        when = cutoff(before)
        settings = getattr(app.state, "whale_settings", None)
        if settings is None:
            try:
                settings = WhaleSettings.from_environment()
            except (ValueError, TypeError):
                return {"status": "UNAVAILABLE", "reason": "INVALID_WHALE_CONFIGURATION", "transfers": [], "production_weight": 0}
        if asset_id:
            return whale_asset_context(app.state.store, asset_id, when, settings)
        return {"status": getattr(app.state, "whale_health", {}).get("status", "UNAVAILABLE"),
                "provider": getattr(app.state, "whale_health", {"status": "UNAVAILABLE"}),
                "transfers": whale_events(app.state.store, as_of=when), "production_weight": 0,
                "warning": "Observed token transfers do not prove beneficial ownership, purpose, or a market trade."}

    @app.get("/api/live/alerts", dependencies=[Depends(authorize)])
    def alerts(limit: int = Query(default=100, ge=1, le=1000)):
        return {"alerts": app.state.store.objects("fast_event", limit=limit)}
