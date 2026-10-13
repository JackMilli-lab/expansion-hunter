"""Deterministic SHORT parity, not predictive performance or live evidence."""

from __future__ import annotations

from datetime import timedelta

import pytest
from test_fast import ASSET, NOW, bar, known_state, pressure_history, snapshot, types
from test_live_runtime import native_candle, native_trade
from test_live_runtime import worker as runtime_worker

from expansion_hunter.config import Settings
from expansion_hunter.fast import FastIgnitionMonitor, detect_support, pressure_metrics
from expansion_hunter.fast_models import FastSettings, FastStage, FastState
from expansion_hunter.live_runtime import LiveObservationWorker
from expansion_hunter.models import Candle

worker = runtime_worker


def mirrored(candle: Candle) -> Candle:
    """Test-only exact geometry reflection around100; real prices are not transformed."""
    return candle.model_copy(update={"open": 200 - candle.open, "high": 200 - candle.low,
                                     "low": 200 - candle.high, "close": 200 - candle.close})


def short_state(stage=FastStage.BREAKDOWN_PRESSURE):
    original = known_state()
    return original.model_copy(update={"stage": stage, "bias": "SHORT", "support": original.resistance,
                                       "resistance": None,
                                       "candles": {"1m": [mirrored(c) for c in original.candles["1m"]]}})


def monitor(**settings):
    return FastIgnitionMonitor(FastSettings(**settings), [short_state()])


def next_minute(instance, *, close=99.88, low=None, high=100.5, volume=100, delay=0, tick_size=None):
    candle = mirrored(bar(NOW + timedelta(minutes=1), close=200 - close,
                          high=200 - low if low is not None else None, low=200 - high,
                          volume=volume, delay=delay))
    return instance.evaluate(ASSET, snapshot(candle.available_at, [candle]), tick_size=tick_size)


def test_automatic_short_pressure_from_completed_lower_highs_and_support():
    instance = FastIgnitionMonitor(FastSettings(pressure_score=50, loaded_score=45, pre_ignition_score=40))
    history = [mirrored(c) for c in pressure_history()]
    result = instance.evaluate(ASSET, snapshot(NOW, [mirrored(bar(NOW))], history))
    assert "BREAKDOWN_PRESSURE" in types(result)
    assert result.state.bias == "SHORT" and result.state.resistance is None
    assert result.state.support.discovered_at == NOW
    assert result.state.price > result.state.support.price
    assert result.state.pressure_metrics["distance_to_support_atr"] >= 0
    assert result.state.pressure_metrics["contribution_lower_highs"] > 0
    assert all(event.bias == "SHORT" and event.support and not event.resistance for event in result.events)


def test_short_can_be_explicitly_disabled_without_changing_long_contracts():
    instance = FastIgnitionMonitor(FastSettings(short_enabled=False))
    history = [mirrored(c) for c in pressure_history()]
    result = instance.evaluate(ASSET, snapshot(NOW, [mirrored(bar(NOW))], history))
    assert result.state.bias == "LONG" and "BREAKDOWN_PRESSURE" not in types(result)


def test_restored_short_state_cannot_alert_when_short_monitoring_is_disabled():
    instance = monitor(short_enabled=False)
    assert next_minute(instance).events == []
    result = instance.observe_trade(ASSET.asset_id, 99.8,
                                    market_event_at=NOW + timedelta(minutes=1, seconds=10),
                                    received_at=NOW + timedelta(minutes=1, seconds=10))
    assert result.events == [] and "SHORT monitoring disabled" in result.issues


def test_support_ignores_future_prices_and_future_availability():
    history = [mirrored(c) for c in pressure_history()]
    baseline = detect_support(history, FastSettings(), NOW)
    future = bar(NOW + timedelta(minutes=5), "5m", close=1, open_price=1, high=2, low=0.5)
    assert detect_support(history + [future], FastSettings(), NOW) == baseline
    delayed = history[-2].model_copy(update={"available_at": NOW + timedelta(hours=1), "low": 1})
    assert detect_support(history + [delayed], FastSettings(), NOW).price == baseline.price


