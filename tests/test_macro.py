"""Synthetic economic/market events test invariants, never historical performance."""

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from expansion_hunter.macro import MacroEngine, scheduled_utc
from expansion_hunter.macro_models import (
    MacroConsensus,
    MacroEvent,
    MacroMarketObservation,
    MacroMode,
    MacroQuality,
    MacroRelease,
    MacroSettings,
    ReleaseKind,
)
from expansion_hunter.providers.macro import BLSCalendarProvider, parse_bls_calendar

T = datetime(2026, 10, 5, 12, 30, tzinfo=UTC)


def event(event_id="cpi", at=T, **changes):
    fields = dict(
        event_id=event_id,
        event_name="Synthetic CPI",
        category="US_CPI",
        scheduled_timestamp=at,
        available_at=at - timedelta(days=7),
        source="SYNTHETIC",
    )
    fields.update(changes)
    return MacroEvent(**fields)


def consensus(event_id="cpi", value=2.8, **changes):
    fields = dict(
        consensus_id="consensus-" + event_id,
        event_id=event_id,
        value=value,
        unit="percent_yoy",
        consensus_timestamp=T - timedelta(minutes=30),
        engine_received_timestamp=T - timedelta(minutes=29),
        source="SYNTHETIC",
    )
    fields.update(changes)
    return MacroConsensus(**fields)


def release(event_id="cpi", value=2.5, **changes):
    fields = dict(
        release_id="initial-" + event_id,
        event_id=event_id,
        value=value,
        unit="percent_yoy",
        actual_release_timestamp=T,
        provider_received_timestamp=T + timedelta(seconds=1),
        engine_received_timestamp=T + timedelta(seconds=2),
        source="SYNTHETIC",
    )
    fields.update(changes)
    return MacroRelease(**fields)


def engine(**settings):
    return MacroEngine([event()], [consensus()], [release()], MacroSettings(**settings))


def tick(seconds, price, **changes):
    at = T + timedelta(seconds=seconds)
    fields = dict(
        asset_id="bitcoin",
        market_event_timestamp=at,
        data_received_timestamp=at,
        price=price,
        source="SYNTHETIC_NATIVE",
    )
    fields.update(changes)
    return MacroMarketObservation(**fields)


def test_macro_initial_surprise_not_probability():
    result = engine().surprise("cpi", T + timedelta(seconds=3))
    assert result.surprise_absolute == pytest.approx(-0.3)
    assert result.surprise_percent == pytest.approx(-0.3 / 2.8 * 100)
    assert result.standardized_surprise is None
    assert result.data_quality == MacroQuality.AVAILABLE
    assert "unvalidated" in result.market_interpretation_context


def test_macro_revision_does_not_overwrite_initial():
    subject = engine()
    revision = release(
        value=2.6,
        release_id="revision-1",
        kind=ReleaseKind.REVISION,
        revision_of="initial-cpi",
        actual_release_timestamp=T + timedelta(days=1),
        provider_received_timestamp=T + timedelta(days=1, seconds=1),
        engine_received_timestamp=T + timedelta(days=1, seconds=2),
    )
    assert subject.add_release(revision)
    assert subject.surprise("cpi", T + timedelta(days=2)).surprise_absolute == pytest.approx(-0.3)
    assert subject.event_view("cpi", T + timedelta(hours=1))["revisions"] == []
    assert subject.event_view("cpi", T + timedelta(days=2))["revisions"][0].value == 2.6


def test_macro_consensus_strictly_received_before_release():
    subject = MacroEngine(
        [event()], [consensus(engine_received_timestamp=T + timedelta(seconds=1))], [release()]
    )
    assert subject.surprise("cpi", T + timedelta(seconds=3)).consensus_value is None
    assert subject.surprise("cpi", T + timedelta(seconds=3)).surprise_absolute is None


def test_macro_latest_consensus_as_known_not_future_consensus():
    subject = engine()
    subject.add_consensus(
        consensus(
            value=3.0,
            consensus_id="next-vintage",
            consensus_timestamp=T + timedelta(hours=1),
            engine_received_timestamp=T + timedelta(hours=1),
        )
    )
    assert subject.surprise("cpi", T + timedelta(hours=2)).consensus_value == 2.8


