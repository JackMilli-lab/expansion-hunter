"""Real ASGI routes and SQLite paper persistence, with no exchange calls."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from expansion_hunter.api import create_app
from expansion_hunter.config import Settings
from expansion_hunter.intelligence_store import IntelligenceStore
from expansion_hunter.macro_models import MacroMarketObservation
from expansion_hunter.paper_journal import PaperJournal

START = datetime(2025, 1, 1, tzinfo=UTC)


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.delenv("COCKPIT_API_TOKEN", raising=False)
    monkeypatch.setattr("expansion_hunter.journal_api.utcnow", lambda: START)
    app = create_app(Settings(db_path=str(tmp_path / "paper-api.sqlite3")))
    app.state.store = IntelligenceStore(str(tmp_path / "paper-api.sqlite3"))
    yield app
    app.state.store.close()


def payload(**changes):
    body = {"request_id": "synthetic-paper-only", "asset_id": "synthetic-test-asset", "symbol": "TESTUSDT",
            "quote_currency": "USDT", "setup": "COMPRESSION_BREAKOUT", "timeframe": "5m",
            "trade": {"mode": "TRADE", "side": "LONG", "entry": 100, "target": 110,
                      "stop": 95, "margin": 1000, "leverage": 2, "entry_fee_rate": 0.001,
                      "exit_fee_rate": 0.001, "funding_cost": 0}}
    body.update(changes)
    return body


async def request(app, method, path="/api/war-room/journal", *, client="127.0.0.1", **kwargs):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(client, 1234)),
                                 base_url="http://testserver") as session:
        return await session.request(method, path, **kwargs)


async def test_live_factory_registers_journal_and_legacy_routes_without_orders(app):
    routes = {route.path for route in app.routes if hasattr(route, "path")}
    assert "/api/war-room/journal" in routes
    assert "/api/war-room/calculate" in routes
    assert "/api/scans/latest" in routes
    assert (await request(app, "POST", "/api/orders", json={})).status_code == 404
    assert (await request(app, "POST", "/api/war-room/journal/execute", json={})).status_code in {404, 405}


async def test_open_read_idempotent_list_and_persistent_history(app):
    response = await request(app, "POST", json=payload())
    assert response.status_code == 200
    position = response.json()
    assert position["mode"] == "PAPER_ONLY"
    assert position["entry_source"] == "USER_DECLARED_NOT_EXECUTED"
    assert position["opened_at"] == START.isoformat().replace("+00:00", "Z")
    assert position["position_size"] == 20
    assert position["feature_snapshot"]["status"] == "UNAVAILABLE"
    repeat = await request(app, "POST", json=payload())
    assert repeat.json() == position
    listed = await request(app, "GET")
    assert len(listed.json()["positions"]) == 1
    assert listed.json()["live_orders"] is False
    fetched = await request(app, "GET", f"/api/war-room/journal/{position['position_id']}")
    assert fetched.json() == position
    history = await request(app, "GET", f"/api/war-room/journal/{position['position_id']}/history")
    assert history.json()["history"][0]["kind"] == "OPENED"


async def test_user_request_collision_does_not_rewrite_original(app):
    original = await request(app, "POST", json=payload())
    changed = payload()
    changed["trade"]["margin"] = 2000
    response = await request(app, "POST", json=changed)
    assert response.status_code == 409
    listed = await request(app, "GET")
    assert listed.json()["positions"] == [original.json()]


async def test_manual_short_close_cost_result_and_actual_saved_analytics(app):
    body = payload()
    body["trade"].update(side="SHORT", target=90, stop=105)
    position = (await request(app, "POST", json=body)).json()
    response = await request(app, "POST", f"/api/war-room/journal/{position['position_id']}/close",
                             json={"exit_price": 90, "reason": "Explicit paper close"})
    assert response.status_code == 200
    closed = response.json()
    assert closed["status"] == "CLOSED"
    assert closed["exit_source"] == "USER_DECLARED_NOT_EXECUTED"
    assert closed["financial_result"]["gross_pnl"] == 200
    assert closed["financial_result"]["net_pnl"] == pytest.approx(196.2)
    report = (await request(app, "GET", "/api/war-room/journal/analytics?initial_balance=1000")).json()
    assert report["sample_size"] == 1
    assert report["winrate"] == 1  # Exactly one declared paper result, not advertised as a live winrate.
    assert report["profit_factor"] is None
    assert report["groups"]["setup"]["COMPRESSION_BREAKOUT"]["sample_size"] == 1
    assert report["mode"] == "PAPER_ONLY"
    again = await request(app, "POST", f"/api/war-room/journal/{position['position_id']}/close",
                          json={"exit_price": 80, "reason": "Rewrite result"})
    assert again.status_code == 409


async def test_missing_funding_and_no_results_show_insufficient_data(app):
    empty = (await request(app, "GET", "/api/war-room/journal/analytics")).json()
    assert empty["status"] == "INSUFFICIENT_DATA"
    assert empty["winrate"] is None
    body = payload()
    del body["trade"]["funding_cost"]
    position = (await request(app, "POST", json=body)).json()
    response = await request(app, "POST", f"/api/war-room/journal/{position['position_id']}/close",
                             json={"exit_price": 110, "reason": "Explicit close without funding evidence"})
    assert response.json()["financial_result"]["net_pnl"] is None
    report = (await request(app, "GET", "/api/war-room/journal/analytics")).json()
    assert report["unknown_cost_results"] == 1
    assert report["winrate"] is None


async def test_actual_archived_interval_causes_background_modeled_close_and_api_result(app):
    opened = (await request(app, "POST", json=payload())).json()
    bar = MacroMarketObservation(asset_id="synthetic-test-asset", market_event_timestamp=START + timedelta(minutes=1),
        interval_start_timestamp=START, data_received_timestamp=START + timedelta(minutes=1),
        resolution_seconds=60, price=111, high=112, low=100, source="synthetic_test_only: journal integration")
    app.state.store.append("market_observation", "synthetic-bar-1", bar.market_event_timestamp,
                           bar.data_received_timestamp, bar, bar.asset_id)
    PaperJournal(app.state.store).evaluate(START + timedelta(minutes=1))
    result = (await request(app, "GET", f"/api/war-room/journal/{opened['position_id']}")).json()
    assert result["status"] == "CLOSED"
    assert result["exit_kind"] == "TARGET"
    assert result["exit_source"] == "PAPER_MODEL_LEVEL_FILL"
    assert result["excursion_status"] == "LOWER_BOUND_EXIT_BAR_ORDER_UNKNOWN"
    assert result["financial_result"]["net_pnl"] == pytest.approx(195.8)


async def test_portfolio_api_preserves_unknown_betas_correlations(app):
    await request(app, "POST", json=payload())
    portfolio = await request(app, "GET", "/api/war-room/journal/portfolio")
    assert portfolio.status_code == 200
    data = portfolio.json()
    assert data["total_exposure"] == 2000
    assert data["btc_adjusted_exposure"] is None
    assert data["data_quality"]["correlations"] == "UNAVAILABLE"


@pytest.mark.parametrize("changes", [
    {"private_api_key": "forbidden"}, {"opened_at": START.isoformat()},
    {"quote_currency": ""}, {"symbol": "<script>"}, {"timeframe": "2m"},
])
async def test_invalid_or_private_or_backdated_input_is_422(app, changes):
    response = await request(app, "POST", json=payload(**changes))
    assert response.status_code == 422
    assert not (await request(app, "GET")).json()["positions"]


@pytest.mark.parametrize("invalid", ["NaN", "Infinity", "-Infinity"])
async def test_nonfinite_body_returns_valid_json_validation_error(app, invalid):
    import json

    raw = json.dumps(payload()).replace('"entry": 100', '"entry": ' + invalid)
    response = await request(app, "POST", content=raw, headers={"content-type": "application/json"})
    assert response.status_code == 422
    assert response.json()["detail"]


async def test_remote_mutation_and_csrf_require_explicit_auth_configuration(app):
    remote = await request(app, "POST", json=payload(), client="203.0.113.5")
    assert remote.status_code == 403
    csrf = await request(app, "POST", json=payload(), headers={"origin": "https://outside.example"})
    assert csrf.status_code == 403
    assert not PaperJournal(app.state.store).list()


async def test_configured_bearer_is_enforced_on_reads_and_writes(tmp_path, monkeypatch):
    monkeypatch.setenv("COCKPIT_API_TOKEN", "synthetic-test-token")
    app = create_app(Settings(db_path=str(tmp_path / "bearer.sqlite3")))
    app.state.store = IntelligenceStore(str(tmp_path / "bearer.sqlite3"))
    try:
        assert (await request(app, "GET")).status_code == 401
        assert (await request(app, "POST", json=payload())).status_code == 401
        query_token = await request(app, "GET", "/api/war-room/journal?token=synthetic-test-token")
        assert query_token.status_code == 401
        allowed = await request(app, "POST", json=payload(), client="203.0.113.5",
                                headers={"authorization": "Bearer synthetic-test-token"})
        assert allowed.status_code == 200
        assert "synthetic-test-token" not in allowed.text
    finally:
        app.state.store.close()


async def test_unknown_position_and_naive_history_cutoff_rejected(app):
    assert (await request(app, "GET", "/api/war-room/journal/unknown")).status_code == 404
    assert (await request(app, "POST", "/api/war-room/journal/unknown/close",
                          json={"exit_price": 1, "reason": "Unknown trade"})).status_code == 404
    opened = (await request(app, "POST", json=payload())).json()
    assert (await request(app, "GET", f"/api/war-room/journal/{opened['position_id']}/history?as_of=2025-01-01T00:00:00")).status_code == 422


async def test_database_missing_is_explicit_503(tmp_path, monkeypatch):
    monkeypatch.delenv("COCKPIT_API_TOKEN", raising=False)
    app = create_app(Settings(db_path=str(tmp_path / "not-started.sqlite3")))
    response = await request(app, "GET")
    assert response.status_code == 503
    assert "database" in response.json()["detail"].lower()