def test_support_latest_candle_is_not_selected_as_anchor():
    history = [mirrored(c) for c in pressure_history()]
    candidate = detect_support(history, FastSettings(), NOW)
    assert candidate.event_timestamp < NOW and candidate.kind != "CONFIRMED_SWING_HIGH"


def test_short_wick_below_close_above_is_sweep_never_confirmation():
    result = next_minute(monitor(), close=100.08, low=99.88)
    assert types(result) == ["LIQUIDITY_SWEEP"]
    assert result.events[0].bias == "SHORT"
    assert result.state.confirmed_at is None and result.state.cooldown_until > NOW


def test_short_1m_close_confirms_immediately_without_5m_or15m():
    result = next_minute(monitor())
    assert types(result) == ["1M_BREAKDOWN_CONFIRMED"]
    assert result.state.stage == FastStage.BREAKDOWN_CONFIRMED
    assert result.events[0].support.price == 100 and result.events[0].resistance is None
    assert result.events[0].data_quality["order_book"] == "UNAVAILABLE"


def test_short_volume_changes_quality_not_confirmation():
    weak = next_minute(monitor(), volume=10)
    strong = next_minute(monitor(), volume=500, high=100.25)
    assert weak.state.stage == strong.state.stage == FastStage.BREAKDOWN_CONFIRMED
    assert weak.events[0].quality == "WEAK" and strong.events[0].quality == "STRONG"


@pytest.mark.parametrize("close,tick,confirmed", [(99.98, None, False), (99.975, None, False),
                                                  (99.88, 0.2, False), (99.8, 0.2, False), (99.79, 0.2, True)])
def test_short_atr_tick_buffer_and_strict_boundary(close, tick, confirmed):
    result = next_minute(monitor(), close=close, tick_size=tick)
    assert ("1M_BREAKDOWN_CONFIRMED" in types(result)) == confirmed


def test_short_intraminute_cross_only_attempt_and_deduplicates():
    instance = monitor()
    instance.evaluate(ASSET, snapshot(NOW, [mirrored(bar(NOW))]))
    arguments = {"market_event_at": NOW + timedelta(seconds=10), "received_at": NOW + timedelta(seconds=11)}
    result = instance.observe_trade(ASSET.asset_id, 99.92, **arguments)
    assert types(result) == ["BREAKDOWN_ATTEMPT"] and result.state.confirmed_at is None
    assert instance.observe_trade(ASSET.asset_id, 99.92, **arguments).events == []


@pytest.mark.parametrize("bias", ["LONG", "SHORT"])
@pytest.mark.parametrize("observed_attempt", [True, False])
def test_level_discovered_after_candle_open_requires_proven_causal_cross(bias, observed_attempt):
    state = known_state() if bias == "LONG" else short_state()
    boundary = state.trigger_level
    boundary.discovered_at = NOW + timedelta(milliseconds=250)
    instance = FastIgnitionMonitor(states=[state])
    current = bar(NOW) if bias == "LONG" else mirrored(bar(NOW))
    instance.evaluate(ASSET, snapshot(NOW + timedelta(seconds=1), [current]))
    if observed_attempt:
        attempt = instance.observe_trade(ASSET.asset_id, 100.08 if bias == "LONG" else 99.92,
                                         market_event_at=NOW + timedelta(seconds=10),
                                         received_at=NOW + timedelta(seconds=11))
        assert types(attempt) == ["BREAKOUT_ATTEMPT" if bias == "LONG" else "BREAKDOWN_ATTEMPT"]
    closed = bar(NOW + timedelta(minutes=1), close=100.12)
    if bias == "SHORT":
        closed = mirrored(closed)
    result = instance.evaluate(ASSET, snapshot(closed.available_at, [closed]))
    confirmation = "1M_BREAKOUT_CONFIRMED" if bias == "LONG" else "1M_BREAKDOWN_CONFIRMED"
    assert (confirmation in types(result)) == observed_attempt


def test_short_failed_breakdown_when_support_is_reclaimed():
    instance = monitor()
    next_minute(instance)
    when = NOW + timedelta(minutes=2)
    result = instance.evaluate(ASSET, snapshot(when, [mirrored(bar(when, close=99.9))]))
    assert types(result) == ["FAILED_BREAKDOWN"]
    assert result.state.cooldown_until > when and result.state.maximum_excursion_pct > 0


