"""Real Chromium contracts for the candidate UI with synthetic test fixtures.

No exchange data, notification credentials, or application server is used. Every
browser request is intercepted; fixture callbacks record local UI effects.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

try:
    import playwright.sync_api as playwright_api
except ImportError:
    playwright_api = None

MODULE = Path(__file__).parents[1] / "src/expansion_hunter/static/candidate_ui.js"
CHROMIUM = shutil.which("chromium") or shutil.which("chromium-browser")
NODE = shutil.which("node")

TEST_DOCUMENT = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body { margin: 0; padding: 16px; font: 14px sans-serif; }
input { display: block; margin: 8px 0; }
.watchlist-item { min-height: 72px; margin: 10px 0; border: 1px solid #888; }
#page-spacer { height: 1800px; }
</style></head><body>
<input id="global-search" aria-label="Search candidates">
<input id="ordinary-input" aria-label="Editable value">
<div id="watchlist-items">
 <div class="watchlist-item" data-symbol="SYNTHUSDT" tabindex="0">Synthetic first candidate</div>
 <div class="watchlist-item" data-symbol="OTHERUSDT" tabindex="0">Synthetic second candidate</div>
</div>
<table><tbody id="discovery-body"></tbody></table>
<div id="alerts-list"></div><div id="page-spacer"></div>
</body></html>"""


@pytest.fixture(scope="module")
def browser():
    if playwright_api is None or CHROMIUM is None:
        pytest.skip("Playwright and system Chromium required for browser contracts")
    with playwright_api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(
                executable_path=CHROMIUM, headless=True, args=["--no-sandbox"]
            )
        except playwright_api.Error as error:
            if "setsockopt: Operation not permitted" in str(error):
                pytest.skip("Chromium launch blocked by execution sandbox: setsockopt is not permitted")
            raise
        yield browser
        browser.close()


@pytest.fixture
def ui(browser):
    context = browser.new_context(
        viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True, service_workers="block"
    )
    page = context.new_page()
    requests = []
    errors = []

    def route(request):
        if request.request.is_navigation_request() and request.request.url == "http://candidate-contract.test/":
            request.fulfill(status=200, content_type="text/html", body=TEST_DOCUMENT)
        else:
            requests.append(request.request.url)
            request.abort()

    page.route("**/*", route)
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto("http://candidate-contract.test/")
    page.evaluate("""() => {
        window.__networkCalls = [];
        window.fetch = (...args) => {
            window.__networkCalls.push(['fetch', ...args]);
            throw new Error('Candidate contract forbids network requests');
        };
        XMLHttpRequest.prototype.open = function(...args) {
            window.__networkCalls.push(['xhr', ...args]);
            throw new Error('Candidate contract forbids XMLHttpRequest');
        };
        window.WebSocket = class {
            constructor(...args) {
                window.__networkCalls.push(['websocket', ...args]);
                throw new Error('Candidate contract forbids WebSockets');
            }
        };
        window.__fixture = {candidate: {asset: {exchange_symbol: 'SYNTHUSDT'}}, bars: [], alerts: []};
        window.__effects = {selected: [], charts: [], directions: [], filters: [], preferences: [], muted: []};
    }""")
    page.add_script_tag(path=str(MODULE))
    page.evaluate("""() => {
        window.__mount = () => {
            window.__ui = window.ExpansionHunterUI.mount({
                onSelect: symbol => window.__effects.selected.push(symbol),
                onOpenChart: symbol => window.__effects.charts.push(symbol),
                getCandidate: () => window.__fixture.candidate,
                getRows: () => ['SYNTHUSDT', 'OTHERUSDT'],
                getMode: () => 'pro',
                getBars: () => window.__fixture.bars,
                getFrame: () => '5m',
                getBTCContext: () => null,
                getFreshness: () => null,
                onDirectionFilter: value => window.__effects.directions.push(value),
                getAlerts: () => window.__fixture.alerts,
                onAlertFilters: value => window.__effects.filters.push(value),
                onNotificationPreferences: value => window.__effects.preferences.push(value),
                onMuteChange: (...value) => window.__effects.muted.push(value)
            });
        };
        window.__mount();
    }""")
    yield page
    network = page.evaluate("window.__networkCalls")
    context.close()
    assert not requests, f"Unexpected browser requests: {requests}"
    assert not network, f"Unexpected network calls: {network}"
    assert not errors, f"Browser errors: {errors}"


