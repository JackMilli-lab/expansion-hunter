from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from expansion_hunter.intelligence_store import IntelligenceStore
from expansion_hunter.providers.whales import EtherscanV2WhaleProvider
from expansion_hunter.whale_flow import (
    TokenPricePoint,
    WalletBalancePoint,
    WalletLabel,
    WhaleProviderBatch,
    WhaleSettings,
    explain_transfer,
    parse_transfer,
    whale_status,
)
from expansion_hunter.whale_runtime import WhaleObservationWorker, run_whale_worker, whale_asset_context

NOW = datetime(2026, 10, 6, 12, tzinfo=UTC)
WALLET = "0x" + "11" * 20
EXCHANGE = "0x" + "22" * 20
TOKEN = "0x" + "33" * 20


def native_row(**updates):
    return {"hash": "0x" + "aa" * 32, "contractAddress": TOKEN, "from": WALLET, "to": EXCHANGE,
            "tokenDecimal": "18", "blockNumber": "100", "confirmations": "20",
            "timeStamp": str(int((NOW - timedelta(minutes=1)).timestamp())), "value": "2500000000000000000",
            "tokenSymbol": "FIXTURE", "tokenName": "Explicit synthetic test token", "logIndex": "0x2", **updates}


def settings(**updates):
    return WhaleSettings(**{"wallets": (WALLET,), "contracts": (TOKEN,),
                            "mapping_available_at": NOW - timedelta(days=1), **updates})


def test_defaults_make_no_whale_claim_or_key_request():
    value = whale_status(WhaleSettings(), False, NOW)
    assert value["status"] == "UNAVAILABLE"
    assert value["reasons"] == ["API_KEY_REQUIRED", "WALLETS_NOT_CONFIGURED"]
    assert value["production_weight"] == 0 and value["events"] == []


@pytest.mark.parametrize("bad", ["bitcoin", "0x123", "0x" + "gg" * 20])
def test_explicit_canonical_address_validation(bad):
    with pytest.raises(ValueError):
        WhaleSettings(wallets=(bad,))


def test_exact_uint256_decimal_amount_is_not_rounded():
    number = str(2**256 - 1)
    event = parse_transfer(native_row(value=number), 1, NOW, 12)
    assert event.quantity == Decimal(number[:-18] + "." + number[-18:])
    assert event.raw_amount == number
    assert event.purpose == "UNKNOWN" and event.notional is None


@pytest.mark.parametrize("changes", [{"confirmations": "2"}, {"tokenDecimal": "256"},
                                    {"value": "-1"}, {"value": str(2**256)},
                                    {"timeStamp": str(int((NOW + timedelta(seconds=1)).timestamp()))}])
def test_invalid_unconfirmed_and_future_rows_are_rejected(changes):
    with pytest.raises(ValueError):
        parse_transfer(native_row(**changes), 1, NOW, 12)


def test_missing_log_index_is_disclosed_not_invented():
    first = parse_transfer(native_row(logIndex=None), 1, NOW, 12)
    second = parse_transfer(native_row(logIndex=None), 1, NOW + timedelta(minutes=1), 12)
    assert first.event_id == second.event_id
    assert first.identity_quality == "ESTIMATED_FINGERPRINT" and first.limitations
    assert first.log_index is None


def test_distinct_native_log_indexes_are_not_collapsed():
    assert parse_transfer(native_row(logIndex="1"), 1, NOW, 12).event_id != parse_transfer(
        native_row(logIndex="2"), 1, NOW, 12).event_id