def test_short_5m_acceptance_then15m_structure_are_secondary():
    instance = monitor()
    assert types(next_minute(instance)) == ["1M_BREAKDOWN_CONFIRMED"]
    when = NOW + timedelta(minutes=5)
    one = [mirrored(bar(NOW + timedelta(minutes=i), close=100.2, high=100.3, low=100.1, open_price=100.15))
           for i in range(2, 6)]
    result = instance.evaluate(ASSET, snapshot(when, one, [mirrored(bar(when, "5m", close=100.2))]))
    assert types(result) == ["5M_ACCEPTED_DOWN"]
    when = NOW + timedelta(minutes=15)
    one = [mirrored(bar(NOW + timedelta(minutes=i), close=100.3, high=100.4, low=100.1, open_price=100.2))
           for i in range(6, 16)]
    result = instance.evaluate(ASSET, snapshot(when, one, fifteen=[mirrored(bar(when, "15m", close=100.3))]))
    assert types(result) == ["15M_EXPANSION_CONFIRMED_DOWN"]


def test_short_failed_5m_acceptance_does_not_force_bearish_continuation():
    instance = monitor()
    next_minute(instance)
    when = NOW + timedelta(minutes=5)
    one = [mirrored(bar(NOW + timedelta(minutes=i), close=100.1)) for i in range(2, 6)]
    result = instance.evaluate(ASSET, snapshot(when, one, [mirrored(bar(when, "5m", close=99.95))]))
    assert types(result) == ["FAILED_BREAKDOWN"]


def test_short_restart_preserves_bias_support_dedupe_and_requires_fresh_data():
    result = next_minute(monitor())
    restored = FastIgnitionMonitor(states=[FastState.model_validate_json(result.state.model_dump_json())])
    assert restored.states[ASSET.asset_id].stale
    assert restored.states[ASSET.asset_id].bias == "SHORT"
    assert next_minute(restored).events == []
    assert not restored.states[ASSET.asset_id].stale


def test_short_support_stays_frozen_after_confirmation():
    instance = monitor()
    result = next_minute(instance)
    boundary = result.state.support.model_dump()
    when = NOW + timedelta(minutes=5)
    one = [mirrored(bar(NOW + timedelta(minutes=i), close=100.2)) for i in range(2, 6)]
    result = instance.evaluate(ASSET, snapshot(when, one, [bar(when, "5m", close=105, high=106)]))
    assert result.state.support.model_dump() == boundary


def test_short_pressure_distances_and_native_flow_missingness_are_explicit():
    history = [mirrored(c) for c in pressure_history()]
    support = detect_support(history, FastSettings(), NOW)
    metrics = pressure_metrics(history, support, FastSettings(), bias="SHORT")
    assert "distance_to_resistance_atr" not in metrics
    assert metrics["taker_buy_ratio"] is None and metrics["taker_sell_ratio"] is None
    assert metrics["liquidity_below_support"] is None


def test_short_stale_candles_cannot_confirm():
    result = next_minute(monitor(), delay=120)
    assert "1M_BREAKDOWN_CONFIRMED" not in types(result) and result.state.stale


