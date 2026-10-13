"""Read-only UI/API integration; fixtures stay inside these tests."""
import json
import shutil
import subprocess
from datetime import UTC, datetime
from importlib.resources import files
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Header, HTTPException

from expansion_hunter.intelligence_store import IntelligenceStore
from expansion_hunter.trade_alert_api import install_trade_alert_routes


@pytest.fixture
def delivery_app(tmp_path):
    app = FastAPI()
    app.state.store = IntelligenceStore(str(tmp_path / "pre-ignition.sqlite3"))

    async def authorize(authorization: str | None = Header(default=None)):
        if authorization != "Bearer TEST_ACCESS":
            raise HTTPException(401, "Valid bearer token required")

    install_trade_alert_routes(app, authorize)
    yield app
    app.state.store.close()


async def test_new_read_only_routes_require_authorization(delivery_app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=delivery_app),
                                base_url="http://test") as client:
        for path in ("/api/trade-alert/pre-ignition", "/api/trade-alert/early-detection"):
            assert (await client.get(path)).status_code == 401


async def test_missing_worker_does_not_create_live_observations(delivery_app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=delivery_app),
                                base_url="http://test",
                                headers={"Authorization": "Bearer TEST_ACCESS"}) as client:
        result = (await client.get("/api/trade-alert/pre-ignition")).json()
    assert result["status"] == "UNAVAILABLE"
    assert result["running"] is False
    assert result["rows"] == []
    assert result["as_of"] is None
    assert result["coverage"] == {}
    assert "unknown live" in result["warning"]


async def test_pre_ignition_reads_cached_snapshot_without_analysis(delivery_app):
    calls = []
    rows = [{"symbol": "TESTUSDT", "bias": bias, "state": "READY", "status": "AVAILABLE",
             "pre_ignition_score": 70, "move_consumed_pct": None,
             "move_consumed_estimate_pct": None, "move_remaining": "UNKNOWN"}
            for bias in ("LONG", "SHORT")]

    def snapshot():
        calls.append("snapshot")
        return {"status": "PARTIAL", "running": True, "as_of": "2026-10-10T12:00:00+00:00",
                "pre_ignition": rows, "counts": {"selected": 500},
                "coverage": {"configured": 500, "live": 2}}

    delivery_app.state.trade_alert_worker = SimpleNamespace(snapshot=snapshot)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=delivery_app),
                                base_url="http://test",
                                headers={"Authorization": "Bearer TEST_ACCESS"}) as client:
        result = (await client.get("/api/trade-alert/pre-ignition")).json()
    assert result["rows"] == rows
    assert result["coverage"] == {"configured": 500, "live": 2}
    assert result["status"] == "PARTIAL"
    assert result["calibration"] == "UNVALIDATED_RESEARCH_SCORE"
    assert calls == ["snapshot"]


async def test_early_detection_uses_real_cached_report_only(delivery_app, monkeypatch):
    # A worker must calculate this before the GET; requests never create reports.
    from expansion_hunter import pre_ignition_research as research

    def no_calculations(*args, **kwargs):
        pytest.fail("GET must not calculate historical research")

    monkeypatch.setattr(research, "evaluate_cached_store", no_calculations)
    now = datetime.now(UTC)
    cached = {"status": "PARTIAL", "date": now.date().isoformat(), "as_of": now.isoformat(),
              "kpi": {"name": "TOP MOVERS DETECTED BEFORE 10% OF MOVE",
                      "detected_before_10_pct": 1, "eligible_monitored_moves": 2,
                      "rate": 0.5, "buckets": {"detected_lt_5_pct": 1,
                                               "missed_completely": 1}},
              "scope": "TEST_CACHE_ONLY", "profitability": {"status": "UNAVAILABLE"}}
    delivery_app.state.store.put_state("pre_ignition_research", "current", now, cached)
    delivery_app.state.store.put_state("pre_ignition_research", f"daily:{now.date().isoformat()}",
                                      now, cached)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=delivery_app),
                                base_url="http://test",
                                headers={"Authorization": "Bearer TEST_ACCESS"}) as client:
        for suffix in ("", f"?date={now.date().isoformat()}"):
            result = (await client.get("/api/trade-alert/early-detection" + suffix)).json()
            assert result["kpi"] == cached["kpi"]
            assert result["profitability"]["status"] == "UNAVAILABLE"
        for invalid in ("2026-99-01", "20261010", "2026-10-10T12:00:00Z"):
            assert (await client.get("/api/trade-alert/early-detection?date=" + invalid)).status_code == 422
    assert delivery_app.state.store.objects("pre_ignition_daily_report") == []


