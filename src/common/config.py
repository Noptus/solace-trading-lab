"""Runtime configuration and topic construction."""

from __future__ import annotations

from functools import lru_cache
from typing import Self

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings shared by services."""

    model_config = SettingsConfigDict(
        env_prefix="STL_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    topic_prefix: str = "solace-lab"
    solace_host: str = "localhost:55555"
    solace_vpn: str = "default"
    solace_username: str = "admin"
    solace_password: str = "admin"
    client_name: str = "solace-trading-lab"
    symbol: str = "FOO"
    market_data_depth: int = Field(default=10, gt=0)
    in_memory_bus: bool = False
    venue_http_port: int = Field(default=9001, gt=0)

    def normalized(self) -> Self:
        """Normalize operator-provided values."""

        return self.model_copy(
            update={"symbol": self.symbol.upper(), "topic_prefix": self.topic_prefix.strip("/")}
        )

    def with_client_name(self, suffix: str) -> Self:
        """Return a copy with a unique client name for multi-service deployments."""

        return self.model_copy(update={"client_name": f"{self.client_name}-{suffix}"})


class Topics:
    """Construct broker topics from one normalized prefix."""

    def __init__(self, prefix: str = "solace-lab") -> None:
        self.prefix = prefix.strip("/")

    def venue_orders(self, symbol: str) -> str:
        return f"{self.prefix}/venue/orders/{symbol.upper()}"

    def venue_order_wildcard(self) -> str:
        return f"{self.prefix}/venue/orders/*"

    def venue_acks(self, symbol: str) -> str:
        return f"{self.prefix}/venue/acks/{symbol.upper()}"

    def venue_fills(self, symbol: str) -> str:
        return f"{self.prefix}/venue/fills/{symbol.upper()}"

    def venue_fill_wildcard(self) -> str:
        return f"{self.prefix}/venue/fills/*"

    def venue_book(self, symbol: str) -> str:
        return f"{self.prefix}/venue/book/{symbol.upper()}"

    def venue_book_wildcard(self) -> str:
        return f"{self.prefix}/venue/book/*"

    def md_l1(self, symbol: str) -> str:
        return f"{self.prefix}/md/L1/{symbol.upper()}"

    def md_l1_wildcard(self) -> str:
        return f"{self.prefix}/md/L1/*"

    def md_l2(self, symbol: str) -> str:
        return f"{self.prefix}/md/L2/{symbol.upper()}"

    def md_l2_wildcard(self) -> str:
        return f"{self.prefix}/md/L2/*"

    def md_trades(self, symbol: str) -> str:
        return f"{self.prefix}/md/trades/{symbol.upper()}"

    def md_trades_wildcard(self) -> str:
        return f"{self.prefix}/md/trades/*"

    def chaos_events(self) -> str:
        return f"{self.prefix}/chaos/events"

    def replay_control(self) -> str:
        return f"{self.prefix}/replay/control"

    def all(self) -> str:
        return f"{self.prefix}/>"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached service settings."""

    return Settings().normalized()