def test_exchange_direction_requires_explicit_validated_causal_label():
    event = parse_transfer(native_row(), 1, NOW, 12)
    label = WalletLabel(chain_id=1, wallet=EXCHANGE, name="Operator-provided exchange label",
                        kind="EXCHANGE", source="Explicit operator verification", available_at=NOW)
    assert explain_transfer(event, as_of=NOW, labels=(label,)).direction == "UNKNOWN"
    verified = label.model_copy(update={"validated_by_operator": True})
    value = explain_transfer(event, as_of=NOW, labels=(verified,))
    assert value.direction == "EXCHANGE_DEPOSIT" and value.purpose == "UNKNOWN"
    assert value.purpose_hypotheses and value.production_weight == 0
    future = verified.model_copy(update={"available_at": NOW + timedelta(seconds=1)})
    assert explain_transfer(event, as_of=NOW, labels=(future,)).direction == "UNKNOWN"


def test_notional_and_wallet_share_require_real_point_in_time_inputs():
    event = parse_transfer(native_row(), 1, NOW, 12)
    price = TokenPricePoint(chain_id=1, contract=TOKEN, price=Decimal("4"), quote_currency="USDT",
                            observed_at=event.event_at, available_at=NOW, source="Synthetic native-price fixture")
    balance = WalletBalancePoint(chain_id=1, contract=TOKEN, wallet=WALLET, quantity=Decimal("10"),
        block_number=99, observed_at=event.event_at, available_at=NOW, source="Synthetic parent-block balance fixture")
    value = explain_transfer(event, as_of=NOW, price=price, balance=balance)
    assert value.notional == 10 and value.notional_quote == "USDT"
    assert value.wallet_fraction_before_transfer == Decimal("0.25")
    assert explain_transfer(event, as_of=NOW, price=price.model_copy(update={
        "observed_at": NOW})).notional is None
    assert explain_transfer(event, as_of=NOW, balance=balance.model_copy(update={
        "block_number": 100})).wallet_fraction_before_transfer is None


@pytest.mark.asyncio
async def test_real_adapter_request_contract_and_provider_rows():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"status": "1", "message": "OK", "result": [native_row()]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = EtherscanV2WhaleProvider(settings(), api_key="synthetic-test-key", client=client, clock=lambda: NOW)
        batch = await provider.transfers(WALLET, TOKEN)
    assert batch.status == "AVAILABLE" and len(batch.events) == 1
    request = requests[0]
    assert request.method == "GET" and request.url.host == "api.etherscan.io"
    assert request.url.params["action"] == "tokentx" and request.url.params["chainid"] == "1"
    assert request.url.params["contractaddress"] == TOKEN
    assert "synthetic-test-key" not in batch.model_dump_json()


@pytest.mark.asyncio
async def test_missing_key_never_requests_network():
    def forbidden(request):
        raise AssertionError("No request is authorized without a configured key")

    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        provider = EtherscanV2WhaleProvider(settings(), api_key="", client=client, clock=lambda: NOW)
        result = await provider.transfers(WALLET)
    assert result.status == "UNAVAILABLE" and result.issues == ["API_KEY_REQUIRED"]


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [httpx.Response(429), httpx.Response(200, json=[]),
                                      httpx.Response(200, json={"status": "0", "message": "NOTOK", "result": "bad key"})])
async def test_provider_failure_visible_without_key_leak(response):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response)) as client:
        provider = EtherscanV2WhaleProvider(settings(), api_key="secret-fixture-never-expose", client=client, clock=lambda: NOW)
        result = await provider.transfers(WALLET)
    assert result.status == "UNAVAILABLE" and result.issues
    assert "secret-fixture-never-expose" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_bounded_pagination_and_ambiguity_are_visible():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={
        "status": "1", "result": [native_row(logIndex=None), native_row(logIndex=None)]}))) as client:
        provider = EtherscanV2WhaleProvider(settings(page_size=2, max_pages=1), api_key="fixture", client=client, clock=lambda: NOW)
        result = await provider.transfers(WALLET)
    assert result.status == "PARTIAL" and len(result.events) == 1
    assert "HISTORY_TRUNCATED_BY_PAGE_BUDGET" in result.issues
    assert "DUPLICATE_OR_INDISTINGUISHABLE_TRANSFER" in result.issues


