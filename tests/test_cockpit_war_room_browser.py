"""Execute the real cockpit JS against explicit synthetic API contracts.

This DOM harness validates frontend behavior, not browser layout or external
exchange connectivity. Root's real Chromium/public-data smoke is separate.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
HTML = ROOT / "src/expansion_hunter/static/cockpit.html"
CATALOG = ROOT / "src/expansion_hunter/static/i18n.en.json"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node required for executable frontend tests")


def run_ui(tmp_path, *, overview=None, live=None, positions=None, analytics=None, action="", inputs=None, universe=None, universe_next=None, stream=False, updates=None, hydrated_live=None, extra_routes=None, history_pages=None):
    now = datetime.now(UTC).isoformat()
    routes = {
        "/api/i18n/en": json.loads(CATALOG.read_text()),
        "/api/cockpit/overview": overview or {
            "assets": [{"asset_id": "synthetic-btc", "exchange_symbol": "BTCUSDT", "price": 100,
                        "live_status": "AVAILABLE", "rank": {"current_rank": 1}}], "watchlist": [],
        },
        "/api/live/health": {"feed_fresh": True, "runtime": {"running": True},
            "feed_health": [{"connection": "CONNECTED", "last_message_timestamp": now}]},
        "/api/live/alerts?limit=100": {"alerts": []},
        "/api/live/assets/BTCUSDT": live or {"status": "AVAILABLE", "live": {"price": 100,
            "received_timestamp": now, "closed_candles": {}, "open_candles": {}}, "fast": []},
        "/api/intelligence/cycle": {"status": "UNAVAILABLE", "cycle": None},
        "/api/intelligence/macro/calendar?days=7": {"events": []},
        "/api/live/chart-interest/HBARUSDT": {"status": "MONITORING_REQUESTED"},
        "/api/war-room/journal?limit=50": {"mode": "PAPER_ONLY", "positions": positions or []},
        "/api/war-room/journal/analytics?quote_currency=USDT": analytics or {
            "closed_positions": 0, "known_cost_results": 0, "winrate": None,
            "censored_positions": 0, "ambiguous_positions": 0, "expectancy_net_pnl": None},
    }
    if universe is not None:
        routes['/api/live/universe'] = universe
    routes.update(extra_routes or {})
    script = HTML.read_text().split("<script>", 1)[1].split("</script>", 1)[0]
    # Test-only read access to the real closed-over cache, never exposed in production.
    script = script.replace("window.addEventListener('eh-desktop-session'",
                            "window.__readLiveCache=()=>S.live;\n"
                            "window.__readChartHistory=()=>S.chartHistory instanceof Map?Object.fromEntries(S.chartHistory):S.chartHistory;\n"
                            "window.addEventListener('eh-desktop-session'", 1)
    defaults = {"calc-mode": "TRADE", "calc-side": "LONG", "calc-entry": "100", "calc-stop": "90",
        "calc-target": "120", "calc-amount": "100", "calc-account": "1000", "calc-leverage": "1",
        "calc-entry-fee": "0.04", "calc-exit-fee": "0.04", "calc-funding": "", "paper-notes": "test only"}
    defaults.update(inputs or {})
    harness = r"""
