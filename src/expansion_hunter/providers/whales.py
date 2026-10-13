"""GET-only Etherscan V2 ERC-20 observation adapter; no signing or wallet access."""
from __future__ import annotations

import asyncio
import os
from typing import Protocol, runtime_checkable

import httpx

from ..models import utcnow
from ..whale_flow import WhaleProviderBatch, WhaleSettings, address, parse_transfer
from .http import _ssl_context


@runtime_checkable
class WhaleTransferProvider(Protocol):
    async def transfers(self, wallet: str, contract: str | None = None, *, start_block: int = 0) -> WhaleProviderBatch: ...
    async def aclose(self) -> None: ...


class EtherscanV2WhaleProvider:
    URL = "https://api.etherscan.io/v2/api"

    def __init__(self, settings: WhaleSettings, *, api_key: str | None = None,
                 client: httpx.AsyncClient | None = None, clock=utcnow, request_interval: float = 0.4):
        self.settings = settings
        self._key = api_key if api_key is not None else os.environ.get("ETHERSCAN_API_KEY", "")
        self._owned = client is None
        self.client = client or httpx.AsyncClient(timeout=20, verify=_ssl_context(),
                                                 follow_redirects=False, trust_env=True)
        self.clock, self.request_interval = clock, max(0.34, float(request_interval))
        self._gate = asyncio.Lock()

    @property
    def key_configured(self) -> bool:
        return bool(self._key)

    async def aclose(self):
        if self._owned:
            await self.client.aclose()

    async def transfers(self, wallet: str, contract: str | None = None, *, start_block: int = 0) -> WhaleProviderBatch:
        wallet = address(wallet)
        if contract:
            contract = address(contract)
        if start_block < 0:
            raise ValueError("Starting block cannot be negative")
        if not self._key:
            return WhaleProviderBatch(status="UNAVAILABLE", retrieved_at=self.clock(), issues=["API_KEY_REQUIRED"])
        events, issues, seen, pages = [], [], set(), 0
        complete = False
        receipt = self.clock()
        async with self._gate:
            for page in range(1, self.settings.max_pages + 1):
                params = {"chainid": self.settings.chain_id, "module": "account", "action": "tokentx",
                          "address": wallet, "startblock": start_block, "endblock": 99999999,
                          "page": page, "offset": self.settings.page_size, "sort": "desc", "apikey": self._key}
                if contract:
                    params["contractaddress"] = contract
                try:
                    response = await asyncio.wait_for(self.client.get(self.URL, params=params), timeout=22)
                    pages += 1
                    response.raise_for_status()
                    body = response.json()
                    receipt = self.clock()
                    if not isinstance(body, dict):
                        raise ValueError("Provider response is not an object")
                    rows = body.get("result") if isinstance(body, dict) else None
                    if body.get("status") == "0" and body.get("message") == "No transactions found":
                        complete = True
                        break
                    if body.get("status") != "1" or not isinstance(rows, list):
                        issues.append("PROVIDER_REJECTED_REQUEST_OR_RATE_LIMITED")
                        break
                    for row in rows:
                        try:
                            event = parse_transfer(row, self.settings.chain_id, receipt, self.settings.min_confirmations)
                            if event.from_wallet != wallet and event.to_wallet != wallet:
                                raise ValueError("Provider returned an unrelated wallet")
                            if contract and event.contract != contract:
                                raise ValueError("Provider returned a different contract")
                            if event.event_id not in seen:
                                seen.add(event.event_id)
                                events.append(event)
                            else:
                                issues.append("DUPLICATE_OR_INDISTINGUISHABLE_TRANSFER")
                        except (KeyError, TypeError, ValueError, OverflowError):
                            issues.append("INVALID_OR_INSUFFICIENTLY_CONFIRMED_TRANSFER")
                    if len(rows) < self.settings.page_size:
                        complete = True
                        break
                    await asyncio.sleep(self.request_interval)
                except (TimeoutError, httpx.HTTPError, ValueError, TypeError) as error:
                    # Exception URLs can contain the API key. Never expose str(error).
                    issues.append("PROVIDER_REQUEST_FAILED:" + type(error).__name__)
                    break
            await asyncio.sleep(self.request_interval)
        if not complete and pages >= self.settings.max_pages:
            issues.append("HISTORY_TRUNCATED_BY_PAGE_BUDGET")
        return WhaleProviderBatch(status="PARTIAL" if issues and events else "UNAVAILABLE" if issues else "AVAILABLE",
                                  retrieved_at=receipt, events=events, issues=list(dict.fromkeys(issues)),
                                  history_complete=complete, pages_requested=pages)
