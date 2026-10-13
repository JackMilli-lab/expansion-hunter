"""Shared provider construction; credentials are read only by their providers."""

from contextlib import asynccontextmanager

from .config import Settings
from .discovery_config import DiscoverySettings
from .providers import AsyncHTTPClient, BinanceDerivativesProvider, BinanceSpotProvider, CoinGeckoProvider
from .scanner import Scanner
from .storage import Store


@asynccontextmanager
async def runtime(settings: Settings):
    store = Store(settings.db_path)
    try:
        async with AsyncHTTPClient(settings) as http:
            yield Scanner(
                CoinGeckoProvider(http, settings),
                BinanceSpotProvider(http, settings),
                BinanceDerivativesProvider(http, settings),
                settings,
                store,
            )
    finally:
        store.close()


@asynccontextmanager
async def discovery_runtime(settings: Settings, discovery_settings: DiscoverySettings):
    """Construct discovery with public data and the operational archive.

    No order-routing client is constructed. Unavailable derivatives do not
    prevent the engine from evaluating independent spot evidence.
    """
    from .discovery import DiscoveryEngine
    from .discovery_store import DiscoveryStore

    store = DiscoveryStore(settings.db_path)
    try:
        async with AsyncHTTPClient(settings) as http:
            yield DiscoveryEngine(
                CoinGeckoProvider(http, settings),
                BinanceSpotProvider(http, settings),
                BinanceDerivativesProvider(http, settings),
                settings,
                discovery_settings,
                store,
            )
    finally:
        store.close()
