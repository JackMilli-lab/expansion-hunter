"""Observation API and cockpit with supervised automatic discovery.

Broad collection runs in a separate process; the independent fast worker uses
public market feeds. No endpoint routes exchange orders. Bind locally by default;
protect remote access with COCKPIT_API_TOKEN and a TLS reverse proxy.
"""

from __future__ import annotations

import asyncio
import hmac
import os
from collections import Counter
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query

from .config import Settings
from .discovery_models import DiscoveryCycle, Freshness
from .intelligence_store import IntelligenceStore
from .models import ScanBatch


def create_app(settings: Settings | None = None, *, discovery_service_factory=None, trade_alert_settings=None,
               pre_ignition_settings=None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app):

        app.state.settings = settings
        app.state.store = IntelligenceStore(settings.db_path)

        def start_signal_workers(config, *, notifications):
            from .notification_runtime import run_notification_worker
            from .pre_ignition_research import run_pre_ignition_research_worker
            from .pre_move_runtime import run_pre_move_deep_worker
            from .trade_alert_research import run_trade_alert_research_worker
            from .trade_alert_runtime import run_trade_alert_worker

            workers = {
                "trade_alert_task": run_trade_alert_worker(app, config),
                "trade_alert_research_task": run_trade_alert_research_worker(app, config),
                "pre_move_deep_task": run_pre_move_deep_worker(app, config),
                "pre_ignition_research_task": run_pre_ignition_research_worker(app, config),
            }
            if notifications:
                workers["notification_task"] = run_notification_worker(app)
            tasks = []
            for name, coroutine in workers.items():
                task = asyncio.create_task(coroutine, name=name)
                setattr(app.state, name, task)
                tasks.append(task)
            return tasks

        if trade_alert_settings is not None:
            signal_tasks = start_signal_workers(trade_alert_settings, notifications=True)
            try:
                yield
            finally:
                for task in signal_tasks:
                    task.cancel()
                await asyncio.gather(*signal_tasks, return_exceptions=True)
                app.state.store.close()
            return
        app.state.live_requested = False
        app.state.paper_journal_health = {"status": "STARTING", "mode": "PAPER_ONLY"}
        from .intelligence_runtime import run_intelligence_worker, run_macro_calendar_worker
        from .journal_runtime import run_journal_worker
        from .notification_runtime import run_notification_worker
        from .universe_live import run_universe_worker
        from .whale_runtime import run_whale_worker

        app.state.paper_journal_task = asyncio.create_task(run_journal_worker(app))
        app.state.intelligence_task = asyncio.create_task(run_intelligence_worker(app))
        app.state.macro_calendar_task = asyncio.create_task(run_macro_calendar_worker(app))
        app.state.whale_task = asyncio.create_task(run_whale_worker(app))
        app.state.notification_task = asyncio.create_task(run_notification_worker(app))
        app.state.universe_task = asyncio.create_task(run_universe_worker(app))
        signal_tasks = start_signal_workers(pre_ignition_settings, notifications=False) if pre_ignition_settings is not None else []
        try:
            factory = discovery_service_factory
            if factory is None:
                from .app_discovery import DiscoveryService

                factory = DiscoveryService
            app.state.discovery_service = factory(settings)
            from .intelligence_api import resume_automatic_observation

            try:
                await resume_automatic_observation(app)
            except Exception as exc:
                from .models import utcnow

                app.state.store.put_state("provider_error", "restart", utcnow(),
                    {"status": "FAILED", "error": f"{type(exc).__name__}: {exc}"})
            yield
        finally:
            from .intelligence_api import stop_application_workers

            await stop_application_workers(app)
            background = (app.state.paper_journal_task, app.state.intelligence_task, app.state.macro_calendar_task,
                          app.state.whale_task, app.state.notification_task, app.state.universe_task, *signal_tasks)
            for task in background:
                task.cancel()
            await asyncio.gather(*background, return_exceptions=True)
            app.state.store.close()

    token = os.environ.get("COCKPIT_API_TOKEN")

    async def authorize(authorization: str | None = Header(default=None)):
        if token and (authorization is None or not hmac.compare_digest(authorization, "Bearer " + token)):
            raise HTTPException(401, "Valid bearer token required")

    app = FastAPI(
        title="Trading Cockpit — Expansion Hunter",
        version="0.1.0",
        lifespan=lifespan,
        description="Causal scanner reports. Scores are rule evidence, not calibrated probabilities.",
    )
    app.state.trade_alert_mode = trade_alert_settings is not None
    app.state.pre_ignition_enabled = trade_alert_settings is not None or pre_ignition_settings is not None

    @app.middleware("http")
    async def report_database(request, call_next):
        response = await call_next(request)
        response.headers["X-Cockpit-Database"] = str(Path(settings.db_path).resolve())
        return response

    @app.get("/health")
    def health():
        pre_worker = getattr(app.state, "trade_alert_worker", None)
        signal_status = pre_worker.snapshot() if pre_worker else {"status": "STARTING", "running": False}
        signal_health = {key: signal_status[key] for key in
                         ("status", "running", "as_of", "counts", "coverage", "connections", "error")
                         if key in signal_status}
        if trade_alert_settings is not None:
            snapshot = signal_status
            return {"service": "trade-alert", "mode": "SIGNAL_ONLY", "live_trading": False,
                    "database": str(Path(settings.db_path).resolve()),
                    "status": snapshot["status"], "running": snapshot.get("running", False),
                    "counts": snapshot.get("counts", {}), "connections": snapshot.get("connections", []),
                    "error": snapshot.get("error"),
                    "research": getattr(app.state, "trade_alert_research_health", {"status": "STARTING"}),
                    "early_detection_research": getattr(app.state, "pre_ignition_research_health", {"status": "STARTING"}),
                    "notifications": getattr(app.state, "notification_health", {"status": "STARTING"})}
        # Sync routes run in FastAPI's worker pool, outside market ingestion.
        # Publish operational evidence without exposing private state histories.
        if not hasattr(app.state, "store"):
            return {"status": "ok", "service": "expansion-hunter", "schema_version": 1,
                    "database": str(Path(settings.db_path).resolve()), "mode": "OBSERVATION",
                    "live_trading": False, "database_available": False,
                    "feed_fresh": False, "feed_age_seconds": None,
                    "fast_monitor_active": False, "fresh_fast_assets": 0,
                    "broad_scanner_active": False, "broad_completed_at": None,
                    "ready": False}
        handler = next(route.endpoint for route in app.router.routes if getattr(route, "name", None) == "live_health")
        report = handler()
        return {"status": "ok", "service": "expansion-hunter", "schema_version": 1,
                "database": report["database"], "mode": "OBSERVATION", "live_trading": False,
                "feed_fresh": report["feed_fresh"], "feed_age_seconds": report["feed_age_seconds"],
                "fast_monitor_active": report["live_worker_running"],
                "fresh_fast_assets": report["fresh_fast_assets"],
                "broad_scanner_active": report["broad_running"],
                "broad_completed_at": report["broad_completed_at"], "ready": report["ready"],
                **({"pre_ignition": signal_health,
                    "early_detection_research": getattr(app.state, "pre_ignition_research_health", {"status": "STARTING"})}
                   if pre_ignition_settings is not None else {})}

    @app.get("/api/scans/latest", response_model=ScanBatch, dependencies=[Depends(authorize)])
    def latest():
        batch = app.state.store.latest()
        if batch is None:
            raise HTTPException(404, "No completed scan; run the scanner CLI")
        return batch

    @app.get("/api/scans/{scan_id}", response_model=ScanBatch, dependencies=[Depends(authorize)])
    def scan(scan_id: str):
        batch = app.state.store.get_batch(scan_id)
        if batch is None:
            raise HTTPException(404, "Completed scan not found")
        return batch

    @app.get("/api/status", dependencies=[Depends(authorize)])
    def status():
        return {"runs": app.state.store.run_status()}

    @app.get("/api/assets/{asset_id}", dependencies=[Depends(authorize)])
    def asset(asset_id: str):
        batch = app.state.store.latest()
        if batch:
            for report in batch.results:
                if report.asset.asset_id == asset_id:
                    return report
        raise HTTPException(404, "Asset not present in latest completed scan")

    @app.get("/api/discovery/latest", response_model=DiscoveryCycle, dependencies=[Depends(authorize)])
    def discovery_latest():
        cycle = app.state.store.latest_cycle()
        if cycle is None:
            raise HTTPException(404, "No discovery cycle; run the discover CLI command")
        return cycle

    @app.get(
        "/api/discovery/cycles/{cycle_id}", response_model=DiscoveryCycle, dependencies=[Depends(authorize)]
    )
    def discovery_cycle(cycle_id: str):
        cycle = app.state.store.get_cycle(cycle_id)
        if cycle is None:
            raise HTTPException(404, "Discovery cycle not found")
        return cycle

    @app.get("/api/discovery/assets/{asset_id}/history", dependencies=[Depends(authorize)])
    def discovery_asset_history(
        asset_id: str, limit: int = Query(default=100, ge=1, le=1000), before: datetime | None = None
    ):
        if before is not None and before.tzinfo is None:
            raise HTTPException(422, "before must include a UTC offset")
        observations = app.state.store.asset_history(asset_id, before=before, limit=limit)
        return {
            "database_path": app.state.store.database_path,
            "asset_id": asset_id,
            "observations": observations,
        }

    @app.get("/api/discovery/events", dependencies=[Depends(authorize)])
    def discovery_events(
        asset_id: str | None = None,
        limit: int = Query(default=100, ge=1, le=1000),
        before: datetime | None = None,
    ):
        if before is not None and before.tzinfo is None:
            raise HTTPException(422, "before must include a UTC offset")
        return {
            "database_path": app.state.store.database_path,
            "events": app.state.store.events(asset_id=asset_id, limit=limit, before=before),
        }

    @app.get("/api/discovery/status", dependencies=[Depends(authorize)])
    def discovery_status():
        cycle = app.state.store.latest_summary()
        status = {
            "database_path": app.state.store.database_path,
            "live_trading_enabled": False,
            "latest_cycle_id": cycle.cycle_id if cycle else None,
            "status": cycle.status if cycle else "NO_DISCOVERY_CYCLE",
            "counts": cycle.counts if cycle else {},
            "provider_issues": cycle.provider_issues if cycle else [],
            "freshness_as_recorded": {},
            "critical_data_unavailable_as_recorded": 0,
            "report_age_seconds": None,
        }
        if cycle:
            status["freshness_as_recorded"] = dict(
                Counter(observation.freshness.value for observation in cycle.observations)
            )
            status["critical_data_unavailable_as_recorded"] = sum(
                1
                for observation in cycle.observations
                for feature in observation.features.values()
                if feature.critical and feature.freshness in {Freshness.STALE, Freshness.MISSING}
            )
            status["report_age_seconds"] = max(0, (datetime.now(UTC) - cycle.completed_at).total_seconds())
        return status

    @app.get("/api/discovery/lists", dependencies=[Depends(authorize)])
    def discovery_lists(limit: int = Query(default=100, ge=1, le=1000)):
        cycle = app.state.store.latest_summary()
        if cycle is None:
            raise HTTPException(404, "No discovery cycle; ranking lists are unavailable")
        return {
            "database_path": app.state.store.database_path,
            "cycle_id": cycle.cycle_id,
            "as_of": cycle.as_of,
            "lists": {name: values[:limit] for name, values in cycle.lists.items()},
            "calibration": "UNCALIBRATED",
        }

    from .intelligence_api import install_routes

    install_routes(app, authorize)
    from .calculator_api import install_routes as install_calculator_routes

    install_calculator_routes(app, authorize)
    from .journal_api import install_routes as install_journal_routes

    install_journal_routes(app, authorize)
    from .trade_alert_api import install_trade_alert_routes
    install_trade_alert_routes(app, authorize)
    return app
