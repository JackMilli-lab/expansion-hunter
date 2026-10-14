"""Execute the shared chart module through its public browser and chart APIs.

The DOM and Lightweight Charts doubles record calls instead of drawing pixels.
These contracts exercise the production module without exposing its internals.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ENGINE = Path(__file__).parents[1] / "src/expansion_hunter/static/chart_engine.js"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node required for executable chart contracts")


HARNESS = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const NOW = Date.parse('2026-10-14T12:00:00Z');
class TestDate extends Date {
  constructor(...values) { super(...(values.length ? values : [NOW])); }
  static now() { return NOW; }
}
const plain = value => JSON.parse(JSON.stringify(value));
const charts = [], resizeObservers = [], timers = new Map();
let timerId = 0;
function schedule(callback) { const id = ++timerId; timers.set(id, callback); return id; }
function flushTimers() {
  let count = 0;
  while (timers.size) {
    assert.ok(++count < 100, 'chart schedules an unbounded timer loop');
    const pending = [...timers.entries()]; timers.clear();
    for (const [, callback] of pending) callback(NOW);
  }
}
async function settle() { await Promise.resolve(); flushTimers(); await Promise.resolve(); }
class Element {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase(); this.style = {}; this.dataset = {};
    this.children = []; this.attributes = {}; this.listeners = new Map();
    this.clientWidth = 900; this.clientHeight = 420; this.offsetWidth = 900;
    this.offsetHeight = 420; this.textContent = ''; this.innerHTML = '';
    this.className = ''; this.ownerDocument = document;
    this.classList = {
      add: (...names) => this.updateClasses(names, true),
      remove: (...names) => this.updateClasses(names, false),
      contains: name => this.className.split(/\s+/).includes(name),
      toggle: (name, force) => {
        const enabled = force ?? !this.classList.contains(name);
        this.updateClasses([name], enabled); return enabled;
      },
    };
  }
  updateClasses(names, enabled) {
    const values = new Set(this.className.split(/\s+/).filter(Boolean));
    for (const name of names) if (enabled) values.add(name); else values.delete(name);
    this.className = [...values].join(' ');
  }
  addEventListener(type, callback) {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type).add(callback);
  }
  removeEventListener(type, callback) { this.listeners.get(type)?.delete(callback); }
  dispatchEvent(event) {
    event.target ??= this; event.currentTarget = this;
    for (const callback of [...(this.listeners.get(event.type) ?? [])]) callback(event);
    return true;
  }
  appendChild(child) { this.children.push(child); child.parentNode = this; return child; }
  append(...children) { for (const child of children) this.appendChild(child); }
  removeChild(child) {
    this.children = this.children.filter(item => item !== child); child.parentNode = null;
    return child;
  }
  replaceChildren(...children) {
    for (const child of this.children) child.parentNode = null;
    this.children = []; this.append(...children);
  }
  remove() { this.parentNode?.removeChild(this); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  querySelectorAll(selector) {
    const matches = [];
    for (const child of this.children) {
      if (selector.startsWith('.') ? child.classList.contains(selector.slice(1)) :
          selector.startsWith('#') ? child.id === selector.slice(1) :
          child.tagName.toLowerCase() === selector.toLowerCase()) matches.push(child);
      matches.push(...child.querySelectorAll(selector));
    }
    return matches;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] ?? null; }
  contains(item) { return item === this || this.children.some(child => child.contains(item)); }
  getBoundingClientRect() {
    return {left:0, top:0, right:900, bottom:420, width:900, height:420, x:0, y:0};
  }
  setPointerCapture() {}
  releasePointerCapture() {}
  focus() {}
}
const document = {
  createElement: tag => new Element(tag), createElementNS: (_namespace, tag) => new Element(tag),
  listeners: new Map(),
  addEventListener(type, callback) { this.listeners.set(type, callback); },
  removeEventListener(type) { this.listeners.delete(type); },
  querySelector() { return null; }, querySelectorAll() { return []; },
};
document.body = new Element('body');
const container = new Element('div'); document.body.appendChild(container);
class Series {
  constructor(chart, kind, options) {
    this.chart = chart; this.kind = kind; this.settings = {...(options ?? {})};
    this.data = []; this.writes = []; this.updates = []; this.priceLines = []; this.removedPriceLines = [];
  }
  setData(rows) {
    let previous = -Infinity;
    for (const row of rows) {
      assert.ok(Number.isFinite(row.time), 'chart must use native UNIX-second times');
      assert.ok(row.time > previous, 'Lightweight Charts data must be ordered and unique');
      previous = row.time;
      for (const key of this.kind === 'candles' ? ['open','high','low','close'] : ['value'])
        assert.ok(Number.isFinite(row[key]), 'series contains a non-finite '+key);
    }
    this.data = plain(rows); this.writes.push(plain(rows));
  }
  update(row) {
    assert.ok(Number.isFinite(row.time) && row.time >= (this.data.at(-1)?.time ?? -Infinity),
      'public series update accepts only the latest native candle or a new candle');
    for (const key of this.kind === 'candles' ? ['open','high','low','close'] : ['value'])
      assert.ok(Number.isFinite(row[key]), 'series update contains a non-finite '+key);
    const copied = plain(row);
    if (this.data.at(-1)?.time === row.time) this.data[this.data.length-1] = copied;
    else this.data.push(copied);
    this.updates.push(copied);
  }
  applyOptions(options) { Object.assign(this.settings, options); }
  options() { return plain(this.settings); }
  createPriceLine(options) {
    const line = {options:plain(options), applyOptions(values) { Object.assign(this.options, plain(values)); }};
    this.priceLines.push(line); return line;
  }
  removePriceLine(line) {
    this.priceLines = this.priceLines.filter(item => item !== line); this.removedPriceLines.push(line);
  }
  priceToCoordinate(price) { return Number.isFinite(price) ? 300 - (price - 90) * 5 : null; }
  coordinateToPrice(y) { return Number.isFinite(y) ? 90 + (300 - y) / 5 : null; }
  priceScale() { return this.chart.priceScale(this.settings.priceScaleId ?? 'right'); }
  barsInLogicalRange(range) {
    return {barsBefore:range.from, barsAfter:this.data.length - 1 - range.to,
      from:this.data[0]?.time, to:this.data.at(-1)?.time};
  }
}
function createChart(target, options) {
  const rangeSubscribers = new Set(), crosshairSubscribers = new Set(), clickSubscribers = new Set();
  const scales = new Map(); let range = null;
  const chart = {
    target, settings:plain(options ?? {}), series:[], removed:false, rangeWrites:[],
    rangeSubscribers, crosshairSubscribers, clickSubscribers,
    addCandlestickSeries(options) { return this.add('candles', options); },
    addHistogramSeries(options) { return this.add('volume', options); },
    addLineSeries(options) { return this.add('line', options); },
    add(kind, options) { const series = new Series(this, kind, options); this.series.push(series); return series; },
    removeSeries(series) { this.series = this.series.filter(item => item !== series); },
    applyOptions(options) { Object.assign(this.settings, plain(options)); },
    resize(width, height) { this.settings.width = width; this.settings.height = height; },
    priceScale(name = 'right') {
      if (!scales.has(name)) scales.set(name, {width:() => 72, applyOptions(options) { this.options = plain(options); }});
      return scales.get(name);
    },
    subscribeCrosshairMove(callback) { crosshairSubscribers.add(callback); },
    unsubscribeCrosshairMove(callback) { crosshairSubscribers.delete(callback); },
    subscribeClick(callback) { clickSubscribers.add(callback); },
    unsubscribeClick(callback) { clickSubscribers.delete(callback); },
    remove() { this.removed = true; rangeSubscribers.clear(); crosshairSubscribers.clear(); clickSubscribers.clear(); },
    emitRange(value) {
      range = value === null ? null : plain(value);
      for (const callback of [...rangeSubscribers]) callback(value);
    },
    emitCrosshair(event) { for (const callback of [...crosshairSubscribers]) callback(event); },
  };
  const timeScale = {
    getVisibleLogicalRange:() => range === null ? null : plain(range),
    setVisibleLogicalRange(value) { range = plain(value); chart.rangeWrites.push(plain(value)); },
    subscribeVisibleLogicalRangeChange(callback) { rangeSubscribers.add(callback); },
    unsubscribeVisibleLogicalRangeChange(callback) { rangeSubscribers.delete(callback); },
    fitContent() { this.setVisibleLogicalRange({from:0, to:Math.max(0, chart.series[0]?.data.length - 1)}); },
    scrollToRealTime() { this.fitContent(); },
    applyOptions(options) { this.options = plain(options); },
    width:() => 828,
    timeToCoordinate(time) {
      const index = chart.series[0]?.data.findIndex(item => item.time === time) ?? -1;
      return index < 0 ? null : this.logicalToCoordinate(index);
    },
    coordinateToTime(x) {
      const index = Math.round(this.coordinateToLogical(x));
      return chart.series[0]?.data[index]?.time ?? null;
    },
    logicalToCoordinate(index) { return (index - (range?.from ?? 0)) * 12; },
    coordinateToLogical(x) { return x / 12 + (range?.from ?? 0); },
    getVisibleRange() {
      return {from:chart.series[0]?.data[0]?.time, to:chart.series[0]?.data.at(-1)?.time};
    },
  };
  chart.timeScale = () => timeScale; charts.push(chart); return chart;
}
const LightweightCharts = {createChart, version:() => '4.2.3',
  CrosshairMode:{Normal:0, Magnet:1, Hidden:2},
  LineStyle:{Solid:0, Dotted:1, Dashed:2, LargeDashed:3, SparseDotted:4}};
const window = {document, LightweightCharts, devicePixelRatio:1, listeners:new Map(),
  addEventListener(type, callback) { this.listeners.set(type, callback); },
  removeEventListener(type) { this.listeners.delete(type); }};
const context = {
  window, document, LightweightCharts, console, Date:TestDate, Math, Number, String,
  Set, Map, WeakMap, WeakSet, Infinity, JSON, Array, Object, Element, HTMLElement:Element,
  performance:{now:() => NOW}, navigator:{maxTouchPoints:0},
  setTimeout:schedule, clearTimeout:id => timers.delete(id),
  requestAnimationFrame:schedule, cancelAnimationFrame:id => timers.delete(id),
  getComputedStyle:() => ({position:'relative', color:'#fff'}),
  ResizeObserver:class {
    constructor(callback) { this.callback = callback; this.disconnected = false; resizeObservers.push(this); }
    observe(target) { this.target = target; }
    unobserve() {}
    disconnect() { this.disconnected = true; }
  },
};
window.requestAnimationFrame = context.requestAnimationFrame;
window.cancelAnimationFrame = context.cancelAnimationFrame;
window.ResizeObserver = context.ResizeObserver;
vm.runInNewContext(source, context, {timeout:1000, filename:'chart_engine.js'});
const engine = window.ExpansionHunterChart;
assert.ok(engine, 'chart module must publish window.ExpansionHunterChart');
for (const name of ['render','dispose','formatTick','roundTick','buildManualFib','selectedFibLevels'])
  assert.equal(typeof engine[name], 'function', 'missing chart API '+name);
function bars(frame = '5m', count = 40) {
  const seconds = {'1m':60,'5m':300,'15m':900,'1h':3600,'4h':14400,'1D':86400}[frame];
  const end = Math.floor(NOW / 1000 / seconds) * seconds;
  return Array.from({length:count}, (_, index) => {
    const open = 100 + index / 10;
    return {timeframe:frame, open_time:new Date((end - (count-index)*seconds)*1000).toISOString(),
      close_time:new Date((end - (count-index-1)*seconds)*1000).toISOString(),
      open, high:open+2, low:open-1, close:open+0.5, volume:10+index, isOpen:false};
  });
}
function options(extra = {}) {
  return {container, bars:bars(), frame:'5m', symbol:'FIXTUREUSDT', currentPrice:104,
    levels:[], fibonacci:null, tickSize:0.01, mode:'rookie',
    layers:new Set(['volume','fib']), manual:{enabled:false, locked:false, anchors:[]}, ...extra};
}
function candleSeries(chart = charts.at(-1)) { return chart.series.find(item => item.kind === 'candles'); }
function volumeSeries(chart = charts.at(-1)) { return chart.series.find(item => item.kind === 'volume'); }
function anchors(first = 100, last = 120) {
  const native = bars();
  return [{time:Date.parse(native[10].open_time)/1000, price:first},
    {time:Date.parse(native[30].open_time)/1000, price:last}];
}
function level(fib, kind, ratio) {
  return fib.levels.find(item => item.kind === kind && Math.abs(item.ratio-ratio) < 1e-12);
}
function closeTo(actual, expected, message) {
  assert.ok(Math.abs(actual-expected) < 1e-9, (message ?? 'numeric mismatch')+': '+actual+' != '+expected);
}
"""