def open_candidate(page, candidate=None, bars=None):
    if candidate is not None:
        page.evaluate("candidate => { window.__fixture.candidate = candidate; }", candidate)
    if bars is not None:
        page.evaluate("bars => { window.__fixture.bars = bars; }", bars)
    page.evaluate("""() => {
        const trigger = document.querySelector('#watchlist-items [data-symbol="SYNTHUSDT"]');
        trigger.focus();
        return window.__ui.open('SYNTHUSDT', trigger);
    }""")
    playwright_api.expect(page.locator("#candidate-sheet")).to_be_visible()


def touch_gesture(page, start, finish):
    """Dispatch a genuine Chromium touch gesture, including its native scroll behavior."""
    session = page.context.new_cdp_session(page)
    session.send("Input.dispatchTouchEvent", {
        "type": "touchStart", "touchPoints": [{"x": start[0], "y": start[1]}]
    })
    for fraction in (0.2, 0.4, 0.6, 0.8, 1):
        session.send("Input.dispatchTouchEvent", {
            "type": "touchMove", "touchPoints": [{
                "x": start[0] + (finish[0] - start[0]) * fraction,
                "y": start[1] + (finish[1] - start[1]) * fraction,
            }],
        })
    session.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    session.detach()


def test_missing_values_zero_real_histories_chart_and_escaped_labels(ui):
    open_candidate(ui)
    histories = ui.locator(".eh-history-values")
    assert histories.all_text_contents() == ["UNAVAILABLE", "UNAVAILABLE"]
    assert ui.locator(".eh-history-grid svg").count() == 0
    assert ui.locator(".eh-mini-chart").count() == 0
    summary = ui.locator(".eh-summary-grid dd")
    assert summary.all_text_contents()[:7] == ["UNAVAILABLE"] * 7
    playwright_api.expect(ui.locator("#candidate-content")).to_contain_text("Mini chart UNAVAILABLE")

    malicious = '<img src=x onerror="window.bad=1"> arbitrary label <script>window.bad=2</script>'
    candidate = {
        "asset": {"exchange_symbol": "SYNTHUSDT", "state": malicious},
        "pre": {"pre_ignition_score": 0},
        "decision": {
            "setup_quality": {"value": 0}, "entry_quality": {"value": 0},
            "reasons": [malicious], "primary_path": {"steps": [{"condition": malicious}]},
        },
        "live": {"order_flow": {malicious: 0}},
    }
    open_candidate(ui, candidate, [{"close": 100}, {"close": None}, {"close": "invalid"}, {"close": 103}])
    playwright_api.expect(ui.locator("#candidate-heading-meta")).to_contain_text("Score 0")
    assert ui.locator(".eh-summary-grid dd").all_text_contents()[:2] == ["0/100", "0/100"]
    assert ui.locator(".eh-history-values").all_text_contents() == ["UNAVAILABLE", "UNAVAILABLE"]
    assert ui.locator(".eh-history-grid svg").count() == 0
    assert len(ui.locator(".eh-mini-chart polyline").get_attribute("points").split()) == 2
    assert ui.locator(".eh-mini-chart line").count() == 0
    assert ui.locator("#candidate-content img, #candidate-content script, #candidate-heading-meta img").count() == 0
    assert ui.evaluate("window.bad") is None
    playwright_api.expect(ui.locator(".eh-path").first).to_contain_text(malicious)
    playwright_api.expect(ui.locator(".eh-pro-details")).not_to_have_attribute("open", "")
    ui.locator(".eh-pro-details summary").click()
    playwright_api.expect(ui.locator(".eh-pro-details")).to_contain_text("arbitrary label")

    candidate.update(score_history=[0, 2], rank_history=[3, 1])
    candidate["decision"].update(trigger=105, invalidation=90)
    open_candidate(ui, candidate)
    assert ui.locator(".eh-history-values").all_text_contents() == ["0 → 2", "#3 → #1"]
    assert ui.locator(".eh-history-grid polyline").count() == 2
    assert ui.locator(".eh-mini-chart line").count() == 2


