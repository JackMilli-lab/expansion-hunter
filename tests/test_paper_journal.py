"""Synthetic, explicitly labeled paper-account arithmetic and causal tests."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from expansion_hunter.intelligence_store import IntelligenceStore
from expansion_hunter.macro_models import MacroMarketObservation, MacroQuality
from expansion_hunter.paper_journal import (
    PaperCloseRequest,
    PaperJournal,
    PaperOpenRequest,
    evaluate_position,
)

START = datetime(2025, 1, 1, tzinfo=UTC)
SOURCE = "synthetic_test_only: native-format paper journal intervals"


@pytest.fixture
def store(tmp_path):
    instance = IntelligenceStore(str(tmp_path / "paper.sqlite3"))
    yield instance
    instance.close()


def request(*, identity="manual-1", side="LONG", funding=0, **changes):
    trade = {"side": side, "entry": 100, "target": 110 if side == "LONG" else 90,
             "stop": 95 if side == "LONG" else 105, "margin": 1000, "leverage": 2,
             "entry_fee_rate": 0.001, "exit_fee_rate": 0.001, "funding_cost": funding}
    trade.update(changes.pop("trade", {}))
    return PaperOpenRequest(request_id=identity, asset_id="test-asset", symbol="TESTUSDT",
                            quote_currency="USDT", trade=trade, **changes)


def observation(index=0, *, high=103, low=98, price=101, start=START, **changes):
    opened = start + timedelta(minutes=index)
    payload = {"asset_id": "test-asset", "market_event_timestamp": opened + timedelta(minutes=1),
               "interval_start_timestamp": opened, "data_received_timestamp": opened + timedelta(minutes=1),
               "resolution_seconds": 60, "price": price, "high": high, "low": low,
               "volume": 100, "source": SOURCE}
    payload.update(changes)
    return MacroMarketObservation(**payload)


def archive(store, rows):
    for index, row in enumerate(rows):
        store.append("market_observation", f"paper-fixture:{index}:{row.market_event_timestamp}",
                     row.market_event_timestamp, row.data_received_timestamp, row, row.asset_id)


def test_real_sqlite_migration_is_additive_wal_and_restart_safe(store):
    store.append("legacy-proof", "proof", START, START, {"original": True})
    journal = PaperJournal(store)
    opened = journal.open(request(), at=START)
    assert journal.get(opened.position_id) == opened
    assert store._db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert store.objects("legacy-proof", START) == [{"original": True}]
    assert store._db.execute("SELECT version FROM paper_journal_migrations").fetchall() == [(1,)]
    other = IntelligenceStore(store.path)
    try:
        assert PaperJournal(other).get(opened.position_id) == opened
        assert PaperJournal(other).list()[0].entry_source == "USER_DECLARED_NOT_EXECUTED"
    finally:
        other.close()


def test_open_idempotency_preserves_original_snapshot_and_time(store):
    journal = PaperJournal(store)
    original = journal.open(request(), at=START)
    duplicate = journal.open(request(), at=START + timedelta(days=1))
    assert duplicate == original
    assert len(journal.history(original.position_id, as_of=START + timedelta(days=1))) == 1
    with pytest.raises(ValueError, match="immutable"):
        journal.open(request(trade={"margin": 2000}), at=START)
    assert journal.get(original.position_id) == original


def test_entry_snapshot_is_causal_sourced_and_immutable(store):
    report = {"as_of": START.isoformat(), "market_regime": "RISK_ON", "timeframes": {"5m": {"state": "COMPRESSION"}}}
    store.put_state("war_room_state", "TESTUSDT", START, report)
    journal = PaperJournal(store)
    position = journal.open(request(), at=START + timedelta(seconds=1))
    assert position.feature_snapshot["status"] == "AVAILABLE"
    assert position.feature_snapshot["source"] == "war_room_state:derived_public_market_data"
    assert position.regime == "RISK_ON"
    store.put_state("war_room_state", "TESTUSDT", START + timedelta(minutes=1),
                    {"as_of": (START + timedelta(minutes=1)).isoformat(), "market_regime": "RISK_OFF"})
    assert journal.get(position.position_id).feature_snapshot["payload"] == report


@pytest.mark.parametrize("offset,status", [(60, "UNAVAILABLE"), (-120, "STALE")])
def test_future_or_stale_snapshot_not_credited_as_entry_context(store, offset, status):
    stamp = START + timedelta(seconds=offset)
    store.put_state("war_room_state", "TESTUSDT", stamp, {"as_of": stamp.isoformat(), "market_regime": "RISK_ON"})
    position = PaperJournal(store).open(request(), at=START)
    assert position.feature_snapshot["status"] == status
    assert position.regime is None


def test_outcome_columns_cannot_become_entry_features(store):
    store.put_state("war_room_state", "TESTUSDT", START, {"as_of": START.isoformat(), "future_return": 2})
    with pytest.raises(ValueError, match="future outcomes"):
        PaperJournal(store).open(request(), at=START)
    assert not PaperJournal(store).list()


def test_backdated_feature_asof_does_not_hide_later_publication(store):
    store.put_state("war_room_state", "TESTUSDT", START + timedelta(hours=1),
                    {"as_of": START.isoformat(), "market_regime": "RISK_ON"})
    opened = PaperJournal(store).open(request(), at=START)
    assert opened.feature_snapshot["status"] == "UNAVAILABLE"
    assert opened.regime is None


def test_open_size_costs_risk_are_existing_calculator_values(store):
    position = PaperJournal(store).open(request(funding=3), at=START)
    assert position.position_size == 20
    assert position.notional == 2000
    assert position.initial_net_risk == pytest.approx(106.9)
    assert position.financial_result is None
    assert position.mfe_return is None
    assert position.mode == "PAPER_ONLY"


@pytest.mark.parametrize("side,high,low,price,net", [
    ("LONG", 112, 100, 111, 192.8), ("SHORT", 100, 88, 89, 193.2),
])
def test_directional_primary_target_closes_cost_aware_without_future_bars(store, side, high, low, price, net):
    position = PaperJournal(store).open(request(side=side, funding=3), at=START)
    rows = [observation(high=high, low=low, price=price), observation(1, high=200, low=1, price=100)]
    result = evaluate_position(position, rows, as_of=START + timedelta(minutes=1))
    assert result.status == "CLOSED"
    assert result.exit_kind == "TARGET"
    assert result.exit_source == "PAPER_MODEL_LEVEL_FILL"
    assert result.financial_result.gross_pnl == 200
    assert result.financial_result.net_pnl == pytest.approx(net)
    assert result.closed_at == START + timedelta(minutes=1)
    assert result.exit_interval_start == START
    assert result.exit_interval_end == result.closed_at
    assert result.samples == 1
    assert result.mfe_return == pytest.approx(0.1)
    assert result.mae_return == 0
    assert result.excursion_status == "LOWER_BOUND_EXIT_BAR_ORDER_UNKNOWN"


@pytest.mark.parametrize("side,high,low,price", [
    ("LONG", 101, 93, 94), ("SHORT", 107, 99, 106),
])
def test_stop_exit_signed_pnl_and_conservative_excursions(store, side, high, low, price):
    position = PaperJournal(store).open(request(side=side), at=START)
    result = evaluate_position(position, [observation(high=high, low=low, price=price)],
                               as_of=START + timedelta(minutes=1))
    assert result.status == "CLOSED"
    assert result.exit_kind == "STOP"
    assert result.financial_result.gross_pnl == -100
    assert result.financial_result.net_pnl < -100
    assert result.financial_result.r_multiple == pytest.approx(-1)
    assert result.mae_return == pytest.approx(-0.05)
    assert result.mfe_return == 0  # The bar's favorable high may have happened after stop.


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_samebar_stop_target_order_is_ambiguous_and_not_win(store, side):
    position = PaperJournal(store).open(request(side=side), at=START)
    result = evaluate_position(position, [observation(high=112, low=88, price=100)],
                               as_of=START + timedelta(minutes=1))
    assert result.status == "AMBIGUOUS"
    assert result.coverage_status == "STOP_TARGET_ORDER_UNKNOWN"
    assert result.financial_result is None
    assert result.closed_at is None
    assert result.exit_source is None
    assert result.mfe_return == result.mae_return == 0


@pytest.mark.parametrize("defect", ["gap", "duplicate", "stale", "source", "extrema", "late", "future"])
def test_missing_or_unavailable_path_never_becomes_later_winner(store, defect):
    position = PaperJournal(store).open(request(), at=START)
    first = observation()
    later = observation(1, high=112, low=100, price=111)
    rows = [first, later]
    if defect == "gap":
        rows = [later]
    elif defect == "duplicate":
        rows.append(first)
    elif defect == "stale":
        first.data_quality = MacroQuality.STALE
    elif defect == "source":
        later.source = "other_provider:synthetic_test_only"
    elif defect == "extrema":
        first.high = None
    elif defect == "late":
        first.data_received_timestamp = START + timedelta(days=1)
    elif defect == "future":
        first.market_event_timestamp = START + timedelta(days=1)
        first.data_received_timestamp = first.market_event_timestamp
    result = evaluate_position(position, rows, as_of=START + timedelta(minutes=2))
    assert result.status == "CENSORED"
    assert result.financial_result is None


def test_open_excursions_use_actual_completed_extrema(store):
    position = PaperJournal(store).open(request(), at=START)
    result = evaluate_position(position, [observation(), observation(1, high=106, low=97, price=105)],
                               as_of=START + timedelta(minutes=2))
    assert result.status == "OPEN"
    assert result.mfe_return == pytest.approx(0.06)
    assert result.mae_return == pytest.approx(-0.03)
    assert result.samples == 2
    assert result.excursion_status == "COMPLETED_INTERVAL_EXTREMA"


def test_partial_entry_minute_not_used_for_preentry_extrema_or_exit(store):
    position = PaperJournal(store).open(request(), at=START + timedelta(seconds=30))
    rows = [observation(high=112, low=94, price=100), observation(1, high=112, low=100, price=111)]
    result = evaluate_position(position, rows, as_of=START + timedelta(minutes=2))
    assert result.status == "CENSORED"
    assert result.coverage_status == "PARTIAL_ENTRY_INTERVAL_UNKNOWN"
    assert result.financial_result is None


def test_safe_partial_entry_bar_allows_subsequent_completed_exit(store):
    position = PaperJournal(store).open(request(), at=START + timedelta(seconds=30))
    result = evaluate_position(position, [observation(), observation(1, high=112, low=100, price=111)],
                               as_of=START + timedelta(minutes=2))
    assert result.status == "CLOSED"
    assert result.samples == 1
    assert result.mfe_return == pytest.approx(0.1)
    assert result.mae_return == 0  # First half-candle excursions are excluded.


def test_completed_candle_exact_boundary_and_missing_tail(store):
    position = PaperJournal(store).open(request(), at=START)
    rows = [observation()]
    before = evaluate_position(position, rows, as_of=START + timedelta(seconds=59))
    assert before.status == "OPEN" and before.samples == 0
    exact = evaluate_position(position, rows, as_of=START + timedelta(seconds=60))
    assert exact.status == "OPEN" and exact.samples == 1
    after_gap = evaluate_position(position, rows, as_of=START + timedelta(seconds=120))
    assert after_gap.status == "CENSORED" and after_gap.coverage_status == "MISSING_TAIL"


def test_background_archived_path_persists_final_state_and_idempotent_events(store):
    journal = PaperJournal(store)
    position = journal.open(request(), at=START)
    archive(store, [observation(), observation(1, high=112, low=100, price=111)])
    report = journal.evaluate(START + timedelta(minutes=2))
    assert report["evaluated"] == report["closed"] == 1
    saved = journal.get(position.position_id)
    assert saved.status == "CLOSED"
    assert saved.source == SOURCE
    assert saved.mfe_return == pytest.approx(0.1)
    assert saved.mae_return == pytest.approx(-0.02)
    assert journal.evaluate(START + timedelta(minutes=3))["evaluated"] == 0
    events = journal.history(position.position_id, as_of=START + timedelta(minutes=3))
    assert [event["kind"] for event in events] == ["OPENED", "EVALUATED"]
    assert len(journal.history(position.position_id, as_of=START)) == 1


def test_unchanged_evaluation_does_not_duplicate_entry_snapshot_or_research_event(store):
    store.put_state("war_room_state", "TESTUSDT", START,
                    {"as_of": START.isoformat(), "market_regime": "RISK_ON", "timeframes": {"1m": {"state": "RANGE"}}})
    journal = PaperJournal(store)
    position = journal.open(request(), at=START)
    archive(store, [observation()])
    journal.evaluate(START + timedelta(minutes=1))
    journal.evaluate(START + timedelta(minutes=1, seconds=10))
    events = journal.history(position.position_id, as_of=START + timedelta(minutes=2))
    assert len(events) == 2
    assert "feature_snapshot" in events[0]["position"]
    assert "feature_snapshot" not in events[1]["position"]
    assert events[1]["immutable_entry_reference"] == position.position_id
    assert journal.get(position.position_id).last_evaluation_at == START + timedelta(minutes=1, seconds=10)


def test_background_evaluation_never_overwrites_concurrent_manual_close(store, monkeypatch):
    from expansion_hunter import paper_journal

    journal = PaperJournal(store)
    position = journal.open(request(), at=START)
    archive(store, [observation(high=112, low=100, price=111)])
    real_evaluate = paper_journal.evaluate_position

    def close_while_computing(current, rows, *, as_of):
        journal.close(current.position_id, PaperCloseRequest(exit_price=101, reason="Concurrent manual decision"),
                      at=as_of + timedelta(seconds=1))
        return real_evaluate(current, rows, as_of=as_of)

    monkeypatch.setattr(paper_journal, "evaluate_position", close_while_computing)
    report = journal.evaluate(START + timedelta(minutes=1))
    assert report["concurrent_updates_skipped"] == 1
    assert report["closed"] == 0
    saved = journal.get(position.position_id)
    assert saved.exit_kind == "USER_DECLARED"
    assert saved.financial_result.exit_price == 101


def test_censored_path_can_recover_only_when_missing_actual_interval_arrives(store):
    journal = PaperJournal(store)
    position = journal.open(request(), at=START)
    second = observation(1, high=112, low=100, price=111)
    archive(store, [second])
    journal.evaluate(START + timedelta(minutes=2))
    assert journal.get(position.position_id).status == "CENSORED"
    first = observation(data_received_timestamp=START + timedelta(minutes=3))
    archive(store, [first])
    journal.evaluate(START + timedelta(minutes=3))
    assert journal.get(position.position_id).status == "CLOSED"
    history = journal.history(position.position_id, as_of=START + timedelta(minutes=2))
    assert history[-1]["position"]["status"] == "CENSORED"  # No earlier view rewritten.


def test_unknown_funding_never_becomes_zero_or_fake_net_win(store):
    journal = PaperJournal(store)
    position = journal.open(request(funding=None), at=START)
    assert position.initial_net_risk is None
    assert any("Funding is UNAVAILABLE" in warning for warning in position.risk_warnings)
    closed = journal.close(position.position_id, PaperCloseRequest(exit_price=110, reason="Manual test close"),
                           at=START + timedelta(minutes=1))
    assert closed.financial_result.gross_pnl == 200
    assert closed.financial_result.net_pnl is None
    assert closed.financial_result.net_status == "UNAVAILABLE"
    assert closed.financial_result.r_multiple is None
    analytics = journal.analytics()
    assert analytics["unknown_cost_results"] == 1
    assert analytics["sample_size"] == 0
    assert analytics["winrate"] is None


def test_paper_entry_preserves_existing_calculator_liquidation_risk_warning(store):
    position = PaperJournal(store).open(request(trade={"leverage": 50, "maintenance_margin_rate": 0.005}), at=START)
    assert any("liquidation" in warning for warning in position.risk_warnings)
    assert any("liquidation execution are not modeled" in assumption for assumption in position.assumptions)


def test_missing_funding_input_defaults_unavailable_not_zero():
    payload = request().model_dump()
    del payload["trade"]["funding_cost"]
    assert PaperOpenRequest.model_validate(payload).trade.funding_cost is None


def test_manual_losing_close_and_final_funding_dont_rewrite_entry_risk(store):
    journal = PaperJournal(store)
    position = journal.open(request(funding=None), at=START)
    closed = journal.close(position.position_id,
                           PaperCloseRequest(exit_price=98, reason="Changed thesis", funding_cost=2),
                           at=START + timedelta(minutes=1))
    assert closed.financial_result.gross_pnl == -40
    assert closed.financial_result.net_pnl == pytest.approx(-45.96)
    assert closed.financial_result.net_status == "USER_INPUT"
    assert closed.initial_net_risk is None
    assert closed.financial_result.r_multiple is None
    assert closed.exit_source == "USER_DECLARED_NOT_EXECUTED"
    with pytest.raises(ValueError, match="cannot be rewritten"):
        journal.close(position.position_id, PaperCloseRequest(exit_price=110, reason="Rewrite"))


def test_ambiguous_result_cannot_be_manually_turned_into_winner(store):
    journal = PaperJournal(store)
    position = journal.open(request(), at=START)
    archive(store, [observation(high=112, low=94, price=100)])
    journal.evaluate(START + timedelta(minutes=1))
    with pytest.raises(ValueError, match="ambiguous"):
        journal.close(position.position_id, PaperCloseRequest(exit_price=110, reason="Pretend win"))
    assert journal.analytics()["ambiguous_positions"] == 1
    assert journal.analytics()["winrate"] is None


def test_manual_close_and_replay_cannot_move_backwards(store):
    journal = PaperJournal(store)
    position = journal.open(request(), at=START)
    with pytest.raises(ValueError, match="backward"):
        evaluate_position(position, [], as_of=START - timedelta(seconds=1))
    with pytest.raises(ValueError, match="precede"):
        journal.close(position.position_id, PaperCloseRequest(exit_price=100, reason="Backdate"),
                      at=START - timedelta(seconds=1))


def test_analytics_records_real_saved_paper_costs_sample_r_and_drawdown(store):
    journal = PaperJournal(store)
    for index, price in enumerate([110, 95, 110]):
        position = journal.open(request(identity=f"position-{index}", setup="COMPRESSION_BREAKOUT"), at=START)
        journal.close(position.position_id, PaperCloseRequest(exit_price=price, reason="Declared fixture close"),
                      at=START + timedelta(minutes=index + 1))
    report = journal.analytics(initial_balance=1000)
    assert report["sample_size"] == 3
    assert report["winrate"] == pytest.approx(2 / 3)
    assert report["realized_net_pnl"] == pytest.approx(195.8 * 2 - 103.9)
    assert report["expectancy_net_pnl"] == pytest.approx((195.8 * 2 - 103.9) / 3)
    assert report["profit_factor"] == pytest.approx(195.8 * 2 / 103.9)
    assert report["maximum_realized_drawdown"] == pytest.approx(103.9)
    assert report["maximum_realized_drawdown_percent"] == pytest.approx(100 * 103.9 / 1195.8)
    assert report["groups"]["setup"]["COMPRESSION_BREAKOUT"]["sample_size"] == 3
    assert report["groups"]["regime"]["UNAVAILABLE"]["sample_size"] == 3
    assert report["r_sample_size"] == 3
    assert report["average_r"] == pytest.approx((195.8 / 103.9 * 2 - 1) / 3)
    assert report["median_r"] == pytest.approx(195.8 / 103.9)


def test_no_user_equity_no_percent_drawdown_or_invented_equity(store):
    journal = PaperJournal(store)
    position = journal.open(request(), at=START)
    journal.close(position.position_id, PaperCloseRequest(exit_price=95, reason="Manual stop"),
                  at=START + timedelta(minutes=1))
    report = journal.analytics()
    assert report["maximum_realized_drawdown"] == pytest.approx(103.9)
    assert report["maximum_realized_drawdown_percent"] is None
    assert report["realized_curve"][0]["equity"] is None


def test_mixed_quote_currencies_never_summed_as_same_money(store):
    journal = PaperJournal(store)
    first = journal.open(request(identity="usd"), at=START)
    eur = request(identity="eur").model_copy(update={"quote_currency": "EUR"})
    second = journal.open(eur, at=START)
    for position in [first, second]:
        journal.close(position.position_id, PaperCloseRequest(exit_price=110, reason="Declared"),
                      at=START + timedelta(minutes=1))
    report = journal.analytics()
    assert report["status"] == "MIXED_QUOTE_CURRENCIES"
    assert report["realized_net_pnl"] is None
    assert report["winrate"] is None
    assert report["known_cost_results"] == 2
    assert report["groups"]["quote_currency"]["EUR"]["sample_size"] == 1
    assert journal.analytics(quote_currency="USDT")["sample_size"] == 1


def test_no_paper_positions_means_no_fake_winrate_or_profit(store):
    report = PaperJournal(store).analytics()
    assert report["status"] == "INSUFFICIENT_DATA"
    assert report["sample_size"] == 0
    assert report["winrate"] is None
    assert report["realized_net_pnl"] is None
    assert report["profit_factor"] is None


def test_portfolio_uses_paper_positions_without_fake_beta_or_correlation(store):
    journal = PaperJournal(store)
    journal.open(request(), at=START)
    journal.open(request(identity="second", side="SHORT"), at=START)
    report = journal.portfolio()
    assert report.total_exposure == 4000
    assert report.net_exposure == 0
    assert report.btc_adjusted_exposure is None
    assert report.data_quality["correlations"] == "UNAVAILABLE"
    assert report.total_risk_to_stop == pytest.approx(208)  # LONG103.9, SHORT104.1.


def test_mixed_currency_portfolio_requires_explicit_currency_filter(store):
    journal = PaperJournal(store)
    journal.open(request(), at=START)
    journal.open(request(identity="eur").model_copy(update={"quote_currency": "EUR"}), at=START)
    with pytest.raises(ValueError, match="MIXED_QUOTE_CURRENCIES"):
        journal.portfolio()
    assert journal.portfolio(quote_currency="USDT").total_exposure == 2000


def test_known_instrument_mismatch_cannot_follow_wrong_assets_candles(store):
    store.put_state("fast_state", "test-asset", START, {"asset": {"exchange_symbol": "OTHERUSDT"}})
    with pytest.raises(ValueError, match="disagree"):
        PaperJournal(store).open(request(), at=START)
    assert not PaperJournal(store).list()


def test_simultaneous_closes_do_not_invent_intraminute_equity_order(store):
    journal = PaperJournal(store)
    first = journal.open(request(identity="win"), at=START)
    second = journal.open(request(identity="loss"), at=START)
    for position, price in [(first, 110), (second, 95)]:
        journal.close(position.position_id, PaperCloseRequest(exit_price=price, reason="Simultaneous declared close"),
                      at=START + timedelta(minutes=1))
    report = journal.analytics(initial_balance=1000)
    assert report["maximum_realized_drawdown"] == 0
    assert len(report["realized_curve"]) == 1
    assert report["realized_curve"][0]["position_count"] == 2


@pytest.mark.parametrize("balance", [float("nan"), float("inf"), -1, 0])
def test_nonfinite_or_invalid_initial_capital_is_not_a_fake_equity_curve(store, balance):
    with pytest.raises(ValueError, match="positive"):
        PaperJournal(store).analytics(initial_balance=balance)


@pytest.mark.parametrize("changes", [
    {"trade": {"entry": float("nan")}}, {"trade": {"target": 99}},
    {"trade": {"leverage": 200}}, {"timeframe": "2m"}, {"additional_targets": [109]},
    {"additional_targets": [120, 120]}, {"private_api_key": "forbidden"},
])
def test_invalid_inputs_and_private_keys_rejected(changes):
    payload = request().model_dump()
    if "trade" in changes:
        payload["trade"].update(changes["trade"])
    else:
        payload.update(changes)
    with pytest.raises(ValidationError):
        PaperOpenRequest.model_validate(payload)


def test_nonfinite_large_manual_inputs_fail_instead_of_persisting_fake_number(store):
    payload = request(trade={"margin": 1e308, "entry": 1e-300, "target": 1, "stop": 1e-301})
    with pytest.raises((ValidationError, OverflowError)):
        PaperJournal(store).open(payload, at=START)
    assert not PaperJournal(store).list()
