"""Small executable verification of the offline million-event harness itself."""

import pytest

from benchmarks.million_events import run_benchmark, run_fast_invariants


def test_fixture_exercises_native_decoder_incremental_state_and_adversarial_cases():
    result = run_benchmark(2500, symbols=5, fast_cases=4, latency_reservoir_size=128)
    assert result["fixture"].startswith("TEST_ONLY_SYNTHETIC:")
    assert result["verdict"] == "PASS", result["failures"]
    assert result["event_attempts"] == 2500
    assert result["decoded_and_applied"] + result["malformed_rejected_at_decoder"] == 2500
    assert result["accepted"] + result["rejected_at_state"] == result["decoded_and_applied"]
    assert set(result["kind_attempts"]) == {"TRADE", "BOOK_TICKER", "DEPTH", "CANDLE"}
    assert result["rejected_at_state"] > 0
    assert result["malformed_rejected_at_decoder"] > 0
    assert result["sequence_gaps_detected"] > 0
    assert result["stale_feed_after_60_seconds"]
    assert result["cache_bounds"]["symbols"] == 5
    assert result["latency_microseconds"]["sample_count"] == 128
    assert result["latency_microseconds"]["p50"] <= result["latency_microseconds"]["p95"]
    assert result["latency_microseconds"]["p95"] <= result["latency_microseconds"]["p99"]


def test_fast_replay_harness_checks_wick_price_future_restart_and_stale_invariants():
    result = run_fast_invariants(cases=12, seed=42)
    assert result["failures"] == []
    assert result["checks"]["wick_is_sweep"] == 6
    assert result["checks"]["close_confirms_without_5m"] == 6
    assert result["checks"]["future_close_rejected"] == 12
    assert result["checks"]["restart_deduplication"] == 12
    assert result["checks"]["stale_close_rejected"] == 12
    assert result["checks"]["future_pivot_does_not_reanchor"] == 1


@pytest.mark.parametrize(
    "options",
    [
        {"event_count": 0},
        {"symbols": 0},
        {"symbols": 51},
        {"fast_cases": 0},
        {"latency_reservoir_size": 0},
    ],
)
def test_load_harness_rejects_invalid_configuration(options):
    with pytest.raises(ValueError):
        run_benchmark(**options)