@pytest.fixture
def no_thread_teardown(monkeypatch):
    # This unit suite exercises actual SQLite logic without the restricted
    # sandbox's idle default-executor teardown; production remains to_thread.
    async def direct(function, *args, **kwargs):
        return function(*args, **kwargs)
    monkeypatch.setattr("asyncio.to_thread", direct)


@pytest.mark.asyncio
async def test_immutable_receipts_dedup_restart_and_asset_identity(tmp_path, no_thread_teardown):
    moment = [NOW]

    class Provider:
        key_configured = True

        async def transfers(self, *args, **kwargs):
            return WhaleProviderBatch(status="AVAILABLE", retrieved_at=moment[0],
                events=[parse_transfer(native_row(), 1, moment[0], 12)], history_complete=True)

    config = settings(token_assets={f"1:{TOKEN}": "fixture-asset"})
    store = IntelligenceStore(str(tmp_path / "whales.sqlite3"))
    worker = WhaleObservationWorker(config, store, Provider(), clock=lambda: moment[0])
    assert (await worker.cycle())["new_events"] == 1
    moment[0] += timedelta(seconds=30)
    restarted = WhaleObservationWorker(config, store, Provider(), clock=lambda: moment[0])
    assert (await restarted.cycle())["new_events"] == 0
    events = store.objects("whale_transfer", moment[0])
    assert len(events) == 1 and events[0]["available_at"] == NOW.isoformat().replace("+00:00", "Z")
    context = whale_asset_context(store, "fixture-asset", moment[0], config)
    assert context["status"] == "AVAILABLE" and len(context["transfers"]) == 1
    assert whale_asset_context(store, "unmapped-identical-ticker", moment[0], config)["status"] == "UNAVAILABLE"
    assert store.objects("whale_transfer", NOW - timedelta(seconds=1)) == []
    moment[0] += timedelta(seconds=601)
    assert restarted.status()["status"] == "STALE"
    assert whale_asset_context(store, "fixture-asset", moment[0], config)["status"] == "STALE"
    store.close()


def test_later_discovered_identity_or_valuation_does_not_rewrite_first_receipt():
    event = parse_transfer(native_row(), 1, NOW, 12)
    later = NOW + timedelta(hours=1)
    label = WalletLabel(chain_id=1, wallet=EXCHANGE, name="Later-known exchange", kind="EXCHANGE",
        validated_by_operator=True, source="Synthetic later verification", available_at=later)
    price = TokenPricePoint(chain_id=1, contract=TOKEN, price=Decimal("2"), quote_currency="USD",
        observed_at=event.event_at, available_at=later, source="Late-arriving synthetic price")
    value = explain_transfer(event, as_of=later, labels=(label,), price=price)
    assert value.direction == "UNKNOWN" and value.notional is None


@pytest.mark.asyncio
async def test_bounded_query_scheduler_is_fair_and_restart_safe(tmp_path, no_thread_teardown):
    called = []

    class Provider:
        key_configured = True

        async def transfers(self, wallet, contract=None, **kwargs):
            called.append(wallet)
            return WhaleProviderBatch(status="AVAILABLE", retrieved_at=NOW, history_complete=True)

    config = settings(wallets=(WALLET, EXCHANGE), max_queries_per_cycle=1)
    store = IntelligenceStore(str(tmp_path / "fair.sqlite3"))
    first = await WhaleObservationWorker(config, store, Provider(), clock=lambda: NOW).cycle()
    second = await WhaleObservationWorker(config, store, Provider(), clock=lambda: NOW).cycle()
    assert called == [WALLET, EXCHANGE]
    assert first["status"] == second["status"] == "PARTIAL"
    assert first["queries_completed"] == 1 and first["queries_configured"] == 2
    assert "QUERY_BUDGET_LIMITED" in first["reasons"]
    store.close()


