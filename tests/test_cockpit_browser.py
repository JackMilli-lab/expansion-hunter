"""Executable cockpit JavaScript contract tests, with explicit API fixtures.

These tests exercise the real application script in a DOM harness. They do not
claim browser rendering or live exchange connectivity; real Chromium/public-feed
smoke evidence is recorded separately. Node is optional for Python-only installs.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

COCKPIT = Path(__file__).parents[1] / "src/expansion_hunter/static/cockpit.html"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node required for executable UI contract tests")


def run_cockpit(tmp_path, overview, health, live=None, *, desktop_token=None):
    script = COCKPIT.read_text().split("<script>", 1)[1].split("</script>", 1)[0]
    routes = {
        "/api/i18n/en": json.loads(COCKPIT.with_name("i18n.en.json").read_text()),
        "/api/cockpit/overview": overview,
        "/api/live/health": health,
        "/api/live/alerts?limit=100": {"alerts": []},
        "/api/intelligence/cycle": {"status": "UNAVAILABLE", "cycle": None},
        "/api/intelligence/macro/calendar?days=7": {"status": "UNAVAILABLE", "events": []},
    }
    if live is not None:
        routes["/api/live/assets/BTCUSDT"] = live
    harness = r"""
const vm = require('node:vm');
const elements = new Map();
const handlers = new Map();
const windowHandlers = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {
    textContent: '', innerHTML: '', className: '', dataset: {}, style: {},
    value: id === 'discovery-filter' ? 'all' : '', clientWidth: 700, clientHeight: 350,
    classList: { add(){}, remove(){}, toggle(){} },
    addEventListener(type, callback){ handlers.set(id+':'+type, callback); },
    setAttribute(){}, append(){}, remove(){}, focus(){}, showModal(){}, close(){},
    querySelector(){ return element('query-child'); }, scrollIntoView(){},
  });
  return elements.get(id);
}
const context = {
  console, Date, Math, String, Number, Set, Map, Infinity,
  AbortController, encodeURIComponent,
  document: {
    hidden:false, getElementById:element, querySelectorAll(){return [];},
    querySelector:element, addEventListener(){}, createElement:element,
  },
  window: {addEventListener(name,handler){windowHandlers.set(name,handler);}, focus(){}},
  ResizeObserver: class {constructor(callback){this.callback=callback;}observe(){this.callback();}},
  setInterval(){return 1;}, setTimeout(){return 1;}, clearTimeout(){},
  fetch: async(path) => ({
    ok: Object.hasOwn(routes,path), status: Object.hasOwn(routes,path) ? 200 : 404,
    json: async()=>routes[path] ?? {detail:'Fixture route not supplied'},
  }),
};
vm.runInNewContext(script, context, {timeout:1000});
if(desktopToken)windowHandlers.get('eh-desktop-session')({detail:{token:desktopToken,desktop:true}});
setTimeout(() => {
  const values = {};
  for (const [id,item] of elements) values[id] = {
    text:item.textContent, html:item.innerHTML, classes:item.className,
  };
  process.stdout.write(JSON.stringify(values));
}, 30);
"""
    program = (
        "const script="
        + json.dumps(script)
        + ";\nconst routes="
        + json.dumps(routes)
        + ";\n"
        + "const desktopToken=" + json.dumps(desktop_token) + ";\n"
        + harness
    )
    target = tmp_path / "cockpit-contract.js"
    target.write_text(program)
    result = subprocess.run([NODE, str(target)], text=True, capture_output=True, timeout=10, check=True)
    return json.loads(result.stdout)


def test_automatic_discovery_needs_no_personal_watchlist(tmp_path):
    runtime = {"status": "DISCOVERING", "running": True, "configured_universe_size": 500}
    values = run_cockpit(
        tmp_path,
        {"assets": [], "watchlist": [], "discovery_runtime": runtime, "discovery_status": {}},
        {"feed_fresh": False, "feed_health": [], "runtime": {"running": False}, "discovery_runtime": runtime},
    )
    assert values["universe-count"]["text"] == "500"
    assert values["coverage-requested"]["text"] == "500"
    assert values["coverage-light"]["text"] == "—"
    assert values["feed-label"]["text"] == "Broad market scanning"
    assert "Stop scanner" in values["live-button"]["html"]
    assert "no manual asset list is required" in values["feed-detail"]["text"]
    assert "no manual" in values["chart-empty-text"]["text"] or "automatically" in values["chart-empty-text"]["text"]


def test_actual_universe_counters_are_distinct_from_displayed_assets(tmp_path):
    now = datetime.now(UTC).isoformat()
    counts = {
        "assets_requested": 500,
        "assets_discovered": 480,
        "assets_eligible": 150,
        "assets_light_scanned": 140,
        "assets_deep_scanned": 10,
        "assets_partial": 20,
        "assets_failed": 3,
        "assets_excluded": 330,
    }
    values = run_cockpit(
        tmp_path,
        {
            "assets": [{"asset_id": "test-only-btc", "exchange_symbol": "BTCUSDT", "price": 100}],
            "watchlist": [],
            "discovery_runtime": {"configured_universe_size": 500, "running": True, "status": "WAITING"},
            "discovery_status": {"counts": counts, "completed_at": now, "status": "DEGRADED"},
        },
        {"feed_fresh": False, "feed_health": [], "runtime": {"running": False}},
    )
    assert values["universe-count"]["text"] == "500"
    assert values["coverage-discovered"]["text"] == "480"
    assert values["coverage-light"]["text"] == "140"
    assert values["coverage-deep"]["text"] == "10"
    assert values["coverage-partial"]["text"] == "20"
    assert values["coverage-failed"]["text"] == "3"
    assert values["coverage-excluded"]["text"] == "330"
    assert values["discovery-badge"]["text"] == "1 observations"


def test_archived_observations_do_not_claim_current_universe_coverage(tmp_path):
    old = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    values = run_cockpit(
        tmp_path,
        {
            "assets": [], "watchlist": [],
            "discovery_status": {"counts": {"assets_discovered": 2, "assets_light_scanned": 2}, "completed_at": old},
        },
        {"feed_fresh": False, "feed_health": [], "runtime": {"running": False}},
    )
    assert values["universe-count"]["text"] == "500"
    assert "archived" in values["universe-note"]["text"]
    assert "ARCHIVE" in values["coverage-status"]["text"]
    assert "Start scanner" in values["live-button"]["html"]


def test_live_native_chart_and_exact_order_book_levels(tmp_path):
    now = datetime.now(UTC)
    candle = {
        "open_time": (now - timedelta(minutes=2)).isoformat(),
        "open": 100, "high": 101, "low": 99, "close": 100.5, "volume": 12,
    }
    levels = [
        {"price": 101, "size": 50, "notional": 5050, "side": "BUY_SIDE", "type": "OBSERVED_ORDER_BOOK", "quality": "AVAILABLE", "status": "RESTING"},
        {"price": 98.5, "size": 60, "notional": 5910, "side": "SELL_SIDE", "type": "OBSERVED_ORDER_BOOK", "quality": "AVAILABLE", "status": "RESTING"},
    ]
    values = run_cockpit(
        tmp_path,
        {"assets": [{"asset_id": "fixture", "exchange_symbol": "BTCUSDT"}], "watchlist": []},
        {
            "feed_fresh": True,
            "feed_health": [{"connection": "CONNECTED", "last_message_timestamp": now.isoformat()}],
            "runtime": {"running": True},
        },
        {
            "status": "AVAILABLE",
            "live": {
                "price": 100.5, "price_quality": "AVAILABLE", "received_timestamp": now.isoformat(),
                "closed_candles": {"1m": [candle]}, "open_candles": {}, "liquidity_map": levels,
                "overlays": [{"kind": "VWAP", "price": 100.2, "timeframe": "1m"}],
            },
            "fast": [],
        },
    )
    assert "<rect" in values["chart"]["html"]
    assert values["coin-price"]["text"] == "100.50"
    assert "101.00" in values["liquidity-list"]["html"]
    assert "98.50" in values["liquidity-list"]["html"]
    assert "BID · DEMAND" in values["liquidity-list"]["html"]
    assert "ASK · SUPPLY" in values["liquidity-list"]["html"]
    assert values["feed-label"]["text"] == "Public feed connected"


def test_desktop_session_bridge_needs_no_manual_token(tmp_path):
    token = 'test-only-desktop-ephemeral-secret-' * 2
    values = run_cockpit(tmp_path, {'assets': [], 'watchlist': []},
                         {'feed_fresh': False, 'runtime': {'running': False}},
                         desktop_token=token)
    serialized = json.dumps(values)
    assert token not in serialized
    source = COCKPIT.read_text()
    assert "eh-desktop-session" in source
    assert "localStorage" not in source
    assert "sessionStorage" not in source


def test_automatic_cockpit_selects_a_native_promoted_candidate(tmp_path):
    now = datetime.now(UTC).isoformat()
    values = run_cockpit(tmp_path, {
        'assets': [
            {'asset_id': 'fixture-archive', 'exchange_symbol': 'ETHUSDT'},
            {'asset_id': 'fixture-live', 'exchange_symbol': 'BTCUSDT',
             'live_status': 'AVAILABLE', 'state': 'RADAR'},
        ], 'watchlist': [],
    }, {'feed_fresh': True, 'runtime': {'running': True}},
       {'status': 'AVAILABLE', 'live': {'price': 101.2, 'received_timestamp': now}, 'fast': []})
    assert values['coin-title']['text'] == 'BTCUSDT'
    assert values['coin-price']['text'] == '101.20'
