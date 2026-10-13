from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from .models import TIMEFRAMES, Model


class Settings(Model):
    top_n: int = Field(default=500, ge=1, le=10000)
    timeframes: list[str] = Field(default_factory=lambda: list(TIMEFRAMES))
    history_bars: int = Field(default=400, ge=220, le=1000)
    concurrency: int = Field(default=6, ge=1, le=20)
    min_quote_volume_24h: float = Field(default=5_000_000, ge=0)
    max_spread_bps: float = Field(default=30, gt=0)
    min_depth_usd: float = Field(default=50_000, ge=0)
    min_score: float = Field(default=55, ge=0, le=100)
    min_confidence: float = Field(default=0.65, ge=0, le=1)
    account_equity: float = Field(default=100_000, gt=0)
    risk_per_trade: float = Field(default=0.005, gt=0, le=0.02)
    max_position_fraction: float = Field(default=0.1, gt=0, le=1)
    min_reward_risk: float = Field(default=2, gt=0)
    max_daily_loss_fraction: float = Field(default=0.02, gt=0, le=0.1)
    max_portfolio_risk_fraction: float = Field(default=0.03, gt=0, le=0.2)
    max_volume_participation: float = Field(default=0.001, gt=0, le=0.01)
    fee_bps: float = Field(default=10, ge=0)
    slippage_bps: float = Field(default=10, ge=0)
    db_path: str = "data/cockpit.sqlite3"
    http_timeout: float = Field(default=20, gt=0, le=120)
    http_retries: int = Field(default=3, ge=0, le=5)
    metric_max_age_seconds: int = Field(default=3600, ge=60)
    binance_spot_url: str = "https://api.binance.com"
    binance_spot_fallback_urls: list[str] = Field(default_factory=list)
    identity_cache_ttl_seconds: int = Field(default=300, ge=0, le=86400)
    binance_futures_url: str = "https://fapi.binance.com"
    coingecko_url: str = "https://api.coingecko.com/api/v3"
    symbol_map_path: str | None = None
    catalyst_path: str | None = None
    entry_refresh_limit: int = Field(default=10, ge=0, le=50)
    excluded_asset_ids: list[str] = Field(
        default_factory=lambda: [
            "tether",
            "usd-coin",
            "dai",
            "ethena-usde",
            "usds",
            "first-digital-usd",
            "true-usd",
            "usdd",
            "paypal-usd",
            "frax",
            "usdt0",
            "usual-usd",
            "usd1",
            "wrapped-bitcoin",
            "weth",
            "staked-ether",
            "wrapped-steth",
            "binance-peg-busd",
        ]
    )

    @field_validator("binance_spot_fallback_urls")
    @classmethod
    def spot_fallbacks(cls, urls: list[str]) -> list[str]:
        """Fallback is explicit and restricted to Binance native public hosts."""
        native_hosts = {
            "api.binance.com",
            "api1.binance.com",
            "api2.binance.com",
            "api3.binance.com",
            "api4.binance.com",
            "api-gcp.binance.com",
            "data-api.binance.vision",
        }
        result = []
        for url in urls:
            parsed = urlsplit(url)
            if (
                parsed.scheme != "https"
                or parsed.hostname not in native_hosts
                or parsed.username
                or parsed.password
                or parsed.port not in (None, 443)
                or parsed.path not in ("", "/")
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("spot fallback must be a native Binance HTTPS origin without credentials")
            normalized = url.rstrip("/")
            if normalized not in result:
                result.append(normalized)
        return result

    @model_validator(mode="after")
    def frames(self):
        if set(self.timeframes) - set(TIMEFRAMES) or not {"1D", "4h"}.issubset(self.timeframes):
            raise ValueError("valid timeframes must include 1D and 4h")
        return self