def test_native_runtime_short_attempt_confirmation_retention_and_persistence(worker):
    asset = worker.assets["BTCUSDT"]
    state = short_state()
    shift = worker.clock() - NOW
    state.asset = asset
    state.support = state.support.model_copy(update={
        "event_timestamp": state.support.event_timestamp + shift,
        "discovered_at": state.support.discovered_at + shift,
    })
    state.candles = {"1m": [c.model_copy(update={"open_time": c.open_time + shift,
                                                "close_time": c.close_time + shift,
                                                "available_at": c.available_at + shift})
                            for c in state.candles["1m"]]}
    state.last_processed["1m"] += shift
    state.last_evaluation_at = worker.clock()
    state.stale = False
    state.data_quality["spot_candles"] = "AVAILABLE"
    worker.monitor.states[asset.asset_id] = state
    worker.state.seed_candles("BTCUSDT", "1m", state.candles["1m"], worker.clock())
    origin = worker.clock()
    attempt = native_trade(origin + timedelta(seconds=10), price=99.92)
    worker.test_clock[0] = attempt.processed_timestamp
    worker.apply(attempt)
    assert state.stage == FastStage.BREAKDOWN_ATTEMPT
    marker = worker.store.get_state("fast_retention", "BTCUSDT")
    assert marker["bias"] == "SHORT" and marker["trigger_price"] == 100
    confirmed = native_candle(origin + timedelta(minutes=1), close=99.88,
                              low=99.8, high=100.25, opened_price=100.2)
    worker.test_clock[0] = confirmed.processed_timestamp
    worker.apply(confirmed)
    assert state.stage == FastStage.BREAKDOWN_CONFIRMED
    events = worker.store.objects("fast_event", worker.clock())
    assert [event["event_type"] for event in events] == ["BREAKDOWN_ATTEMPT", "1M_BREAKDOWN_CONFIRMED"]
    assert events[-1]["bias"] == "SHORT" and events[-1]["support"]["price"] == 100
    assert worker.store.get_state("fast_state", asset.asset_id)["resistance"] is None
    assert worker.store._db.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0] == 2


@pytest.mark.parametrize("lifetime", [60, 900])
def test_temporary_chart_interest_subscribes_without_permanent_watchlist(tmp_path, lifetime):
    worker = LiveObservationWorker(Settings(db_path=str(tmp_path / "interest.sqlite3")), clock=lambda: NOW)
    try:
        worker.store.put_state("chart_interest", "HBARUSDT", NOW, {
            "asset_id": "hedera-hashgraph", "symbol": "HBARUSDT", "created_at": NOW.isoformat(),
            "expires_at": (NOW + timedelta(seconds=lifetime)).isoformat(),
        })
        membership = worker.membership()
        assert list(membership) == ["HBARUSDT"]
        worker.assets = worker._subscriptions(membership)
        assert "HBARUSDT" in worker.assets and worker.store.watchlist() == []
        assert worker.monitor.states == {}
        assert worker.metrics()["chart_interest"]["subscribed_symbols"] == ["HBARUSDT"]
    finally:
        worker.store.close()


@pytest.mark.parametrize("offset,lifetime,expected_issue", [(0, 901, True), (0, 0, True),
                                                            (-1000, 900, False), (60, 900, False)])
def test_temporary_chart_interest_rejects_expired_future_or_unbounded_request(tmp_path, offset, lifetime, expected_issue):
    worker = LiveObservationWorker(Settings(db_path=str(tmp_path / "interest.sqlite3")), clock=lambda: NOW)
    try:
        created = NOW + timedelta(seconds=offset)
        worker.store.put_state("chart_interest", "HBARUSDT", NOW, {
            "asset_id": "hedera-hashgraph", "symbol": "HBARUSDT", "created_at": created.isoformat(),
            "expires_at": (created + timedelta(seconds=lifetime)).isoformat(),
        })
        assert worker.membership() == {}
        assert bool(worker.metrics()["chart_interest"]["invalid_requests"]) == expected_issue
    finally:
        worker.store.close()


def test_temporary_chart_interest_survives_restart_but_expires(tmp_path):
    settings = Settings(db_path=str(tmp_path / "interest.sqlite3"))
    worker = LiveObservationWorker(settings, clock=lambda: NOW)
    worker.store.put_state("chart_interest", "HBARUSDT", NOW, {
        "asset_id": "hedera-hashgraph", "symbol": "HBARUSDT", "created_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(seconds=900)).isoformat(),
    })
    worker.store.close()
    restarted = LiveObservationWorker(settings, clock=lambda: NOW + timedelta(minutes=1))
    try:
        assert "HBARUSDT" in restarted.membership()
        restarted.clock = lambda: NOW + timedelta(minutes=15)
        assert restarted.membership() == {}
    finally:
        restarted.store.close()