def test_native_dialog_full_height_escape_and_open_chart(ui):
    open_candidate(ui)
    sheet = ui.get_by_role("dialog", name=re.compile("SYNTHUSDT"))
    assert sheet.evaluate("node => node.tagName === 'DIALOG' && node.matches(':modal')")
    assert sheet.get_attribute("aria-labelledby")
    before = sheet.bounding_box()
    ui.locator("#candidate-expand").click()
    ui.wait_for_function("document.querySelector('#candidate-sheet').getBoundingClientRect().height >= innerHeight - 20")
    assert sheet.bounding_box()["height"] >= before["height"]
    ui.keyboard.press("Escape")
    playwright_api.expect(sheet).not_to_be_visible()
    playwright_api.expect(ui.locator('#watchlist-items [data-symbol="SYNTHUSDT"]')).to_be_focused()

    open_candidate(ui)
    ui.locator("#candidate-open-chart").click()
    assert ui.evaluate("window.__effects.charts") == ["SYNTHUSDT"]


def test_watchlist_touch_actions_scroll_and_keyboard_guards(ui):
    ui.locator("#ordinary-input").focus()
    ui.keyboard.press("/")
    ui.keyboard.press("ArrowDown")
    playwright_api.expect(ui.locator("#ordinary-input")).to_have_value("/")
    playwright_api.expect(ui.locator("#ordinary-input")).to_be_focused()
    assert ui.evaluate("window.__effects.selected") == []

    ui.locator("#ordinary-input").evaluate("node => node.blur()")
    ui.keyboard.press("/")
    playwright_api.expect(ui.locator("#global-search")).to_be_focused()
    open_candidate(ui)
    ui.locator("#candidate-close").focus()
    ui.keyboard.press("/")
    ui.keyboard.press("ArrowDown")
    playwright_api.expect(ui.locator("#candidate-close")).to_be_focused()
    assert ui.evaluate("window.__effects.selected") == ["SYNTHUSDT"]
    ui.keyboard.press("Escape")

    ui.locator('#watchlist-items [data-symbol="SYNTHUSDT"]').evaluate("node => node.blur()")
    ui.keyboard.press("ArrowDown")
    ui.keyboard.press("ArrowDown")
    assert ui.evaluate("window.__effects.selected") == ["SYNTHUSDT", "SYNTHUSDT", "OTHERUSDT"]

    ui.evaluate("window.__ui.enhanceWatchlist()")
    row = ui.locator('#watchlist-items [data-symbol="SYNTHUSDT"]')
    actions = row.locator(".eh-row-actions")
    playwright_api.expect(actions).not_to_be_visible()
    rect = row.bounding_box()
    y = rect["y"] + rect["height"] / 2
    touch_gesture(ui, (310, y), (75, y + 2))
    playwright_api.expect(actions).to_be_visible()
    row.locator("[data-local-mute]").click()
    assert ui.evaluate("window.__ui.isMuted('SYNTHUSDT')") is True
    assert ui.evaluate("window.__effects.muted.length") == 1
    row.locator("[data-open-chart]").click()
    assert ui.evaluate("window.__effects.charts") == ["SYNTHUSDT"]

    second = ui.locator('#watchlist-items [data-symbol="OTHERUSDT"]')
    second.scroll_into_view_if_needed()
    ui.evaluate("window.scrollTo(0, 0)")
    rect = second.bounding_box()
    y = rect["y"] + rect["height"] / 2
    touch_gesture(ui, (180, y), (183, y - 140))
    ui.wait_for_function("window.scrollY > 20")
    playwright_api.expect(second.locator(".eh-row-actions")).not_to_be_visible()


