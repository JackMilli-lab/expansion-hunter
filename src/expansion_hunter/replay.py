"""Causal decision replay, deliberately separate from unvalidated P&L claims."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from .config import Settings
from .models import AssetSnapshot, RegimeReport, ScanResult, Status
from .technical import analyze


def point_in_time(snapshot: AssetSnapshot, as_of: datetime) -> AssetSnapshot:
    """Exclude all data that was unavailable at the evaluation boundary."""
    candles = {
        tf: [c for c in bars if c.close_time <= as_of and c.available_at <= as_of]
        for tf, bars in snapshot.candles.items()
    }

    def visible(metrics):
        output = {}
        for key, value in metrics.items():
            if value.available_at is not None and value.available_at > as_of:
                output[key] = value.model_copy(
                    update={
                        "value": None,
                        "status": Status.UNAVAILABLE,
                        "reason": "not available at replay boundary",
                    }
                )
            elif value.observed_at is not None and value.observed_at > as_of:
                output[key] = value.model_copy(
                    update={
                        "value": None,
                        "status": Status.UNAVAILABLE,
                        "reason": "observation after replay boundary",
                    }
                )
            else:
                output[key] = value
        return output

    return snapshot.model_copy(
        update={
            "as_of": as_of,
            "candles": candles,
            "market": visible(snapshot.market),
            "derivatives": visible(snapshot.derivatives),
            "catalysts": [
                e for e in snapshot.catalysts if e.available_at <= as_of and e.observed_at <= as_of
            ],
        }
    )


def replay(
    snapshot: AssetSnapshot,
    boundaries: Iterable[datetime],
    settings: Settings,
    benchmark: AssetSnapshot | None = None,
) -> list[ScanResult]:
    """Unknown historical market regime blocks entries. Supply recorded runs for exact regime replay.

    A recently downloaded historical series remains invisible before its recorded
    availability; use independently archived point-in-time data for research.
    """
    from .rules import evaluate

    reports = []
    for when in sorted(set(boundaries)):
        cut = point_in_time(snapshot, when)
        tech = {tf: analyze(bars, tf, when) for tf, bars in cut.candles.items()}
        btc = point_in_time(benchmark, when) if benchmark else None
        regime = RegimeReport(
            as_of=when,
            available_at=when,
            reasons=["Historical regime not supplied; entries disabled"],
            missing=["historical_regime"],
        )
        reports.append(evaluate(cut, tech, regime, settings, benchmark=btc))
    return reports