def test_temporary_chart_interest_respects_capacity_and_personal_priority(tmp_path):
    worker = LiveObservationWorker(Settings(db_path=str(tmp_path / "interest.sqlite3")), clock=lambda: NOW)
    worker.feed_settings.max_symbols = 1
    worker.feed_settings.benchmark_symbols = []
    try:
        worker.store.watchlist_add("ripple", "XRPUSDT")
        worker.store.put_state("chart_interest", "HBARUSDT", NOW, {
            "asset_id": "hedera-hashgraph", "symbol": "HBARUSDT", "created_at": NOW.isoformat(),
            "expires_at": (NOW + timedelta(seconds=900)).isoformat(),
        })
        worker.assets = worker._subscriptions(worker.membership())
        assert list(worker.assets) == ["XRPUSDT"]
        assert worker.metrics()["chart_interest"]["unavailable_due_capacity"] == ["HBARUSDT"]
        assert len(worker.store.watchlist()) == 1
    finally:
        worker.store.close()


def test_cached_active_fibonacci_and_cycle_context_reach_selected_live_payload(worker):
    worker._analysis["BTCUSDT"] = {"active_fibonacci": {"1m": {"status": "INSUFFICIENT_DATA"}},
                                    "sessions": {"status": "UNAVAILABLE"}, "market_regime": "RISK_OFF",
                                    "cycle_context": {"phase": "EARLY_BEAR"},
                                    "macro_context": {"status": "EVENT_RISK"}}
    worker._publish("BTCUSDT", worker.clock())
    payload = worker.store.get_state("live_asset", "BTCUSDT")
    assert payload["active_fibonacci"]["1m"]["status"] == "INSUFFICIENT_DATA"
    assert payload["market_regime"] == "RISK_OFF" and payload["sessions"]["status"] == "UNAVAILABLE"


@pytest.mark.parametrize("bias", ["LONG", "SHORT"])
def test_demoted_state_explicit_repromotion_starts_fresh_episode(bias):
    old = known_state() if bias == "LONG" else short_state()
    old.stage = FastStage.DEMOTED
    old.demoted_at = NOW
    old.confirmed_at = old.accepted_at = old.structural_at = NOW - timedelta(minutes=10)
    old.cooldown_until = NOW + timedelta(days=1)
    old.dedupe_keys = ["old-episode"]
    instance = FastIgnitionMonitor(states=[old])
    when = NOW + timedelta(seconds=1)
    candle = bar(NOW) if bias == "LONG" else mirrored(bar(NOW))
    result = instance.evaluate(ASSET, snapshot(when, [candle]))
    assert result.state.stage == FastStage.WATCH and result.events == []
    assert result.state.promoted_at == when and result.state.demoted_at is None
    assert result.state.resistance is None and result.state.support is None
    assert result.state.confirmed_at is None and result.state.cooldown_until is None
    assert result.state.dedupe_keys == [] and not result.state.stale
    assert len(result.state.candles["1m"]) == 1


def test_trade_alone_cannot_resurrect_demoted_episode():
    state = short_state(FastStage.DEMOTED)
    state.demoted_at = NOW
    instance = FastIgnitionMonitor(states=[state])
    result = instance.observe_trade(ASSET.asset_id, 99.8, market_event_at=NOW + timedelta(seconds=10),
                                    received_at=NOW + timedelta(seconds=10))
    assert result.state.stage == FastStage.DEMOTED and result.events == []


def test_promote_preserves_active_frozen_support_and_rejects_future_demotion():
    instance = monitor()
    confirmed = next_minute(instance).state
    before = instance.states[ASSET.asset_id].model_dump()
    active = instance.promote(ASSET, NOW + timedelta(minutes=2))
    assert active.model_dump() == before and active.support == confirmed.support
    active.stage = FastStage.DEMOTED
    active.demoted_at = NOW + timedelta(minutes=3)
    with pytest.raises(ValueError, match="promotion cannot precede"):
        instance.promote(ASSET, NOW + timedelta(minutes=2))