const vm=require('node:vm');
const elements=new Map(),handlers=new Map(),requests=[],intervals=[];
let resolveCalculation,resolveHistory,clockOffset=0,updateIndex=0,historyIndex=0,fullReads=0,beforeHistory=null;
class TestDate extends Date {static now(){return Date.now()+clockOffset}}
const delayedCalculation=new Promise(resolve=>{resolveCalculation=resolve});
const delayedHistory=new Promise(resolve=>{resolveHistory=resolve});
function element(id){if(!elements.has(id))elements.set(id,{
 textContent:'',innerHTML:'',className:'',dataset:{},style:{},value:defaults[id]??'',
 disabled:id==='paper-save',clientWidth:700,clientHeight:350,attributes:{},htmlWrites:0,
 getBoundingClientRect(){return {left:0,top:0,width:700,height:350}},setPointerCapture(){},releasePointerCapture(){},
 classList:{add(){},remove(){},toggle(){}},
 addEventListener(type,callback){handlers.set(id+':'+type,callback)},
 setAttribute(key,value){this.attributes[key]=String(value)},append(){},remove(){},focus(){},showModal(){this.open=true},close(){this.open=false},
 querySelector(){return element('query-child')},scrollIntoView(){},
});const item=elements.get(id);if(!item.htmlTracked){let html=item.innerHTML;Object.defineProperty(item,'innerHTML',{get(){return html},set(value){html=value;this.htmlWrites++}});item.htmlTracked=true}return item}
const calculated={status:'CALCULATED',position_size:1,notional:100,net_pnl:null,estimated_funding:null};
const context={console,Date:TestDate,Math,String,Number,Set,Map,Infinity,AbortController,encodeURIComponent,TextDecoder,
 crypto:require('node:crypto'),
 document:{body:{dataset:{mode:'rookie'}},hidden:false,getElementById:element,querySelectorAll(){return []},querySelector:element,
  addEventListener(){},createElement:element},
 window:{addEventListener(){},focus(){}},
 ResizeObserver:class{constructor(callback){this.callback=callback}observe(){this.callback()}},
 setInterval(callback){intervals.push(callback);return intervals.length},setTimeout(){return 1},clearTimeout(){},
 fetch:async(path,options={})=>{
  const method=options.method??'GET',body=options.body?JSON.parse(options.body):null;
  requests.push({path,method,body});
  if(path.split('?')[0].endsWith('/assets/BTCUSDT/history')){
   const fixture=historyPages?.[Math.min(historyIndex++,historyPages.length-1)];
   if(action==='history-race')await delayedHistory;
   const status=fixture?.http_status??(fixture?200:404);
   return {ok:status===200,status,json:async()=>fixture??{detail:'Missing native history fixture'}};
  }
  if(path==='/api/live/assets/BTCUSDT'){fullReads++;if(Number(defaults['first-full-failure'])&&fullReads===1)return {ok:false,status:Number(defaults['first-full-failure']),json:async()=>({detail:'Native history warming'})};if(fullHydration&&fullReads>1)return {ok:true,status:200,json:async()=>fullHydration};}
  if(path.startsWith('/api/live/updates/')){
   const fixture=incrementalUpdates?.[Math.min(updateIndex++,incrementalUpdates.length-1)]??routes[path.split('?')[0]];
   const status=fixture?.http_status??(fixture?200:404);
   return {ok:status===200,status,json:async()=>fixture??{detail:'Missing incremental test route'}};
  }
  if(path==='/api/live/universe/stream'&&stream){
   const packet='event: universe\r\ndata: '+JSON.stringify(routes['/api/live/universe'])+'\r\n\r\n';
   const boundary=packet.indexOf('\r\n')+1,chunks=[packet.slice(0,boundary),packet.slice(boundary)];let n=0;
   return {ok:true,status:200,body:{getReader(){return {read:async()=>n<chunks.length?{done:false,value:new TextEncoder().encode(chunks[n++])}:await new Promise(()=>{})}}}};
  }
  if(path==='/api/war-room/calculate')return{ok:true,status:200,json:async()=>action==='race'?await delayedCalculation:calculated};
  if(path==='/api/war-room/journal'&&method==='POST')return{ok:true,status:200,json:async()=>({position_id:'synthetic-paper',status:'OPEN'})};
  if(path.endsWith('/close')&&method==='POST')return{ok:true,status:200,json:async()=>({status:'CLOSED'})};
  return{ok:Object.hasOwn(routes,path),status:Object.hasOwn(routes,path)?200:404,
   json:async()=>routes[path]??{detail:'Missing test route'}};
 },
};
vm.runInNewContext(script,context,{timeout:1500});
async function tick(){await new Promise(resolve=>setImmediate(resolve))}
(async()=>{
 for(let n=0;n<6;n++)await tick();
 if(['save','invalid','input','race'].includes(action)){
  const pending=handlers.get('calculator-form:submit')({preventDefault(){}});
  if(action==='race'){
   await tick();handlers.get('timeframes:click')({target:{closest:()=>({dataset:{frame:'5m'}})}});
   resolveCalculation(calculated);
  }
  await pending;
  if(action==='input'){element('calc-entry').value='101';handlers.get('calculator-form:input')({})}
  await handlers.get('paper-save:click')();
 }
 if(['universe-update','universe-page','universe-stale','universe-older'].includes(action)){
  element('discovery-body').htmlBeforeUpdate=element('discovery-body').htmlWrites;
  element('chart').htmlBeforeUpdate=element('chart').htmlWrites;
  if(nextUniverse){routes['/api/live/universe']=nextUniverse;intervals[3]();for(let n=0;n<6;n++)await tick();}
  if(action==='universe-page')handlers.get('discovery-next:click')({});
 }
 if(action==='search'){
  element('global-search').value=defaults['search-query']??'HBAR';handlers.get('global-search:input')({});
 }
 if(action==='select-unmonitored'){
  handlers.get('search-results:click')({target:{closest:()=>({dataset:{symbol:'HBARUSDT'}})}});
  for(let n=0;n<6;n++)await tick();
 }
 if(action==='incremental'){
  for(let n=0;n<Number(defaults['update-count']??1);n++){clockOffset+=Number(defaults['advance-clock-seconds']??0)*1000;intervals[0]();for(let wait=0;wait<6;wait++)await tick();}
 }
 if(action==='pro')handlers.get('experience-mode:click')({target:{closest:()=>({dataset:{mode:'pro'}})}});
 if(action==='frame'){handlers.get('timeframes:click')({target:{closest:()=>({dataset:{frame:defaults['selected-frame']??'5m'}})}});for(let n=0;n<6;n++)await tick();}
 if(['history-pan','history-zoom','history-race','history-cap','history-cap-reset'].includes(action)){
  if(action==='history-zoom'){
   for(let n=0;n<Number(defaults['zoom-count']??16);n++){handlers.get('chart-zoom-out:click')({});for(let wait=0;wait<6;wait++)await tick();}
  }else{
   const pointer=(x)=>({pointerId:1,clientX:x,clientY:150,pointerType:'mouse',button:0,preventDefault(){}});
   const pan=()=>{handlers.get('chart:pointerdown')(pointer(300));handlers.get('chart:pointermove')(pointer(999999));handlers.get('chart:pointerup')(pointer(999999));};
   pan();
   beforeHistory={chart:{...element('chart').dataset},price:element('coin-price').textContent};
   if(action==='history-race'){
    for(let n=0;n<6;n++)await tick();
    handlers.get('timeframes:click')({target:{closest:()=>({dataset:{frame:'5m'}})}});
    for(let n=0;n<6;n++)await tick();
    resolveHistory();
   }
   if(action.startsWith('history-cap')){
    for(let n=1;n<Number(defaults['history-pan-count']??21);n++){for(let wait=0;wait<6;wait++)await tick();pan();}
    for(let n=0;n<6;n++)await tick();
    intervals[0]();for(let n=0;n<6;n++)await tick();
    if(action==='history-cap-reset'){handlers.get('reset-chart:click')({});for(let n=0;n<6;n++)await tick();}
   }
  }
  for(let n=0;n<12;n++)await tick();
 }
 if(['pan','pan-reset','pan-boundary','pinch','vertical-scroll','crosshair','wheel','zoom-buttons','pan-new-bar','pan-frame'].includes(action)){
  const pointer=(id,x,y,type='mouse')=>({pointerId:id,clientX:x,clientY:y,pointerType:type,button:0,preventDefault(){}});
  if(['pan','pan-reset','pan-boundary','pan-new-bar','pan-frame'].includes(action)){
   handlers.get('chart:pointerdown')(pointer(1,300,150));
   handlers.get('chart:pointermove')(pointer(1,action==='pan-boundary'?9999:416.4,150));
   handlers.get('chart:pointerup')(pointer(1,416.4,150));
  }
  if(action==='pan-reset')handlers.get('reset-chart:click')({});
  if(action==='pan-frame')handlers.get('timeframes:click')({target:{closest:()=>({dataset:{frame:'5m'}})}});
  if(action==='pan-new-bar'){
   const bars=routes['/api/live/assets/BTCUSDT'].live.closed_candles['1m'];
   const latest=bars.at(-1),newOpen=new Date(new Date(latest.open_time).getTime()+60000).toISOString(),received=new Date(new Date(newOpen).getTime()+60000).toISOString();
   routes['/api/live/updates/BTCUSDT']={incremental:true,monitoring:'ACTIVE',status:'AVAILABLE',live:{symbol:'BTCUSDT',received_timestamp:received,last_received_timestamp:received,closed_candles:{'1m':[{...latest,open_time:newOpen}]},open_candles:{}},fast:[]};
   intervals[0]();for(let n=0;n<6;n++)await tick();
  }
  if(action==='pinch'){
   handlers.get('chart:pointerdown')(pointer(1,200,100,'touch'));
   handlers.get('chart:pointerdown')(pointer(2,400,100,'touch'));
   handlers.get('chart:pointermove')(pointer(2,600,100,'touch'));
   handlers.get('chart:pointerup')(pointer(2,600,100,'touch'));
   handlers.get('chart:pointerup')(pointer(1,200,100,'touch'));
  }
  if(action==='vertical-scroll'){
   handlers.get('chart:pointerdown')(pointer(1,200,100,'touch'));
   handlers.get('chart:pointermove')(pointer(1,201,220,'touch'));
   handlers.get('chart:pointercancel')({...pointer(1,201,220,'touch'),type:'pointercancel'});
  }
  if(action==='crosshair')handlers.get('chart:pointermove')(pointer(1,200,100));
  if(action==='wheel')handlers.get('chart:wheel')({...pointer(1,300,100),ctrlKey:true,deltaY:-100});
  if(action==='zoom-buttons'){handlers.get('chart-zoom-in:click')({});handlers.get('chart-zoom-out:click')({});}
 }
 if(action==='close'){
  handlers.get('journal-body:click')({target:{closest:()=>({dataset:{paperClose:'synthetic-position'}})}});
  element('paper-exit-price').value='98.25';element('paper-exit-reason').value='Explicit simulated exit';
  await handlers.get('paper-close-form:submit')({preventDefault(){}});
 }
 const values={};for(const[id,item]of elements)values[id]={text:item.textContent,html:item.innerHTML,disabled:item.disabled,dataset:item.dataset,attributes:item.attributes,style:item.style,htmlWrites:item.htmlWrites,htmlBeforeUpdate:item.htmlBeforeUpdate};
 const candleState=routes['/api/live/assets/BTCUSDT']?.live,closed=candleState?.closed_candles?.['1m'];
 const cachedLive=JSON.parse(JSON.stringify(context.window.__readLiveCache()));
 const chartHistory=context.window.__readChartHistory();
 process.stdout.write(JSON.stringify({values,requests,cachedLive,chartHistory,beforeHistory,nativeCandleState:{price:candleState?.price,count:closed?.length,last:closed?.at(-1)}}));
})().catch(error=>{console.error(error);process.exitCode=1});
"""
    program = ("const script=" + json.dumps(script) + ";const routes=" + json.dumps(routes)
        + ";const nextUniverse="+json.dumps(universe_next)+";const stream="+json.dumps(stream)+";const incrementalUpdates="+json.dumps(updates)+";const fullHydration="+json.dumps(hydrated_live)+";const historyPages="+json.dumps(history_pages)+";const defaults=" + json.dumps(defaults) + ";const action=" + json.dumps(action) + ";" + harness)
    target = tmp_path / "war-room-ui.js"
    target.write_text(program)
    completed = subprocess.run([NODE, str(target)], capture_output=True, text=True, timeout=10, check=True)
    return json.loads(completed.stdout)


def war_room_fixture():
    now = datetime.now(UTC).isoformat()
    return {"symbol": "BTCUSDT", "as_of": now, "bias": "BULLISH",
        "setup_quality": {"value": 89}, "entry_quality": {"value": 32, "stop": 90, "target": 120},
        "alignment": {"value": 70, "coverage": .75},
        "expected_path": {"primary": "bullish", "steps": [
            {"state": "BREAKOUT", "price": 101, "condition": "Closed minute above known level"},
            {"state": "RETEST", "price": 101, "condition": "Acceptance remains conditional"},
            {"state": "LIQUIDITY_DESTINATION", "price": 120, "condition": "Observed target only"},
            {"state": "LIQUIDITY_DESTINATION", "price": 130, "condition": "Fourth step is not displayed"}]},
        "scenarios": {"bullish": {"trigger": 101, "invalidation": 90, "targets": [{"price": 120}]}},
        "timeframes": {"1m": {"state": "HH", "bias": "BULLISH", "history": []}},
        "patterns": [{"name": "ASCENDING_TRIANGLE", "timeframe": "1m", "status": "FORMING", "trigger": 101}],
        "setup_library": {"active_setups": [{"name": "BREAKOUT_RETEST", "timeframe": "1m",
            "direction_hypothesis": "BULLISH", "trigger": 101, "invalidation": 90}]}}


def test_compact_table_bounds_50_rows_and_translates_decision_fields(tmp_path):
    report = war_room_fixture()
    rows = [{"asset_id": f"synthetic-{n}", "exchange_symbol": "BTCUSDT" if n == 0 else f"TEST{n}USDT",
        "price": 100, "state": "BREAKOUT_PRESSURE", "freshness": "FRESH",
        "rank": {"current_rank": n+1}, "war_room": report} for n in range(60)]
    output = run_ui(tmp_path, overview={"assets": rows, "watchlist": []})
    table = output["values"]["discovery-body"]["html"]
    assert table.count('data-symbol=') == 50
    assert "101.00" in table and "90.00" in table and "120.00" in table
    assert "89" in table and "32" in table and "Bullish scenario" in table
    assert "BREAKOUT_PRESSURE" not in table and "BULLISH" not in table
    assert output["values"]["discovery-badge"]["text"] == "60 observations"


def test_conditional_path_shows_three_known_steps_without_future_candles(tmp_path):
    now = datetime.now(UTC).isoformat()
    output = run_ui(tmp_path, live={"status": "AVAILABLE", "live": {"price": 100,
        "received_timestamp": now, "war_room": war_room_fixture(), "closed_candles": {}, "open_candles": {}}, "fast": []})
    path = output["values"]["expected-path"]["html"]
    assert path.count('<li>') == 3
    assert "130.00" not in path and "not forecasts" in path
    assert "89/100" in output["values"]["war-summary"]["html"]
    assert "32/100" in output["values"]["war-summary"]["html"]
    assert "<rect" not in output["values"]["chart"]["html"]


def test_relative_negative_dependency_and_unknown_identity_stay_explicit(tmp_path):
    now = datetime.now(UTC).isoformat()
    output = run_ui(tmp_path, live={"status": "AVAILABLE", "live": {"price": 100,
        "received_timestamp": now, "relative_context": {"windows": {"15m": {
            "vs_btc": {"value": -.25, "status": "AVAILABLE"}, "vs_eth": {"value": None, "status": "UNAVAILABLE"}}},
            "btc_dependency": {"correlation": -.7, "beta": -.8, "dependency": "HIGH", "relationship": "INVERSE", "sample_count": 30}},
        "large_trade_flow": {"quality": "UNAVAILABLE", "large_trade_count": None,
            "minimum_quote_notional": 100000, "quote_currency": "USDT"},
        "session_context": {"active_windows": ["LONDON"], "quality": "DERIVED"}}, "fast": []})
    context = output["values"]["context-summary"]["html"]
    assert "-0.25 pp" in context and "-0.7 / -0.8" in context
    assert "London" in context and "24/7" in context
    assert "Large" in context or "large" in context
    assert "institution" in context.lower()
    assert "Unavailable" in context


def test_valid_trade_save_is_explicit_and_preserves_unknown_funding(tmp_path):
    output = run_ui(tmp_path, action="save")
    posts = [r for r in output["requests"] if r["method"] == "POST" and r["path"] == "/api/war-room/journal"]
    assert len(posts) == 1
    body = posts[0]["body"]
    assert body["asset_id"] == "synthetic-btc" and body["symbol"] == "BTCUSDT"
    assert body["trade"]["mode"] == "TRADE" and body["trade"]["stop"] == 90 and body["trade"]["target"] == 120
    assert body["trade"]["funding_cost"] is None
    assert body["trade"]["entry_fee_rate"] == .0004
    assert body["request_id"] and body["quote_currency"] == "USDT"
    assert output["values"]["paper-save"]["disabled"] is True


@pytest.mark.parametrize("action,inputs", [
    ("invalid", {"calc-mode": "MAX_RISK"}),
    ("invalid", {"calc-stop": ""}),
    ("invalid", {"calc-target": "99"}),
    ("input", {}),
    ("race", {}),
])
def test_invalid_changed_or_old_context_never_opens_a_paper_position(tmp_path, action, inputs):
    output = run_ui(tmp_path, action=action, inputs=inputs)
    assert not any(r["method"] == "POST" and r["path"] == "/api/war-room/journal" for r in output["requests"])
    assert output["values"]["paper-save"]["disabled"] is True


def test_initial_load_does_not_open_positions_and_unknown_outcomes_are_not_losses(tmp_path):
    position = {"position_id": "synthetic-position", "status": "CENSORED", "coverage_status": "MISSING_TAIL",
        "request": {"symbol": "BTCUSDT", "timeframe": "1m", "trade": {"entry": 100, "stop": 90, "target": 120, "side": "LONG"}},
        "financial_result": None, "mfe_return": None, "mae_return": None}
    output = run_ui(tmp_path, positions=[position])
    assert not any(r["method"] == "POST" for r in output["requests"])
    assert "coverage incomplete" in output["values"]["journal-body"]["html"]
    assert "Unknown" in output["values"]["journal-stats"]["html"]
    assert "0%" not in output["values"]["journal-stats"]["html"]


def test_manual_close_uses_declared_exit_and_does_not_invent_funding(tmp_path):
    output = run_ui(tmp_path, action="close")
    close = next(r for r in output["requests"] if r["method"] == "POST" and r["path"].endswith('/close'))
    assert close["body"] == {"exit_price": 98.25, "reason": "Explicit simulated exit"}


def test_clean_default_overlays_and_mobile_table_scrolling_are_explicit():
    source = HTML.read_text()
    assert "layers:new Set(['structure','vwap','fib'])" in source
    assert '.decision-table{min-width:0;table-layout:fixed}' in source
    assert 'class="table-scroll"><table class="data-table decision-table"' in source
    assert 'class="overlay-toggle on" data-layer="fib"' in source
    assert 'placeholder="Blank = unknown"' in source


@pytest.mark.parametrize('query', ['HBAR', 'ADA', 'XLM'])
def test_universe_search_finds_actual_supported_assets_without_symbol_preferences(tmp_path, query):
    assets = [{'asset_id': f'synthetic-{symbol}', 'exchange_symbol': symbol+'USDT', 'price': 1,
               'state': 'DORMANT'} for symbol in ['HBAR', 'ADA', 'XLM', 'BTC']]
    output = run_ui(tmp_path, overview={'assets': assets, 'watchlist': []},
                    action='search', inputs={'search-query': query})
    matches = output['values']['search-results']['html']
    assert 'data-symbol="'+query+'USDT"' in matches
    assert 'Broad discovery' in matches
    assert output['values']['search-count']['text'] == '4 supported pairs'
    assert not any(request['method'] == 'POST' for request in output['requests'])


def test_best_directional_candidates_require_actual_live_evidence(tmp_path):
    def asset(symbol, bias, quality, status='AVAILABLE'):
        return {'asset_id': 'synthetic-'+symbol, 'exchange_symbol': symbol+'USDT',
                'live_status': status, 'price': 100,
                'war_room': {'bias': bias, 'setup_quality': {'value': quality},
                             'entry_quality': {'value': quality-10}, 'expected_path': {}}}
    output = run_ui(tmp_path, overview={'assets': [
        asset('ADA', 'BULLISH', 75), asset('XLM', 'BEARISH', 85),
        asset('HBAR', 'BULLISH', 99, 'STALE'), asset('BTC', 'NEUTRAL', 90),
    ], 'watchlist': []})
    assert 'ADA' in output['values']['best-long']['html']
    assert 'HBAR' not in output['values']['best-long']['html']
    assert 'XLM' in output['values']['best-short']['html']
    assert 'BTC' not in output['values']['best-short']['html']


def test_current_timeframe_autofib_is_default_and_does_not_repeat_higher_frame(tmp_path):
    now = datetime.now(UTC).isoformat()
    candle = {'open_time': now, 'open': 100, 'high': 101.5, 'low': 98.5, 'close': 100.5, 'volume': 10}
    def fib(frame, ratio, price):
        return {'timeframe': frame, 'status': 'AVAILABLE', 'quality': 'DERIVED',
                'levels': [{'ratio': ratio, 'price': price}],
                'golden_pocket': {'lower_price': 99.8, 'upper_price': 100.1}}
    live = {'status': 'AVAILABLE', 'live': {'price': 100.5, 'received_timestamp': now,
        'closed_candles': {'1m': [candle], '5m': [candle]}, 'open_candles': {},
        'active_fibonacci': {'1m': fib('1m', .618, 100.2), '5m': fib('5m', 1.272, 100.8),
                             '4h': fib('4h', .786, 101.2)},
        'overlays': [{'kind': 'FIBONACCI', 'timeframe': '1m', 'price': 100.6, 'ratio': .382}]}, 'fast': []}
    current = run_ui(tmp_path, live=live)['values']['chart']['html']
    assert '1m Fib 0.618' in current and 'Golden Pocket' in current
    assert '4h Fib' not in current and '5m Fib' not in current and 'Fib 0.382' not in current
    selected = run_ui(tmp_path, live=live, action='frame', inputs={'selected-frame': '5m'})['values']['chart']['html']
    assert '5m Fib 1.272' in selected and '1m Fib' not in selected and '4h Fib' not in selected


def test_new_short_decision_does_not_reuse_old_bullish_targets(tmp_path):
    now = datetime.now(UTC).isoformat()
    report = war_room_fixture()
    report['scenarios']['bullish']['targets'].append({'price': 140})
    report['swing_phases'] = {'1m': {'current_phase': 'COMPRESSION', 'side': 'SHORT',
        'decision': {'side': 'SHORT', 'entry_status': 'SHORT_SETUP',
            'entry_zone': {'lower': 99, 'upper': 99.5}, 'trigger': 99, 'invalidation': 103,
            'target_1': 92, 'target_2': None, 'setup_quality': {'value': 81}, 'entry_quality': {'value': None},
            'primary_path': [
                {'state': 'BREAKDOWN', 'level': 99, 'condition': 'Closed candle below known support'},
                {'state': 'RETEST', 'level': 99, 'condition': 'Support turns into resistance'},
                {'state': 'LIQUIDITY_DESTINATION', 'level': 92, 'condition': 'Only if acceptance continues'},
            ]}}}
    output = run_ui(tmp_path, live={'status': 'AVAILABLE', 'live': {'price': 100,
        'received_timestamp': now, 'war_room': report, 'closed_candles': {}, 'open_candles': {}}, 'fast': []})
    strip = output['values']['war-summary']['html']
    assert 'Short' in strip and '81/100' in strip and '99.00 – 99.50' in strip
    assert '103.00' in strip and '92.00' in strip
    assert '140.00' not in strip and '120.00' not in strip and '89/100' not in strip
    assert 'Entry quality</small><strong>Unknown' in strip
    assert output['values']['expected-path']['html'].count('<li>') == 3


def test_rookie_and_pro_keep_the_same_backend_decision(tmp_path):
    now = datetime.now(UTC).isoformat()
    live = {'status': 'AVAILABLE', 'live': {'price': 100, 'received_timestamp': now,
        'war_room': war_room_fixture(), 'closed_candles': {}, 'open_candles': {}}, 'fast': []}
    rookie = run_ui(tmp_path, live=live)
    pro = run_ui(tmp_path, live=live, action='pro')
    assert rookie['values']['war-summary']['html'] == pro['values']['war-summary']['html']
    assert rookie['values']['expected-path']['html'] == pro['values']['expected-path']['html']
    assert not any(request['method'] == 'POST' for request in pro['requests'])


def test_english_mobile_product_has_five_navigation_destinations_and_no_forced_wide_radar():
    source = HTML.read_text()
    assert '<html lang="en">' in source and '<body data-mode="rookie">' in source
    assert '/api/i18n/en' in source and '/api/i18n/nl' not in source
    for destination in ['#overview', '#watchlist', '#live-chart', '#signals', '#more']:
        assert 'href="'+destination+'"' in source
    assert 'id="experience-mode"' in source and 'id="global-search"' in source
    assert '.decision-table thead{display:none}' in source
    assert '.decision-table tr{display:grid;' in source
    assert 'data-layer="fib"' in source and 'data-layer="scenarios"' in source
    assert source.index('id="live-chart"') < source.index('id="war-room"') < source.index('id="discovery"')


def test_selecting_unmonitored_pair_requests_native_chart_without_paper_trade(tmp_path):
    output = run_ui(tmp_path, action='select-unmonitored', overview={'assets': [
        {'asset_id': 'synthetic-btc', 'exchange_symbol': 'BTCUSDT', 'live_status': 'AVAILABLE'},
        {'asset_id': 'synthetic-hbar', 'exchange_symbol': 'HBARUSDT', 'price': 1.25,
         'live_status': 'UNAVAILABLE', 'state': 'DORMANT'},
    ], 'watchlist': []})
    posts = [request for request in output['requests'] if request['method'] == 'POST']
    assert len(posts) == 1 and posts[0]['path'] == '/api/live/chart-interest/HBARUSDT'
    assert output['values']['coin-title']['text'] == 'HBARUSDT'
    assert output['values']['chart-empty-title']['text'] == 'Loading native candles…'
    assert '<rect' not in output['values']['chart']['html']
    assert not any(request['path'] == '/api/watchlist' for request in output['requests'])


def chart_history_fixture():
    from datetime import timedelta

    now = datetime.now(UTC)
    bars = [{'open_time': (now - timedelta(minutes=200-index)).isoformat(),
             'open': 100 + index/100, 'high': 101 + index/100, 'low': 99 + index/100,
             'close': 100.5 + index/100, 'volume': 10} for index in range(200)]
    return {'status': 'AVAILABLE', 'live': {'price': bars[-1]['close'],
        'received_timestamp': now.isoformat(), 'closed_candles': {'1m': bars, '5m': bars},
        'open_candles': {}}, 'fast': []}


def test_drag_pan_uses_received_history_and_is_bounded(tmp_path):
    live = chart_history_fixture()
    output = run_ui(tmp_path, live=live, action='pan')
    chart = output['values']['chart']['dataset']
    assert chart['offset'] == '20' and chart['visibleCount'] == '100'
    assert chart['firstTimestamp'] == live['live']['closed_candles']['1m'][80]['open_time']
    assert chart['lastTimestamp'] == live['live']['closed_candles']['1m'][179]['open_time']
    note = output['values']['chart-window-note']['text']
    assert 'History' in note and ' – ' in note
    assert str(datetime.fromisoformat(chart['firstTimestamp']).year) in note
    assert str(datetime.fromisoformat(chart['lastTimestamp']).year) in note
    oldest = run_ui(tmp_path, live=live, action='pan-boundary')['values']['chart']['dataset']
    assert oldest['offset'] == '100'
    assert oldest['firstTimestamp'] == live['live']['closed_candles']['1m'][0]['open_time']


def test_reset_and_timeframe_switch_return_to_latest_candles(tmp_path):
    live = chart_history_fixture()
    for action in ['pan-reset', 'pan-frame']:
        output = run_ui(tmp_path, live=live, action=action)
        chart = output['values']['chart']['dataset']
        assert chart['offset'] == '0' and chart['window'] == '100'
        assert chart['lastTimestamp'] == live['live']['closed_candles']['1m'][-1]['open_time']
        assert output['values']['chart-window-note']['text'] == 'Latest candles'


def test_touch_pinch_zooms_without_inventing_bars_and_vertical_scroll_does_not_pan(tmp_path):
    live = chart_history_fixture()
    zoomed = run_ui(tmp_path, live=live, action='pinch')['values']['chart']['dataset']
    assert zoomed['window'] == '50' and zoomed['visibleCount'] == '50'
    assert zoomed['firstTimestamp'] in {bar['open_time'] for bar in live['live']['closed_candles']['1m']}
    scroll = run_ui(tmp_path, live=live, action='vertical-scroll')['values']['chart']['dataset']
    assert scroll['offset'] == '0' and scroll['window'] == '100'
    assert '.chart-svg{touch-action:pan-y;' in HTML.read_text()


def test_crosshair_has_actual_horizontal_and_vertical_coordinates(tmp_path):
    output = run_ui(tmp_path, live=chart_history_fixture(), action='crosshair')
    vertical = output['values']['chart-crosshair-x']
    horizontal = output['values']['chart-crosshair-y']
    assert vertical['style']['visibility'] == horizontal['style']['visibility'] == 'visible'
    assert vertical['attributes']['x1'] == vertical['attributes']['x2']
    assert float(vertical['attributes']['x1']) > 16
    assert horizontal['attributes']['y1'] == horizontal['attributes']['y2'] == '100'
    assert 'cursor' in output['values']['chart-hover']['html']
    assert 'id="chart-crosshair-x"' in output['values']['chart']['html']


def test_ctrl_wheel_zoom_and_buttons_adjust_real_viewport(tmp_path):
    live = chart_history_fixture()
    wheel = run_ui(tmp_path, live=live, action='wheel')['values']['chart']['dataset']
    assert wheel['window'] == '90' and wheel['visibleCount'] == '90'
    buttons = run_ui(tmp_path, live=live, action='zoom-buttons')['values']['chart']['dataset']
    assert buttons['window'] == '100' and buttons['offset'] == '0'


def test_new_received_bar_keeps_panned_history_anchored(tmp_path):
    live = chart_history_fixture()
    original_first = live['live']['closed_candles']['1m'][80]['open_time']
    output = run_ui(tmp_path, live=live, action='pan-new-bar')
    chart = output['values']['chart']['dataset']
    assert chart['offset'] == '21'
    assert chart['firstTimestamp'] == original_first
    assert chart['visibleCount'] == '100'


def test_broad_bias_remains_a_hypothesis_and_stale_fast_bias_cannot_override_it(tmp_path):
    output = run_ui(tmp_path, overview={'assets': [
        {'asset_id': 'synthetic-hbar', 'exchange_symbol': 'HBARUSDT', 'bias': 'SHORT',
         'fast_bias': 'LONG', 'fast_state_stale': True, 'live_status': 'AVAILABLE',
         'scores': {'expansion_readiness': {'value': 99}}, 'state': 'RADAR'},
    ], 'watchlist': []})
    row = output['values']['discovery-body']['html']
    assert 'Short' in row and 'discovery hypothesis' in row
    assert '<span class="table-score">99</span>' not in row
    assert 'Long' not in row


def native_universe_fixture(count=500, *, price_offset=0, at=None):
    at = at or datetime.now(UTC).isoformat()
    return {'as_of': at, 'status': 'AVAILABLE', 'counts': {'assets_requested': 500}, 'assets': [
        {'asset_id': f'binance:spot:TEST{index:03}USDT', 'symbol': f'TEST{index:03}USDT',
         'price': 100 + index + price_offset, 'price_change_pct': index/100,
         'quote_volume': 100000 + index, 'rank': index+1, 'relative_strength_btc': index/100,
         'source_timestamp': at, 'received_timestamp': at, 'quality': 'NATIVE', 'freshness': 'LIVE'}
        for index in range(count)]}


def test_all_500_native_universe_assets_are_searchable_and_paginated(tmp_path):
    universe = native_universe_fixture()
    output = run_ui(tmp_path, overview={'assets': [], 'watchlist': []}, universe=universe,
                    action='search', inputs={'search-query': 'TEST499'})
    assert output['values']['search-count']['text'] == '500 supported pairs'
    assert 'TEST499USDT' in output['values']['search-results']['html']
    assert output['values']['discovery-body']['html'].count('data-symbol=') == 50
    assert output['values']['discovery-page']['text'] == 'Page 1 / 10 · 500 assets · 50 per page'
    assert '500 fresh / 500 observed / target 500' in output['values']['universe-feed-detail']['text']
    second = run_ui(tmp_path, overview={'assets': [], 'watchlist': []}, universe=universe,
                    action='universe-page')
    assert 'Page 2 / 10' in second['values']['discovery-page']['text']
    assert 'data-symbol="TEST050USDT"' in second['values']['discovery-body']['html']
    assert 'data-symbol="TEST000USDT"' not in second['values']['discovery-body']['html']


def test_one_second_update_patches_only_visible_price_nodes_without_rebuilding_table(tmp_path):
    now = datetime.now(UTC)
    from datetime import timedelta

    first = native_universe_fixture(at=(now-timedelta(seconds=2)).isoformat())
    second = native_universe_fixture(price_offset=10, at=now.isoformat())
    output = run_ui(tmp_path, overview={'assets': [], 'watchlist': []}, universe=first,
                    universe_next=second, action='universe-update')
    body = output['values']['discovery-body']
    assert body['htmlWrites'] == body['htmlBeforeUpdate']
    assert output['values']['discovery-price-TEST000USDT']['text'] == '110.00'
    assert output['values']['discovery-price-TEST049USDT']['text'] == '159.00'
    assert 'discovery-price-TEST499USDT' not in output['values']
    assert output['values']['discovery-change-TEST049USDT']['text'] == '+0.49% 24h'
    assert output['values']['discovery-rank-TEST000USDT']['text'] == '#1'
    assert any(request['path'] == '/api/live/universe' for request in output['requests'])
    assert not any(request['path'] == '/api/discovery/latest' for request in output['requests'])


def test_native_price_update_preserves_existing_deep_war_room_and_asset_identity(tmp_path):
    now = datetime.now(UTC).isoformat()
    report = war_room_fixture()
    universe = {'as_of': now, 'status': 'AVAILABLE', 'assets': [
        {'asset_id': 'binance:spot:BTCUSDT', 'symbol': 'BTCUSDT', 'price': 123.5, 'rank': 2,
         'source_timestamp': now, 'received_timestamp': now, 'quality': 'NATIVE', 'freshness': 'LIVE'}]}
    output = run_ui(tmp_path, overview={'assets': [
        {'asset_id': 'synthetic-btc', 'exchange_symbol': 'BTCUSDT', 'price': 100,
         'war_room': report, 'rank': {'current_rank': 1}}], 'watchlist': []}, universe=universe,
                    action='save')
    assert output['values']['discovery-badge']['text'] == '1 observations'
    table = output['values']['discovery-body']['html']
    assert '89' in table and '32' in table and '101.00' in table and '90.00' in table
    post = next(request for request in output['requests'] if request['path'] == '/api/war-room/journal' and request['method'] == 'POST')
    assert post['body']['asset_id'] == 'synthetic-btc'


def test_older_native_snapshot_cannot_roll_price_back(tmp_path):
    from datetime import timedelta

    now = datetime.now(UTC)
    first = native_universe_fixture(count=1, price_offset=20, at=now.isoformat())
    old = native_universe_fixture(count=1, price_offset=-50, at=(now-timedelta(seconds=5)).isoformat())
    output = run_ui(tmp_path, overview={'assets': [], 'watchlist': []}, universe=first,
                    universe_next=old, action='universe-older')
    assert output['values']['discovery-price-TEST000USDT']['text'] == '120.00'
    assert output['values']['discovery-body']['htmlWrites'] == output['values']['discovery-body']['htmlBeforeUpdate']


def test_newer_fast_price_is_not_overwritten_by_older_universe_quote(tmp_path):
    from datetime import timedelta

    now = datetime.now(UTC)
    universe = native_universe_fixture(count=1, at=(now-timedelta(seconds=2)).isoformat())
    output = run_ui(tmp_path, overview={'assets': [
        {'asset_id': 'actual-test-id', 'exchange_symbol': 'TEST000USDT', 'price': 333,
         'price_as_of': now.isoformat(), 'war_room': war_room_fixture()}], 'watchlist': []}, universe=universe)
    assert output['values']['discovery-price-TEST000USDT']['text'] == '333.00'


def test_quiet_assets_and_stale_snapshots_do_not_become_fresh_on_each_poll(tmp_path):
    from datetime import timedelta

    old = (datetime.now(UTC)-timedelta(minutes=2)).isoformat()
    universe = native_universe_fixture(count=2, at=old)
    universe['status'] = 'STALE'
    for row in universe['assets']:
        row['freshness'] = 'STALE'
    output = run_ui(tmp_path, overview={'assets': [], 'watchlist': []}, universe=universe)
    assert '0 fresh / 2 observed' in output['values']['universe-feed-detail']['text']
    assert output['values']['discovery-freshness-TEST000USDT']['text'] == 'Universe stale'
    assert 'stale / unavailable' in output['values']['universe-feed-status']['text']


def test_missing_native_values_are_visible_without_fabricated_prices_or_scores(tmp_path):
    now = datetime.now(UTC).isoformat()
    universe = {'as_of': now, 'status': 'PARTIAL', 'assets': [
        {'asset_id': 'binance:spot:TEST000USDT', 'symbol': 'TEST000USDT', 'price': None, 'rank': None,
         'source_timestamp': None, 'received_timestamp': None, 'quality': 'UNAVAILABLE', 'freshness': 'UNAVAILABLE'}]}
    output = run_ui(tmp_path, overview={'assets': [], 'watchlist': []}, universe=universe)
    assert '1 observations' == output['values']['discovery-badge']['text']
    assert output['values']['discovery-price-TEST000USDT']['text'] == '—'
    assert '0 fresh / 0 observed' in output['values']['universe-feed-detail']['text']
    assert '<span class="table-score">—</span>' in output['values']['discovery-body']['html']


def test_fetch_sse_parses_split_crlf_frames_with_bearer_capable_transport(tmp_path):
    universe = native_universe_fixture(count=2)
    output = run_ui(tmp_path, overview={'assets': [], 'watchlist': []}, universe=universe, stream=True)
    assert output['values']['discovery-price-TEST000USDT']['text'] == '100.00'
    assert 'live stream' in output['values']['universe-feed-detail']['text']
    assert any(request['path'] == '/api/live/universe/stream' for request in output['requests'])
    source = HTML.read_text()
    assert "headers.Authorization='Bearer '+token" in source
    assert 'new EventSource' not in source


def test_one_second_selected_native_price_patches_header_and_line_without_inventing_ohlc(tmp_path):
    from datetime import timedelta

    now = datetime.now(UTC)
    live = chart_history_fixture()
    live['live']['price_source_timestamp'] = (now-timedelta(seconds=3)).isoformat()
    first = {'as_of': (now-timedelta(seconds=2)).isoformat(), 'status': 'AVAILABLE', 'assets': [
        {'asset_id': 'binance:spot:BTCUSDT', 'symbol': 'BTCUSDT', 'price': 102, 'rank': 1,
         'source_timestamp': (now-timedelta(seconds=2)).isoformat(),
         'received_timestamp': (now-timedelta(seconds=2)).isoformat(), 'quality': 'NATIVE', 'freshness': 'LIVE'}]}
    second = {'as_of': now.isoformat(), 'status': 'AVAILABLE', 'assets': [
        {**first['assets'][0], 'price': 103, 'source_timestamp': now.isoformat(),
         'received_timestamp': now.isoformat()}]}
    output = run_ui(tmp_path, live=live, universe=first, universe_next=second, action='universe-update')
    values = output['values']
    assert values['coin-price']['text'] == values['chart-current-label']['text'] == '103.00'
    assert values['coin-price']['dataset']['sourceTimestamp'] == now.isoformat()
    assert values['chart-current-price']['dataset']['sourceTimestamp'] == now.isoformat()
    assert values['chart-current-price']['dataset']['origin'] == 'UNIVERSE'
    assert 'Native universe price' in values['coin-age']['text']
    assert values['chart']['htmlWrites'] == values['chart']['htmlBeforeUpdate']
    assert output['nativeCandleState']['count'] == 200
    assert output['nativeCandleState']['last'] == live['live']['closed_candles']['1m'][-1]
    assert output['nativeCandleState']['price'] == live['live']['price']
    assert float(values['chart-current-line']['attributes']['y1']) == float(values['chart-current-line']['attributes']['y2'])


def test_newer_fast_native_price_wins_selected_ticker_comparison(tmp_path):
    from datetime import timedelta

    now = datetime.now(UTC)
    live = chart_history_fixture()
    live['live']['price_source_timestamp'] = now.isoformat()
    live['live']['price_received_timestamp'] = now.isoformat()
    universe = {'as_of': now.isoformat(), 'status': 'AVAILABLE', 'assets': [
        {'asset_id': 'binance:spot:BTCUSDT', 'symbol': 'BTCUSDT', 'price': 102, 'rank': 1,
         'source_timestamp': (now-timedelta(seconds=2)).isoformat(),
         'received_timestamp': now.isoformat(), 'quality': 'NATIVE', 'freshness': 'LIVE'}]}
    output = run_ui(tmp_path, live=live, universe=universe)
    assert output['values']['coin-price']['text'] == '102.49'
    assert output['values']['chart-current-label']['text'] == '102.49'
    assert output['values']['chart-current-price']['dataset']['origin'] == 'FAST'
    assert output['values']['coin-price']['dataset']['sourceTimestamp'] == now.isoformat()


def incremental_live_fixture():
    """A received native history, not a claim about actual exchange prices."""
    from datetime import timedelta

    now = datetime.now(UTC)
    boundary = now.replace(second=0, microsecond=0)
    bars = [{'open_time': (boundary-timedelta(minutes=201-index)).isoformat(),
             'open': 100+index/100, 'high': 101+index/100, 'low': 99+index/100,
             'close': 100.5+index/100, 'volume': 10} for index in range(200)]
    analysis_at = (now-timedelta(minutes=2)).isoformat()
    return {'status': 'AVAILABLE', 'live': {'symbol': 'BTCUSDT', 'asset_id': 'synthetic-btc',
        'price': 102.49, 'price_quality': 'AVAILABLE', 'price_source_timestamp': now.isoformat(),
        'price_received_timestamp': now.isoformat(), 'last_received_timestamp': now.isoformat(),
        'received_timestamp': now.isoformat(), 'analysis_at': analysis_at,
        'war_room': war_room_fixture(), 'active_fibonacci': {'1m': {'status': 'INSUFFICIENT_DATA'}},
        'closed_candles': {'1m': bars, '5m': [dict(bars[0])]}, 'open_candles': {}}, 'fast': []}


def incremental_delta(live, *, new_closed=True, new_open=True):
    from datetime import timedelta

    now = datetime.now(UTC)
    boundary = now.replace(second=0, microsecond=0)
    received = now.isoformat()
    closed = list(live['live']['closed_candles']['1m'][-2:])
    if new_closed:
        closed.append({'open_time': (boundary-timedelta(minutes=1)).isoformat(),
                       'open': 102.5, 'high': 104, 'low': 102.4, 'close': 103.2, 'volume': 15})
    opened = {'1m': {'open_time': boundary.isoformat(), 'open': 103.2, 'high': 104.4,
                    'low': 103, 'close': 103.5, 'volume': 2}} if new_open else {}
    return {'incremental': True, 'monitoring': 'ACTIVE', 'status': 'AVAILABLE', 'live': {
        'symbol': 'BTCUSDT', 'asset_id': 'synthetic-btc', 'received_timestamp': received,
        'last_received_timestamp': received, 'price': 103.5, 'price_quality': 'NATIVE',
        'price_source_timestamp': received, 'price_received_timestamp': received,
        'closed_candles': {'1m': closed}, 'open_candles': opened,
        'order_book': {'asks': [[104.5, 12]], 'bids': [[103.4, 10]]}}, 'fast': []}


def test_incremental_candles_merge_without_replacing_cached_other_timeframes(tmp_path):
    live = incremental_live_fixture()
    delta = incremental_delta(live)
    output = run_ui(tmp_path, live=live, action='incremental', updates=[delta], inputs={'update-count': 3})
    cache = output['cachedLive']['live']
    assert len(cache['closed_candles']['1m']) == 201
    assert cache['closed_candles']['1m'][:200] == live['live']['closed_candles']['1m']
    assert cache['closed_candles']['5m'] == live['live']['closed_candles']['5m']
    assert cache['open_candles']['1m'] == delta['live']['open_candles']['1m']
    assert cache['war_room'] == live['live']['war_room']
    assert cache['active_fibonacci'] == live['live']['active_fibonacci']
    requests = output['requests']
    assert sum(request['path'] == '/api/live/assets/BTCUSDT' for request in requests) == 1
    updates = [request['path'] for request in requests if request['path'].startswith('/api/live/updates/')]
    assert len(updates) == 3
    assert all('timeframe=1m' in path and 'analysis_after=' in path for path in updates)
    assert output['values']['coin-price']['text'] == '103.50'


def test_incremental_closed_candle_conflict_is_visible_and_does_not_rewrite_history(tmp_path):
    live = incremental_live_fixture()
    delta = incremental_delta(live, new_closed=False, new_open=False)
    conflict = dict(live['live']['closed_candles']['1m'][-1], high=110, close=109)
    delta['live']['closed_candles']['1m'] = [conflict]
    output = run_ui(tmp_path, live=live, action='incremental', updates=[delta])
    assert output['cachedLive']['live']['closed_candles']['1m'] == live['live']['closed_candles']['1m']
    assert 'Conflicting closed candle rejected' in output['values']['chart-feed-caption']['text']


def test_incremental_future_and_invalid_bars_are_rejected_with_visible_reason(tmp_path):
    from datetime import timedelta

    live = incremental_live_fixture()
    delta = incremental_delta(live, new_closed=False, new_open=False)
    future = dict(live['live']['closed_candles']['1m'][-1],
                  open_time=(datetime.now(UTC)+timedelta(minutes=1)).isoformat())
    invalid = dict(live['live']['closed_candles']['1m'][-1], open=200, high=100)
    delta['live']['closed_candles']['1m'] = [future, invalid]
    delta['live']['open_candles']['1m'] = future
    output = run_ui(tmp_path, live=live, action='incremental', updates=[delta])
    assert output['cachedLive']['live']['closed_candles']['1m'] == live['live']['closed_candles']['1m']
    assert output['cachedLive']['live']['open_candles'] == {}
    assert 'Rejected future or invalid' in output['values']['chart-feed-caption']['text']


def test_out_of_order_incremental_receipt_cannot_roll_back_received_native_state(tmp_path):
    from datetime import timedelta

    live = incremental_live_fixture()
    delta = incremental_delta(live)
    old = (datetime.fromisoformat(live['live']['received_timestamp'])-timedelta(seconds=30)).isoformat()
    delta['live']['last_received_timestamp'] = delta['live']['received_timestamp'] = old
    delta['live']['price'] = 90
    output = run_ui(tmp_path, live=live, action='incremental', updates=[delta])
    assert output['cachedLive']['live'] == live['live']
    assert output['values']['coin-price']['text'] == '102.49'


def test_missing_monitor_retains_real_cached_history_and_throttles_full_fallback(tmp_path):
    live = incremental_live_fixture()
    unavailable = {'incremental': True, 'monitoring': 'UNAVAILABLE', 'status': 'UNAVAILABLE',
                   'live': None, 'fast': [], 'reason': 'Native worker unavailable'}
    output = run_ui(tmp_path, live=live, action='incremental', updates=[unavailable],
                    inputs={'update-count': 5, 'advance-clock-seconds': 3})
    assert sum(request['path'] == '/api/live/assets/BTCUSDT' for request in output['requests']) == 2
    assert output['cachedLive']['live']['closed_candles'] == live['live']['closed_candles']
    assert output['cachedLive']['status'] == 'STALE'
    assert output['cachedLive']['live']['received_timestamp'] == live['live']['received_timestamp']
    assert 'retained history is stale' in output['values']['chart-feed-caption']['text']


def test_incremental_provider_failure_keeps_history_visible_and_stale(tmp_path):
    live = incremental_live_fixture()
    output = run_ui(tmp_path, live=live, action='incremental',
                    updates=[{'http_status': 503, 'detail': 'Native provider disconnected'}])
    assert output['cachedLive']['live']['closed_candles'] == live['live']['closed_candles']
    assert output['cachedLive']['status'] == 'STALE'
    assert 'Connection lost' in output['values']['chart-feed-caption']['text']
    assert sum(request['path'] == '/api/live/assets/BTCUSDT' for request in output['requests']) == 1


def test_incremental_analysis_cache_changes_only_when_actual_new_analysis_arrives(tmp_path):
    live = incremental_live_fixture()
    delta = incremental_delta(live)
    now = datetime.now(UTC).isoformat()
    delta['live']['analysis_at'] = now
    delta['live']['active_fibonacci'] = {'1m': {'status': 'AVAILABLE', 'levels': []}}
    delta['live']['scores'] = {'stored_energy': {'value': 63, 'probability': None}}
    output = run_ui(tmp_path, live=live, action='incremental', updates=[delta, incremental_delta(live)],
                    inputs={'update-count': 2})
    cache = output['cachedLive']['live']
    assert cache['analysis_at'] == now
    assert cache['active_fibonacci'] == delta['live']['active_fibonacci']
    assert cache['scores'] == delta['live']['scores']
    queries = [request['path'] for request in output['requests'] if request['path'].startswith('/api/live/updates/')]
    from urllib.parse import parse_qs, urlsplit

    assert parse_qs(urlsplit(queries[-1]).query)['analysis_after'] == [now]


def test_timeframe_change_reuses_native_cached_histories_and_requests_own_delta(tmp_path):
    live = incremental_live_fixture()
    delta = incremental_delta(live, new_closed=False, new_open=False)
    delta['live']['closed_candles'] = {'5m': list(live['live']['closed_candles']['5m'])}
    output = run_ui(tmp_path, live=live, action='frame', updates=[delta], inputs={'selected-frame': '5m'})
    assert output['cachedLive']['live']['closed_candles']['1m'] == live['live']['closed_candles']['1m']
    assert output['cachedLive']['live']['closed_candles']['5m'] == live['live']['closed_candles']['5m']
    assert output['values']['chart']['dataset']['visibleCount'] == '1'
    assert sum(request['path'] == '/api/live/assets/BTCUSDT' for request in output['requests']) == 1
    queries = [request['path'] for request in output['requests'] if request['path'].startswith('/api/live/updates/')]
    assert len(queries) == 1 and 'timeframe=5m' in queries[0]


def test_incremental_closed_history_is_bounded_240_without_dropping_other_frames(tmp_path):
    from datetime import timedelta

    live = incremental_live_fixture()
    first = live['live']['closed_candles']['1m'][0]
    earlier = [dict(first, open_time=(datetime.fromisoformat(first['open_time'])-timedelta(minutes=40-index)).isoformat())
               for index in range(40)]
    live['live']['closed_candles']['1m'] = earlier+live['live']['closed_candles']['1m']
    delta = incremental_delta(live)
    output = run_ui(tmp_path, live=live, action='incremental', updates=[delta])
    history = output['cachedLive']['live']['closed_candles']['1m']
    assert len(history) == 240
    assert history[0] == live['live']['closed_candles']['1m'][1]
    assert history[-1] == delta['live']['closed_candles']['1m'][-1]
    assert output['cachedLive']['live']['closed_candles']['5m'] == live['live']['closed_candles']['5m']


def test_open_candle_remains_on_missing_update_until_actual_finalization_arrives(tmp_path):
    live = incremental_live_fixture()
    delta = incremental_delta(live)
    current = delta['live']['open_candles']['1m']
    live['live']['open_candles']['1m'] = current
    no_open = incremental_delta(live, new_closed=False, new_open=False)
    output = run_ui(tmp_path, live=live, action='incremental', updates=[no_open])
    assert output['cachedLive']['live']['open_candles']['1m'] == current
    previous = dict(delta['live']['closed_candles']['1m'][-1])
    live['live']['open_candles']['1m'] = previous
    finalization = incremental_delta(live, new_open=False)
    finalization['live']['closed_candles']['1m'][-1]['volume'] = 16
    output = run_ui(tmp_path, live=live, action='incremental', updates=[finalization])
    assert output['cachedLive']['live']['open_candles'] == {}
    assert output['cachedLive']['live']['closed_candles']['1m'][-1]['volume'] == 16


def test_failed_initial_history_is_hydrated_later_without_rolling_new_native_price_back(tmp_path):
    live = incremental_live_fixture()
    delta = incremental_delta(live)
    output = run_ui(tmp_path, live=live, action='incremental', updates=[delta],
                    inputs={'first-full-failure': 404, 'update-count': 3, 'advance-clock-seconds': 5})
    assert sum(request['path'] == '/api/live/assets/BTCUSDT' for request in output['requests']) == 2
    assert len(output['cachedLive']['live']['closed_candles']['1m']) == 201
    assert output['cachedLive']['live']['closed_candles']['5m'] == live['live']['closed_candles']['5m']
    assert output['cachedLive']['live']['price'] == delta['live']['price']
    assert output['values']['coin-price']['text'] == '103.50'
    assert output['cachedLive']['live']['received_timestamp'] == delta['live']['received_timestamp']


def gapped_incremental_fixture():
    from copy import deepcopy
    from datetime import timedelta

    live = incremental_live_fixture()
    for bar in live['live']['closed_candles']['1m']:
        bar['open_time'] = (datetime.fromisoformat(bar['open_time'])-timedelta(minutes=8)).isoformat()
    hydrated = deepcopy(live)
    last = hydrated['live']['closed_candles']['1m'][-1]
    supplied = [dict(last, open_time=(datetime.fromisoformat(last['open_time'])+timedelta(minutes=index)).isoformat(),
                     volume=20+index) for index in range(1, 10)]
    hydrated['live']['closed_candles']['1m'].extend(supplied)
    delta = incremental_delta(live, new_closed=False, new_open=False)
    delta['live']['closed_candles']['1m'] = supplied[-2:]
    return live, delta, hydrated, supplied


def test_missing_native_interval_is_visible_and_never_filled_before_real_backfill(tmp_path):
    live, delta, _, supplied = gapped_incremental_fixture()
    output = run_ui(tmp_path, live=live, action='incremental', updates=[delta],
                    inputs={'update-count': 3, 'advance-clock-seconds': 2})
    actual = output['cachedLive']['live']['closed_candles']['1m']
    assert actual == live['live']['closed_candles']['1m']+supplied[-2:]
    assert all(bar not in actual for bar in supplied[:-2])
    assert 'history backfill pending' in output['values']['chart-feed-caption']['text']
    assert sum(request['path'] == '/api/live/assets/BTCUSDT' for request in output['requests']) == 1


def test_gap_queues_one_budgeted_native_backfill_without_repeated_full_storm(tmp_path):
    _, delta, hydrated, supplied = gapped_incremental_fixture()
    # The first upload stops well before the returned last-two update bars.
    from copy import deepcopy

    initial = deepcopy(hydrated)
    initial['live']['closed_candles']['1m'] = initial['live']['closed_candles']['1m'][:-9]
    output = run_ui(tmp_path, live=initial, hydrated_live=hydrated, action='incremental', updates=[delta],
                    inputs={'update-count': 6, 'advance-clock-seconds': 5})
    actual = output['cachedLive']['live']['closed_candles']['1m']
    assert actual == hydrated['live']['closed_candles']['1m']
    assert all(bar in actual for bar in supplied)
    assert 'history backfill pending' not in output['values']['chart-feed-caption']['text']
    assert sum(request['path'] == '/api/live/assets/BTCUSDT' for request in output['requests']) == 2
    assert output['cachedLive']['live']['closed_candles']['5m'] == initial['live']['closed_candles']['5m']


def test_contiguous_exclusive_close_boundary_does_not_request_backfill(tmp_path):
    from datetime import timedelta

    live = incremental_live_fixture()
    last = live['live']['closed_candles']['1m'][-1]
    last['close_time'] = (datetime.fromisoformat(last['open_time'])+timedelta(minutes=1)).isoformat()
    delta = incremental_delta(live)
    assert delta['live']['closed_candles']['1m'][-1]['open_time'] == last['close_time']
    output = run_ui(tmp_path, live=live, action='incremental', updates=[delta],
                    inputs={'update-count': 4, 'advance-clock-seconds': 5})
    assert 'backfill pending' not in output['values']['chart-feed-caption']['text']
    assert sum(request['path'] == '/api/live/assets/BTCUSDT' for request in output['requests']) == 1


def paginated_history_fixture():
    """An explicit archived page preceding a separate 240-bar live cache."""
    from datetime import timedelta

    live = incremental_live_fixture()
    first = live['live']['closed_candles']['1m'][0]
    first_at = datetime.fromisoformat(first['open_time'])
    recent = [dict(first, open_time=(first_at-timedelta(minutes=40-index)).isoformat())
              for index in range(40)]
    live['live']['closed_candles']['1m'] = recent+live['live']['closed_candles']['1m']
    oldest_at = datetime.fromisoformat(recent[0]['open_time'])
    older = [dict(first, open_time=(oldest_at-timedelta(minutes=500-index)).isoformat())
             for index in range(500)]
    page = {'symbol': 'BTCUSDT', 'timeframe': '1m', 'status': 'AVAILABLE',
            'as_of': live['live']['received_timestamp'], 'candles': older,
            'has_more': False, 'next_before': older[0]['open_time']}
    return live, page


def history_requests(output):
    return [request['path'] for request in output['requests']
            if request['path'].split('?')[0].endswith('/assets/BTCUSDT/history')]


def cached_chart_history(output, frame='1m'):
    """Read the test-only archive state without imposing its storage layout."""
    history = output['chartHistory']
    if isinstance(history, dict):
        entry = history.get('BTCUSDT:'+frame, history.get(frame))
        if entry is None and isinstance(history.get('BTCUSDT'), dict):
            entry = history['BTCUSDT'].get(frame)
    else:
        entry = None
    if isinstance(entry, list):
        return entry
    assert isinstance(entry, dict), history
    return entry.get('candles', entry.get('bars', []))


def test_native_history_page_preserves_viewport_quote_and_bounded_live_cache(tmp_path):
    from urllib.parse import parse_qs, urlsplit

    live, page = paginated_history_fixture()
    output = run_ui(tmp_path, live=live, action='history-pan', history_pages=[page])
    requests = history_requests(output)
    assert len(requests) == 1
    query = parse_qs(urlsplit(requests[0]).query)
    assert query['timeframe'] == ['1m']
    assert query['before'] == [live['live']['closed_candles']['1m'][0]['open_time']]
    archive = cached_chart_history(output)
    assert archive == page['candles']+live['live']['closed_candles']['1m']
    assert len(archive) == 740
    chart = output['values']['chart']['dataset']
    assert chart['firstTimestamp'] == output['beforeHistory']['chart']['firstTimestamp']
    assert chart['lastTimestamp'] == output['beforeHistory']['chart']['lastTimestamp']
    assert output['values']['coin-price']['text'] == output['beforeHistory']['price'] == '102.49'
    assert output['cachedLive']['live'] == live['live']
    assert len(output['cachedLive']['live']['closed_candles']['1m']) == 240


def test_zoom_out_loads_native_archive_and_can_show_more_than_240_received_candles(tmp_path):
    live, page = paginated_history_fixture()
    output = run_ui(tmp_path, live=live, action='history-zoom', history_pages=[page])
    chart = output['values']['chart']['dataset']
    assert int(chart['window']) > 240 and int(chart['visibleCount']) > 240
    assert chart['lastTimestamp'] == live['live']['closed_candles']['1m'][-1]['open_time']
    assert len(cached_chart_history(output)) == 740
    assert len(history_requests(output)) == 1
    assert len(output['cachedLive']['live']['closed_candles']['1m']) == 240
    assert output['values']['coin-price']['text'] == '102.49'


def test_native_history_page_rejects_conflicts_future_bars_and_invalid_ohlc(tmp_path):
    from datetime import timedelta

    live, page = paginated_history_fixture()
    supplied = page['candles'][:3]
    conflicting = dict(live['live']['closed_candles']['1m'][0], close=99.25)
    future = dict(supplied[0], open_time=(datetime.now(UTC)+timedelta(minutes=5)).isoformat())
    invalid = dict(supplied[0], open=200, high=100)
    page['candles'] = supplied+[conflicting, future, invalid]
    output = run_ui(tmp_path, live=live, action='history-pan', history_pages=[page])
    archive = cached_chart_history(output)
    assert archive == live['live']['closed_candles']['1m']
    assert future not in archive and invalid not in archive and conflicting not in archive
    assert output['cachedLive']['live'] == live['live']
    assert output['values']['coin-price']['text'] == '102.49'
    assert 'rejected' in output['values']['chart-window-note']['text'].lower()


def test_native_history_page_response_after_timeframe_switch_cannot_replace_selected_frame(tmp_path):
    live, page = paginated_history_fixture()
    delta = incremental_delta(live, new_closed=False, new_open=False)
    delta['live']['closed_candles'] = {'5m': list(live['live']['closed_candles']['5m'])}
    output = run_ui(tmp_path, live=live, action='history-race', history_pages=[page], updates=[delta])
    assert len(history_requests(output)) == 1
    chart = output['values']['chart']['dataset']
    assert chart['offset'] == '0' and chart['visibleCount'] == '1'
    assert chart['firstTimestamp'] == live['live']['closed_candles']['5m'][0]['open_time']
    assert output['cachedLive']['live']['closed_candles'] == live['live']['closed_candles']
    assert output['values']['coin-price']['text'] == '103.50'


def active_fib_fixture(frame, status='AVAILABLE', **changes):
    view = {'timeframe': frame, 'status': status, 'quality': 'DERIVED',
            'levels': [{'timeframe': frame, 'ratio': .618, 'price': 100.2}],
            'golden_pocket': {'timeframe': frame, 'lower_price': 99.8, 'upper_price': 100.1},
            'evaluated_at': datetime.now(UTC).isoformat()}
    return {**view, **changes}


@pytest.mark.parametrize('status', ['INSUFFICIENT_DATA', 'NO_SWING', 'DISABLED', 'STALE', 'UNAVAILABLE'])
def test_selected_frame_fib_status_is_visible_without_legacy_levels_or_golden_pocket(tmp_path, status):
    live = incremental_live_fixture()
    live['live']['active_fibonacci'] = {'1m': active_fib_fixture('1m', status)}
    live['live']['overlays'] = [{'kind': 'FIBONACCI', 'timeframe': '1m', 'ratio': .382, 'price': 100.6}]
    live['live']['fibonacci'] = {'golden_pockets': [{'timeframe': '1m', 'lower': 99.8, 'upper': 100.1}]}
    output = run_ui(tmp_path, live=live)
    assert output['values']['chart-fib-status']['text'] == '1m Fib · '+status
    chart = output['values']['chart']['html']
    assert '1m Fib 0.618' not in chart and 'Fib 0.382' not in chart and 'Golden Pocket' not in chart


def test_missing_selected_frame_fib_is_unavailable_and_cannot_reuse_other_frame(tmp_path):
    live = incremental_live_fixture()
    live['live']['active_fibonacci'] = {'5m': active_fib_fixture('5m')}
    live['live']['overlays'] = [{'kind': 'FIBONACCI', 'timeframe': '1m', 'ratio': .382, 'price': 100.6}]
    live['live']['fibonacci'] = {'golden_pockets': [{'timeframe': '1m', 'lower': 99.8, 'upper': 100.1}]}
    output = run_ui(tmp_path, live=live)
    assert output['values']['chart-fib-status']['text'] == '1m Fib · UNAVAILABLE'
    chart = output['values']['chart']['html']
    assert '5m Fib' not in chart and 'Fib 0.382' not in chart and 'Golden Pocket' not in chart


def test_stale_fib_quality_suppresses_its_levels_and_golden_pocket(tmp_path):
    live = incremental_live_fixture()
    live['live']['active_fibonacci'] = {'1m': active_fib_fixture('1m', quality='STALE')}
    output = run_ui(tmp_path, live=live)
    assert 'STALE' in output['values']['chart-fib-status']['text']
    assert '1m Fib 0.618' not in output['values']['chart']['html']
    assert 'Golden Pocket' not in output['values']['chart']['html']


def test_incremental_fib_cache_merges_frames_and_survives_later_omission(tmp_path):
    from datetime import timedelta

    live = incremental_live_fixture()
    old_at = datetime.now(UTC)-timedelta(seconds=5)
    live['live']['active_fibonacci'] = {
        '1m': active_fib_fixture('1m', 'INSUFFICIENT_DATA', evaluated_at=old_at.isoformat()),
        '5m': active_fib_fixture('5m', 'NO_SWING', evaluated_at=old_at.isoformat()),
    }
    delta = incremental_delta(live)
    current = active_fib_fixture('1m', evaluated_at=datetime.now(UTC).isoformat())
    delta['live']['active_fibonacci'] = {'1m': current}
    omitted = incremental_delta(live)
    output = run_ui(tmp_path, live=live, action='incremental', updates=[delta, omitted],
                    inputs={'update-count': 2})
    assert output['cachedLive']['live']['active_fibonacci'] == {
        '1m': current, '5m': live['live']['active_fibonacci']['5m'],
    }
    assert output['values']['chart-fib-status']['text'] == '1m Fib · AVAILABLE'


def test_incremental_fib_version_is_guarded_per_frame_independent_of_shared_analysis_at(tmp_path):
    from datetime import timedelta

    live = incremental_live_fixture()
    newer = datetime.now(UTC)-timedelta(seconds=2)
    older = newer-timedelta(seconds=10)
    retained = active_fib_fixture('1m', evaluated_at=newer.isoformat())
    live['live']['active_fibonacci'] = {'1m': retained}
    delta = incremental_delta(live)
    delta['live']['analysis_at'] = datetime.now(UTC).isoformat()
    delta['live']['active_fibonacci'] = {'1m': active_fib_fixture('1m', 'NO_SWING', evaluated_at=older.isoformat())}
    output = run_ui(tmp_path, live=live, action='incremental', updates=[delta])
    assert output['cachedLive']['live']['active_fibonacci']['1m'] == retained
    assert output['values']['chart-fib-status']['text'] == '1m Fib · AVAILABLE'


def test_public_native_fib_payload_overrides_shared_analysis_and_caches_each_selected_frame(tmp_path):
    from test_cockpit_browser import public_native_fixtures, run_cockpit

    _, routes = public_native_fixtures()
    views = {'1m': active_fib_fixture('1m'), '5m': active_fib_fixture('5m', 'INSUFFICIENT_DATA')}
    for frame, view in views.items():
        payload = routes[f'/api/market/assets/BTCUSDT?timeframe={frame}']
        payload['active_fibonacci'] = {frame: view}
        # A shared cached research result must not mask the frame-specific view.
        payload['analysis']['active_fibonacci'] = {'1m': active_fib_fixture('1m', 'NO_SWING')}
    output = run_cockpit(tmp_path, {'assets': [], 'watchlist': []}, {'feed_fresh': False},
                         extra_routes=routes, required_token='fixture-private-access-code',
                         actions=[{'type': 'frame', 'frame': frame} for frame in ['1m', '5m', '1m']],
                         capture_contract=True)
    for frame, snapshot in zip(['1m', '1m', '5m', '1m'], output['snapshots'], strict=True):
        assert snapshot['values']['chart-fib-status']['text'] == frame+' Fib · '+views[frame]['status']
        assert snapshot['state']['live']['live']['active_fibonacci'][frame] == views[frame]
    cache = output['state']['live']['live']['active_fibonacci']
    assert cache['1m'] == views['1m'] and cache['5m'] == views['5m']


def test_latest_fib_does_not_appear_as_known_inside_an_older_chart_viewport(tmp_path):
    from datetime import timedelta

    live = incremental_live_fixture()
    latest = live['live']['closed_candles']['1m'][-1]
    source_at = (datetime.fromisoformat(latest['open_time'])+timedelta(minutes=1)).isoformat()
    live['live']['active_fibonacci'] = {'1m': active_fib_fixture('1m', source_timestamp=source_at)}
    panned = run_ui(tmp_path, live=live, action='pan')
    assert '1m Fib 0.618' not in panned['values']['chart']['html']
    assert 'Golden Pocket' not in panned['values']['chart']['html']
    assert 'history' in panned['values']['chart-fib-status']['text'].lower()
    latest_view = run_ui(tmp_path, live=live, action='pan-reset')
    assert latest_view['values']['chart-fib-status']['text'] == '1m Fib · AVAILABLE'
    assert '1m Fib 0.618' in latest_view['values']['chart']['html']


@pytest.mark.parametrize('reset', [False, True])
def test_history_cap_keeps_a_continuous_older_archive_until_reset_returns_to_live(tmp_path, reset):
    from datetime import timedelta

    live, first_page = paginated_history_fixture()
    first = first_page['candles'][0]
    before = datetime.fromisoformat(live['live']['closed_candles']['1m'][0]['open_time'])
    older = [dict(first, open_time=(before-timedelta(minutes=10500-index)).isoformat())
             for index in range(10500)]
    pages = []
    for index in range(21):
        end = len(older)-500*index
        candles = older[end-500:end]
        pages.append({**first_page, 'candles': candles, 'has_more': index < 20,
                      'next_before': candles[0]['open_time']})
    delta = incremental_delta(live)
    action = 'history-cap-reset' if reset else 'history-cap'
    output = run_ui(tmp_path, live=live, action=action, history_pages=pages, updates=[delta])
    assert len(history_requests(output)) == 21
    archive = cached_chart_history(output)
    hot = output['cachedLive']['live']['closed_candles']['1m']
    assert len(hot) == 240 and hot[-1] == delta['live']['closed_candles']['1m'][-1]
    assert output['values']['coin-price']['text'] == '103.50'
    if reset:
        assert archive == hot
        chart = output['values']['chart']['dataset']
        assert chart['offset'] == '0'
        assert chart['lastTimestamp'] == delta['live']['open_candles']['1m']['open_time']
        assert output['values']['chart-window-note']['text'] == 'Latest candles'
    else:
        assert archive == older[:10000]
        assert all(datetime.fromisoformat(right['open_time'])-datetime.fromisoformat(left['open_time']) == timedelta(minutes=1)
                   for left, right in zip(archive, archive[1:]))
        assert delta['live']['closed_candles']['1m'][-1] not in archive
        assert output['values']['chart']['dataset']['lastTimestamp'] <= archive[-1]['open_time']


def test_native_timeframe_switch_can_progress_while_previous_frame_request_is_pending(tmp_path):
    from datetime import timedelta

    from test_cockpit_browser import public_native_fixtures, run_cockpit

    _, routes = public_native_fixtures()
    delayed_path = '/api/market/assets/BTCUSDT?timeframe=5m'
    delayed = routes[delayed_path]
    delayed['market']['price'] = 990
    delayed['active_fibonacci'] = {'5m': active_fib_fixture('5m')}
    routes[delayed_path] = {'__status': 200, '__body': delayed, '__delay_ms': 90}
    selected_path = '/api/market/assets/BTCUSDT?timeframe=15m'
    selected = routes[selected_path]
    selected['market']['price'] = 103.5
    selected['active_fibonacci'] = {'15m': active_fib_fixture('15m', 'NO_SWING')}
    # Both detailed observations are newer than the broad native status row.
    received = datetime.now(UTC).isoformat()
    previous = (datetime.fromisoformat(received)-timedelta(seconds=1)).isoformat()
    for row in routes['/api/market/status']['assets']:
        if row['symbol'] == 'BTCUSDT':
            row['received_at'] = previous
            row['price_evidence']['exchange_timestamp'] = previous
    initial = routes['/api/market/assets/BTCUSDT?timeframe=1m']
    for key in ['price_source_timestamp', 'price_received_timestamp', 'last_received_timestamp']:
        initial['market'][key] = previous
    for detail in [delayed, selected]:
        for key in ['price_source_timestamp', 'price_received_timestamp', 'last_received_timestamp']:
            detail['market'][key] = received
    output = run_cockpit(tmp_path, {'assets': [], 'watchlist': []}, {'feed_fresh': False},
                         extra_routes=routes, required_token='fixture-private-access-code',
                         actions=[{'type': 'frame', 'frame': frame} for frame in ['5m', '15m', '15m']],
                         capture_contract=True)
    events = output['events']
    new_fetch = next(index for index, event in enumerate(events)
                     if event['kind'] == 'fetch' and event['path'] == selected_path)
    old_response = next(index for index, event in enumerate(events)
                        if event['kind'] == 'response' and event['path'] == delayed_path)
    assert new_fetch < old_response
    assert output['state']['frame'] == '15m'
    assert output['values']['coin-price']['text'] == '103.50'
    assert output['values']['chart-fib-status']['text'] == '15m Fib · NO_SWING'
    assert output['state']['live']['live']['active_fibonacci']['15m'] == selected['active_fibonacci']['15m']
    assert '5m' not in output['state']['live']['live']['closed_candles']


@pytest.mark.parametrize('frame', ['1m', '5m', '15m', '1h', '4h', '1D'])
def test_public_native_latest_fib_extensions_and_pocket_remain_visible_after_rest_receipt(tmp_path, frame):
    from test_cockpit_browser import public_native_fixtures, run_cockpit

    _, routes = public_native_fixtures()
    payload = routes[f'/api/market/assets/BTCUSDT?timeframe={frame}']
    received = datetime.now(UTC).isoformat()
    payload['market']['open_candles'] = {}
    assert datetime.fromisoformat(payload['candles'][-1]['close_time']) < datetime.fromisoformat(received)
    view = active_fib_fixture(frame, source_timestamp=received, evaluated_at=received,
                              levels=[{'timeframe': frame, 'ratio': ratio, 'price': price}
                                      for ratio, price in [(1.272, 400), (1.414, 600), (1.618, 1000)]],
                              golden_pocket={'timeframe': frame, 'lower_price': 250, 'upper_price': 260})
    payload['active_fibonacci'] = {frame: view}
    output = run_cockpit(tmp_path, {'assets': [], 'watchlist': []}, {'feed_fresh': False},
                         extra_routes=routes, required_token='fixture-private-access-code',
                         actions=[{'type': 'frame', 'frame': frame}], capture_contract=True)
    assert output['values']['chart-fib-status']['text'] == frame+' Fib · AVAILABLE'
    chart = output['values']['chart']['html']
    for ratio in [1.272, 1.414, 1.618]:
        assert frame+' Fib '+str(ratio) in chart
    assert 'Golden Pocket' in chart
    assert output['state']['live']['live']['open_candles'] == {}
