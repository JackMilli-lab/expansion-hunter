"""Observed ERC-20 transfers, with explicit identity and valuation uncertainty.

Transfers are not executed exchange trades. This isolated observation module
does not add any production alert weight or claim that a wallet's intent is known.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime
from decimal import Decimal, localcontext
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .models import Model, aware, utcnow

ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
HASH = re.compile(r"^0x[0-9a-fA-F]{64}$")


def address(value: str) -> str:
    if not isinstance(value, str) or not ADDRESS.fullmatch(value):
        raise ValueError("Expected an explicit EVM address")
    return value.lower()


class WhaleSettings(Model):
    chain_id: int = Field(default=1, gt=0)
    wallets: tuple[str, ...] = ()
    contracts: tuple[str, ...] = ()
    token_assets: dict[str, str] = Field(default_factory=dict)
    mapping_available_at: datetime = Field(default_factory=utcnow)
    min_confirmations: int = Field(default=12, ge=1)
    poll_seconds: float = Field(default=120, ge=10)
    stale_seconds: float = Field(default=600, ge=30)
    max_pages: int = Field(default=2, ge=1, le=10)
    page_size: int = Field(default=100, ge=1, le=1000)
    overlap_blocks: int = Field(default=64, ge=1, le=10000)
    max_queries_per_cycle: int = Field(default=25, ge=1, le=2500)
    production_weight: Literal[0] = 0

    @field_validator("token_assets")
    @classmethod
    def token_mapping(cls, values):
        result = {}
        for key, asset_id in values.items():
            chain, contract = key.split(":", 1)
            if not chain.isdecimal() or int(chain) < 1 or not asset_id.strip():
                raise ValueError("Asset mappings require explicit chain:contract and asset ID")
            result[f"{int(chain)}:{address(contract)}"] = asset_id
        return result

    @field_validator("wallets", "contracts")
    @classmethod
    def addresses(cls, values):
        if len(values) > 50:
            raise ValueError("Observation is capped at 50 explicitly configured addresses")
        return tuple(dict.fromkeys(address(value) for value in values))

    @classmethod
    def from_environment(cls) -> WhaleSettings:
        def split(value):
            return tuple(part.strip() for part in value.split(",") if part.strip())
        return cls(
            chain_id=int(os.environ.get("COCKPIT_WHALE_CHAIN_ID", "1")),
            wallets=split(os.environ.get("COCKPIT_WHALE_WALLETS", "")),
            contracts=split(os.environ.get("COCKPIT_WHALE_CONTRACTS", "")),
            token_assets=json.loads(os.environ.get("COCKPIT_WHALE_ASSET_MAP", "{}")),
            max_queries_per_cycle=int(os.environ.get("COCKPIT_WHALE_MAX_QUERIES", "25")),
        )


class WalletLabel(Model):
    chain_id: int = Field(gt=0)
    wallet: str
    name: str
    kind: Literal["EXCHANGE", "WALLET", "UNKNOWN"] = "UNKNOWN"
    validated_by_operator: bool = False
    source: str
    available_at: datetime
    confidence: float | None = Field(default=None, ge=0, le=1)

    _address = field_validator("wallet")(address)


class TokenPricePoint(Model):
    chain_id: int = Field(gt=0)
    contract: str
    price: Decimal = Field(gt=0)
    quote_currency: str
    observed_at: datetime
    available_at: datetime
    source: str

    _address = field_validator("contract")(address)

    @model_validator(mode="after")
    def causal(self):
        if self.available_at < self.observed_at:
            raise ValueError("Price receipt precedes observation")
        return self


class WalletBalancePoint(Model):
    chain_id: int = Field(gt=0)
    contract: str
    wallet: str
    quantity: Decimal = Field(ge=0)
    block_number: int = Field(ge=0)
    observed_at: datetime
    available_at: datetime
    source: str

    _addresses = field_validator("contract", "wallet")(address)

    @model_validator(mode="after")
    def causal(self):
        if self.available_at < self.observed_at:
            raise ValueError("Balance receipt precedes observation")
        return self


class WhaleTransfer(Model):
    event_id: str
    chain_id: int = Field(gt=0)
    contract: str
    transaction_hash: str
    log_index: int | None = Field(default=None, ge=0)
    identity_quality: Literal["NATIVE_LOG_INDEX", "ESTIMATED_FINGERPRINT"]
    block_number: int = Field(ge=0)
    block_hash: str | None = None
    confirmations: int = Field(ge=0)
    confirmation_status: Literal["PROVIDER_CONFIRMED"] = "PROVIDER_CONFIRMED"
    from_wallet: str
    to_wallet: str
    token_symbol: str
    token_name: str
    token_decimals: int = Field(ge=0, le=255)
    raw_amount: str
    quantity: Decimal = Field(ge=0)
    event_at: datetime
    available_at: datetime
    retrieved_at: datetime
    provider: str = "ETHERSCAN_V2"
    source: str = "https://api.etherscan.io/v2/api"
    quality: Literal["PROVIDER_REPORTED"] = "PROVIDER_REPORTED"
    from_identity: str = "UNKNOWN"
    to_identity: str = "UNKNOWN"
    identity_source: str | None = None
    identity_confidence: float | None = Field(default=None, ge=0, le=1)
    direction: str = "UNKNOWN"
    purpose: Literal["UNKNOWN"] = "UNKNOWN"
    purpose_hypotheses: tuple[str, ...] = ()
    notional: Decimal | None = None
    notional_quote: str | None = None
    valuation_observed_at: datetime | None = None
    valuation_source: str | None = None
    wallet_fraction_before_transfer: Decimal | None = None
    balance_source: str | None = None
    production_weight: Literal[0] = 0
    limitations: tuple[str, ...] = ()

    _addresses = field_validator("contract", "from_wallet", "to_wallet")(address)

    @model_validator(mode="after")
    def causal(self):
        if self.available_at < self.event_at or self.retrieved_at != self.available_at:
            raise ValueError("Transfer requires its real first retrieval timestamp")
        return self


class WhaleProviderBatch(Model):
    provider: str = "ETHERSCAN_V2"
    status: Literal["AVAILABLE", "PARTIAL", "UNAVAILABLE"]
    retrieved_at: datetime
    events: list[WhaleTransfer] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    history_complete: bool = False
    pages_requested: int = 0


def parse_transfer(row: dict, chain_id: int, receipt: datetime, min_confirmations: int) -> WhaleTransfer:
    from datetime import UTC

    receipt = aware(receipt)
    transaction = str(row["hash"]).lower()
    if not HASH.fullmatch(transaction):
        raise ValueError("Invalid native transaction hash")
    contract = address(row["contractAddress"])
    sender, recipient = address(row["from"]), address(row["to"])
    decimals, block, confirmations = int(row["tokenDecimal"]), int(row["blockNumber"]), int(row["confirmations"])
    raw = str(row["value"])
    if not raw.isascii() or not raw.isdecimal() or int(raw) >= 2**256:
        raise ValueError("Invalid unsigned ERC-20 token amount")
    if not 0 <= decimals <= 255 or block < 0 or confirmations < min_confirmations:
        raise ValueError("Transfer lacks sufficient provider confirmations or valid units")
    event_at = datetime.fromtimestamp(int(row["timeStamp"]), UTC)
    if event_at > receipt:
        raise ValueError("Provider reports an event in the future")
    native_index = row.get("logIndex")
    index = int(native_index, 16) if isinstance(native_index, str) and native_index.startswith("0x") else (
        int(native_index) if native_index not in (None, "") else None)
    if index is not None and index < 0:
        raise ValueError("Invalid log index")
    identity = f"{chain_id}:{transaction}:{index}" if index is not None else hashlib.sha256(
        json.dumps([chain_id, transaction, contract, sender, recipient, raw], separators=(",", ":")).encode()
    ).hexdigest()
    # Construct decimal digits/exponent directly: Decimal.scaleb/division with
    # the default 28-digit context would round large native token integers.
    quantity = Decimal((0, tuple(int(value) for value in raw), -decimals))
    return WhaleTransfer(
        event_id="erc20:" + identity, chain_id=chain_id, contract=contract,
        transaction_hash=transaction, log_index=index,
        identity_quality="NATIVE_LOG_INDEX" if index is not None else "ESTIMATED_FINGERPRINT",
        block_number=block, block_hash=row.get("blockHash") or None, confirmations=confirmations,
        from_wallet=sender, to_wallet=recipient, token_symbol=str(row.get("tokenSymbol", "")),
        token_name=str(row.get("tokenName", "")), token_decimals=decimals, raw_amount=raw,
        quantity=quantity, event_at=event_at, available_at=receipt, retrieved_at=receipt,
        limitations=(() if index is not None else (
            "Provider omitted log index; indistinguishable same-transaction transfers may be collapsed",)),
    )


def explain_transfer(event: WhaleTransfer, *, as_of: datetime,
                     labels: tuple[WalletLabel, ...] = (), price: TokenPricePoint | None = None,
                     balance: WalletBalancePoint | None = None) -> WhaleTransfer:
    now = aware(as_of)
    if event.available_at > now:
        raise ValueError("Future transfer receipt is unavailable")
    known = {label.wallet: label for label in labels if label.chain_id == event.chain_id
             and label.validated_by_operator and label.available_at <= event.available_at}
    sender, recipient = known.get(event.from_wallet), known.get(event.to_wallet)
    direction = "UNKNOWN"
    if sender and sender.kind == "EXCHANGE" and recipient and recipient.kind == "EXCHANGE":
        direction = "EXCHANGE_TO_EXCHANGE"
    elif recipient and recipient.kind == "EXCHANGE":
        direction = "EXCHANGE_DEPOSIT"
    elif sender and sender.kind == "EXCHANGE":
        direction = "EXCHANGE_WITHDRAWAL"
    elif sender and recipient and sender.kind == recipient.kind == "WALLET":
        direction = "WALLET_TO_WALLET"
    updates = dict(from_identity=sender.name if sender else "UNKNOWN",
                   to_identity=recipient.name if recipient else "UNKNOWN", direction=direction,
                   identity_source="; ".join(dict.fromkeys(label.source for label in (sender, recipient) if label)) or None,
                   identity_confidence=min((label.confidence for label in (sender, recipient)
                                            if label and label.confidence is not None), default=None),
                   purpose_hypotheses=(("Possible custody/inventory transfer; executed sale is not observed",)
                                       if direction != "UNKNOWN" else ()))
    if (price and price.chain_id == event.chain_id and price.contract == event.contract
            and price.available_at <= event.available_at and price.observed_at <= event.event_at):
        with localcontext() as context:
            context.prec = max(100, len(event.quantity.as_tuple().digits) + len(price.price.as_tuple().digits) + 2)
            updates.update(notional=event.quantity * price.price, notional_quote=price.quote_currency,
                           valuation_observed_at=price.observed_at, valuation_source=price.source)
    if (balance and balance.chain_id == event.chain_id and balance.contract == event.contract
            and balance.wallet == event.from_wallet and balance.block_number == event.block_number - 1
            and balance.observed_at <= event.event_at and balance.available_at <= event.available_at and balance.quantity > 0):
        with localcontext() as context:
            context.prec = 36
            updates.update(wallet_fraction_before_transfer=event.quantity / balance.quantity,
                           balance_source=balance.source)
    return event.model_copy(update=updates)


def whale_status(settings: WhaleSettings, key_configured: bool, at: datetime | None = None) -> dict:
    reasons = []
    if not key_configured:
        reasons.append("API_KEY_REQUIRED")
    if not settings.wallets:
        reasons.append("WALLETS_NOT_CONFIGURED")
    return {"status": "UNAVAILABLE" if reasons else "READY_FOR_OBSERVATION", "reasons": reasons,
            "provider": "ETHERSCAN_V2", "chain_id": settings.chain_id,
            "wallets_configured": len(settings.wallets), "contracts_configured": len(settings.contracts),
            "api_key_configured": key_configured, "updated_at": aware(at or utcnow()).isoformat(),
            "production_weight": 0, "purpose": "UNKNOWN", "events": []}
