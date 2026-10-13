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


def run_cockpit(
    tmp_path, overview, health, live=None, *, desktop_token=None,
    extra_routes=None, actions=(), session_storage=None, required_token=None,
    capture_contract=False,
):
    script = COCKPIT.read_text().split("<script>", 1)[1].split("</script>", 1)[0]
    # The state reader exists only in this executable test copy of the script.
    script = script.rsplit("})();", 1)[0] + """
window.__readState=()=>({token:S.token,authNeeded:S.authNeeded,selected:S.selected,
  frame:S.frame,assets:S.assets,native:S.native,live:S.live,chartBars:S.chartBars});
})();
"""
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
    routes.update(extra_routes or {})
    harness = r"""
const vm = require('node:vm');
const calls = [], events = [], snapshots = [];
const storage = new Map(Object.entries(initialStorage));
const settle = () => new Promise(resolve => setTimeout(resolve, 35));
let elements, handlers, windowHandlers, context;
function boot() {
  elements = new Map(); handlers = new Map(); windowHandlers = new Map();
  function element(id) {
    if (!elements.has(id)) {
      const item = {
        textContent:'',innerHTML:'',className:'',dataset:{},style:{},open:false,
        value:id==='discovery-filter'?'all':'',clientWidth:700,clientHeight:350,
        addEventListener(type, callback){handlers.set(id+':'+type,callback);},
        setAttribute(){},append(){},remove(){},focus(){},scrollIntoView(){},
        showModal(){this.open=true;events.push({kind:'open'});},
        close(){this.open=false;events.push({kind:'close'});},
        querySelector(){return element('query-child');},
      };
      item.classList = {
        contains(name){return item.className.split(/\s+/).includes(name);},
        add(name){this.toggle(name,true);},remove(name){this.toggle(name,false);},
        toggle(name,force){
          const classes=new Set(item.className.split(/\s+/).filter(Boolean));
          const enabled=force===undefined?!classes.has(name):!!force;
          if(enabled)classes.add(name);else classes.delete(name);
          item.className=[...classes].join(' ');return enabled;
        },
      };
      elements.set(id,item);
    }
    return elements.get(id);
  }
  context = {
    console,Date,Math,String,Number,Set,Map,Infinity,AbortController,encodeURIComponent,
    document:{hidden:false,body:element('body'),getElementById:element,
      querySelectorAll(){return [];},querySelector:element,addEventListener(){},createElement:element},
    window:{addEventListener(name,handler){windowHandlers.set(name,handler);},focus(){}},
    sessionStorage:{
      getItem(key){return storage.get(key)??null;},
      setItem(key,value){storage.set(key,String(value));events.push({kind:'save',key,value:String(value)});},
      removeItem(key){storage.delete(key);events.push({kind:'remove',key});},
    },
    ResizeObserver:class{constructor(callback){this.callback=callback;}observe(){this.callback();}},
    setInterval(){return 1;},setTimeout(){return 1;},clearTimeout(){},
    fetch:async(path,options={})=>{
      const call={path,method:options.method??'GET',headers:options.headers??{}};
      calls.push(call);events.push({kind:'fetch',...call});
      const fixture=routes[path],wrapped=fixture&&Object.hasOwn(fixture,'__status');
      const authorized=!requiredToken||path.startsWith('/api/i18n/')||
        call.headers.Authorization==='Bearer '+requiredToken;
      const status=!authorized?401:wrapped?fixture.__status:fixture===undefined?404:200;
      const payload=!authorized?{detail:'Unauthorized'}:wrapped?fixture.__body:
        fixture??{detail:'Fixture route not supplied'};
      if(fixture?.__delay_ms)await new Promise(resolve=>setTimeout(resolve,fixture.__delay_ms));
      events.push({kind:'response',path,status});
      return {ok:status>=200&&status<300,status,json:async()=>payload};
    },
  };
  vm.runInNewContext(script,context,{timeout:1000});
  if(desktopToken)windowHandlers.get('eh-desktop-session')({detail:{token:desktopToken,desktop:true}});
}
function readValues() {
  const values = {};
  for (const [id,item] of elements) values[id] = {
    text:item.textContent,html:item.innerHTML,classes:item.className,open:item.open,
  };
  return values;
}
function snapshot() {
  return JSON.parse(JSON.stringify({values:readValues(),state:context.window.__readState(),
    storage:Object.fromEntries(storage),calls:[...calls],events:[...events]}));
}
(async()=>{
  boot();await settle();snapshots.push(snapshot());
  for(const action of actions) {
    if(action.type==='access') {
      await handlers.get('access-button:click')();
      context.document.getElementById('api-token').value=action.token;
      await handlers.get('access-form:submit')({preventDefault(){},
        currentTarget:elements.get('access-form')});
    } else if(action.type==='frame') {
      const button={dataset:{frame:action.frame}};
      await handlers.get('timeframes:click')({target:{closest(){return button;}}});
    } else if(action.type==='reload') {
      boot();
    } else throw new Error('Unsupported harness action '+action.type);
    await settle();snapshots.push(snapshot());
  }
  process.stdout.write(JSON.stringify(captureContract?{...snapshot(),snapshots}:readValues()));
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    program = (
        "const script="
        + json.dumps(script)
        + ";\nconst routes="
        + json.dumps(routes)
        + ";\n"
        + "const desktopToken=" + json.dumps(desktop_token) + ";\n"
        + "const actions=" + json.dumps(actions) + ";\n"
        + "const initialStorage=" + json.dumps(session_storage or {}) + ";\n"
        + "const requiredToken=" + json.dumps(required_token) + ";\n"
        + "const captureContract=" + json.dumps(capture_contract) + ";\n"
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
    result = run_cockpit(tmp_path, {'assets': [], 'watchlist': []},
                         {'feed_fresh': False, 'runtime': {'running': False}},
                         desktop_token=token, capture_contract=True)
    serialized = json.dumps(result['values'])
    assert token not in serialized
    assert token not in json.dumps(result['storage'])
    assert not any(event['kind'] == 'save' for event in result['events'])
    source = COCKPIT.read_text()
    assert "eh-desktop-session" in source
    assert "localStorage" not in source
    assert "expansion-hunter-access-token" in source


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


def response_fixture(status, body, *, delay_ms=0):
    return {'__status': status, '__body': body, '__delay_ms': delay_ms}


def native_fixtures(*, stale=False, analysis=True):
    """Use the native worker's published row and market snapshot field names."""
    now = datetime.now(UTC)
    observed = now - timedelta(days=1) if stale else now
    stamp = observed.isoformat()
    quality = 'STALE' if stale else 'AVAILABLE'
    frames = {'1m': 60, '5m': 300, '15m': 900, '1h': 3600, '4h': 14400, '1D': 86400}
    assets = [
        {
            'symbol': f'FIXTURE{index:02}USDT', 'asset_id': f'native-fixture-{index}',
            'price': 10 + index, 'received_at': stamp, 'status': 'STALE' if stale else 'READY',
            'price_evidence': {'type': 'NATIVE_TRADE', 'source': 'BINANCE_PUBLIC',
                               'exchange_timestamp': stamp},
            'data_quality': {frame: quality for frame in frames},
            'history_counts': {frame: 36 for frame in frames},
        }
        for index in range(29)
    ]
    assets.append({**assets[0], 'symbol': 'BTCUSDT', 'asset_id': 'native-btc', 'price': 100.5})
    status = {
        'status': 'RUNNING', 'running': True, 'as_of': now.isoformat(), 'assets': assets,
        'counts': {'requested': 30, 'scanned': 30, 'ready': 0 if stale else 30},
        'coverage': {'configured': 30, 'selected': 30, 'live': 0 if stale else 30},
        'connections': [{'connection': 'CONNECTED', 'stale': stale,
                         'last_message_timestamp': stamp, 'provider': 'BINANCE_PUBLIC'}],
    }
    cached_analysis = {
        'as_of': stamp,
        'scores': {'expansion_readiness': {'value': 63.25, 'status': 'UNCALIBRATED'},
                   'stored_energy': {'value': 44}},
        'score_levels': {'support': 97, 'resistance': 103},
        'overlays': [{'kind': 'VWAP', 'price': 100.25, 'timeframe': '1m'}],
        'war_room': {'as_of': stamp, 'bias': 'LONG', 'setup_quality': {'value': 62},
                     'entry_quality': {'value': 51}, 'swing_phases': {},
                     'expected_path': {'steps': []}, 'scenarios': {}},
    } if analysis else None
    routes = {'/api/trade-alert/status': status}
    for frame, duration in frames.items():
        closed = datetime.fromtimestamp(int(observed.timestamp() // duration) * duration, UTC)
        candle = {
            'open_time': (closed - timedelta(seconds=duration)).isoformat(),
            'close_time': closed.isoformat(), 'open': 100, 'high': 101,
            'low': 99, 'close': 100.5, 'volume': 12, 'timeframe': frame,
        }
        routes[f'/api/trade-alert/assets/BTCUSDT?timeframe={frame}'] = {
            'symbol': 'BTCUSDT', 'as_of': now.isoformat(), 'timeframe': frame,
            'market': {'symbol': 'BTCUSDT', 'price': 100.5, 'price_quality': quality,
                       'price_source': 'BINANCE_PUBLIC', 'price_source_timestamp': stamp,
                       'price_received_timestamp': stamp, 'last_received_timestamp': stamp,
                       'data_quality': {key: quality for key in frames},
                       'closed_candles': {}, 'open_candles': {}},
            'candles': [candle], 'analysis': cached_analysis,
            'analysis_status': 'DERIVED' if analysis else 'UNAVAILABLE',
        }
    return status, routes


def test_access_validates_code_before_saving_and_restores_tab_session(tmp_path):
    token = 'fixture-valid-access-code'
    result = run_cockpit(
        tmp_path, {'assets': [], 'watchlist': []}, {'feed_fresh': False},
        extra_routes={'/api/trade-alert/status': {'running': False, 'assets': []}},
        required_token=token,
        actions=[{'type': 'access', 'token': 'wrong-code'},
                 {'type': 'access', 'token': token}, {'type': 'reload'}],
        capture_contract=True,
    )
    initial, wrong, connected, reloaded = result['snapshots']
    assert initial['state']['authNeeded'] is True
    assert wrong['values']['access-dialog']['open'] is True
    assert 'Incorrect access code' in wrong['values']['access-error']['text']
    assert 'hidden' not in wrong['values']['access-error']['classes']
    assert wrong['state']['authNeeded'] is True
    assert wrong['storage'] == {}
    assert not any(event['kind'] == 'save' for event in wrong['events'])
    assert connected['storage'] == {'expansion-hunter-access-token': token}
    assert connected['values']['access-dialog']['open'] is False
    assert connected['state']['authNeeded'] is False
    saved_index = next(index for index, event in enumerate(connected['events'])
                       if event['kind'] == 'save')
    validation = connected['events'][:saved_index]
    assert any(event['kind'] == 'fetch' and event['method'] == 'GET'
               and event['path'] == '/api/trade-alert/status'
               and event['headers'].get('Authorization') == f'Bearer {token}'
               for event in validation)
    assert any(event['kind'] == 'response' and event['path'] == '/api/trade-alert/status'
               and event['status'] == 200 for event in validation)
    assert not any(event['kind'] == 'close' for event in validation)
    assert reloaded['state']['token'] == token
    assert reloaded['storage'] == connected['storage']
    reload_calls = reloaded['calls'][len(connected['calls']):]
    protected = [call for call in reload_calls if not call['path'].startswith('/api/i18n/')]
    assert protected
    assert all(call['headers'].get('Authorization') == f'Bearer {token}' for call in protected)


def test_public_translation_success_preserves_protected_auth_failure(tmp_path):
    translation = json.loads(COCKPIT.with_name('i18n.en.json').read_text())
    result = run_cockpit(
        tmp_path, response_fixture(401, {'detail': 'Unauthorized'}),
        response_fixture(401, {'detail': 'Unauthorized'}),
        extra_routes={
            '/api/trade-alert/status': response_fixture(401, {'detail': 'Unauthorized'}),
            '/api/live/alerts?limit=100': response_fixture(401, {'detail': 'Unauthorized'}),
            '/api/i18n/en': response_fixture(200, translation, delay_ms=20),
        }, required_token='fixture-protected-code', capture_contract=True,
    )
    assert result['state']['authNeeded'] is True
    responses = [event for event in result['events'] if event['kind'] == 'response']
    first_unauthorized = next(index for index, event in enumerate(responses) if event['status'] == 401)
    translation_index = next(index for index, event in enumerate(responses)
                             if event['path'] == '/api/i18n/en' and event['status'] == 200)
    assert first_unauthorized < translation_index


@pytest.mark.parametrize('legacy_fails', [False, True])
def test_native_status_selects_btc_and_hydrates_without_legacy_overview(tmp_path, legacy_fails):
    native, routes = native_fixtures()
    routes['/api/live/alerts?limit=100'] = response_fixture(500, {'detail': 'Alert archive unavailable'})
    overview = response_fixture(500, {'detail': 'Legacy archive unavailable'}) if legacy_fails else {
        'assets': [], 'watchlist': [],
    }
    result = run_cockpit(
        tmp_path, overview, {'feed_fresh': False, 'runtime': {'running': False}},
        extra_routes=routes, capture_contract=True,
    )
    state, values = result['state'], result['values']
    detail = routes['/api/trade-alert/assets/BTCUSDT?timeframe=1m']
    assert len(state['assets']) == 30
    assert state['native']['assets'] == native['assets']
    assert state['selected'] == 'BTCUSDT'
    assert values['coin-title']['text'] == 'BTCUSDT'
    assert values['coin-price']['text'] == '100.50'
    paths = [call['path'] for call in result['calls']]
    assert paths.index('/api/live/assets/BTCUSDT') < paths.index('/api/trade-alert/assets/BTCUSDT?timeframe=1m')
    assert state['live']['live']['native_source'] is True
    assert state['live']['live']['closed_candles']['1m'] == detail['candles']
    assert state['live']['live']['price_source_timestamp'] == detail['market']['price_source_timestamp']
    assert state['live']['live']['scores'] == detail['analysis']['scores']
    assert state['live']['live']['war_room'] == detail['analysis']['war_room']
    assert '<rect' in values['chart']['html']
    assert 'hidden' in values['chart-stale']['classes']


def test_native_fallback_requests_all_six_received_timeframe_histories(tmp_path):
    _, routes = native_fixtures()
    frames = ['1m', '5m', '15m', '1h', '4h', '1D']
    result = run_cockpit(
        tmp_path, {'assets': [], 'watchlist': []}, {'feed_fresh': False},
        extra_routes=routes, actions=[{'type': 'frame', 'frame': frame} for frame in frames],
        capture_contract=True,
    )
    for frame, snapshot in zip(frames, result['snapshots'][1:], strict=True):
        path = f'/api/trade-alert/assets/BTCUSDT?timeframe={frame}'
        assert any(call['path'] == path for call in snapshot['calls'])
        assert snapshot['state']['frame'] == frame
        assert snapshot['state']['live']['live']['closed_candles'][frame] == routes[path]['candles']
        assert snapshot['state']['chartBars'][0]['open_time'] == routes[path]['candles'][0]['open_time']


def test_native_stale_receipts_and_missing_analysis_remain_explicit(tmp_path):
    _, routes = native_fixtures(stale=True, analysis=False)
    result = run_cockpit(
        tmp_path, {'assets': [], 'watchlist': []}, {'feed_fresh': False},
        extra_routes=routes, capture_contract=True,
    )
    values, live = result['values'], result['state']['live']
    assert result['state']['selected'] == 'BTCUSDT'
    assert values['coin-price']['text'] == '100.50'
    assert live['status'] == 'STALE'
    assert 'hidden' not in values['chart-stale']['classes']
    assert 'stale' in (values['coin-age']['text'] + values['chart-feed-caption']['text']).lower()
    assert values['readiness']['html'].startswith('—')
    assert not live['live'].get('scores')
    assert not live['live'].get('war_room')
    assert 'no current structural path' in values['expected-path']['text']