@pytest.mark.parametrize("before", [T - timedelta(seconds=1), T, T + timedelta(seconds=1)])
def test_macro_late_receipt_not_known_early(before):
    subject = engine()
    assert subject.surprise("cpi", before).actual_initial_value is None
    assert subject.context(before).mode == MacroMode.EVENT_IMMINENT


def test_macro_missing_initial_actual_and_consensus_are_unknown():
    subject = MacroEngine([event()])
    assert subject.surprise("cpi", T + timedelta(days=1)).data_quality == MacroQuality.UNAVAILABLE
    subject.add_release(release(value=None, data_quality=MacroQuality.UNAVAILABLE))
    assert subject.surprise("cpi", T + timedelta(days=1)).actual_initial_value is None


@pytest.mark.parametrize("value", [0, -1])
def test_macro_percentage_surprise_requires_positive_consensus(value):
    subject = MacroEngine([event()], [consensus(value=value)], [release()])
    result = subject.surprise("cpi", T + timedelta(seconds=3))
    assert result.surprise_absolute == pytest.approx(2.5 - value)
    assert result.surprise_percent is None


def test_macro_unit_mismatch_cannot_make_surprise():
    subject = MacroEngine([event()], [consensus(unit="jobs")], [release()])
    result = subject.surprise("cpi", T + timedelta(seconds=3))
    assert result.data_quality == MacroQuality.UNAVAILABLE
    assert result.surprise_absolute is None


def test_macro_previous_value_must_exist_before_release():
    subject = MacroEngine(
        [event()],
        [consensus(previous_value=2.9, previous_value_timestamp=T - timedelta(days=30))],
        [release()],
    )
    assert subject.surprise("cpi", T + timedelta(seconds=3)).previous_value_as_known_before_release == 2.9


def test_macro_append_only_duplicate_and_collision():
    subject = engine()
    assert not subject.add_event(event())
    assert not subject.add_consensus(consensus())
    assert not subject.add_release(release())
    with pytest.raises(ValueError, match="immutable"):
        subject.add_release(release(value=2.6))
    with pytest.raises(ValueError, match="one initial"):
        subject.add_release(release(release_id="second-initial"))


def test_macro_calendar_repoll_retains_original_availability():
    subject = engine()
    original = subject.events["cpi"].available_at
    assert not subject.add_event(event(available_at=T))
    assert subject.events["cpi"].available_at == original
    with pytest.raises(ValueError, match="immutable"):
        subject.add_event(event(scheduled_timestamp=T + timedelta(hours=1), available_at=T))
    with pytest.raises(ValueError, match="immutable"):
        subject.add_event(event(available_at=original - timedelta(days=1)))


def test_macro_out_of_order_revision_receipt_stays_separate():
    revision = release(
        value=2.6,
        release_id="revision",
        kind=ReleaseKind.REVISION,
        revision_of="initial-cpi",
        actual_release_timestamp=T + timedelta(seconds=10),
        provider_received_timestamp=T + timedelta(seconds=11),
        engine_received_timestamp=T + timedelta(seconds=12),
    )
    subject = MacroEngine([event()], [consensus()], [revision])
    assert subject.initial_release("cpi", T + timedelta(seconds=15)) is None
    delayed = release(
        provider_received_timestamp=T + timedelta(seconds=20),
        engine_received_timestamp=T + timedelta(seconds=21),
    )
    subject.add_release(delayed)
    assert subject.surprise("cpi", T + timedelta(seconds=22)).actual_initial_value == 2.5


def test_macro_pending_revision_cannot_conflict_with_initial():
    revision = release(
        event_id="other",
        release_id="revision",
        kind=ReleaseKind.REVISION,
        revision_of="initial-cpi",
        actual_release_timestamp=T + timedelta(seconds=10),
        provider_received_timestamp=T + timedelta(seconds=11),
        engine_received_timestamp=T + timedelta(seconds=12),
    )
    subject = MacroEngine(releases=[revision])
    with pytest.raises(ValueError, match="pending revision"):
        subject.add_release(release())


@pytest.mark.parametrize(
    "offset, expected",
    [
        (-3601, MacroMode.NORMAL),
        (-3600, MacroMode.EVENT_RISK),
        (-301, MacroMode.EVENT_RISK),
        (-300, MacroMode.EVENT_IMMINENT),
        (3, MacroMode.EVENT_RELEASED),
        (33, MacroMode.EVENT_REACTION),
        (903, MacroMode.EVENT_COOLDOWN),
        (3603, MacroMode.NORMAL),
    ],
)
def test_macro_risk_boundaries(offset, expected):
    assert engine().context(T + timedelta(seconds=offset)).mode == expected


def test_macro_overdue_schedule_is_not_a_fabricated_release():
    subject = MacroEngine([event()])
    context = subject.context(T + timedelta(seconds=10))
    assert context.mode == MacroMode.EVENT_IMMINENT
    assert context.actual_release_timestamp is None
    assert context.data_quality == MacroQuality.UNAVAILABLE


def test_macro_unknown_calendar_not_invented_normal_evidence():
    result = MacroEngine([event(available_at=T + timedelta(days=1))]).context(T)
    assert result.data_quality == MacroQuality.UNAVAILABLE
    assert "not evidence of no events" in result.reasons[0]


def test_macro_calendar_pit_range_and_no_mutable_engine_reference():
    subject = engine()
    assert subject.calendar(T - timedelta(days=8)) == []
    result = subject.calendar(T, T - timedelta(hours=1), T + timedelta(hours=1))
    result[0].event_name = "changed outside"
    assert subject.events["cpi"].event_name == "Synthetic CPI"


def test_macro_standardization_uses_only_prior_available_surprises():
    events, consensuses, releases = [event()], [consensus()], [release()]
    for i, surprise in enumerate((-0.2, 0, 0.2)):
        at = T - timedelta(days=30 * (i + 1))
        key = "prior" + str(i)
        events.append(event(key, at))
        consensuses.append(
            consensus(
                key,
                value=2.0,
                consensus_timestamp=at - timedelta(hours=1),
                engine_received_timestamp=at - timedelta(minutes=59),
            )
        )
        releases.append(
            release(
                key,
                value=2.0 + surprise,
                actual_release_timestamp=at,
                provider_received_timestamp=at + timedelta(seconds=1),
                engine_received_timestamp=at + timedelta(seconds=2),
            )
        )
    subject = MacroEngine(events, consensuses, releases, MacroSettings(minimum_standardization_samples=3))
    result = subject.surprise("cpi", T + timedelta(seconds=3))
    assert result.historical_sample_count == 3
    assert result.standardized_surprise == pytest.approx(-1.5)
    # An old economic period learned after today's release must not enter the baseline.
    key = "late-old"
    at = T - timedelta(days=20)
    subject.add_event(event(key, at))
    subject.add_consensus(
        consensus(
            key,
            consensus_timestamp=at - timedelta(hours=1),
            engine_received_timestamp=at - timedelta(minutes=1),
        )
    )
    subject.add_release(
        release(
            key,
            actual_release_timestamp=at,
            provider_received_timestamp=T + timedelta(seconds=4),
            engine_received_timestamp=T + timedelta(seconds=5),
        )
    )
    assert subject.surprise("cpi", T + timedelta(seconds=10)).historical_sample_count == 3


def test_macro_schedule_timezone_dst_conversion():
    assert scheduled_utc(datetime(2026, 1, 15, 8, 30), "America/New_York").hour == 13
    assert scheduled_utc(datetime(2026, 7, 15, 8, 30), "America/New_York").hour == 12
    with pytest.raises(ValueError, match="nonexistent"):
        scheduled_utc(datetime(2026, 3, 8, 2, 30), "America/New_York")
    with pytest.raises(ValueError, match="ambiguous"):
        scheduled_utc(datetime(2026, 11, 1, 1, 30), "America/New_York")
    first = scheduled_utc(datetime(2026, 11, 1, 1, 30), "America/New_York", fold=0)
    second = scheduled_utc(datetime(2026, 11, 1, 1, 30), "America/New_York", fold=1)
    assert second - first == timedelta(hours=1)


def test_macro_naive_timestamp_and_release_order_rejected():
    with pytest.raises(ValueError, match="timezone"):
        event(scheduled_timestamp=datetime(2026, 10, 5))
    with pytest.raises(ValueError, match="ordered"):
        release(provider_received_timestamp=T - timedelta(seconds=1))
    with pytest.raises(ValueError, match="publication"):
        consensus(consensus_timestamp=T + timedelta(seconds=1))


def test_macro_reaction_ticks_and_first_move_reversal():
    observations = [tick(-1, 100), tick(1, 101), tick(5, 102), tick(10, 99)]
    result = engine().reaction("cpi", "bitcoin", observations, T + timedelta(seconds=12), 10)
    assert result.data_quality == MacroQuality.AVAILABLE
    assert result.return_pct == pytest.approx(-1)
    assert result.event_mfe_pct == pytest.approx(2)
    assert result.event_mae_pct == pytest.approx(-1)
    assert result.reversed_first_move
    assert result.resolution_seconds == 0


def test_macro_reaction_horizon_not_matured():
    result = engine().reaction("cpi", "bitcoin", [tick(-1, 100), tick(5, 101)], T + timedelta(seconds=5), 10)
    assert result.return_pct is None
    assert "not matured" in result.reasons[0]


def test_macro_reaction_cannot_know_future_receipts():
    observations = [tick(-1, 100), tick(1, 101), tick(10, 99, data_received_timestamp=T + timedelta(hours=1))]
    result = engine().reaction("cpi", "bitcoin", observations, T + timedelta(seconds=12), 10)
    assert result.return_pct is None
    assert result.data_quality == MacroQuality.STALE


def test_macro_reaction_endpoint_cannot_be_one_second_price_for_ten_second_claim():
    result = engine().reaction("cpi", "bitcoin", [tick(-1, 100), tick(1, 101)], T + timedelta(seconds=12), 10)
    assert result.return_pct is None
    assert "endpoint" in result.reasons[0]


def test_macro_reaction_one_minute_bars_cannot_measure_ten_seconds():
    observations = [tick(-1, 100), tick(60, 105, interval_start_timestamp=T, resolution_seconds=60)]
    result = engine().reaction("cpi", "bitcoin", observations, T + timedelta(seconds=70), 10)
    assert result.data_quality == MacroQuality.UNAVAILABLE
    assert result.return_pct is None


def test_macro_reaction_completed_minute_bars_use_real_interval():
    observations = [
        tick(0, 100, interval_start_timestamp=T - timedelta(seconds=60), resolution_seconds=60),
        tick(60, 102, interval_start_timestamp=T, resolution_seconds=60, high=103, low=99, volume=200),
    ]
    result = engine().reaction("cpi", "bitcoin", observations, T + timedelta(seconds=61), 60)
    assert result.data_quality == MacroQuality.AVAILABLE
    assert result.return_pct == pytest.approx(2)
    assert result.event_mfe_pct == pytest.approx(3)
    assert result.event_mae_pct == pytest.approx(-1)


def test_macro_reaction_interval_crossing_release_is_excluded():
    observations = [
        tick(-1, 100),
        tick(30, 102, interval_start_timestamp=T - timedelta(seconds=30), resolution_seconds=60),
    ]
    result = engine().reaction("cpi", "bitcoin", observations, T + timedelta(seconds=61), 60)
    assert result.return_pct is None


def test_macro_reaction_stale_baseline_and_mixed_providers_are_rejected():
    subject = engine()
    result = subject.reaction(
        "cpi", "bitcoin", [tick(-70, 100), tick(10, 101)], T + timedelta(seconds=12), 10
    )
    assert result.data_quality == MacroQuality.STALE
    result = subject.reaction(
        "cpi", "bitcoin", [tick(-1, 100), tick(10, 101, source="OTHER")], T + timedelta(seconds=12), 10
    )
    assert result.return_pct is None
    assert "mixes providers" in result.reasons[0]