async def test_no_cached_kpi_has_no_invented_successes(delivery_app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=delivery_app),
                                base_url="http://test",
                                headers={"Authorization": "Bearer TEST_ACCESS"}) as client:
        result = (await client.get("/api/trade-alert/early-detection")).json()
    assert result["status"] == "UNAVAILABLE"
    assert result.get("kpi", {}).get("detected_before_10_pct") is None


def test_dashboard_executes_and_never_presents_future_consumption_as_live():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node unavailable for inline dashboard execution")
    path = str(files("expansion_hunter").joinpath("static/trade_alert.html"))
    rows = [{"symbol": "TESTUSDT", "bias": bias, "state": "READY", "status": "AVAILABLE",
             "score": 73, "early_detection_priority": 80, "score_velocity": 3,
             "score_acceleration": 1, "family_decomposition": {
                 "order_flow": {"normalized": None, "status": "UNAVAILABLE", "weight": 0.1}},
             "move_consumed_pct": 97, "move_consumed_estimate_pct": 8,
             "move_consumed_estimate_source": None, "move_consumed_estimate_sample_count": 0,
             "move_remaining": "UNKNOWN", "reason": "Test evidence"}
            for bias in ("LONG", "SHORT")]
    script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const observations=JSON.parse(fs.readFileSync(0,'utf8'));
const html=fs.readFileSync(process.argv[1],'utf8');
const elements={},handlers={};
const document={hidden:false,getElementById:id=>elements[id]||(elements[id]={value:'',innerHTML:'',textContent:''}),addEventListener:()=>{}};
const sandbox={document,window:{addEventListener:(name,fn)=>handlers[name]=fn},sessionStorage:{getItem:()=>null,setItem:()=>{}},AbortController,setTimeout,clearTimeout,setInterval:()=>{},console,alert:()=>{},fetch:async path=>({ok:true,json:async()=>path.endsWith('/early-detection')?{status:'UNAVAILABLE'}:observations})};
vm.runInNewContext(html.split('<script>')[1].split('</script>')[0],sandbox);
setImmediate(()=>{
  assert(elements['pre-ignition'].innerHTML.includes('LONG'));
  assert(elements['pre-ignition'].innerHTML.includes('SHORT'));
  assert(elements['pre-ignition'].innerHTML.includes('UNKNOWN live'));
  assert(!elements['pre-ignition'].innerHTML.includes('8% · ESTIMATED'));
  assert(!elements['pre-ignition'].innerHTML.includes('97%'));
  assert(elements['early-kpi'].innerHTML.includes('UNAVAILABLE'));
  assert(!elements['early-kpi'].innerHTML.includes('<strong>0</strong>'));
  handlers.offline();
  assert(!elements['pre-ignition'].innerHTML.includes('Test evidence'));
  assert(elements['pre-ignition'].innerHTML.includes('SCANNER OFFLINE / DATA GAP'));
  console.log('dashboard executed with symmetric rows and honest missing evidence');
});
"""
    result = subprocess.run([node, "-e", script, path],
                            input=json.dumps({"status": "PARTIAL", "running": True, "assets": [],
                                              "pre_ignition": rows}),
                            capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr
