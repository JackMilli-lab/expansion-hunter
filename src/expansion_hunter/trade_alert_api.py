"""Read-only Trade Alert dashboard/API. Test delivery requires explicit action."""
import os
from datetime import date as utc_date
from datetime import datetime
from importlib.resources import files

from fastapi import Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse


def install_trade_alert_routes(app, authorize):
    from .native_chart import NativeChartService

    charts = NativeChartService()
    app.state.native_chart_service = charts

    def public_market_enabled():
        return os.getenv("COCKPIT_PUBLIC_MARKET_DATA", "false").strip().lower() in {"true", "1", "yes"}

    async def market_read_access(authorization: str | None = Header(default=None)):
        if not public_market_enabled():
            await authorize(authorization)

    @app.get("/trade-alert", response_class=HTMLResponse)
    async def page():
        return files("expansion_hunter").joinpath("static/trade_alert.html").read_text()

    @app.get("/api/trade-alert/status", dependencies=[Depends(authorize)])
    async def status():
        worker = getattr(app.state, "trade_alert_worker", None)
        snapshot = worker.snapshot() if worker else {"status": "UNAVAILABLE", "running": False,
            "reason": "Start the existing application with the trade-alert command", "assets": [], "counts": {}}
        snapshot["research"] = getattr(app.state, "trade_alert_research_health", {"status": "UNAVAILABLE"})
        snapshot["notifications"] = getattr(app.state, "notification_health", {"status": "UNAVAILABLE"})
        snapshot["deep_analysis"] = getattr(app.state, "pre_move_deep_health", {"status": "UNAVAILABLE"})
        return snapshot

    @app.get("/api/trade-alert/pre-ignition", dependencies=[Depends(authorize)])
    async def pre_ignition():
        """Publish already evaluated rows; never run historical analysis here."""
        worker = getattr(app.state, "trade_alert_worker", None)
        snapshot = worker.snapshot() if worker else {}
        return {"status": snapshot.get("status", "UNAVAILABLE"),
                "running": snapshot.get("running", False),
                "as_of": snapshot.get("as_of"),
                "coverage": snapshot.get("coverage", {}),
                "counts": snapshot.get("counts", {}),
                "rows": snapshot.get("pre_ignition", []),
                "calibration": "UNVALIDATED_RESEARCH_SCORE",
                "warning": "Scores are not probabilities. Actual eventual move consumed is unknown live."}

    @app.get("/api/trade-alert/early-detection", dependencies=[Depends(authorize)])
    def early_detection(date: str | None = None):
        """Only read the research worker's cached report, outside ingestion."""
        if date is not None:
            try:
                if utc_date.fromisoformat(date).isoformat() != date:
                    raise ValueError("Use YYYY-MM-DD")
            except ValueError as exc:
                raise HTTPException(422, "date must be a valid UTC YYYY-MM-DD") from exc
        from .pre_ignition_research import cached_daily_report

        return cached_daily_report(app.state.store, date=date)

    @app.get("/api/trade-alert/assets/{symbol}", dependencies=[Depends(authorize)])
    async def asset(symbol: str, timeframe: str = "5m"):
        from .models import utcnow
        from .trade_alert_runtime import FRAMES

        worker = getattr(app.state, "trade_alert_worker", None)
        if timeframe not in FRAMES:
            raise HTTPException(422, "Unsupported timeframe")
        symbol = symbol.upper()
        if worker is None or symbol not in worker.assets:
            raise HTTPException(404, "Not a currently covered native market")
        now = utcnow()
        analysis = getattr(app.state, "pre_move_analysis", {}).get(symbol)
        bars = worker._history(symbol, timeframe, now)
        fibonacci = await charts.fibonacci(worker, symbol, timeframe, bars, now)
        return {"symbol": symbol, "as_of": now.isoformat(), "timeframe": timeframe,
                "market": worker._state(symbol).snapshot(symbol, now, include_history=False),
                "candles": [bar.model_dump(mode="json") for bar in bars],
                "pre_move": [row for row in worker.snapshot().get("pre_move", []) if row["symbol"] == symbol],
                "pre_ignition": [row for row in worker.snapshot().get("pre_ignition", [])
                                 if row["symbol"] == symbol],
                "analysis": analysis,
                "analysis_status": "DERIVED" if analysis else "UNAVAILABLE",
                **fibonacci,
                "warning": "Conditional paths are hypotheses, not predicted future candles"}

    @app.get("/api/market/status", dependencies=[Depends(market_read_access)])
    async def market_status():
        # Only current public exchange observations. Private archive, delivery,
        # watchlist and account state remain behind the original authorization.
        snapshot = await status()
        visible = {key: snapshot[key] for key in
                   ("status", "running", "as_of", "mode", "live_trading", "counts", "coverage", "connections")
                   if key in snapshot}
        asset_keys = ("symbol", "asset_id", "price", "received_at", "price_evidence", "tick_size",
                      "history_counts", "data_quality", "status", "pre_ignition")
        visible["assets"] = [{key: row[key] for key in asset_keys if key in row}
                             for row in snapshot.get("assets", [])]
        visible["public_market_data"] = public_market_enabled()
        return visible

    @app.get("/api/market/assets/{symbol}", dependencies=[Depends(market_read_access)])
    async def market_asset(symbol: str, timeframe: str = "5m"):
        # Reuse native rolling candles and a cached own-timeframe projection.
        snapshot = await asset(symbol, timeframe)
        visible = {key: snapshot[key] for key in
                   ("symbol", "as_of", "timeframe", "market", "candles", "analysis", "analysis_status",
                    "active_fibonacci", "fibonacci_as_of", "warning")}
        visible["public_market_data"] = public_market_enabled()
        return visible

    async def native_history(symbol: str, timeframe: str, before: datetime | None, limit: int):
        from .models import aware, utcnow
        from .trade_alert_runtime import FRAMES

        if timeframe not in FRAMES:
            raise HTTPException(422, "Unsupported timeframe")
        if not 1 <= limit <= 1000:
            raise HTTPException(422, "limit must be between 1 and 1000")
        if before is not None:
            try:
                before = aware(before)
            except ValueError as exc:
                raise HTTPException(422, "before must be an ISO timestamp with a timezone") from exc
        symbol = symbol.upper()
        worker = getattr(app.state, "trade_alert_worker", None)
        if worker is None or symbol not in worker.assets:
            raise HTTPException(404, "Not a currently covered native market")
        payload, code = await charts.history(worker, symbol, timeframe, before, limit, utcnow())
        return JSONResponse(payload, status_code=code)

    @app.get("/api/trade-alert/assets/{symbol}/history", dependencies=[Depends(authorize)])
    async def asset_history(symbol: str, timeframe: str = "5m", before: datetime | None = None, limit: int = 500):
        return await native_history(symbol, timeframe, before, limit)

    @app.get("/api/market/assets/{symbol}/history", dependencies=[Depends(market_read_access)])
    async def market_asset_history(symbol: str, timeframe: str = "5m", before: datetime | None = None, limit: int = 500):
        return await native_history(symbol, timeframe, before, limit)

    @app.get("/api/trade-alert/history", dependencies=[Depends(authorize)])
    def history(limit: int = 100):
        if not 1 <= limit <= 1000:
            raise HTTPException(422, "limit must be between 1 and 1000")
        return {"alerts": app.state.store.objects("trade_alert", limit=limit),
                "outcomes": app.state.store.objects("trade_alert_outcome", limit=limit),
                "misses": app.state.store.objects("trade_alert_miss", limit=limit),
                "scope": "Immutable observations; future outcomes are separate research-only objects"}

    @app.post("/api/trade-alert/telegram/test", dependencies=[Depends(authorize)])
    async def test_telegram(request: Request):
        if not os.getenv("COCKPIT_API_TOKEN") and request.client and request.client.host not in {
            "127.0.0.1", "localhost", "::1", "testclient"}:
            raise HTTPException(403, "Configure remote dashboard authentication first")
        import httpx

        from .notification_runtime import TelegramSettings, send_telegram_test
        settings = TelegramSettings.from_env()
        async with httpx.AsyncClient(timeout=settings.timeout_seconds) as client:
            return await send_telegram_test(client, settings)
