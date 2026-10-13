"""Reuse existing War Room analysis for a bounded promoted watch, off fast path."""
from __future__ import annotations

import asyncio
from concurrent.futures import ProcessPoolExecutor
from functools import partial
from multiprocessing import get_context

from .live_runtime import _calculate_analysis, chart_snapshot
from .models import utcnow


def candidate_fast_state(candidate, episodes):
    """Translate actual frozen boundary evidence into the existing interface."""
    episode = episodes.get(f"{candidate['asset_id']}|{candidate['bias']}|CONTINUATION", {})
    if not episode and candidate.get("trigger") and candidate.get("trigger_discovered_at"):
        # New early detector may discover a causal boundary before the older
        # pressure thresholds create a native episode. Do not invent a break.
        episode = {"trigger": candidate["trigger"], "atr": candidate.get("metrics", {}).get("trigger_atr"),
                   "discovered_at": candidate["trigger_discovered_at"], "phase": "STRUCTURE_WATCH"}
    if not episode:
        return {}
    boundary = {"price": episode["trigger"], "lower": episode["trigger"], "upper": episode["trigger"],
                "atr": episode["atr"], "discovered_at": episode["discovered_at"],
                "event_timestamp": episode["discovered_at"], "reason": "Previously defined native 5m boundary"}
    stage = "1M_BREAKDOWN_CONFIRMED" if candidate["bias"] == "SHORT" else "1M_BREAKOUT_CONFIRMED"
    if episode.get("phase") != "BREAK_CONFIRMED":
        stage = {"RADAR": "RADAR", "WATCH": "WATCH", "READY": "LOADED"}.get(candidate["state"],
            candidate["state"] if candidate["state"] in {"WATCH", "PRE_IGNITION", "LOADED"} else "WATCH")
    return {"stage": stage, "bias": candidate["bias"], "last_evaluation_at": candidate["as_of"],
            "support" if candidate["bias"] == "SHORT" else "resistance": boundary,
            "data_quality": candidate.get("data_quality", {})}


def select_deep_candidates(published, limit):
    """Prefer measurable internal change; legacy selection remains available."""
    if limit <= 0:
        return {}
    new_enabled = published.get("pre_ignition_enabled", False)
    if new_enabled:
        limit = min(limit, 40)
    candidates = published.get("pre_ignition", []) if new_enabled else published.get("pre_move", [])
    priority = "early_detection_priority" if new_enabled else "score"
    selected = {}
    for row in sorted(candidates, key=lambda r: (-(r.get(priority) or 0), r.get("symbol", ""),
                                               r.get("bias", ""))):
        if (row.get("status") not in {None, "AVAILABLE"}
                or row.get("state") in {"NORMAL", "FAILED", "EXHAUSTION"}):
            continue
        selected.setdefault(row["symbol"], row)
        if len(selected) >= limit:
            break
    return selected


async def run_pre_move_deep_worker(app, config):
    """Single CPU process; rolling histories are frozen before submission."""
    app.state.pre_move_analysis = {}
    app.state.pre_move_deep_health = {"status": "STARTING", "configured_limit": config.deep_watch_limit}
    if not config.deep_watch_limit:
        app.state.pre_move_deep_health = {"status": "DISABLED", "configured_limit": 0}
        return
    executor = ProcessPoolExecutor(max_workers=1, mp_context=get_context("spawn"))
    last_revision = {}
    try:
        while True:
            worker = getattr(app.state, "trade_alert_worker", None)
            now = utcnow()
            if worker:
                # Published copies are independent of the fast evaluation thread.
                selected = select_deep_candidates(worker.snapshot(), config.deep_watch_limit)
                for symbol in (app.state.pre_move_analysis.keys() | last_revision.keys()) - selected.keys():
                    app.state.pre_move_analysis.pop(symbol, None)
                    last_revision.pop(symbol, None)
                for symbol, row in selected.items():
                    try:
                        snapshot = chart_snapshot(worker.assets[symbol], worker._state(symbol), now)
                        revision = tuple((frame, bars[-1].close_time if bars else None)
                                         for frame, bars in snapshot.candles.items())
                        revision += (("bias", row["bias"]),)
                        if last_revision.get(symbol) == revision:
                            continue
                        btc = chart_snapshot(worker.assets["BTCUSDT"], worker._state("BTCUSDT"), now) if "BTCUSDT" in worker.assets else None
                        eth = chart_snapshot(worker.assets["ETHUSDT"], worker._state("ETHUSDT"), now) if "ETHUSDT" in worker.assets else None
                        old = app.state.pre_move_analysis.get(symbol, {}).get("war_room", {})
                        fast = candidate_fast_state(row, worker._episode_state.get("episodes", {}))
                        operation = partial(_calculate_analysis, symbol, snapshot, None, btc, eth,
                                            {"provider": "BINANCE_NATIVE", **row.get("data_quality", {})},
                                            now, fast, old, {}, row["bias"])
                        analysis = await asyncio.get_running_loop().run_in_executor(executor, operation)
                        app.state.pre_move_analysis[symbol] = analysis
                        analysis["pre_ignition"] = {key: row.get(key) for key in (
                            "pre_ignition_score", "early_detection_priority", "state", "score_velocity",
                            "score_acceleration", "rank_velocity", "rank_acceleration", "evidence_coverage")}
                        last_revision[symbol] = revision
                        await asyncio.to_thread(app.state.store.put_state, "trade_alert_analysis", symbol, now, analysis)
                        await asyncio.to_thread(app.state.store.append, "trade_alert_analysis", f"{symbol}:{now.isoformat()}",
                                                now, now, analysis, worker.assets[symbol].asset_id)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        app.state.pre_move_deep_health = {"status": "FAILED", "symbol": symbol,
                                                        "reason": f"{type(exc).__name__}: {exc}"}
                if app.state.pre_move_deep_health.get("status") != "FAILED":
                    app.state.pre_move_deep_health = {"status": "RUNNING", "as_of": now.isoformat(),
                        "selected": len(selected), "calculated": len(app.state.pre_move_analysis),
                        "configured_limit": config.deep_watch_limit,
                        "scope": "Reused conditional War Room; historical similarity is unvalidated"}
            await asyncio.sleep(config.deep_refresh_seconds)
    finally:
        # Shutdown does not run CPU work on the event loop.
        await asyncio.to_thread(executor.shutdown, wait=True, cancel_futures=True)
        app.state.pre_move_deep_health = {"status": "STOPPED"}