def test_alert_filters_confirmed_delivery_and_tab_notification_preferences(ui):
    alerts = [
        {"event_id": "synthetic-ready", "symbol": "SYNTHUSDT", "side": "LONG", "state": "READY",
         "alert_delivered_timestamp": "2020-01-01T12:00:00Z"},
        {"event_id": "synthetic-trigger", "symbol": "OTHERUSDT", "bias": "BEARISH", "state": "TRIGGER",
         "alert_delivered_timestamp": "not-a-date"},
        {"event_id": "synthetic-created", "symbol": "THIRDUSDT", "direction": "LONG", "event_type": "BREAKOUT",
         "alert_created_timestamp": "2020-01-01T12:00:00Z"},
        {"event_id": "synthetic-failed", "symbol": "NEUTRALUSDT", "state": "INVALIDATED"},
    ]
    ui.evaluate("alerts => {window.__fixture.alerts = alerts;}", alerts)
    ui.locator("#eh-alert-direction").select_option("LONG")
    ui.locator("#eh-alert-state").select_option("READY")
    assert ui.evaluate("window.__ui.filterAlerts(window.__fixture.alerts).map(a => a.event_id)") == ["synthetic-ready"]
    ui.locator("#eh-alert-direction").select_option("SHORT")
    ui.locator("#eh-alert-state").select_option("TRIGGER")
    assert ui.evaluate("window.__ui.filterAlerts(window.__fixture.alerts).map(a => a.event_id)") == ["synthetic-trigger"]
    assert ui.evaluate("window.__effects.filters.at(-1)") == {"direction": "SHORT", "state": "TRIGGER"}

    statuses = ui.evaluate("window.__fixture.alerts.map(a => window.__ui.deliveryStatus(a))")
    assert statuses[0].startswith("Delivered ")
    assert statuses[1:] == ["Delivery not confirmed"] * 3
    for delivered in (None, "", False, 0, "0", "invalid timestamp", "2999-01-01T00:00:00Z"):
        assert ui.evaluate("value => window.__ui.deliveryStatus({alert_delivered_timestamp:value})", delivered) == "Delivery not confirmed"

    ui.locator(".eh-notification-prefs summary").click()
    ui.locator("#eh-notify-ready").uncheck()
    ui.locator("#eh-notify-cooldown").fill("600")
    ui.locator("#eh-notify-cooldown").blur()
    assert ui.evaluate("window.__ui.shouldNotify(window.__fixture.alerts[0])") is False
    assert ui.evaluate("window.__ui.shouldNotify(window.__fixture.alerts[1])") is True
    assert ui.evaluate("window.__ui.shouldNotify(window.__fixture.alerts[1])") is False
    ui.locator("#eh-notify-trigger").uncheck()
    assert ui.evaluate("window.__ui.shouldNotify(window.__fixture.alerts[2])") is False
    ui.evaluate("window.__ui.setMuted('SYNTHUSDT', true)")
    assert ui.evaluate("window.__ui.isMuted('SYNTHUSDT')") is True
    assert ui.evaluate("window.__effects.preferences.at(-1)") == {"ready": False, "trigger": False, "cooldown": 600}

    ui.evaluate("() => {window.__ui.destroy(); window.__mount();}")
    playwright_api.expect(ui.locator("#eh-notify-ready")).not_to_be_checked()
    playwright_api.expect(ui.locator("#eh-notify-trigger")).not_to_be_checked()
    playwright_api.expect(ui.locator("#eh-notify-cooldown")).to_have_value("600")
    assert ui.evaluate("window.__ui.isMuted('SYNTHUSDT')") is True
    ui.locator(".eh-notification-prefs summary").click()
    ui.locator("#eh-notify-ready").check()
    ui.locator("#eh-notify-trigger").check()
    ui.locator("#eh-notify-cooldown").fill("0")
    ui.locator("#eh-notify-cooldown").blur()
    assert ui.evaluate("window.__ui.shouldNotify(window.__fixture.alerts[0])") is False
    ui.evaluate("window.__ui.setMuted('SYNTHUSDT', false)")
    assert ui.evaluate("window.__ui.shouldNotify(window.__fixture.alerts[0])") is True
    assert ui.evaluate("window.__ui.shouldNotify(window.__fixture.alerts[0])") is True

    # A new same-origin tab shares local mute storage, with fresh notification
    # preferences. This is deliberately a browser storage contract.
    ui.evaluate("window.__ui.setMuted('SYNTHUSDT', true)")
    ui.locator("#eh-notify-ready").uncheck()
    tab = ui.context.new_page()
    tab_requests = []

    def tab_route(request):
        if request.request.is_navigation_request() and request.request.url == "http://candidate-contract.test/":
            request.fulfill(status=200, content_type="text/html", body=TEST_DOCUMENT)
        else:
            tab_requests.append(request.request.url)
            request.abort()

    try:
        tab.route("**/*", tab_route)
        tab.goto("http://candidate-contract.test/")
        tab.add_script_tag(path=str(MODULE))
        tab.evaluate("window.__ui = window.ExpansionHunterUI.mount({})")
        playwright_api.expect(tab.locator("#eh-notify-ready")).to_be_checked()
        playwright_api.expect(tab.locator("#eh-notify-trigger")).to_be_checked()
        playwright_api.expect(tab.locator("#eh-notify-cooldown")).to_have_value("60")
        assert tab.evaluate("window.__ui.isMuted('SYNTHUSDT')") is True
    finally:
        tab.close()
    assert not tab_requests, f"Unexpected second-tab requests: {tab_requests}"


