"""Causal context regression tests with explicitly artificial prices."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from expansion_hunter.analog import AnalogSettings, match_analogs
from expansion_hunter.config import Settings
from expansion_hunter.discovery import DiscoveryEngine
from expansion_hunter.discovery_config import DiscoverySettings
from expansion_hunter.discovery_models import DiscoveryCycle, DiscoveryFeature, DiscoveryObservation
from expansion_hunter.discovery_ranking import FORMULA_VERSION
from expansion_hunter.intelligence import observation_vector
from expansion_hunter.models import TIMEFRAMES, Asset, AssetSnapshot, Candle, Regime

NOW = datetime(2026, 10, 5, 12, 1, tzinfo=UTC)


def cached_snapshot(asset_id="bitcoin", *, frames=("4h", "1D")):
    candles = {}
    for frame in frames:
        seconds = TIMEFRAMES[frame]
        endpoint = datetime.fromtimestamp(int(NOW.timestamp()) // seconds * seconds, UTC)
        candles[frame] = [
            Candle(
                open_time=endpoint - timedelta(seconds=seconds * (240 - index)),
                close_time=endpoint - timedelta(seconds=seconds * (239 - index)),
                available_at=endpoint - timedelta(seconds=seconds * (239 - index)) + timedelta(seconds=1),
                open=100 + index, high=101 + index, low=99 + index, close=100 + index,
                volume=1000, source="synthetic_test_only:spot:cached-context",
            )
            for index in range(240)
        ]
    return AssetSnapshot(asset=Asset(asset_id=asset_id, symbol=asset_id.upper(), name="Synthetic test"),
                         as_of=NOW - timedelta(seconds=30), candles=candles)


def engine(latest=None, *, universe_size=500):
    # This provider has no methods: calling a market provider would fail the test.
    no_provider_requests = object()
    store = SimpleNamespace(latest_cycle=lambda: latest)
    return DiscoveryEngine(no_provider_requests, no_provider_requests, None, Settings(top_n=universe_size),
                           DiscoverySettings(universe_size=universe_size), store, clock=lambda: NOW)


def test_cached_benchmark_history_produces_real_regime_evidence_without_requests():
    scanner = engine()
    btc = cached_snapshot()
    eth = cached_snapshot("ethereum")
    scanner._snapshots = {"bitcoin": btc, "ethereum": eth}
    before = btc.model_dump()
    result = scanner._market_regime(NOW)
    assert result.regime == Regime.RISK_ON
    assert result.confidence > 0.4
    assert result.metrics["btc_close"] == 339
    assert result.metrics["btc_ema200"] is not None
    assert result.metrics["btc_relative_spot_volume"] == 1
    assert "fresh_btc_4h_and_daily_structure" not in result.missing
    assert "btc_funding" in result.missing
    assert result.available_at == NOW
    assert btc.model_dump() == before  # Align a copy, never rewrite stored evidence.


def test_regime_context_cannot_see_future_or_late_bars():
    scanner = engine()
    btc = cached_snapshot()
    scanner._snapshots = {"bitcoin": btc}
    baseline = scanner._market_regime(NOW)
    future = btc.candles["4h"][-1].model_copy(update={
        "open_time": NOW + timedelta(hours=4), "close_time": NOW + timedelta(hours=8),
        "available_at": NOW + timedelta(hours=8), "close": 100000, "high": 100001,
    })
    btc.candles["4h"].append(future)
    assert scanner._market_regime(NOW).model_dump() == baseline.model_dump()
    # A candle that closes before the decision but is received later is unavailable.
    btc.candles["1D"][-1].available_at = NOW + timedelta(minutes=1)
    unknown = scanner._market_regime(NOW)
    assert unknown.metrics["btc_close"] is None
    assert unknown.risk_multiplier == 0
    assert "fresh_btc_4h_and_daily_structure" in unknown.missing


@pytest.mark.parametrize("frames", [(), ("4h",), ("1D",)])
def test_missing_benchmark_frames_stay_unknown(frames):
    scanner = engine()
    scanner._snapshots = {"bitcoin": cached_snapshot(frames=frames)}
    result = scanner._market_regime(NOW)
    assert result.regime == Regime.NEUTRAL
    assert result.metrics["btc_close"] is None
    assert result.risk_multiplier == 0
    assert "fresh_btc_4h_and_daily_structure" in result.missing


def observation(as_of=NOW):
    return DiscoveryObservation(asset=Asset(asset_id="unknown", symbol="UNKNOWN", name="Synthetic unknown"),
        as_of=as_of, features={"relative_volume": DiscoveryFeature(value=1.2, source_timestamp=as_of,
            retrieved_at=as_of, provider="synthetic_test_only:spot", timeframe="5m", freshness="FRESH")})


def test_later_cycle_publication_cannot_be_backdated_into_original_decision():
    original = observation()
    context = {
        "regime": "RISK_ON", "regime_available_at": NOW.isoformat(),
        "cycle": "EARLY_BULL", "cycle_available_at": (NOW + timedelta(seconds=5)).isoformat(),
        "macro_mode": "EVENT_RELEASED", "macro_mode_available_at": (NOW + timedelta(seconds=5)).isoformat(),
    }
    vector = observation_vector(original, "event", context)
    assert vector.decision_time == NOW
    assert vector.available_at == NOW
    assert vector.regime == "RISK_ON"
    assert vector.cycle is None
    assert vector.macro_context is None
    assert vector.factors["relative_volume"].usable(NOW)


@pytest.mark.parametrize("availability", [None, "not-a-time", "2026-10-05T12:00:00"])
def test_undated_or_invalid_context_does_not_become_original_decision_evidence(availability):
    context = {"cycle": "EARLY_BULL", "cycle_available_at": availability,
               "regime": "RISK_ON", "macro_mode": "EVENT_RELEASED"}
    vector = observation_vector(observation(), "event", context)
    assert vector.regime is None
    assert vector.cycle is None
    assert vector.macro_context is None


def test_context_is_available_at_its_exact_publication_boundary():
    context = {"cycle": "EARLY_BULL", "cycle_available_at": NOW.isoformat()}
    assert observation_vector(observation(), "event", context).cycle == "EARLY_BULL"
    context["cycle_available_at"] = (NOW + timedelta(microseconds=1)).isoformat()
    assert observation_vector(observation(), "event", context).cycle is None


def test_late_features_do_not_enter_original_decision_vector():
    original = observation()
    original.features["volume_acceleration"] = DiscoveryFeature(
        value=99, source_timestamp=NOW, retrieved_at=NOW + timedelta(seconds=1),
        provider="synthetic_test_only:spot", timeframe="5m", freshness="FRESH",
    )
    vector = observation_vector(original, "event", {})
    assert set(vector.factors) == {"relative_volume"}


def test_legacy_analogue_vectors_are_excluded_after_context_schema_change():
    query = observation_vector(observation(), "query", {})
    reference = observation_vector(observation(NOW - timedelta(days=2)), "legacy", {})
    reference.schema_version = "analog-v1"
    report = match_analogs(query, [reference], AnalogSettings(feature_names=["relative_volume"], minimum_features=1))
    assert "feature schema versions differ" in report.excluded["legacy"]
    assert report.matches == []


@pytest.mark.parametrize("change", ["universe", "formula", "oversized"])
def test_changed_universe_or_formula_forces_fresh_broad_scan(change):
    row = observation()
    row.last_light_at = NOW
    latest = DiscoveryCycle(cycle_id="old", started_at=NOW, as_of=NOW, completed_at=NOW,
        database_path=":memory:", config_hash="synthetic", observations=[row],
        config={"discovery": {"universe_size": 500}}, code_version=FORMULA_VERSION)
    assert engine(latest)._last_broad == NOW
    if change == "universe":
        latest.config["discovery"]["universe_size"] = 1000
    elif change == "formula":
        latest.code_version = "old-formula"
    else:
        latest.observations *= 501
    assert engine(latest)._last_broad is None
