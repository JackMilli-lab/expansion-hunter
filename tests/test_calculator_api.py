"""Manual calculator HTTP tests: no market feed or order execution fixtures."""
from __future__ import annotations

import httpx
import pytest

from expansion_hunter.api import create_app
from expansion_hunter.config import Settings


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.delenv("COCKPIT_API_TOKEN", raising=False)
    return create_app(Settings(db_path=str(tmp_path / "unused-calculation.sqlite3")))


async def post(app, path="/api/war-room/calculate", *, client="127.0.0.1", headers=None, **kwargs):
    transport = httpx.ASGITransport(app=app, client=(client, 1234))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as request:
        return await request.post(path, headers=headers, **kwargs)


def trade(**changes):
    return dict(mode="TRADE", side="LONG", entry=100, target=110, stop=95, margin=1000,
                leverage=2, entry_fee_rate=0.001, exit_fee_rate=0.001, funding_cost=3, **changes)


async def test_trade_arithmetic_returns_fees_funding_stop_and_no_storage(app, tmp_path):
    response = await post(app, json=trade())
    assert response.status_code == 200
    result = response.json()
    assert result["position_size"] == 20
    assert result["notional"] == 2000
    assert result["gross_pnl"] == 200
    assert result["estimated_fees"] == 4.2
    assert result["net_pnl"] == pytest.approx(192.8)
    assert result["loss_at_stop"] == pytest.approx(106.9)
    assert result["estimated_liquidation_price"] is None
    assert result["liquidation_status"] == "UNAVAILABLE_MAINTENANCE_RATE_REQUIRED"
    assert not (tmp_path / "unused-calculation.sqlite3").exists()
    assert not hasattr(app.state, "store")  # Routes do not require a DB/feed lifespan.


async def test_short_trade_uses_directional_validation_and_signed_pnl(app):
    payload = trade()
    payload.update(side="SHORT", target=90, stop=105)
    response = await post(app, json=payload)
    assert response.status_code == 200
    result = response.json()
    assert result["gross_pnl"] == 200
    assert result["net_pnl"] == pytest.approx(193.2)


async def test_max_risk_and_target_profit_discriminated_modes(app):
    risk = await post(app, json={"mode": "MAX_RISK", "entry": 100, "stop": 95, "target": 110,
                                "account_size": 10000, "max_risk_amount": 100})
    assert risk.status_code == 200
    assert risk.json()["actual_risk_amount"] == pytest.approx(100)
    target = await post(app, json={"mode": "TARGET_PROFIT", "entry": 100, "target": 110,
                                  "desired_profit": 250, "stop": 95})
    assert target.status_code == 200
    assert target.json()["net_pnl"] == pytest.approx(250)


@pytest.mark.parametrize("changes", [
    {"entry": 0}, {"target": 90}, {"stop": 105}, {"leverage": 126},
    {"entry_fee_rate": -1}, {"mode": "ORDER"}, {"private_api_key": "not-accepted"},
])
async def test_invalid_manual_inputs_rejected(app, changes):
    payload = trade()
    payload.update(changes)
    response = await post(app, json=payload)
    assert response.status_code == 422
    assert response.json()["detail"]


@pytest.mark.parametrize("invalid", ["NaN", "Infinity", "-Infinity"])
async def test_nonfinite_json_values_return_valid_422_json(app, invalid):
    raw = '{"mode":"TRADE","entry":' + invalid + ',"target":110,"stop":95,"margin":1000}'
    response = await post(app, content=raw, headers={"content-type": "application/json"})
    assert response.status_code == 422
    assert response.json()["detail"]


async def test_finite_inputs_overflow_is_422_not_server_error(app):
    payload = trade()
    payload.update(entry=1e-300, stop=5e-301, target=1, margin=1e300)
    response = await post(app, json=payload)
    assert response.status_code == 422
    assert "non-finite" in response.json()["detail"]


async def test_missing_funding_is_unavailable_not_invented_zero(app):
    payload = trade()
    payload["funding_cost"] = None
    response = await post(app, json=payload)
    assert response.status_code == 200
    assert response.json()["net_pnl"] is None
    assert response.json()["net_status"] == "UNAVAILABLE"


async def test_portfolio_requires_supplied_data_and_preserves_partial_quality(app):
    response = await post(app, "/api/war-room/portfolio", json={"positions": [
        {"symbol": "BTCUSDT", "side": "LONG", "entry": 100, "quantity": 2},
        {"symbol": "ETHUSDT", "side": "SHORT", "entry": 10, "quantity": 5},
    ]})
    assert response.status_code == 200
    result = response.json()
    assert result["total_exposure"] == 250
    assert result["net_exposure"] == 150
    assert result["btc_adjusted_exposure"] is None
    assert result["total_risk_to_stop"] is None
    assert result["data_quality"]["correlations"] == "UNAVAILABLE"


async def test_portfolio_invalid_stop_and_unknown_correlation_rejected(app):
    invalid = await post(app, "/api/war-room/portfolio", json={"positions": [
        {"symbol": "X", "side": "LONG", "entry": 100, "quantity": 1, "stop": 105},
    ]})
    assert invalid.status_code == 422
    unknown = await post(app, "/api/war-room/portfolio", json={"positions": [], "correlations": [
        {"first_symbol": "X", "second_symbol": "Y", "correlation": 0.9},
    ]})
    assert unknown.status_code == 422


async def test_remote_control_and_cross_origin_rejected_without_token(app):
    remote = await post(app, client="203.0.113.5", json=trade())
    assert remote.status_code == 403
    csrf = await post(app, json=trade(), headers={"origin": "https://outside.example"})
    assert csrf.status_code == 403


async def test_bearer_auth_required_for_remote_and_not_accepted_in_query(tmp_path, monkeypatch):
    monkeypatch.setenv("COCKPIT_API_TOKEN", "synthetic-test-token")
    app = create_app(Settings(db_path=str(tmp_path / "unused.sqlite3")))
    missing = await post(app, client="203.0.113.5", json=trade())
    assert missing.status_code == 401
    query = await post(app, "/api/war-room/calculate?token=synthetic-test-token",
                       client="203.0.113.5", json=trade())
    assert query.status_code == 401
    allowed = await post(app, client="203.0.113.5", json=trade(),
                         headers={"authorization": "Bearer synthetic-test-token"})
    assert allowed.status_code == 200
    assert "synthetic-test-token" not in allowed.text


async def test_legacy_health_still_works_and_no_order_endpoint(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/health")
        assert response.status_code == 200
        assert response.json()["schema_version"] == 1
        assert response.json()["ready"] is False
        assert response.json()["database_available"] is False
        assert not hasattr(app.state, "store")
        assert (await client.post("/api/orders", json={})).status_code == 404
