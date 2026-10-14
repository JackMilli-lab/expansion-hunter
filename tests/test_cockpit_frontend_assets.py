"""Public interface assets do not grant access to intelligence observations."""
import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from expansion_hunter.intelligence_api import install_routes

ROOT = Path(__file__).parents[1]


@pytest.mark.asyncio
async def test_only_allowlisted_interface_assets_are_public():
    app = FastAPI()

    async def private_access():
        raise HTTPException(401, "Authentication required")

    install_routes(app, private_access)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://app") as client:
        for name in ["chart-engine.js", "candidate-ui.js", "manifest.webmanifest", "sw.js", "icon.svg"]:
            response = await client.get("/cockpit-assets/" + name)
            assert response.status_code == 200
            assert response.content
            assert response.headers["cache-control"] == "no-cache"
        assert (await client.get("/cockpit-assets/.env")).status_code == 404
        assert (await client.get("/cockpit-assets/intelligence_api.py")).status_code == 404
        assert (await client.get("/api/intelligence/cycle")).status_code == 401
        assert (await client.get("/cockpit-assets/sw.js")).headers["service-worker-allowed"] == "/"


def test_chart_modules_and_manifest_are_wired_into_actual_cockpit():
    static = ROOT / "src/expansion_hunter/static"
    html = (static / "cockpit.html").read_text()
    assert "/cockpit-assets/chart-engine.js" in html
    assert "/cockpit-assets/candidate-ui.js" in html
    assert "S.ui=window.ExpansionHunterUI.mount" in html
    assert "S.nativeChart=module.render" in html
    assert "S.ui.shouldNotify(a)" in html
    manifest = json.loads((static / "manifest.webmanifest").read_text())
    assert manifest["start_url"] == "/cockpit" and manifest["display"] == "standalone"
    assert manifest["theme_color"] == "#0B0D12"


def test_service_worker_never_intercepts_market_or_private_api_data(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for executable service-worker contract")
    source = (ROOT / "src/expansion_hunter/static/sw.js").read_text()
    harness = r"""
const vm=require('node:vm'),assert=require('node:assert/strict'),handlers={};
const context={URL,Request,Response,Set,Promise,self:{location:{origin:'https://app'},
  addEventListener:(type,fn)=>handlers[type]=fn,skipWaiting(){},clients:{claim(){}}},
  caches:{keys:async()=>[],open:async()=>({addAll:async()=>{},put:async()=>{}}),match:async()=>undefined},
  fetch:async()=>new Response('actual interface shell')};
vm.runInNewContext(SOURCE,context);
for(const path of ['/api/market/status','/api/live/alerts','/api/trade-alert/assets/BTCUSDT','/api/live/universe/stream']){
 let intercepted=false;handlers.fetch({request:new Request('https://app'+path),respondWith(){intercepted=true}});
 assert.equal(intercepted,false);
}
let intercepted=false;
handlers.fetch({request:new Request('https://app/cockpit'),respondWith(p){intercepted=true;p.catch(()=>{})},waitUntil(){}});
assert.equal(intercepted,true);
"""
    script = tmp_path / "service-worker-contract.js"
    script.write_text(harness.replace("SOURCE", json.dumps(source)))
    result = subprocess.run([node, str(script)], text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