def run_engine(tmp_path: Path, action: str) -> dict:
    program = (
        "const source=" + json.dumps(ENGINE.read_text()) + ";\n" + HARNESS
        + "\n(async()=>{\n" + action + "\nawait settle();\n"
        + "process.stdout.write(JSON.stringify({passed:true,charts:charts.length}));\n"
        + "})().catch(error=>{console.error(error);process.exitCode=1;});\n"
    )
    target = tmp_path / "chart-engine-contract.js"
    target.write_text(program)
    result = subprocess.run([NODE, str(target)], capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads(result.stdout)


def test_public_api_handles_unavailable_library_and_invalid_native_bars(tmp_path):
    run_engine(tmp_path, r"""
const fallback = new Element('svg');
fallback.textContent = 'Existing native SVG fallback';
container.appendChild(fallback);
delete window.LightweightCharts; delete context.LightweightCharts;
assert.equal(engine.render(options()), null);
assert.equal(charts.length, 0);
assert.equal(fallback.parentNode, container, 'missing chart library preserves the fallback DOM');
assert.equal(fallback.textContent, 'Existing native SVG fallback');
window.LightweightCharts = LightweightCharts; context.LightweightCharts = LightweightCharts;
const native = bars();
const invalid = [bars('4h'), [{...native[0], open_time:'not-a-date'}],
  [{...native[0], close:NaN}], [{...native[0], low:102, high:99}],
  [native[1],native[0]], [native[0],native[0]],
  [{...native[0], open_time:new Date(NOW+300000).toISOString(),
    close_time:new Date(NOW+600000).toISOString()}],
  [{...native[0], close_time:new Date(NOW+300000).toISOString(), isOpen:false}]];
for (const rows of invalid) assert.equal(engine.render(options({bars:rows})), null);
assert.equal(charts.length, 0, 'invalid native bars must not create a chart');
""")


@pytest.mark.parametrize("frame", ["1m", "5m", "15m", "1h", "4h", "1D"])
def test_native_ohlc_and_volume_share_exact_selected_timeframe_times(tmp_path, frame):
    run_engine(tmp_path, "const frame=" + json.dumps(frame) + ";\n" + r"""
const native = bars(frame);
const before = plain(native);
const controller = engine.render(options({frame, bars:native}));
assert.ok(controller, 'valid native selected-frame candles must render');
for (const name of ['reset','zoom','setManual','getManual','dispose'])
  assert.equal(typeof controller[name], 'function', 'missing controller method '+name);
const candles = candleSeries(), volume = volumeSeries();
assert.ok(candles, 'native candles use the candlestick series');
assert.ok(volume, 'received native volume uses the histogram series');
assert.deepEqual(candles.data.map(row => ({time:row.time,open:row.open,high:row.high,low:row.low,close:row.close})),
  native.map(row => ({time:Date.parse(row.open_time)/1000,open:row.open,high:row.high,low:row.low,close:row.close})));
assert.deepEqual(volume.data.map(row => ({time:row.time,value:row.value})),
  native.map(row => ({time:Date.parse(row.open_time)/1000,value:row.volume})));
assert.deepEqual(native, before, 'render must preserve server candle objects');
engine.dispose(container);
assert.ok(charts.at(-1).removed);
""")


def test_mixed_unordered_or_future_rows_cannot_reach_public_series(tmp_path):
    run_engine(tmp_path, r"""
const native = bars();
const badTime = NOW / 1000 + 300;
const mixed = [...native].reverse();
mixed.push({...native[0], timeframe:'4h', open_time:new Date((badTime+300)*1000).toISOString()});
mixed.push({...native[0], open_time:new Date(badTime*1000).toISOString(),
  close_time:new Date((badTime+300)*1000).toISOString()});
mixed.push({...native[10]});
const controller = engine.render(options({bars:mixed}));
if (controller) {
  const candles = candleSeries(), volume = volumeSeries();
  assert.ok(candles.data.length > 0);
  assert.deepEqual(candles.data.map(row => row.time), [...new Set(candles.data.map(row => row.time))].sort((a,b)=>a-b));
  for (const row of candles.data) {
    assert.ok(row.time <= NOW/1000, 'future candles are excluded');
    assert.ok(native.some(item => Date.parse(item.open_time)/1000 === row.time), 'other-timeframe candles are excluded');
  }
  assert.deepEqual(volume.data.map(row => row.time), candles.data.map(row => row.time));
}
engine.dispose(container);
const forming = {...native.at(-1), open_time:new Date(NOW-60000).toISOString(),
  close_time:new Date(NOW+240000).toISOString(), isOpen:true};
assert.ok(engine.render(options({bars:[...native, forming]})), 'received current open candle is allowed');
assert.equal(candleSeries().data.at(-1).time, Date.parse(forming.open_time)/1000);
""")


def test_native_series_updates_only_changed_tail_and_reloads_prepended_history(tmp_path):
    run_engine(tmp_path, r"""
const native = bars();
assert.ok(engine.render(options({bars:native})));
const candles = candleSeries(), volume = volumeSeries();
assert.equal(candles.writes.length, 1); assert.equal(volume.writes.length, 1);
assert.ok(engine.render(options({bars:plain(native)})));
assert.equal(candles.writes.length, 1); assert.equal(volume.writes.length, 1);
assert.equal(candles.updates.length, 0); assert.equal(volume.updates.length, 0);
const revised = plain(native);
revised.at(-1).close += 0.2; revised.at(-1).volume += 5;
assert.ok(engine.render(options({bars:revised})));
assert.equal(candles.writes.length, 1); assert.equal(volume.writes.length, 1);
assert.equal(candles.updates.length, 1); assert.equal(volume.updates.length, 1);
closeTo(candles.data.at(-1).close, revised.at(-1).close);
closeTo(volume.data.at(-1).value, revised.at(-1).volume);
const forming = {timeframe:'5m',open_time:new Date(NOW).toISOString(),
  close_time:new Date(NOW+300000).toISOString(),open:104.6,high:104.9,low:104.2,
  close:104.75,volume:2,isOpen:true};
assert.ok(engine.render(options({bars:[...revised,forming]})));
assert.equal(candles.writes.length, 1); assert.equal(volume.writes.length, 1);
assert.equal(candles.updates.length, 2); assert.equal(volume.updates.length, 2);
const older = {...native[0],open_time:new Date(Date.parse(native[0].open_time)-300000).toISOString(),
  close_time:native[0].open_time,open:98,high:99,low:97,close:98.5};
assert.ok(engine.render(options({bars:[older,...revised,forming]})));
assert.equal(candles.writes.length, 2); assert.equal(volume.writes.length, 2);
assert.equal(candles.updates.length, 2); assert.equal(volume.updates.length, 2);
assert.equal(candles.data[0].time, Date.parse(older.open_time)/1000);
assert.equal(candles.data.at(-1).time, Date.parse(forming.open_time)/1000);
assert.ok(engine.render(options({frame:'1h',bars:bars('1h')})));
assert.equal(candles.writes.length, 3); assert.equal(volume.writes.length, 3);
assert.deepEqual(candles.data.map(row => row.time), bars('1h').map(row => Date.parse(row.open_time)/1000));
""")


def test_tick_formatter_keeps_non_power_of_ten_precision_and_matches_rounding(tmp_path):
    run_engine(tmp_path, r"""
for (const [price,tick,formatted,rounded] of [
  [0.0123456,0.00001,'0.01235',0.01235],
  [1.234,0.05,'1.25',1.25], [1.2,0.05,'1.20',1.2],
  [12.62,0.25,'12.50',12.5], [12.6,2.5,'12.5',12.5],
  [0.000001234,0.00000005,'0.00000125',0.00000125],
  [103.333333,'.00001','103.33333',103.33333],
]) {
  assert.equal(engine.formatTick(price,tick), formatted);
  assert.equal(typeof engine.roundTick(price,tick), 'number');
  closeTo(engine.roundTick(price,tick), rounded);
  closeTo(Number(engine.formatTick(price,tick)), engine.roundTick(price,tick));
}
const controller = engine.render(options({tickSize:0.05}));
assert.ok(controller);
assert.equal(candleSeries().settings.priceFormat.minMove, 0.05);
assert.equal(candleSeries().settings.priceFormat.formatter(1.2), '1.20');
""")


def test_native_price_lines_overlay_geometry_and_text_use_the_same_tick(tmp_path):
    run_engine(tmp_path, r"""
const original = {price:103.333333,layer:'liq',label:'Observed wick',color:'#91a7bd'};
const before = plain(original);
const controller = engine.render(options({tickSize:'.00001',currentPrice:99.123456,
  levels:[original],layers:new Set(['volume','fib','liq'])}));
assert.ok(controller);
const candles = candleSeries();
const nativeLine = candles.priceLines.find(line => line.options.title === 'Observed wick');
assert.ok(nativeLine);
closeTo(nativeLine.options.price, 103.33333);
closeTo(candles.priceLines.find(line => line.options.title === '').options.price, 99.12346);
assert.equal(candles.settings.priceFormat.minMove, 0.00001);
assert.equal(candles.settings.priceFormat.formatter(original.price), '103.33333');
const overlay = container.querySelector('svg');
const expectedY = candles.priceToCoordinate(nativeLine.options.price);
assert.ok(overlay.querySelectorAll('line').some(line => Math.abs(Number(line.getAttribute('y1'))-expectedY)<1e-9),
  'overlay line uses the actual tick-rounded native price-line coordinate');
assert.ok(overlay.querySelectorAll('text').some(text => text.textContent === 'Observed wick  103.33333'));
assert.deepEqual(original, before, 'drawing cannot round or overwrite authoritative level objects');
""")


def test_crosshair_legend_reads_received_native_ohlc_and_volume(tmp_path):
    run_engine(tmp_path, r"""
const native = bars();
assert.ok(engine.render(options({bars:native})));
const chart = charts.at(-1), legend = container.querySelector('.eh-chart-legend');
assert.ok(legend);
assert.ok(legend.textContent.includes('O 103.90'));
chart.emitCrosshair({time:Date.parse(native[7].open_time)/1000,point:{x:108,y:245}});
for (const value of ['FIXTUREUSDT','5m','O 100.70','H 102.70','L 99.70','C 101.20','+0.50%','Vol 17'])
  assert.ok(legend.textContent.includes(value), 'native crosshair legend must contain '+value);
chart.emitCrosshair({point:null});
assert.ok(legend.textContent.includes('O 103.90'));
assert.ok(legend.textContent.includes('C 104.40'));
""")


def test_automatic_fibonacci_respects_native_historical_viewport_cutoff(tmp_path):
    run_engine(tmp_path, r"""
const native = bars();
const fib = {...engine.buildManualFib(anchors(100,120),'5m',0.01),manual:false,
  source_timestamp:native.at(-1).close_time,discovered_at:native[5].close_time};
const controller = engine.render(options({bars:native,fibonacci:fib,onOlder:null}));
assert.ok(controller);
const chart = charts.at(-1), legend = container.querySelector('.eh-chart-legend');
const overlay = container.querySelector('svg');
assert.ok(legend.textContent.includes('FIB 5m swing'));
assert.equal(overlay.querySelectorAll('circle').length, 2);
chart.emitRange({from:0,to:20}); await settle();
assert.equal(legend.textContent.includes('FIB 5m swing'), false,
  'automatic Fib discovered after the native viewport cutoff must leave the legend');
assert.equal(overlay.querySelectorAll('circle').length, 0,
  'automatic anchors from a later source cannot appear over earlier native history');
assert.equal(overlay.querySelectorAll('line').length, 0);
chart.emitRange({from:20,to:native.length+5}); await settle();
assert.ok(legend.textContent.includes('FIB 5m swing'));
const laterDiscovery = {...fib,source_timestamp:native[5].close_time,
  discovered_at:native.at(-1).close_time};
assert.ok(engine.render(options({bars:native,fibonacci:laterDiscovery,onOlder:null})));
assert.ok(legend.textContent.includes('FIB 5m swing'));
chart.emitRange({from:0,to:20}); await settle();
assert.equal(legend.textContent.includes('FIB 5m swing'), false,
  'discovery after the native cutoff hides the automatic Fib even when its source is earlier');
assert.equal(overlay.querySelectorAll('circle').length, 0);
controller.setManual({enabled:true,locked:false,anchors:anchors(101,119)});
assert.ok(legend.textContent.includes('MANUAL'), 'explicit local manual drawing remains available in history');
assert.equal(overlay.querySelectorAll('circle').length, 2);
controller.setManual({enabled:false});
assert.equal(legend.textContent.includes('FIB 5m swing'), false);
chart.emitRange({from:20,to:native.length+5}); await settle();
assert.ok(legend.textContent.includes('FIB 5m swing'), 'latest native viewport restores the automatic Fib');
assert.equal(legend.textContent.includes('MANUAL'), false);
assert.equal(overlay.querySelectorAll('circle').length, 2);
""")


def test_automatic_fibonacci_uses_exact_anchors_without_mutating_server_metadata(tmp_path):
    run_engine(tmp_path, r"""
const fib = {...engine.buildManualFib(anchors(100,120),'5m',0.01),manual:false,
  quality:'DERIVED',source_timestamp:bars().at(-1).close_time};
const serverLevel = level(fib,'RETRACEMENT',0.618);
Object.assign(serverLevel,{price:107.64500000000001,priceText:'107.65',
  pair_id:'confirmed-native-swing',quality:'DERIVED',source:'NATIVE_CANDLES',
  sources:['native-fixture-only'],confluence:[{kind:'VWAP',source:'NATIVE_VOLUME',price:107.64}]});
const before = plain(fib);
const selected = engine.selectedFibLevels(fib,'pro',115,0.01);
const exact = selected.find(item => item.kind === 'RETRACEMENT' && item.ratio === 0.618);
assert.ok(exact);
closeTo(exact.price,107.64,'automatic level uses the exact confirmed-anchor calculation');
assert.equal(exact.priceText,'107.64');
for (const key of ['pair_id','quality','source']) assert.equal(exact[key],serverLevel[key]);
assert.deepEqual(plain(exact.sources),plain(serverLevel.sources));
assert.deepEqual(plain(exact.confluence),plain(serverLevel.confluence));
assert.ok(engine.render(options({fibonacci:fib,mode:'pro',currentPrice:115})));
const overlay = container.querySelector('svg'), legend = container.querySelector('.eh-chart-legend');
const expectedY = candleSeries().priceToCoordinate(107.64);
assert.ok(overlay.querySelectorAll('line').some(line => Math.abs(Number(line.getAttribute('y1'))-expectedY)<1e-9),
  'automatic SVG level uses the same exact tick-rounded price as the selected level');
assert.ok(legend.textContent.includes('FIB 5m swing'));
assert.equal(legend.textContent.includes('MANUAL'),false,'automatic precision repair keeps the automatic legend');
assert.deepEqual(plain(fib),before,'automatic precision repair preserves the server JSON and level metadata');
""")


@pytest.mark.parametrize("short", [False, True])
def test_manual_fibonacci_uses_directional_retracements_and_extensions(tmp_path, short):
    run_engine(tmp_path, "const short=" + json.dumps(short) + ";\n" + r"""
const selected = anchors(short ? 120 : 100, short ? 100 : 120);
const before = plain(selected);
const fib = engine.buildManualFib(selected, '5m', 0.01);
assert.ok(fib);
assert.equal(fib.timeframe, '5m');
assert.equal(fib.direction, short ? 'DOWN' : 'UP');
closeTo(level(fib,'RETRACEMENT',0.618).price, short ? 112.36 : 107.64);
closeTo(level(fib,'EXTENSION',1.272).price, short ? 94.56 : 125.44);
closeTo(level(fib,'RETRACEMENT',0.65).price, short ? 113 : 107);
assert.ok(fib.golden_pocket.lower_price < fib.golden_pocket.upper_price);
assert.deepEqual(selected, before, 'manual fib must preserve original anchor objects');
const pro = engine.selectedFibLevels(fib, 'pro', short ? 105 : 115, 0.01);
assert.ok(Array.isArray(pro) && pro.length > 0);
assert.ok(pro.some(item => item.kind === 'RETRACEMENT' && item.ratio === 0.618));
assert.ok(pro.some(item => item.kind === 'EXTENSION' && item.ratio === 1.272));
for (const item of pro) {
  assert.equal(item.timeframe, '5m');
  closeTo(item.price, engine.roundTick(item.price, 0.01));
}
const rookie = engine.selectedFibLevels(fib, 'rookie', short ? 105 : 115, 0.01);
assert.ok(rookie.some(item => item.ratio === 0.618));
assert.ok(rookie.some(item => item.ratio === 0.65));
assert.equal(rookie.some(item => item.ratio === 0.705), false);
const shownExtensions = rookie.filter(item => item.ratio > 1);
assert.equal(shownExtensions.length, 1, 'rookie mode selects one nearby extension');
assert.equal(shownExtensions[0].ratio, 1.272);
""")


def test_manual_lock_preserves_anchors_across_updates_without_mutating_server_fib(tmp_path):
    run_engine(tmp_path, r"""
const serverFib = engine.buildManualFib(anchors(100,120), '5m', 0.01);
const serverBefore = plain(serverFib);
let controller = engine.render(options({fibonacci:serverFib}));
controller.setManual({enabled:true, locked:false, anchors:anchors(99,121)});
controller.setManual({enabled:true, locked:true});
const locked = plain(controller.getManual());
assert.equal(locked.enabled, true); assert.equal(locked.locked, true);
assert.equal(locked.anchors.length, 2);
controller.setManual({enabled:true, locked:true, anchors:anchors(80,160)});
assert.deepEqual(plain(controller.getManual()).anchors, locked.anchors, 'locked manual anchors cannot move');
const newer = engine.buildManualFib(anchors(95,125), '5m', 0.01);
const newerBefore = plain(newer);
controller = engine.render(options({fibonacci:newer, manual:controller.getManual()}));
assert.deepEqual(plain(controller.getManual()).anchors, locked.anchors);
assert.equal(controller.getManual().locked, true);
assert.deepEqual(plain(serverFib), serverBefore);
assert.deepEqual(plain(newer), newerBefore, 'manual drawing cannot overwrite authoritative automatic fib input');
controller.setManual({enabled:true, locked:false});
controller.setManual({enabled:true, anchors:anchors(101,119)});
assert.equal(controller.getManual().locked, false);
assert.deepEqual(plain(controller.getManual()).anchors, anchors(101,119));
""")


def test_navigation_bounds_older_callbacks_and_disposes_public_subscriptions(tmp_path):
    run_engine(tmp_path, r"""
let calls = 0, release;
const pending = new Promise(resolve => { release = resolve; });
const controller = engine.render(options({onOlder:() => { calls++; return pending; }}));
const chart = charts.at(-1);
assert.ok(chart.rangeSubscribers.size > 0, 'history loading observes the public logical range');
controller.reset();
const initial = chart.timeScale().getVisibleLogicalRange();
assert.ok(initial && Number.isFinite(initial.from) && Number.isFinite(initial.to));
controller.zoom(0.8);
const zoomed = chart.timeScale().getVisibleLogicalRange();
assert.ok(Number.isFinite(zoomed.from) && Number.isFinite(zoomed.to) && zoomed.to > zoomed.from);
assert.notEqual(zoomed.to-zoomed.from, initial.to-initial.from);
chart.emitRange({from:20,to:39}); await settle();
assert.equal(calls, 0, 'current/right-side range does not request older history');
chart.emitRange({from:-1,to:15}); await settle();
assert.equal(calls, 1);
for (let index = 0; index < 25; index++) chart.emitRange({from:-1-index,to:15-index});
await settle();
assert.equal(calls, 1, 'range events must not duplicate an in-flight history request');
release({has_more:false}); await settle();
for (let index = 0; index < 25; index++) chart.emitRange({from:-1000,to:-980});
await settle();
assert.equal(calls, 1, 'unchanged oldest timestamp cannot trigger duplicate page requests');
controller.reset();
const reset = chart.timeScale().getVisibleLogicalRange();
assert.ok(Number.isFinite(reset.from) && Number.isFinite(reset.to) && reset.to > reset.from);
controller.dispose();
assert.equal(chart.removed, true);
assert.equal(chart.rangeSubscribers.size, 0);
assert.equal(chart.crosshairSubscribers.size, 0);
assert.ok(resizeObservers.every(observer => observer.disconnected));
chart.emitRange({from:-5,to:5}); await settle(); assert.equal(calls, 1);
engine.dispose(container); engine.dispose(container);
""")
