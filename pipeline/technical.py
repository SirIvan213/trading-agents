"""Run the Technical Analyst end to end with live defaults."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from agents.llm import AgentConfig, config_from_env, make_client
from agents.technical import run_technical_analyst
from pipeline.data import AlpacaMarketData, MarketData


def run_technical(
    symbol: str,
    now: datetime,
    *,
    data: MarketData | None = None,
    client: Any = None,
    config: AgentConfig | None = None,
) -> dict:
    """Return the analyst report as a JSON-serializable dict.

    Defaults: AlpacaMarketData(), make_client(), config_from_env().
    """
    report = run_technical_analyst(
        symbol,
        now,
        data if data is not None else AlpacaMarketData(),
        client=client if client is not None else make_client(),
        config=config if config is not None else config_from_env(),
    )
    return report.model_dump(mode="json")