@pytest.mark.skipif(NODE is None, reason="Node required for executable UI contracts")
def test_candidate_contracts_in_node_without_browser_or_network():
    """Fallback checks real module output; it makes no browser layout claims."""
    program = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const elements = new Map();
class Element {
  constructor(tag='div') {
    this.tagName=tag.toUpperCase(); this.listeners=new Map(); this.children=[];
    this.style={}; this.dataset={}; this.attributes={}; this.scrollTop=0;
    this.isConnected=true; this.parentNode=null; this.open=false; this.value='';
    const classes=new Set();
    this.classList={add(name){classes.add(name)},remove(name){classes.delete(name)},
      toggle(name,force){const enabled=force??!classes.has(name);if(enabled)classes.add(name);else classes.delete(name);return enabled}};
  }
  set id(value) {this._id=value;elements.set(value,this)}
  get id() {return this._id}
  set innerHTML(value) {
    this.html=value;
    for(const [,id] of value.matchAll(/\bid="([^"]+)"/g)) {
      if(!elements.has(id)){const node=new Element();node.id=id;node.parentNode=this;this.children.push(node)}
    }
  }
  get innerHTML() {return this.html??''}
  setAttribute(key,value){this.attributes[key]=String(value)}
  addEventListener(type,handler){if(!this.listeners.has(type))this.listeners.set(type,[]);this.listeners.get(type).push(handler)}
  removeEventListener(type,handler){this.listeners.set(type,(this.listeners.get(type)??[]).filter(item=>item!==handler))}
  dispatch(type){for(const handler of this.listeners.get(type)??[])handler({target:this,preventDefault(){}})}
  appendChild(node){node.parentNode=this;this.children.push(node);return node}
  insertBefore(node){return this.appendChild(node)}
  insertAdjacentElement(position,node){this.parentNode.appendChild(node)}
  querySelector(){return null}
  querySelectorAll(){return []}
  focus(){doc.activeElement=this}
  showModal(){this.open=true}
  close(){this.open=false;this.dispatch('close')}
  remove(){this.isConnected=false;if(this.id)elements.delete(this.id)}
}
const doc=new Element('document');doc.body=new Element('body');doc.head=new Element('head');
doc.activeElement=doc.body;doc.createElement=tag=>new Element(tag);
doc.getElementById=id=>elements.get(id)??null;
for(const id of ['watchlist-items','alerts-list','global-search','discovery-body']) {
  const node=new Element();node.id=id;doc.body.appendChild(node);
}
const memory=()=>{const values=new Map();return {getItem:key=>values.get(key)??null,setItem:(key,value)=>values.set(key,String(value))}};
const network=[];const forbidden=(...args)=>{network.push(args);throw Error('Network forbidden in test')};
const global={document:doc,localStorage:memory(),sessionStorage:memory(),fetch:forbidden,WebSocket:forbidden};
vm.runInNewContext(SOURCE,{window:global,document:doc,Date,Map,Set,Number,String,Math,console,fetch:forbidden});
let candidate={asset:{exchange_symbol:'SYNTHUSDT'}},bars=[];
const effects={filters:[],preferences:[],muted:[]};
const mount=()=>global.ExpansionHunterUI.mount({
  getCandidate:()=>candidate,getBars:()=>bars,getFrame:()=> '5m',getMode:()=> 'pro',
  onAlertFilters:value=>effects.filters.push(value),
  onNotificationPreferences:value=>effects.preferences.push(value),
  onMuteChange:(...value)=>effects.muted.push(value)
});
let ui=mount();ui.open('SYNTHUSDT');
const html=()=>doc.getElementById('candidate-content').innerHTML;
const fact=name=>html().match(new RegExp('<dt>'+name+'</dt><dd>(.*?)</dd>'))?.[1];
assert.equal((html().match(/class="eh-history-values">UNAVAILABLE/g)??[]).length,2);
assert.equal((html().match(/<polyline/g)??[]).length,0);
for(const name of ['Setup quality','Entry quality','Trigger','Invalidation','Targets','Move consumed','BTC context'])assert.equal(fact(name),'UNAVAILABLE');
const malicious='<img src=x onerror="window.bad=1">';
candidate={asset:{state:malicious},pre:{pre_ignition_score:0,trigger:600,invalidation:500},
  decision:{setup_quality:{value:0},entry_quality:{value:0},trigger:null,invalidation:null,target_1:null,
    reasons:[malicious],primary_path:{steps:[{condition:malicious}]}},
  war_room:{expected_path:{primary:'bull'},scenarios:{bull:{trigger:700,invalidation:400,targets:[800]}}}};