def test_macro_reaction_volume_shock_requires_complete_previous_window():
    observations = [
        tick(-5, 100, volume=2),
        tick(0, 100, volume=3),
        tick(5, 101, volume=4),
        tick(10, 102, volume=6),
    ]
    result = engine().reaction("cpi", "bitcoin", observations, T + timedelta(seconds=12), 10)
    assert result.volume == 10
    assert result.volume_shock == 2
    assert result.liquidity_shock is None


def test_macro_settings_reject_inverted_windows():
    with pytest.raises(ValueError, match="imminent"):
        MacroSettings(risk_window_seconds=60, imminent_window_seconds=300)


ICS = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:cpi-october
SUMMARY:Consumer Price Index
DTSTART;TZID=America/New_York:20261015T083000
END:VEVENT
BEGIN:VEVENT
UID:jobs-november
SUMMARY:Employment Situation
DTSTART:20261106T133000Z
END:VEVENT
END:VCALENDAR
"""


def test_bls_official_calendar_parse_and_receipt_not_backdated():
    events, issues = parse_bls_calendar(ICS, T, BLSCalendarProvider.URL)
    assert not issues
    assert events[0].scheduled_timestamp == datetime(2026, 10, 15, 12, 30, tzinfo=UTC)
    assert events[0].available_at == T
    assert events[0].category == "US_CPI"
    assert events[1].category == "US_NFP"


def test_bls_calendar_bad_records_are_visible_and_no_fake_midnight():
    bad = ICS.replace("DTSTART;TZID=America/New_York:20261015T083000", "DTSTART;VALUE=DATE:20261015")
    events, issues = parse_bls_calendar(bad, T, BLSCalendarProvider.URL)
    assert len(events) == 1
    assert "all-day" in issues[0]
    with pytest.raises(ValueError, match="iCalendar"):
        parse_bls_calendar("blocked html", T, BLSCalendarProvider.URL)


async def test_bls_calendar_provider_and_unavailable_numeric_channels():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text=ICS))
    ) as client:
        provider = BLSCalendarProvider(client, retries=0, clock=lambda: T)
        result = await provider.calendar(T, T + timedelta(days=40))
        assert len(result.events) == 2
        assert result.calendar_quality == MacroQuality.AVAILABLE
        assert result.consensus_quality == MacroQuality.UNAVAILABLE
        assert result.release_quality == MacroQuality.UNAVAILABLE
        releases = await provider.releases(["BLS:cpi-october"])
        assert releases.releases == []
        assert releases.release_quality == MacroQuality.UNAVAILABLE


async def test_bls_provider_failure_is_explicit_not_engine_failure():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(403))) as client:
        result = await BLSCalendarProvider(client, retries=0, clock=lambda: T).calendar(
            T, T + timedelta(days=30)
        )
        assert result.calendar_quality == MacroQuality.UNAVAILABLE
        assert result.events == []
        assert "403" in result.issues[-1]


async def test_bls_provider_retries_transport_without_fake_releases():
    attempts = 0

    def respond(request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("synthetic interruption", request=request)
        return httpx.Response(200, text=ICS)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await BLSCalendarProvider(client, retries=1, clock=lambda: T).calendar(
            T, T + timedelta(days=40)
        )
        assert attempts == 2
        assert result.calendar_quality == MacroQuality.AVAILABLE
        assert result.releases == []


async def test_bls_provider_total_operation_timeout_is_visible():
    async def delayed(request):
        await asyncio.sleep(10)
        return httpx.Response(200, text=ICS)

    async with httpx.AsyncClient(transport=httpx.MockTransport(delayed)) as client:
        result = await BLSCalendarProvider(
            client, retries=0, request_timeout_seconds=0.01, clock=lambda: T
        ).calendar(T, T + timedelta(days=40))
        assert result.calendar_quality == MacroQuality.UNAVAILABLE
        assert "total request timeout" in result.issues[-1]