@pytest.mark.asyncio
async def test_dedup_survives_more_than_archive_list_limit(tmp_path, no_thread_teardown):
    event = parse_transfer(native_row(), 1, NOW, 12)
    store = IntelligenceStore(str(tmp_path / "long_archive.sqlite3"))
    store.append("whale_transfer", event.event_id, event.event_at, NOW, event)
    later = NOW + timedelta(seconds=1)
    store.append_many([("whale_transfer", f"newer-fixture-{index}", NOW, later,
                        {"event_id": f"newer-fixture-{index}"}, None) for index in range(10001)])
    assert not any(row["event_id"] == event.event_id for row in store.objects("whale_transfer", later))
    assert store.object("whale_transfer", event.event_id, NOW - timedelta(seconds=1)) is None
    assert store.object("whale_transfer", event.event_id, NOW)["event_id"] == event.event_id

    class Provider:
        key_configured = True

        async def transfers(self, *args, **kwargs):
            return WhaleProviderBatch(status="AVAILABLE", retrieved_at=later,
                events=[parse_transfer(native_row(), 1, later, 12)], history_complete=True)

    value = await WhaleObservationWorker(settings(), store, Provider(), clock=lambda: later).cycle()
    assert value["status"] == "AVAILABLE" and value["new_events"] == 0
    assert store.object("whale_transfer", event.event_id, later)["available_at"] == event.model_dump(mode="json")["available_at"]
    store.close()


@pytest.mark.asyncio
async def test_invalid_environment_configuration_does_not_abort_lifespan(tmp_path, monkeypatch):
    monkeypatch.setenv("COCKPIT_WHALE_WALLETS", "not-an-address")
    app = SimpleNamespace(state=SimpleNamespace(settings=SimpleNamespace(db_path=str(tmp_path / "unused.sqlite3"))))
    await run_whale_worker(app)
    assert app.state.whale_health["status"] == "UNAVAILABLE"
    assert app.state.whale_health["reasons"] == ["INVALID_WHALE_CONFIGURATION"]
    assert not (tmp_path / "unused.sqlite3").exists()


@pytest.mark.asyncio
async def test_cancelled_worker_waits_for_storage_before_closing_connection(tmp_path, monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()
    order = []

    class TrackedStore(IntelligenceStore):
        def close(self):
            order.append("closed")
            super().close()

    async def controlled_thread(function, *args, **kwargs):
        # Deterministic scheduling seam: cancellation occurs while a real
        # SQLite write is pending, with no timing/thread-flakiness assumptions.
        if getattr(function, "__name__", "") == "put_state":
            order.append("write_started")
            started.set()
            await release.wait()
            value = function(*args, **kwargs)
            order.append("write_finished")
            return value
        return function(*args, **kwargs)

    class Provider:
        key_configured = False

    monkeypatch.setattr("expansion_hunter.whale_runtime.IntelligenceStore", TrackedStore)
    monkeypatch.setattr("asyncio.to_thread", controlled_thread)
    app = SimpleNamespace(state=SimpleNamespace(settings=SimpleNamespace(db_path=str(tmp_path / "lifecycle.sqlite3"))))
    config = settings()
    task = asyncio.create_task(run_whale_worker(app, settings=config, provider=Provider()))
    await asyncio.wait_for(started.wait(), 2)
    assert app.state.whale_settings is config
    task.cancel()
    await asyncio.sleep(0)
    assert "closed" not in order
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert order == ["write_started", "write_finished", "closed"]
    assert app.state.whale_health["status"] == "STOPPED"


@pytest.mark.asyncio
async def test_worker_exception_is_visible(tmp_path, no_thread_teardown):
    class FailedProvider:
        key_configured = True

        async def transfers(self, *args, **kwargs):
            raise RuntimeError("Fixture failure")

    store = IntelligenceStore(str(tmp_path / "failed.sqlite3"))
    worker = WhaleObservationWorker(settings(), store, FailedProvider(), clock=lambda: NOW)
    value = await worker.cycle()
    assert value["status"] == "UNAVAILABLE" and "RuntimeError" in value["reasons"][0]
    store.close()
