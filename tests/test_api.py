"""Read-only operator API invariants, with no network or invented market reports."""

import httpx
import pytest

from expansion_hunter.api import create_app
from expansion_hunter.config import Settings


@pytest.mark.asyncio
async def test_empty_store_is_explicit_and_api_has_no_mutating_routes(tmp_path, monkeypatch):
    monkeypatch.delenv("COCKPIT_API_TOKEN", raising=False)
    app = create_app(Settings(db_path=str(tmp_path / "api.sqlite3")))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await client.get("/health")).json()["status"] == "ok"
            assert (await client.get("/api/scans/latest")).status_code == 404
            assert (await client.get("/api/assets/bitcoin")).status_code == 404
            assert (await client.get("/api/scans/unknown")).status_code == 404
            assert (await client.get("/api/status")).json() == {"runs": []}
            assert (await client.post("/api/scans/latest")).status_code == 405


@pytest.mark.asyncio
async def test_optional_token_protects_reports_without_echoing_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("COCKPIT_API_TOKEN", "synthetic-test-token")
    app = create_app(Settings(db_path=str(tmp_path / "api.sqlite3")))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            unauthorized = await client.get("/api/status")
            assert unauthorized.status_code == 401
            assert "synthetic-test-token" not in unauthorized.text
            assert (
                await client.get("/api/status", headers={"Authorization": "Bearer incorrect"})
            ).status_code == 401
            assert (
                await client.get("/api/status", headers={"Authorization": "Bearer synthetic-test-token"})
            ).status_code == 200
