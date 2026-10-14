/* Expansion Hunter chart adapter. Lightweight Charts 4.2 public API only.
 * Native candles remain authoritative; manual drawings are local UI state.
 */
(function (global) {
  'use strict';
  const instances = new WeakMap();
  const intervals = {'1m':60, '5m':300, '15m':900, '1h':3600, '4h':14400, '1D':86400};
  const ratios = ['0', '0.382', '0.5', '0.618', '0.65', '0.705', '0.786', '1', '1.272', '1.414', '1.618'];
  const powers = [1n];
  const pow10 = n => { while (powers.length <= n) powers.push(powers[powers.length - 1] * 10n); return powers[n]; };
  const finite = value => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value));
  const copy = value => value == null ? value : JSON.parse(JSON.stringify(value));
  function decimal(value) {
    if (!finite(value)) return null;
    const match = String(value).trim().match(/^([+-]?)(\d*)(?:\.(\d*))?(?:e([+-]?\d+))?$/i);
    if (!match || !(match[2] || match[3])) return null;
    const exponent = Number(match[4] || 0);
    if (Math.abs(exponent) > 100) return null;
    let scale = (match[3] || '').length - exponent;
    let n = BigInt((match[2] || '0') + (match[3] || '')) * (match[1] === '-' ? -1n : 1n);
    if (scale < 0) { n *= pow10(-scale); scale = 0; }
    while (scale > 0 && n % 10n === 0n) { n /= 10n; scale--; }
    return {n, scale};
  }
  function decimalText(n, scale) {
    const sign = n < 0n ? '-' : '', digits = (n < 0n ? -n : n).toString().padStart(scale + 1, '0');
    return sign + (scale ? digits.slice(0, -scale) + '.' + digits.slice(-scale) : digits);
  }
  function roundedDecimal(value, tickSize) {
    const price = decimal(value), tick = decimal(tickSize);
    if (!price) return null;
    if (!tick || tick.n <= 0n) return {text:decimalText(price.n, price.scale), value:Number(value), precision:null};
    const scale = Math.max(price.scale, tick.scale);
    const p = price.n * pow10(scale - price.scale), t = tick.n * pow10(scale - tick.scale);
    const sign = p < 0n ? -1n : 1n, abs = p < 0n ? -p : p;
    const ticks = (abs / t + (abs % t * 2n >= t ? 1n : 0n)) * sign;
    const text = decimalText(ticks * tick.n, tick.scale);
    return {text, value:Number(text), precision:tick.scale};
  }
  const formatTick = (price, tick) => roundedDecimal(price, tick)?.text ?? '—';
  const roundTick = (price, tick) => roundedDecimal(price, tick)?.value ?? null;
  function stamp(value) {
    if (value == null || value === '') return null;
    const n = typeof value === 'number' ? value : Date.parse(value) / 1000;
    return Number.isFinite(n) ? Math.floor(n) : null;
  }
  function normalizeBars(bars, frame, now = Date.now() / 1000) {
    if (!Array.isArray(bars) || !intervals[frame]) return null;
    let previous = -Infinity;
    const result = [];
    for (const bar of bars) {
      const time = stamp(bar?.open_time ?? bar?.time), values = ['open','high','low','close','volume'].map(k => Number(bar?.[k]));
      if (bar?.timeframe && bar.timeframe !== frame || time === null || time <= previous || time > now ||
          ['open','high','low','close','volume'].some(k => !finite(bar?.[k])) || values.slice(0,4).some(v => v <= 0) ||
          values[4] < 0 || values[1] < Math.max(values[0],values[3]) || values[2] > Math.min(values[0],values[3]) || values[1] < values[2]) return null;
      const isOpen = bar.isOpen === true || bar.is_open === true || bar.closed === false;
      const end = stamp(bar.close_time) ?? time + intervals[frame];
      if (end <= time || (!isOpen && end > now) || (isOpen && result.some(b => b.isOpen))) return null;
      result.push({time,open:values[0],high:values[1],low:values[2],close:values[3],volume:values[4],isOpen,closeTime:end,source:bar});
      previous = time;
    }
    if (result.slice(0,-1).some(bar => bar.isOpen)) return null;
    return result;
  }
  function validFib(view, frame, now = Date.now()/1000) {
    if (!view || view.timeframe && view.timeframe !== frame || view.status !== 'AVAILABLE' ||
        ['STALE','UNAVAILABLE','MISSING'].includes(view.quality)) return false;
    return !['evaluated_at','as_of','source_timestamp','discovered_at'].some(k => stamp(view[k]) > now) &&
      !(view.anchors??[]).some(anchor=>anchor.timeframe&&anchor.timeframe!==frame || stamp(anchor.anchor_timestamp??anchor.time)>now);
  }
  function selectedFibLevels(view, mode, currentPrice, tickSize) {
    const input = [...(view?.levels ?? [...(view?.retracements ?? []), ...(view?.extensions ?? [])])];
    const geometry=!view?.manual && view?.anchors?.length===2 ? buildManualFib(view.anchors,view.timeframe,tickSize) : null;
    const precise=new Map((geometry?.levels??[]).map(level=>[level.ratio,level]));
    if (view?.anchors?.length===2) {
      for (const [ratio,anchor] of [[0,view.anchors[1]],[1,view.anchors[0]]]) {
        if (!input.some(level=>Number(level.ratio)===ratio)) input.push({ratio,price:anchor.price,kind:'ANCHOR',timeframe:view.timeframe});
      }
    }
    const levels = input.filter(level => (!level.timeframe || level.timeframe === view.timeframe) && finite(level.ratio) && finite(level.price) && Number(level.price) > 0)
      .map(level => {
        const ratio=Number(level.ratio),computed=precise.get(ratio);
        return {...level,price:computed?.price??roundTick(level.price,tickSize),priceText:computed?.priceText??formatTick(level.price,tickSize),ratio};
      });
    if (mode === 'pro') return levels;
    const extensions = levels.filter(level => level.ratio > 1).sort((a,b) => Math.abs(a.price - Number(currentPrice)) - Math.abs(b.price - Number(currentPrice)));
    return levels.filter(level => [0.382,0.5,0.618,0.65,0.786].includes(level.ratio) || level === extensions[0]);
  }
  function buildManualFib(anchors, frame, tickSize) {
    if (!Array.isArray(anchors) || anchors.length !== 2 || !intervals[frame]) return null;
    const a = decimal(anchors[0]?.price), b = decimal(anchors[1]?.price);
    const times = anchors.map(anchor => stamp(anchor.anchor_timestamp ?? anchor.time));
    if (!a || !b || a.n <= 0n || b.n <= 0n || times.some(t => t === null || t > Date.now()/1000) || times[0] >= times[1]) return null;
    const scale = Math.max(a.scale,b.scale), first = a.n * pow10(scale-a.scale), second = b.n * pow10(scale-b.scale), delta = second-first;
    if (delta === 0n) return null;
    const levels = ratios.map(r => {
      const ratio = decimal(r), denominator = pow10(ratio.scale);
      const n = Number(r) > 1 ? first * denominator + delta * ratio.n : second * denominator - delta * ratio.n;
      const raw = decimalText(n,scale+ratio.scale), rounded = roundedDecimal(raw,tickSize);
      return {ratio:Number(r),price:rounded.value,priceText:rounded.text,kind:Number(r)>1?'EXTENSION':'RETRACEMENT',timeframe:frame};
    });
    const pocket = levels.filter(level => [0.618,0.65].includes(level.ratio));
    return {status:'AVAILABLE',quality:'UI_MANUAL',timeframe:frame,direction:delta>0n?'UP':'DOWN',anchors:copy(anchors),levels,
      golden_pocket:{lower_price:Math.min(...pocket.map(l=>l.price)),upper_price:Math.max(...pocket.map(l=>l.price)),timeframe:frame},manual:true};
  }
  function element(doc, tag, styles, text) {
    const node = doc.createElement(tag);
    if (styles) Object.assign(node.style,styles);
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function svgElement(doc, tag, attrs) {
    const node = doc.createElementNS('http://www.w3.org/2000/svg',tag);
    for (const [key,value] of Object.entries(attrs || {})) node.setAttribute(key,String(value));
    return node;
  }
  const hasLayer = (layers,key) => layers == null || (typeof layers.has === 'function' ? layers.has(key) : Array.isArray(layers) ? layers.includes(key) : layers[key] === true);
  function createController(container, library) {
    const doc = container.ownerDocument || global.document;
    const labelWidth = () => (container.clientWidth || 700)<=540 ? 98 : 175;
    const host = element(doc,'div',{position:'absolute',inset:'0',zIndex:'1'});
    host.className = 'eh-native-chart';
    host.setAttribute('aria-label','Native market candlestick chart');
    const overlay = svgElement(doc,'svg',{width:'100%',height:'100%','aria-label':'Fibonacci drawings'});
    Object.assign(overlay.style,{position:'absolute',inset:'0',pointerEvents:'none',zIndex:'2',overflow:'hidden'});
    const legend = element(doc,'div',{position:'absolute',left:'12px',top:'8px',right:labelWidth()+'px',fontSize:'10px',lineHeight:'16px',color:'#aeb9c5',zIndex:'3',pointerEvents:'none'});
    legend.className = 'eh-chart-legend';
    const countdown = element(doc,'div',{position:'absolute',right:'7px',bottom:'29px',fontSize:'10px',color:'#aeb9c5',zIndex:'3',pointerEvents:'none'});
    countdown.className = 'eh-chart-countdown';
    const attribution = element(doc,'a',{position:'absolute',left:'12px',bottom:'5px',color:'#91a7bd',fontSize:'9px',zIndex:'3',textDecoration:'none'},'TradingView');
    attribution.href = 'https://www.tradingview.com/'; attribution.target = '_blank'; attribution.rel = 'noopener noreferrer';
    attribution.title = 'Charting by TradingView Lightweight Charts';
    container.appendChild(host); container.appendChild(overlay); container.appendChild(legend); container.appendChild(countdown); container.appendChild(attribution);
    let chart;
    try {
      chart = library.createChart(host,{width:Math.max(320,container.clientWidth || 700),height:Math.max(240,container.clientHeight || 350),
        layout:{background:{type:'solid',color:'#10151d'},textColor:'#8e9bac',fontFamily:'Inter, system-ui, sans-serif',fontSize:10},
        grid:{vertLines:{color:'rgba(89,110,137,0.10)'},horzLines:{color:'rgba(89,110,137,0.14)'}},
        rightPriceScale:{borderVisible:false,minimumWidth:labelWidth(),scaleMargins:{top:0.15,bottom:0.20}},
        timeScale:{borderVisible:false,timeVisible:true,secondsVisible:false,rightOffset:6,minBarSpacing:2,barSpacing:7},
        crosshair:{mode:library.CrosshairMode?.Normal ?? 0},
        handleScroll:{mouseWheel:true,pressedMouseMove:true,horzTouchDrag:true,vertTouchDrag:false},
        handleScale:{axisPressedMouseMove:true,mouseWheel:true,pinch:true},kineticScroll:{mouse:true,touch:true}});
    } catch (error) {
      [host,overlay,legend,countdown,attribution].forEach(node=>node.remove());
      return null;
    }
    const candles = chart.addCandlestickSeries({upColor:'#22c55e',downColor:'#ef4444',borderVisible:false,wickUpColor:'#22c55e',wickDownColor:'#ef4444',priceLineVisible:false,lastValueVisible:false});
    const volume = chart.addHistogramSeries({priceScaleId:'volume',priceFormat:{type:'volume'},lastValueVisible:false,priceLineVisible:false});
    chart.priceScale('volume').applyOptions({scaleMargins:{top:0.85,bottom:0},visible:false});
    const state = {options:null,bars:[],key:null,manual:{enabled:false,locked:false,anchors:null},lines:[],currentLine:null,
      loading:false,requested:new Set(),disposed:false,drawing:false,drag:null,hovered:null,hasDrawn:false,lastTap:0,lastError:null};
    const scale = chart.timeScale();
    function knownInVisibleRange(view) {
      const range=scale.getVisibleLogicalRange();
      if(!range || range.to>=state.bars.length-1)return true;
      const last=state.bars[Math.floor(range.to)],source=stamp(view.source_timestamp),discovered=stamp(view.discovered_at);
      return !!last && (source!==null || discovered!==null) && (source===null || source<=last.closeTime) && (discovered===null || discovered<=last.closeTime);
    }
    function activeFib() {
      if (state.manual.enabled && state.manual.anchors) return buildManualFib(state.manual.anchors,state.options.frame,state.options.tickSize);
      const view=state.options?.fibonacci;
      if(!validFib(view,state.options?.frame))return null;
      if(!knownInVisibleRange(view))return null;
      return view;
    }
    function updateLegend(bar) {
      const opts = state.options;
      if (!opts) return;
      const value = bar || state.bars.at(-1);
      const parts = [opts.symbol || '',opts.frame];
      if (value) {
        const delta = (value.close/value.open-1)*100;
        parts.push('O '+formatTick(value.open,opts.tickSize),'H '+formatTick(value.high,opts.tickSize),'L '+formatTick(value.low,opts.tickSize),'C '+formatTick(value.close,opts.tickSize),
          (delta>=0?'+':'')+delta.toFixed(2)+'%','Vol '+Number(value.volume).toLocaleString('en-US',{maximumFractionDigits:2}));
      }
      const fib = activeFib(), anchors = fib?.anchors;
      if (fib && hasLayer(opts.layers,'fib') && anchors?.length===2) parts.push('FIB '+opts.frame+' swing '+formatTick(anchors[0].price,opts.tickSize)+' → '+formatTick(anchors[1].price,opts.tickSize)+(state.manual.enabled?' · MANUAL'+(state.manual.locked?' LOCKED':''):''));
      if (fib?.developing_anchor || fib?.developing) parts.push('DEVELOPING');
      if (!decimal(opts.tickSize) || decimal(opts.tickSize).n<=0n) parts.push('Tick UNAVAILABLE');
      if (opts.stale) parts.push('STALE DATA');
      legend.textContent = parts.filter(Boolean).join('  ·  ');
    }
    function updateCountdown() {
      const last = state.bars.at(-1);
      if (!last || state.options?.stale || !last.isOpen) { countdown.textContent = state.options?.stale?'STALE':last?'Closed candle':''; return; }
      const remaining = Math.max(0,Math.ceil(last.closeTime-Date.now()/1000));
      countdown.textContent = 'Closes '+Math.floor(remaining/60)+':'+String(remaining%60).padStart(2,'0');
    }
    function anchorX(anchor, width) {
      const time = stamp(anchor?.anchor_timestamp ?? anchor?.time);
      if (time === null) return 0;
      const native = scale.timeToCoordinate(time);
      if (native !== null && Number.isFinite(native)) return Math.max(0,Math.min(width,native));
      const range = scale.getVisibleRange?.();
      if (range && time < Number(range.from)) return 0;
      const logical = scale.getVisibleLogicalRange(), index = state.bars.findIndex(b=>b.time>=time);
      if (logical && index>=0 && scale.logicalToCoordinate) return Math.max(0,Math.min(width,scale.logicalToCoordinate(index) ?? 0));
      return time <= (state.bars[0]?.time ?? 0) ? 0 : width;
    }
    function draw() {
      if (!state.options || state.disposed) return;
      updateLegend(state.hovered);
      overlay.replaceChildren();
      const opts = state.options, height = host.clientHeight || container.clientHeight || 350;
      const compact=(container.clientWidth || 700)<=540;
      const scaleWidth = chart.priceScale('right').width?.() || labelWidth(), width = (host.clientWidth || container.clientWidth || 700)-scaleWidth;
      if (width <= 0) return;
      const fib = activeFib(), current = finite(opts.currentPrice)?Number(opts.currentPrice):state.bars.at(-1)?.close;
      const labels = [];
      const currentRounded=roundTick(current,opts.tickSize),currentY=currentRounded===null?null:candles.priceToCoordinate(currentRounded);
      const occupied = Number.isFinite(currentY) ? [currentY] : [];
      function line(price, text, color, start=0, priority=0, details='', dashed=true) {
        const rounded = roundTick(price,opts.tickSize);
        if (rounded===null) return;
        const y = candles.priceToCoordinate(rounded);
        if (!Number.isFinite(y) || y<18 || y>height-30) return;
        overlay.appendChild(svgElement(doc,'line',{x1:start,x2:width,y1:y,y2:y,stroke:color,'stroke-width':priority?1.2:0.7,'stroke-opacity':priority?0.85:0.40,...(dashed?{'stroke-dasharray':'3 5'}:{})}));
        labels.push({y,text,color,priority,details,price:rounded});
      }
      if (fib && hasLayer(opts.layers,'fib')) {
        const start = anchorX(fib.anchors?.[0],width);
        const geometry=!fib.manual && fib.anchors?.length===2 ? buildManualFib(fib.anchors,fib.timeframe,opts.tickSize) : null;
        const pocket = geometry?.golden_pocket??fib.golden_pocket;
        const lower = roundTick(pocket?.lower ?? pocket?.lower_price,opts.tickSize), upper = roundTick(pocket?.upper ?? pocket?.upper_price,opts.tickSize);
        const y1 = lower===null?null:candles.priceToCoordinate(lower), y2 = upper===null?null:candles.priceToCoordinate(upper);
        if (lower !== null && upper !== null && Number.isFinite(y1) && Number.isFinite(y2)) {
          const y = Math.max(18,Math.min(y1,y2)), bottom = Math.min(height-30,Math.max(y1,y2));
          if (bottom>y) overlay.appendChild(svgElement(doc,'rect',{x:start,y,width:Math.max(0,width-start),height:bottom-y,fill:'#edc18b','fill-opacity':0.10}));
        }
        const levels = selectedFibLevels(fib,opts.mode,current,opts.tickSize);
        const above = levels.filter(l=>l.price>=current).sort((a,b)=>a.price-b.price)[0], below = levels.filter(l=>l.price<current).sort((a,b)=>b.price-a.price)[0];
        levels.forEach(level=>{
          const distance = finite(current) && current>0 ? (level.price/current-1)*100 : null;
          const matches = level.confluence ?? [], count = matches.length;
          const label = level.ratio+'  '+level.priceText+(distance===null||compact?'':'  '+(distance>=0?'+':'')+distance.toFixed(2)+'%')+(count>1?'  ×'+count:'');
          const details = count>0 ? 'Confluence: '+matches.map(item=>typeof item==='string'?item:item.kind??item.label??item.source??'Observed level').join(', ') : '';
          line(level.price,label,level===above||level===below?'#f4d299':'#c7af81',start,level===above||level===below?3:[0.618,0.65].includes(level.ratio)?2:1,details);
        });
        const points = (fib.anchors ?? []).slice(0,2).map((anchor,index)=>{
          const rounded=roundTick(anchor.price,opts.tickSize);
          return {anchor,index,x:anchorX(anchor,width),y:rounded===null?null:candles.priceToCoordinate(rounded)};
        });
        if (points.length===2 && points.every(p=>Number.isFinite(p.y))) {
          overlay.appendChild(svgElement(doc,'line',{x1:points[0].x,y1:points[0].y,x2:points[1].x,y2:points[1].y,stroke:'#edc18b','stroke-width':1,'stroke-opacity':0.55}));
          points.forEach(point=>{
            if (point.y<0 || point.y>height-25) return;
            const dot = svgElement(doc,'circle',{cx:point.x,cy:point.y,r:state.manual.enabled?5:3,fill:'#edc18b',stroke:'#10151d','stroke-width':1.4,'data-anchor-index':point.index});
            const title = svgElement(doc,'title');
            title.textContent = (state.manual.enabled?'Manual':'Confirmed wick')+' anchor · '+new Date(stamp(point.anchor.anchor_timestamp ?? point.anchor.time)*1000).toISOString()+' · '+formatTick(point.anchor.price,opts.tickSize);
            dot.appendChild(title);
            if (state.manual.enabled && !state.manual.locked) {
              dot.style.pointerEvents = 'all'; dot.style.cursor = 'grab';
              dot.addEventListener('pointerdown',event=>{
                if (state.manual.locked) return;
                state.drag = {index:point.index,id:event.pointerId};
                container.setPointerCapture?.(event.pointerId); event.preventDefault(); event.stopPropagation();
              });
            }
            overlay.appendChild(dot);
          });
        }
        const developing = fib.developing_anchor ?? fib.developing;
        if (developing?.price && finite(developing.price)) {
          const x = anchorX(developing,width), y = candles.priceToCoordinate(roundTick(developing.price,opts.tickSize));
          if (Number.isFinite(y)) {
            const dot = svgElement(doc,'circle',{cx:x,cy:y,r:4,fill:'none',stroke:'#edc18b','stroke-dasharray':'2 2'});
            const title = svgElement(doc,'title'); title.textContent='DEVELOPING · unconfirmed wick anchor'; dot.appendChild(title); overlay.appendChild(dot);
          }
        }
      }
      for (const view of opts.htfFibonacci ?? []) {
        if (!validFib(view,view.timeframe) || !knownInVisibleRange(view) || view.timeframe===opts.frame || !hasLayer(opts.layers,'fib')) continue;
        const geometry=!view.manual && view.anchors?.length===2 ? buildManualFib(view.anchors,view.timeframe,opts.tickSize) : null;
        const start=anchorX(view.anchors?.[0],width), pocket=geometry?.golden_pocket??view.golden_pocket;
        const lower=roundTick(pocket?.lower_price ?? pocket?.lower,opts.tickSize),upper=roundTick(pocket?.upper_price ?? pocket?.upper,opts.tickSize);
        const a=lower===null?null:candles.priceToCoordinate(lower),b=upper===null?null:candles.priceToCoordinate(upper);
        if (Number.isFinite(a)&&Number.isFinite(b)) overlay.appendChild(svgElement(doc,'rect',{x:start,y:Math.max(18,Math.min(a,b)),width:Math.max(0,width-start),height:Math.max(0,Math.min(height-30,Math.max(a,b))-Math.max(18,Math.min(a,b))),fill:'#91a7bd','fill-opacity':0.045}));
        const half=selectedFibLevels(view,'pro',current,opts.tickSize).find(level=>level.ratio===0.5);
        if (half) line(half.price,view.timeframe+' 0.5  '+formatTick(half.price,opts.tickSize),'#91a7bd',start,0);
      }
      (opts.levels ?? []).filter(level=>level.layer!=='fib' && hasLayer(opts.layers,level.layer)).forEach(level=>line(level.price,(compact?'':level.label+'  ')+formatTick(level.price,opts.tickSize),level.color||'#91a7bd',0,0,'',!!level.dash));
      labels.sort((a,b)=>b.priority-a.priority || a.y-b.y).forEach(label=>{
        if (occupied.some(y=>Math.abs(y-label.y)<17)) return;
        occupied.push(label.y);
        const group = svgElement(doc,'g');
        group.appendChild(svgElement(doc,'rect',{x:width+1,y:label.y-8,width:scaleWidth-2,height:16,rx:2,fill:'#10151d','fill-opacity':0.97}));
        const text = svgElement(doc,'text',{x:width+scaleWidth-7,y:label.y+3,'text-anchor':'end',fill:label.color,'font-size':compact?10:9,'font-family':'Inter, system-ui, sans-serif'});
        text.textContent = label.text; group.appendChild(text);
        if (label.details) {
          group.style.pointerEvents='all'; group.style.cursor='help';
          const title=svgElement(doc,'title');title.textContent=label.details;group.appendChild(title);
        }
        overlay.appendChild(group);
      });
    }
    function crosshairMove(param) {
      const bar = state.bars.find(b=>b.time===param.time);
      state.hovered=bar||null; updateLegend(bar);
    }
    function requestOlder(range) {
      draw();
      if (!state.hasDrawn || state.drawing || state.loading || !range || range.from>8 || !state.options?.onOlder) return;
      const first=state.bars.find(bar=>!bar.isOpen);
      if (!first || state.requested.has(first.time)) return;
      state.requested.add(first.time); state.loading=true;
      const request={symbol:state.options.symbol,frame:state.options.frame,before:first.source.open_time ?? new Date(first.time*1000).toISOString(),time:first.time};
      try { Promise.resolve(state.options.onOlder(request)).catch(()=>{}).finally(()=>{state.loading=false;}); }
      catch (error) { state.loading=false; }
    }
    function resize() {
      if (state.disposed) return;
      chart.applyOptions({width:Math.max(320,container.clientWidth||700),height:Math.max(240,container.clientHeight||350),rightPriceScale:{minimumWidth:labelWidth()}});
      legend.style.right=labelWidth()+'px';draw();
    }
    function doubleClick(){controller.reset();}
    function tap(event){
      if (event.pointerType!=='touch' || event.isPrimary===false || state.drag) return;
      const now=Date.now(); if (now-state.lastTap<300) controller.reset(); state.lastTap=now;
    }
    function dragMove(event) {
      if (!state.drag || state.manual.locked || event.pointerId!==state.drag.id) return;
      const rect=container.getBoundingClientRect(), p=candles.coordinateToPrice(event.clientY-rect.top), time=scale.coordinateToTime(event.clientX-rect.left);
      if (!finite(p) || Number(p)<=0 || time===null) return;
      const nearest=state.bars.reduce((best,bar)=>!best||Math.abs(bar.time-Number(time))<Math.abs(best.time-Number(time))?bar:best,null);
      if (!nearest) return;
      const next=copy(state.manual.anchors), index=state.drag.index;
      next[index]={...next[index],price:roundTick(p,state.options.tickSize),anchor_timestamp:new Date(nearest.time*1000).toISOString(),timeframe:state.options.frame};
      if (!buildManualFib(next,state.options.frame,state.options.tickSize)) return;
      state.manual.anchors=next; draw(); updateLegend(state.hovered);
      state.options.onManualChange?.(copy(state.manual)); event.preventDefault();
    }
    function dragEnd(event) {
      if (!state.drag || event.pointerId!==state.drag.id) return;
      try { container.releasePointerCapture?.(event.pointerId); } catch(error) { /* Capture may already be released. */ }
      state.drag=null;
    }
    chart.subscribeCrosshairMove(crosshairMove); scale.subscribeVisibleLogicalRangeChange(requestOlder);
    container.addEventListener('dblclick',doubleClick);container.addEventListener('pointerup',tap);
    container.addEventListener('pointermove',dragMove);container.addEventListener('pointerup',dragEnd);container.addEventListener('pointercancel',dragEnd);
    const observer=global.ResizeObserver ? new global.ResizeObserver(resize) : null;observer?.observe(container);
    const timer=global.setInterval ? global.setInterval(updateCountdown,1000) : null;
    const controller = {
      update(opts, bars) {
        const key=(opts.symbol||'')+':'+opts.frame, changed=key!==state.key;
        let range = !changed ? scale.getVisibleLogicalRange() : null;
        const priorBars=state.bars,oldFirst=priorBars[0]?.time, oldLength=priorBars.length;
        const followingLatest=range && range.to>=oldLength-1;
        if (changed) { state.requested.clear();state.manual={enabled:false,locked:false,anchors:null};state.hovered=null; }
        state.options=opts;state.bars=bars;state.key=key;state.drawing=true;
        host.dataset.timeframe=opts.frame;host.dataset.symbol=opts.symbol||'';host.dataset.visibleCount=String(bars.length);
        const tick=decimal(opts.tickSize);
        candles.applyOptions({priceFormat:{type:'custom',formatter:p=>formatTick(p,opts.tickSize),...(tick&&tick.n>0n?{minMove:Number(opts.tickSize)}:{})}});
        const candleData=({time,open,high,low,close})=>({time,open,high,low,close});
        const volumeData=({time,volume,close,open})=>({time,value:volume,color:close>=open?'rgba(34,197,94,0.28)':'rgba(239,68,68,0.28)'});
        const same=(a,b)=>a&&b&&a.time===b.time&&a.open===b.open&&a.high===b.high&&a.low===b.low&&a.close===b.close&&a.volume===b.volume;
        let incremental=!changed&&oldLength>0&&bars.length>=oldLength&&bars[oldLength-1]?.time===priorBars[oldLength-1].time;
        for(let i=0;incremental&&i<oldLength-1;i++) if(!same(priorBars[i],bars[i])) incremental=false;
        if(incremental){
          if(!same(priorBars[oldLength-1],bars[oldLength-1])){candles.update(candleData(bars[oldLength-1]));volume.update(volumeData(bars[oldLength-1]));}
          for(let i=oldLength;i<bars.length;i++){candles.update(candleData(bars[i]));volume.update(volumeData(bars[i]));}
        }else{
          candles.setData(bars.map(candleData));volume.setData(bars.map(volumeData));
        }
        state.lines.forEach(line=>candles.removePriceLine(line));state.lines=[];
        for (const level of opts.levels ?? []) if (level.layer!=='fib'&&hasLayer(opts.layers,level.layer)&&finite(level.price)) {
          state.lines.push(candles.createPriceLine({price:roundTick(level.price,opts.tickSize),color:level.color||'#91a7bd',lineWidth:1,lineStyle:level.dash?2:0,lineVisible:false,axisLabelVisible:false,title:level.label||''}));
        }
        if (state.currentLine) candles.removePriceLine(state.currentLine);
        const current=finite(opts.currentPrice)?Number(opts.currentPrice):bars.at(-1)?.close;
        state.currentLine = finite(current) ? candles.createPriceLine({price:roundTick(current,opts.tickSize),color:opts.stale?'#edc18b':'#38bdf8',lineWidth:1,lineStyle:2,axisLabelVisible:true,title:opts.stale?'STALE':''}) : null;
        if (range && oldFirst!==bars[0]?.time) {
          const shift=bars.findIndex(bar=>bar.time===oldFirst);
          if (shift>=0) range={from:range.from+shift,to:range.to+shift};
        }
        else if (range && followingLatest && bars.length>oldLength) {
          const shift=bars.length-oldLength;
          range={from:range.from+shift,to:range.to+shift};
        }
        if (changed) scale.setVisibleLogicalRange({from:Math.max(-2,bars.length-100),to:bars.length+5});
        else if (range) scale.setVisibleLogicalRange(range);
        if (opts.manual) controller.setManual(opts.manual);
        state.drawing=false;state.hasDrawn=true; updateLegend(state.hovered);updateCountdown();draw();
        return controller;
      },
      reset() { state.drawing=true;scale.setVisibleLogicalRange({from:Math.max(-2,state.bars.length-100),to:state.bars.length+5});state.drawing=false;draw(); },
      zoom(factor) {
        const range=scale.getVisibleLogicalRange();if(!range||!finite(factor)||factor<=0)return;
        const count=Math.max(20,Math.min(2000,(range.to-range.from)*factor));scale.setVisibleLogicalRange({from:range.to-count,to:range.to});draw();
      },
      setManual(value={}) {
        if (!state.options) return copy(state.manual);
        if (value.enabled===false) state.manual.enabled=false;
        else if (value.enabled===true) {
          state.manual.enabled=true;
          if (!state.manual.anchors) state.manual.anchors=copy(state.options.fibonacci?.anchors??null);
        }
        if (value.anchors && !state.manual.locked && buildManualFib(value.anchors,state.options.frame,state.options.tickSize)) state.manual.anchors=copy(value.anchors);
        if (typeof value.locked==='boolean') state.manual.locked=value.locked;
        draw();updateLegend(state.hovered);return copy(state.manual);
      },
      getManual() { return copy(state.manual); },
      getState() { return {frame:state.options?.frame,symbol:state.options?.symbol,bars:state.bars.length,manual:copy(state.manual),error:state.lastError}; },
      dispose() {
        if(state.disposed)return;state.disposed=true;observer?.disconnect();if(timer!==null)global.clearInterval?.(timer);
        chart.unsubscribeCrosshairMove?.(crosshairMove);scale.unsubscribeVisibleLogicalRangeChange?.(requestOlder);
        container.removeEventListener('dblclick',doubleClick);container.removeEventListener('pointerup',tap);container.removeEventListener('pointermove',dragMove);container.removeEventListener('pointerup',dragEnd);container.removeEventListener('pointercancel',dragEnd);
        chart.remove();[host,overlay,legend,countdown,attribution].forEach(node=>node.remove());instances.delete(container);
      }
    };
    return controller;
  }
  global.ExpansionHunterChart = Object.freeze({
    render(options) {
      const library=global.LightweightCharts;
      if(!options?.container) return null;
      if(!library?.createChart) { instances.get(options.container)?.dispose();return null; }
      const bars=normalizeBars(options.bars,options.frame);
      if(!bars) { instances.get(options.container)?.dispose();return null; }
      let controller=instances.get(options.container);
      if(!controller){controller=createController(options.container,library);if(!controller)return null;instances.set(options.container,controller);}
      try { return controller.update(options,bars); }
      catch (error) { controller.dispose();return null; }
    },
    dispose(container){instances.get(container)?.dispose();},formatTick,roundTick,buildManualFib,selectedFibLevels,normalizeBars
  });
})(window);