bars=[{close:100},{close:null},{close:'invalid'},{close:103}];ui.refresh();
assert.match(doc.getElementById('candidate-heading-meta').textContent,/Score 0$/);
assert.equal(fact('Setup quality'),'0/100');assert.equal(fact('Entry quality'),'0/100');
for(const name of ['Trigger','Invalidation','Targets'])assert.equal(fact(name),'UNAVAILABLE');
assert.equal((html().match(/class="eh-history-values">UNAVAILABLE/g)??[]).length,2);
assert.equal((html().match(/<line /g)??[]).length,0);
assert.equal(html().match(/<polyline points="([^"]+)"/)[1].split(' ').length,2);
assert.ok(!html().includes(malicious));assert.ok(html().includes('&lt;img'));
candidate.score_history=[0,2];candidate.rank_history=[3,1];candidate.decision.trigger=105;candidate.decision.invalidation=90;ui.refresh();
assert.ok(html().includes('0 → 2'));assert.ok(html().includes('#3 → #1'));
assert.equal((html().match(/<line /g)??[]).length,2);
for(const value of [undefined,null,'',false,0,'0','not-a-date',new Date(Date.now()+86400000).toISOString()]) {
  assert.equal(ui.deliveryStatus({alert_delivered_timestamp:value}),'Delivery not confirmed');
}
assert.equal(ui.deliveryStatus({alert_created_timestamp:'2020-01-01T00:00:00Z'}),'Delivery not confirmed');
assert.match(ui.deliveryStatus({alert_delivered_timestamp:'2020-01-01T00:00:00Z'}),/^Delivered /);
const alerts=[{symbol:'SYNTHUSDT',side:'LONG',state:'READY'},{symbol:'OTHERUSDT',bias:'BEARISH',state:'TRIGGER'}];
doc.getElementById('eh-alert-direction').value='SHORT';doc.getElementById('eh-alert-direction').dispatch('change');
doc.getElementById('eh-alert-state').value='TRIGGER';doc.getElementById('eh-alert-state').dispatch('change');
assert.equal(ui.filterAlerts(alerts).length,1);assert.equal(ui.filterAlerts(alerts)[0].symbol,'OTHERUSDT');
const ready=doc.getElementById('eh-notify-ready');ready.checked=false;ready.dispatch('change');
assert.equal(ui.shouldNotify(alerts[0]),false);assert.equal(ui.shouldNotify(alerts[1]),true);assert.equal(ui.shouldNotify(alerts[1]),false);
ui.setMuted('OTHERUSDT',true);assert.equal(ui.isMuted('OTHERUSDT'),true);
assert.equal(ui.shouldNotify(alerts[1]),false);assert.equal(effects.muted.length,1);
ui.destroy();ui=mount();assert.equal(doc.getElementById('eh-notify-ready').checked,false);assert.equal(ui.isMuted('OTHERUSDT'),true);
assert.equal(network.length,0);
process.stdout.write(JSON.stringify({status:'PASS',missing:'UNAVAILABLE',zeroRetained:true,noFakeHistory:true,noNetwork:true}));
"""
    completed = subprocess.run(
        [NODE, "-e", "const SOURCE=" + json.dumps(MODULE.read_text()) + ";\n" + program],
        check=True, text=True, capture_output=True, timeout=10,
    )
    assert json.loads(completed.stdout)["status"] == "PASS"