def test_runtime_demote_repromote_records_transition_and_new_real_pressure(worker):
    from test_live_runtime import seed_pressure
    seed_pressure(worker)
    asset = worker.assets["BTCUSDT"]
    old = worker.monitor.states[asset.asset_id]
    prior_level = old.resistance.model_copy()
    original_events = worker.store.objects("fast_event", worker.clock())
    old.stage = FastStage.DEMOTED
    old.demoted_at = worker.clock()
    old.cooldown_until = worker.clock() + timedelta(days=1)
    worker.test_clock[0] += timedelta(seconds=1)
    worker._evaluate("BTCUSDT", worker.clock())
    state = worker.monitor.states[asset.asset_id]
    assert state.stage == FastStage.BREAKOUT_PRESSURE and not state.stale
    assert state.resistance.discovered_at > prior_level.discovered_at
    assert state.cooldown_until is None and state.demoted_at is None
    transitions = worker.store.objects("fast_membership_transition", worker.clock())
    assert len(transitions) == 1 and transitions[0]["previous_stage"] == "DEMOTED"
    assert transitions[0]["stage"] == "WATCH" and transitions[0]["reason"] == "DYNAMIC_DISCOVERY"
    assert len(worker.store.objects("fast_event", worker.clock())) > len(original_events)
    assert worker.store.get_state("fast_state", asset.asset_id)["stage"] == "BREAKOUT_PRESSURE"


def test_restart_then_temporary_chart_interest_repromotes_hbar_without_old_levels(tmp_path):
    from expansion_hunter.models import Asset
    asset = Asset(asset_id="hedera-hashgraph", symbol="HBAR", name="Test-only HBAR", exchange_symbol="HBARUSDT")
    settings = Settings(db_path=str(tmp_path / "restored-interest.sqlite3"))
    prior = LiveObservationWorker(settings, clock=lambda: NOW)
    old = FastState(asset=asset, stage=FastStage.DEMOTED, demoted_at=NOW,
                    cooldown_until=NOW + timedelta(days=1), candles={"1m": [bar(NOW - timedelta(days=1))]})
    prior.store.put_state("fast_state", asset.asset_id, NOW, old)
    prior.store.put_state("chart_interest", "HBARUSDT", NOW, {
        "asset_id": asset.asset_id, "symbol": asset.exchange_symbol, "asset": asset.model_dump(mode="json"),
        "created_at": NOW.isoformat(), "expires_at": (NOW + timedelta(seconds=900)).isoformat(),
    })
    prior.store.close()
    restarted = LiveObservationWorker(settings, clock=lambda: NOW + timedelta(seconds=1))
    try:
        restarted.assets = restarted._subscriptions(restarted.membership())
        immediately_restored = restarted.store.get_state("fast_state", asset.asset_id)
        assert immediately_restored["stage"] == "WATCH" and immediately_restored["stale"]
        restarted.state.seed_candles("HBARUSDT", "1m", [bar(NOW)], restarted.clock())
        restarted._evaluate("HBARUSDT", restarted.clock())
        current = restarted.monitor.states[asset.asset_id]
        assert current.stage == FastStage.WATCH and not current.stale
        assert current.cooldown_until is None and current.demoted_at is None
        assert len(current.candles["1m"]) == 1 and current.candles["1m"][0].close_time == NOW
        transition = restarted.store.objects("fast_membership_transition", restarted.clock())[-1]
        assert transition["reason"] == "TEMPORARY_CHART_INTEREST"
        assert restarted.store.watchlist() == []
    finally:
        restarted.store.close()


def test_directional_deep_scores_use_broad_bias_until_fresh_fast_level_exists(worker):
    worker._discovery_bias = {"BTCUSDT": "SHORT"}
    level_less = FastState(asset=worker.assets["BTCUSDT"])
    assert worker._analysis_bias("BTCUSDT", level_less) == "SHORT"
    assert worker._analysis_bias("UNKNOWNUSDT", None) == "UNAVAILABLE"
    current = short_state()
    current.stale = False
    assert worker._analysis_bias("BTCUSDT", current) == "SHORT"
    current.stale = True
    assert worker._analysis_bias("BTCUSDT", current) == "UNAVAILABLE"
    long = known_state()
    long.stale = False
    assert worker._analysis_bias("BTCUSDT", long) == "LONG"
