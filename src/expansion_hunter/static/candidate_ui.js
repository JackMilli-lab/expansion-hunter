/* Candidate detail and browser-local controls. All market values come from callers. */
(function (global) {
  'use strict';
  const UNAVAILABLE = 'UNAVAILABLE';
  const escape = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const number = value => {
    if (value === null || value === undefined || typeof value === 'boolean' || typeof value === 'object' || String(value).trim() === '') return null;
    const result = Number(value); return Number.isFinite(result) ? result : null;
  };
  const scalar = value => number(value?.value ?? value?.score ?? value);
  const format = (value, digits = 2) => {
    const n = scalar(value); return n === null ? UNAVAILABLE : n.toLocaleString('en-US', {maximumFractionDigits:digits});
  };
  const label = value => value === undefined || value === null || value === '' ? UNAVAILABLE : String(value).replace(/_/g, ' ');
  const side = value => /^(LONG|BULLISH)$/i.test(value ?? '') ? 'LONG' : /^(SHORT|BEARISH)$/i.test(value ?? '') ? 'SHORT' : /^NEUTRAL$/i.test(value ?? '') ? 'NEUTRAL' : UNAVAILABLE;
  const stamp = value => {
    if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/.test(value)) return null;
    const parsed = new Date(value); return Number.isFinite(parsed.getTime()) ? parsed : null;
  };
  const steps = value => Array.isArray(value) ? value : Array.isArray(value?.steps) ? value.steps : [];
  const state = alert => {
    const raw = String(alert.state ?? alert.stage ?? alert.event_type ?? '').toUpperCase();
    if (/FAIL|INVALID/.test(raw)) return 'FAILED';
    if (/TRIGGER|BREAKOUT|BREAKDOWN/.test(raw)) return 'TRIGGER';
    if (/READY/.test(raw)) return 'READY';
    if (/ACTIVE|EXPANSION/.test(raw)) return 'ACTIVE';
    return raw || UNAVAILABLE;
  };
  function deliveryStatus(alert) {
    const delivered = stamp(alert?.alert_delivered_timestamp);
    return delivered && delivered.getTime() <= Date.now() ? 'Delivered ' + delivered.toLocaleString('en-US') : 'Delivery not confirmed';
  }
  function mount(options = {}) {
    const doc = global.document;
    if (!doc?.body || !doc.createElement) return null;
    const listeners = [], added = [], rowStates = new WeakMap();
    const listen = (target, event, handler, settings) => {
      if (!target) return;
      target.addEventListener(event, handler, settings);
      listeners.push(() => target.removeEventListener(event, handler, settings));
    };
    const read = (storage, key, fallback) => { try { return JSON.parse(storage.getItem(key)) ?? fallback; } catch (_) { return fallback; } };
    const save = (storage, key, value) => { try { storage.setItem(key, JSON.stringify(value)); } catch (_) { /* Memory remains usable when storage is disabled. */ } };
    const storage = kind => { try { return global[kind]; } catch (_) { return {getItem(){},setItem(){}}; } };
    const local = storage('localStorage'), session = storage('sessionStorage');
    const storedMute = read(local, 'eh.localMutedSymbols.v1', []);
    const muted = new Set(Array.isArray(storedMute) ? storedMute.filter(value => typeof value === 'string') : []);
    const storedPreferences = read(session, 'eh.tabAlertPreferences.v1', {});
    const preferences = {ready: storedPreferences.ready !== false, trigger: storedPreferences.trigger !== false,
      cooldown: Math.max(0, Math.min(3600, number(storedPreferences.cooldown) ?? 60))};
    const lastNotification = new Map();
    let activeSymbol = null, previousFocus = null, previousFocusSymbol = null, fullHeight = false, lastRender = '', selectedIndex = -1;
    const filters = {direction:'ALL', state:'ALL'};
    if (!doc.getElementById('candidate-ui-style')) {
      const style = doc.createElement('style'); style.id = 'candidate-ui-style';
      style.textContent = `
dialog.eh-candidate-sheet{position:fixed;inset:auto 0 0;margin:0 auto;width:min(880px,100%);max-width:100%;height:min(90dvh,1000px);max-height:100dvh;padding:0;border:0;border-radius:20px 20px 0 0;background:var(--panel,#12151C);color:var(--text,#E8EAF0);overflow:hidden;box-shadow:0 -16px 100px #0008}
dialog.eh-candidate-sheet.full-height{height:100dvh;border-radius:0}dialog.eh-candidate-sheet::backdrop{background:#07090dbd;backdrop-filter:blur(4px)}
.eh-sheet-layout{height:100%;display:flex;flex-direction:column}.eh-sheet-head{display:flex;gap:12px;align-items:center;padding:18px 22px;background:var(--panel,#12151C);flex:none}.eh-sheet-title{flex:1;min-width:0}.eh-sheet-title h2{margin:0;font-size:22px;letter-spacing:-.5px}.eh-sheet-title p{margin:6px 0 0;font-size:12px;color:var(--muted,#9AA3B2)}
.eh-sheet-head button,.eh-chart-button,.eh-row-actions button,.eh-row-toggle{min-height:44px;min-width:44px;border:0;border-radius:8px;background:var(--panel2,#181C25);color:var(--text,#E8EAF0);padding:9px 12px}.eh-sheet-head button:focus-visible,.eh-chart-button:focus-visible,.eh-row-actions button:focus-visible,.eh-row-toggle:focus-visible{outline:2px solid var(--accent,#38bdf8);outline-offset:3px}
.eh-sheet-scroll{overflow-y:auto;overscroll-behavior:contain;padding:0 22px calc(24px + env(safe-area-inset-bottom));min-height:0}.eh-sheet-scroll h3{font-size:13px;font-weight:600;margin:22px 0 12px}.eh-history-grid,.eh-summary-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px 20px}.eh-fact{min-width:0}.eh-fact dt{font-size:12px;color:var(--muted,#9AA3B2);margin-bottom:7px}.eh-fact dd{font-size:14px;line-height:1.6;margin:0;overflow-wrap:anywhere}.eh-history-grid{margin:10px 0 24px}.eh-history-values{font:14px ui-monospace,monospace;color:var(--text,#E8EAF0);overflow-wrap:anywhere}.eh-history-grid svg{width:100%;height:44px;margin-top:8px}.eh-note{font-size:12px!important;line-height:1.7!important;color:var(--muted,#9AA3B2);margin:10px 0!important}.eh-summary-grid{margin:0}.eh-path{margin:15px 0}.eh-path h4{font-size:12px;font-weight:600;margin:0 0 8px;color:var(--muted,#9AA3B2)}.eh-path ol{padding-left:18px;margin:0;font-size:13px;line-height:1.7}.eh-path li{margin:8px 0}.eh-mini-chart{width:100%;height:auto;max-height:210px;background:var(--bg,#0B0D12);border-radius:10px}.eh-chart-button{display:flex;width:100%;justify-content:center;background:var(--accent,#38bdf8);color:#07111a;font-weight:600;margin-top:12px}.eh-pro-details{margin-top:24px}.eh-pro-details summary{cursor:pointer;padding:12px 0;font-size:14px;min-height:44px}.eh-pro-details pre{white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.7 ui-monospace,monospace;color:var(--muted,#9AA3B2);background:var(--bg,#0B0D12);padding:12px;border-radius:8px}
#candidate-sheet[data-freshness=STALE] .eh-mini-chart,#candidate-sheet[data-freshness=UNAVAILABLE] .eh-mini-chart{opacity:.55}
.watchlist-item.eh-swipe-row{flex-wrap:wrap;touch-action:pan-y;position:relative}.eh-row-actions{display:none;width:100%;gap:8px;padding-top:8px}.eh-swipe-row.actions-open .eh-row-actions{display:flex}.eh-row-toggle{background:transparent;padding:8px;color:var(--muted,#9AA3B2)}.eh-row-actions button{flex:1;font-size:12px}.eh-swipe-row.local-muted .watchlist-details{opacity:.65}.eh-row-scope{width:100%;font-size:11px;color:var(--muted,#9AA3B2);margin-top:5px}
.eh-alert-controls{display:flex;gap:10px;padding:12px 20px;flex-wrap:wrap}.eh-alert-controls label{font-size:12px;color:var(--muted,#9AA3B2);display:flex;align-items:center;gap:8px}.eh-alert-controls select{min-height:44px;width:auto;max-width:170px}.eh-notification-prefs{margin:12px 20px 18px}.eh-notification-prefs summary{min-height:44px;cursor:pointer;font-size:13px;padding:10px 0}.eh-notification-prefs label{display:flex;gap:10px;align-items:center;font-size:12px;min-height:44px;margin:0}.eh-notification-prefs input[type=checkbox]{width:18px;height:18px;accent-color:var(--accent,#38bdf8)}.eh-notification-prefs input[type=number]{width:95px;min-height:44px}.eh-keyboard-selected{outline:2px solid var(--accent,#38bdf8);outline-offset:-2px}
@media(max-width:540px){.eh-sheet-head{padding:14px 15px;gap:8px}.eh-sheet-scroll{padding-left:15px;padding-right:15px}.eh-sheet-title h2{font-size:20px}.eh-sheet-head button{font-size:12px;padding:8px}.eh-history-grid,.eh-summary-grid{gap:16px 12px}.eh-fact dd{font-size:13px}}
@media(prefers-reduced-motion:reduce){.eh-candidate-sheet,.eh-row-actions{scroll-behavior:auto}}
`;
      doc.head.appendChild(style); added.push(style);
    }
    const dialog = doc.createElement('dialog'); dialog.id = 'candidate-sheet'; dialog.className = 'eh-candidate-sheet';
    dialog.setAttribute('aria-labelledby','candidate-title'); dialog.setAttribute('aria-describedby','candidate-sheet-scope');
    dialog.innerHTML = `<div class="eh-sheet-layout"><header class="eh-sheet-head"><div class="eh-sheet-title"><h2 id="candidate-title">Candidate</h2><p id="candidate-heading-meta"></p></div><button type="button" id="candidate-expand" aria-label="Expand candidate sheet to full height" aria-pressed="false">Full height</button><button type="button" id="candidate-close" aria-label="Close candidate details" autofocus>✕</button></header><div class="eh-sheet-scroll" id="candidate-content"></div></div>`;
    doc.body.appendChild(dialog); added.push(dialog);
    const find = id => doc.getElementById(id);
    const fact = (name, value) => `<div class="eh-fact"><dt>${escape(name)}</dt><dd>${escape(value)}</dd></div>`;
    const history = (name, values, rank = false) => {
      const points = (Array.isArray(values) ? values : []).map(item => scalar(typeof item === 'object' && item !== null ? item.value ?? item.score ?? item.current_rank ?? item.rank : item)).filter(value => value !== null).slice(-16);
      let chart = '';
      if (points.length > 1) {
        const lo = Math.min(...points), hi = Math.max(...points), span = hi - lo || 1;
        const coordinates = points.map((point,index) => `${4 + index * 212 / (points.length-1)},${rank ? 5 + (point-lo)*34/span : 39-(point-lo)*34/span}`).join(' ');
        chart = `<svg viewBox="0 0 220 44" role="img" aria-label="${escape(name)} from supplied observations"><polyline points="${coordinates}" fill="none" stroke="var(--accent,#38bdf8)" stroke-width="2"/></svg>`;
      }
      return `<div class="eh-fact"><dt>${escape(name)}</dt><dd class="eh-history-values">${points.length ? points.map(value => (rank?'#':'')+format(value,1)).join(' → ') : UNAVAILABLE}</dd>${chart}</div>`;
    };
    const pathMarkup = (name, path) => {
      const values = steps(path);
      return `<section class="eh-path"><h4>${escape(name)}</h4>${values.length ? '<ol>' + values.slice(0,3).map(step => {
        const description = typeof step === 'string' ? step : step.condition ?? step.reason ?? step.description ?? step.state;
        const level = number(step?.level ?? step?.price);
        return `<li>${escape(label(description))}${level===null?'':` <span class="eh-history-values">${format(level,8)}</span>`}</li>`;
      }).join('') + '</ol>' : `<p class="eh-note">${UNAVAILABLE}</p>`}</section>`;
    };
    function resolve(symbol) {
      const supplied = options.getCandidate?.(symbol) ?? {};
      const asset = supplied.asset ?? supplied;
      const wrappedLive = supplied.live;
      const live = wrappedLive?.live ?? wrappedLive ?? {};
      const frame = options.getFrame?.() ?? supplied.timeframe ?? '5m';
      const report = supplied.war_room ?? live.war_room ?? asset.war_room ?? {};
      const swing = report.swing_phases?.[frame] ?? {};
      const decision = supplied.decision ?? swing.decision;
      const primary = report.scenarios?.[report.expected_path?.primary] ?? {};
      const pre = supplied.pre ?? supplied.pre_ignition ?? asset.pre_ignition ?? {};
      return {supplied,asset,live,frame,report,swing,decision,primary,pre};
    }
    function miniChart(bars, trigger, invalidation) {
      const source = Array.isArray(bars) ? bars : [];
      const valid = source.filter(bar => number(bar?.close)!==null && number(bar?.close)>0).slice(-60);
      if (!valid.length) return `<p class="eh-note">Mini chart ${UNAVAILABLE} · no received candles for this asset and timeframe.</p>`;
      const closes = valid.map(bar => number(bar.close));
      const levels = [{name:'Trigger',value:number(trigger),color:'#38bdf8'}, {name:'Invalidation',value:number(invalidation),color:'#B96D77'}].filter(level => level.value!==null && level.value>0);
      const lo = Math.min(...closes,...levels.map(level=>level.value)), hi = Math.max(...closes,...levels.map(level=>level.value));
      const span = Math.max(hi-lo, Math.abs(hi)*.002, 1e-9), bottom = lo-span*.1, top = hi+span*.1;
      const y = value => 168-(value-bottom)/(top-bottom)*150;
      const line = closes.map((value,index)=>`${14+(closes.length===1?0:index*390/(closes.length-1))},${y(value)}`).join(' ');
      return `<svg class="eh-mini-chart" viewBox="0 0 560 190" role="img" aria-label="Received candle closes with supplied trigger and invalidation"><polyline points="${line}" fill="none" stroke="var(--accent,#38bdf8)" stroke-width="2"/>${levels.map(level=>`<line x1="14" x2="410" y1="${y(level.value)}" y2="${y(level.value)}" stroke="${level.color}" stroke-dasharray="4 4"/><text x="418" y="${y(level.value)-4}" fill="${level.color}" font-size="11">${escape(level.name)}</text><text x="418" y="${y(level.value)+10}" fill="${level.color}" font-size="11">${format(level.value,8)}</text>`).join('')}</svg><p class="eh-note">${valid.length} received candle close${valid.length===1?'':'s'} · overview only. Open the full chart for timestamps and candle detail.</p>`;
    }
    function freshness(view) {
      const supplied = options.getFreshness?.(activeSymbol);
      if (typeof supplied === 'string') return supplied;
      const quality = supplied?.status ?? view.supplied.freshness ?? view.asset.live_status ?? view.live.status ?? UNAVAILABLE;
      const source = supplied?.source_timestamp ?? view.pre.as_of ?? view.decision?.as_of ?? view.report.as_of ?? view.asset.price_as_of ?? view.live.received_timestamp;
      const date = stamp(source), seconds = date ? (Date.now()-date.getTime())/1000 : null;
      const stale = seconds===null || seconds<0 || seconds>30 || /STALE|UNAVAILABLE|MISSING|OFFLINE/.test(quality);
      return (seconds===null||seconds<0 ? UNAVAILABLE : stale ? 'STALE' : label(quality)) + (date ? ' · observation '+date.toLocaleString('en-US') : ' · observation timestamp unavailable');
    }
    function btcContext(view) {
      const context = options.getBTCContext?.() ?? view.supplied.btc_context ?? view.report.btc_context;
      if (context===null || context===undefined) return UNAVAILABLE;
      if (typeof context==='string') return label(context);
      const parts = [context.regime ?? context.bias ?? context.state, context.reason, context.freshness ?? context.status].filter(value=>value!==undefined && value!==null && value!=='');
      const value = number(context.price ?? context.btc_price); if(value!==null)parts.push('Price '+format(value,8));
      return parts.length ? parts.map(label).join(' · ') : UNAVAILABLE;
    }
    function detailMarkup(view) {
      const {asset,live,decision,report,pre} = view;
      const fields = [
        ['Component scores',decision?.setup_quality?.components ?? pre.components ?? live.scores ?? asset.scores],
        ['Setup families',pre.family_decomposition],
        ['Volume',live.volume ?? live.volume_metrics ?? asset.volume_metrics ?? pre.volume_metrics ?? pre.metrics],
        ['Structure',report.structure ?? report.structure_states ?? live.structure ?? view.swing],
        ['Fibonacci confluence',decision?.fib_context ?? live.active_fibonacci?.[view.frame]],
        ['Liquidity',report.liquidity ?? live.liquidity ?? asset.liquidity],
        ['Order flow',live.order_flow ?? live.spot_cvd ?? live.cvd],
        ['OI / funding',live.futures ?? live.open_interest ?? asset.futures],
        ['Baselines',pre.baseline ?? pre.baselines ?? asset.baselines],
        ['Historical analogs',report.historical_analogs ?? asset.historical_analogs],
        ['Fast features',pre.fast_features],
        ['Data quality',pre.data_quality ?? live.data_quality],
        ['Model version',pre.model_version]
      ];
      return `<details class="eh-pro-details"><summary>Details · Pro</summary><p class="eh-note">Stored observations and model components. Scores are not calibrated probabilities.</p>${fields.map(([name,value])=>`<h3>${escape(name)}</h3><pre>${escape(value===undefined||value===null||typeof value==='object'&&Object.keys(value).length===0 ? UNAVAILABLE : typeof value==='object'?JSON.stringify(value,null,2):value)}</pre>`).join('')}</details>`;
    }
    function render() {
      if (!activeSymbol) return;
      const view = resolve(activeSymbol), {asset,pre,report,swing,decision,primary,supplied} = view;
      const direction = side(decision?.side ?? pre.bias ?? swing.side ?? report.bias ?? asset.bias);
      const stage = pre.state ?? asset.state ?? asset.stage ?? decision?.entry_status ?? UNAVAILABLE;
      find('candidate-title').textContent = activeSymbol;
      const currentScore = scalar(pre.pre_ignition_score ?? pre.score ?? asset.score);
      find('candidate-heading-meta').textContent = direction+' · '+label(stage)+' · '+view.frame+' · Score '+(currentScore===null?UNAVAILABLE:format(currentScore,1));
      const trigger = decision ? decision.trigger : pre.trigger ?? primary.trigger ?? report.expected_path?.trigger;
      const invalidation = decision ? decision.invalidation : pre.invalidation ?? primary.invalidation ?? report.expected_path?.invalidation;
      const targets = decision ? decision.targets ?? [decision.target_1,decision.target_2] : primary.targets ?? pre.targets ?? [];
      const targetPrices = (Array.isArray(targets)?targets:[]).map(target=>number(target?.price ?? target)).filter(value=>value!==null);
      const consumed = number(pre.move_consumed_estimate_pct ?? supplied.move_consumed_estimate_pct ?? asset.move_consumed_estimate_pct);
      const setup = decision ? decision.setup_quality : report.setup_quality;
      const entry = decision ? decision.entry_quality : report.entry_quality;
      const qualityText = value => scalar(value)===null ? UNAVAILABLE : format(value,1)+'/100'+(value?.status?' · '+label(value.status):'');
      const dataFreshness = freshness(view); dialog.dataset.freshness = /STALE|OFFLINE/.test(dataFreshness)?'STALE':/UNAVAILABLE/.test(dataFreshness)?'UNAVAILABLE':'AVAILABLE';
      const paths = decision ? {base:decision.primary_path, alternative:decision.alternative_path, failure:decision.failure_path} : {
        base:report.scenarios?.base?.steps ?? report.expected_path?.steps,
        bull:report.scenarios?.bull?.steps ?? report.scenarios?.bullish?.steps ?? (report.scenarios?.bullish?.condition?[report.scenarios.bullish]:undefined),
        bear:report.scenarios?.bear?.steps ?? report.scenarios?.bearish?.steps ?? (report.scenarios?.bearish?.condition?[report.scenarios.bearish]:undefined)};
      const bars = options.getBars?.(activeSymbol,view.frame) ?? supplied.bars ?? [];
      const why = decision?.reasons ?? pre.reasons ?? asset.reasons;
      const html = `<p id="candidate-sheet-scope" class="eh-note">Observed setup and conditional scenarios. Missing API data is marked ${UNAVAILABLE}.</p><dl class="eh-history-grid">${history('Score history',supplied.score_history ?? pre.score_history ?? asset.score_history)}${history('Rank history',supplied.rank_history ?? pre.rank_history ?? asset.rank_history,true)}</dl><h3>Trade summary</h3><dl class="eh-summary-grid">${fact('Setup quality',qualityText(setup))}${fact('Entry quality',qualityText(entry))}${fact('Trigger',format(trigger,8))}${fact('Invalidation',format(invalidation,8))}${fact('Targets',targetPrices.length?targetPrices.map(value=>format(value,8)).join(' · '):UNAVAILABLE)}${fact('Move consumed',consumed===null?UNAVAILABLE:format(consumed,1)+'% · estimate from historical baseline; eventual move unknown')}${fact('BTC context',btcContext(view))}${fact('Data freshness',dataFreshness)}</dl>${Array.isArray(why)&&why.length?`<p class="eh-note">${escape(why.slice(0,3).map(label).join(' · '))}</p>`:''}<h3>Expected path · conditional</h3><p class="eh-note">Scenarios describe conditions to review, with no guaranteed future candle or outcome.</p>${Object.entries(paths).map(([name,path])=>pathMarkup(name==='base'?'Base / primary':name==='alternative'?'Alternative / range':name==='failure'?'Failure / invalidation':name[0].toUpperCase()+name.slice(1),path)).join('')}<h3>Observed mini chart · ${escape(view.frame)}</h3>${miniChart(bars,trigger,invalidation)}<button type="button" class="eh-chart-button" id="candidate-open-chart">Open full chart</button>${options.getMode?.()==='pro'?detailMarkup(view):''}`;
      if (html!==lastRender) {
        const content = find('candidate-content'), scroll = content.scrollTop, focused = doc.activeElement?.id;
        const detailsOpen = content.querySelector('.eh-pro-details')?.open;
        content.innerHTML = html; content.scrollTop = scroll; lastRender = html;
        if(detailsOpen && content.querySelector('.eh-pro-details'))content.querySelector('.eh-pro-details').open=true;
        if(focused==='candidate-open-chart')find(focused)?.focus();
      }
    }
    function close() { if(dialog.open)dialog.close(); }
    function open(symbol) {
      if (!symbol) return;
      if(!dialog.open){previousFocus=doc.activeElement;previousFocusSymbol=previousFocus?.closest?.('[data-symbol]')?.dataset.symbol??null;}
      activeSymbol=String(symbol); options.onSelect?.(activeSymbol); render();
      if(!dialog.open)dialog.showModal();
      find('candidate-close').focus();
    }
    listen(find('candidate-close'),'click',close);
    listen(dialog,'close',()=>{activeSymbol=null;lastRender='';
      const replacement=previousFocusSymbol?[...doc.querySelectorAll('[data-symbol]')].find(node=>node.dataset.symbol===previousFocusSymbol&&node.matches('button,a,[tabindex]')):null;
      (previousFocus?.isConnected?previousFocus:replacement??find('global-search'))?.focus();previousFocus=null;previousFocusSymbol=null;
    });
    listen(dialog,'cancel',event=>{event.preventDefault();close();});
    listen(dialog,'click',event=>{
      if(event.target===dialog){const rect=dialog.getBoundingClientRect();if(event.clientX<rect.left||event.clientX>rect.right||event.clientY<rect.top||event.clientY>rect.bottom)close();}
      if(event.target.closest('#candidate-open-chart')){const symbol=activeSymbol;close();options.onOpenChart?.(symbol);}
    });
    listen(find('candidate-expand'),'click',()=>{fullHeight=!fullHeight;dialog.classList.toggle('full-height',fullHeight);find('candidate-expand').setAttribute('aria-pressed',String(fullHeight));find('candidate-expand').textContent=fullHeight?'Compact height':'Full height';});
    function setMuted(symbol, value) {
      symbol=String(symbol);if(value)muted.add(symbol);else muted.delete(symbol);
      save(local,'eh.localMutedSymbols.v1',[...muted]);options.onMuteChange?.(symbol,muted.has(symbol));enhanceWatchlist();return muted.has(symbol);
    }
    function enhanceWatchlist() {
      const list = find('watchlist-items'); if(!list)return;
      for(const row of list.querySelectorAll('[data-symbol]')) {
        const symbol=row.dataset.symbol; row.classList.add('eh-swipe-row');row.classList.toggle('local-muted',muted.has(symbol));
        if(!row.querySelector('.eh-row-actions')) {
          const toggle=doc.createElement('button');toggle.type='button';toggle.className='eh-row-toggle';toggle.setAttribute('aria-label','Quick actions for '+symbol);toggle.setAttribute('aria-expanded','false');toggle.textContent='⋯';row.appendChild(toggle);
          const actions=doc.createElement('div');actions.className='eh-row-actions';actions.innerHTML=`<button type="button" data-local-mute="${escape(symbol)}"></button><button type="button" data-open-chart="${escape(symbol)}">Open chart</button><span class="eh-row-scope">Mute applies to browser notifications on this browser only.</span>`;actions.style.flexWrap='wrap';row.appendChild(actions);
        }
        const muteButton=row.querySelector('[data-local-mute]');muteButton.textContent=(muted.has(symbol)?'Unmute':'Mute')+' locally';muteButton.setAttribute('aria-pressed',String(muted.has(symbol)));
      }
    }
    const watchlist=find('watchlist-items');
    // Capture quick-action clicks before the cockpit's existing row-selection listener.
    listen(watchlist,'click',event=>{
      const clickedRow=event.target.closest('[data-symbol]');
      if(clickedRow&&Number(clickedRow.dataset.swipeUntil)>Date.now()&&!event.target.closest('button')){event.preventDefault();event.stopImmediatePropagation();return;}
      const target=event.target.closest('.eh-row-toggle,[data-local-mute],[data-open-chart]');if(!target)return;
      event.preventDefault();event.stopImmediatePropagation();const row=target.closest('[data-symbol]');
      if(target.matches('.eh-row-toggle')){const isOpen=row.classList.toggle('actions-open');target.setAttribute('aria-expanded',String(isOpen));}
      else if(target.hasAttribute('data-local-mute'))setMuted(target.dataset.localMute,!muted.has(target.dataset.localMute));
      else options.onOpenChart?.(target.dataset.openChart);
    },true);
    listen(watchlist,'pointerdown',event=>{const row=event.target.closest('[data-symbol]');if(!row||event.button>0||event.target.closest('button'))return;rowStates.set(row,{x:event.clientX,y:event.clientY,id:event.pointerId,vertical:false});});
    listen(watchlist,'pointermove',event=>{
      const row=event.target.closest('[data-symbol]'), gesture=row&&rowStates.get(row);if(!gesture||gesture.id!==event.pointerId)return;
      const dx=event.clientX-gesture.x,dy=event.clientY-gesture.y;
      if(Math.abs(dy)>Math.abs(dx)&&Math.abs(dy)>8){gesture.vertical=true;return;}
      if(!gesture.vertical&&Math.abs(dx)>40&&Math.abs(dx)>Math.abs(dy)*1.5){gesture.swiped=true;row.classList.add('actions-open');row.querySelector('.eh-row-toggle')?.setAttribute('aria-expanded','true');}
    },{passive:true});
    for(const name of ['pointerup','pointercancel'])listen(watchlist,name,event=>{const row=event.target.closest('[data-symbol]');if(row){if(rowStates.get(row)?.swiped)row.dataset.swipeUntil=String(Date.now()+350);rowStates.delete(row);}});
    const alertList=find('alerts-list');
    if(alertList?.parentNode) {
      const controls=doc.createElement('div');controls.className='eh-alert-controls';controls.innerHTML=`<label>Direction<select id="eh-alert-direction" aria-label="Alert direction"><option value="ALL">All directions</option><option value="LONG">LONG</option><option value="SHORT">SHORT</option><option value="NEUTRAL">NEUTRAL</option></select></label><label>State<select id="eh-alert-state" aria-label="Alert state"><option value="ALL">All states</option><option value="READY">READY</option><option value="TRIGGER">TRIGGER</option><option value="ACTIVE">ACTIVE</option><option value="FAILED">FAILED</option></select></label>`;
      alertList.parentNode.insertBefore(controls,alertList);added.push(controls);
      const prefs=doc.createElement('details');prefs.className='eh-notification-prefs';prefs.innerHTML=`<summary>Notification preferences · this tab</summary><p class="eh-note">These preferences apply to browser notifications while this tab is open. Enable notifications using the bell. Background push is unavailable.</p><label><input type="checkbox" id="eh-notify-ready"> READY alerts</label><label><input type="checkbox" id="eh-notify-trigger"> TRIGGER alerts</label><label>Cooldown per symbol<input type="number" id="eh-notify-cooldown" min="0" max="3600" step="1" aria-label="Notification cooldown in seconds"> seconds</label>`;
      alertList.insertAdjacentElement('afterend',prefs);added.push(prefs);
      find('eh-notify-ready').checked=preferences.ready;find('eh-notify-trigger').checked=preferences.trigger;find('eh-notify-cooldown').value=String(preferences.cooldown);
      for(const name of ['direction','state'])listen(find('eh-alert-'+name),'change',event=>{filters[name]=event.target.value;options.onAlertFilters?.({...filters});});
      const changePrefs=()=>{preferences.ready=find('eh-notify-ready').checked;preferences.trigger=find('eh-notify-trigger').checked;preferences.cooldown=Math.max(0,Math.min(3600,number(find('eh-notify-cooldown').value)??60));find('eh-notify-cooldown').value=String(preferences.cooldown);save(session,'eh.tabAlertPreferences.v1',preferences);options.onNotificationPreferences?.({...preferences});};
      for(const id of ['eh-notify-ready','eh-notify-trigger','eh-notify-cooldown'])listen(find(id),'change',changePrefs);
    }
    function filterAlerts(alerts) {
      return (Array.isArray(alerts)?alerts:[]).filter(alert=>(filters.direction==='ALL'||side(alert.side??alert.bias??alert.direction)===filters.direction)&&(filters.state==='ALL'||state(alert)===filters.state));
    }
    function shouldNotify(alert) {
      const symbol=String(alert.symbol??alert.exchange_symbol??alert.asset_id??'');if(muted.has(symbol))return false;
      const category=state(alert);if(category==='READY'&&!preferences.ready||category==='TRIGGER'&&!preferences.trigger)return false;
      const now=Date.now(),previous=lastNotification.get(symbol);if(previous!==undefined&&now-previous<preferences.cooldown*1000)return false;
      lastNotification.set(symbol,now);return true;
    }
    listen(doc,'keydown',event=>{
      if(event.defaultPrevented||event.altKey||event.ctrlKey||event.metaKey||event.shiftKey||doc.querySelector('dialog[open]'))return;
      if(event.target?.closest?.('input,textarea,select,[contenteditable]'))return;
      if(event.key==='/'){const search=find('global-search');if(search){event.preventDefault();search.focus();}return;}
      if(event.key.toLowerCase()==='l'||event.key.toLowerCase()==='s'){event.preventDefault();options.onDirectionFilter?.(event.key.toLowerCase()==='l'?'LONG':'SHORT');return;}
      if(!['ArrowUp','ArrowDown'].includes(event.key))return;
      const explicit=options.getRows?.();
      const rows=(Array.isArray(explicit)?explicit:[...doc.querySelectorAll('#discovery-body [data-symbol]')]).filter(row=>typeof row==='string'||row.symbol||row.exchange_symbol||row.dataset?.symbol);
      if(!rows.length)return;event.preventDefault();selectedIndex=Math.max(0,Math.min(rows.length-1,selectedIndex+(event.key==='ArrowDown'?1:-1)));
      for(const row of rows)row.classList?.remove('eh-keyboard-selected');
      const row=rows[selectedIndex],symbol=typeof row==='string'?row:row.dataset?.symbol??row.exchange_symbol??row.symbol;row.classList?.add('eh-keyboard-selected');options.onSelect?.(symbol);row.scrollIntoView?.({block:'nearest'});
    });
    enhanceWatchlist();
    return {open,close,refresh(){enhanceWatchlist();if(dialog.open)render();},enhanceWatchlist,filterAlerts,shouldNotify,
      isMuted:symbol=>muted.has(String(symbol)),setMuted,deliveryStatus,
      destroy(){close();listeners.forEach(remove=>remove());added.forEach(node=>node.remove());}};
  }
  global.ExpansionHunterUI={mount,deliveryStatus};
})(window);
