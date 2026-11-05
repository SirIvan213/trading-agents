"""Run all three analysts for one symbol with live defaults."""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from agents.llm import AgentConfig, config_for, make_client
from agents.orchestrator import AGENTS, run_analysts
from pipeline.data import LiveData


def run_analysts_sync(
    symbol: str,
    now: datetime,
    *,
    data: Any = None,
    client: Any = None,
    configs: dict[str, AgentConfig] | None = None,
) -> dict:
    """Return the DecisionPacket as a JSON-serializable dict.

    Defaults: LiveData(), make_client(), config_for(agent) for each agent.
    """
    packet = asyncio.run(run_analysts(
        symbol,
        now,
        data=data if data is not None else LiveData(),
        clients=client if client is not None else make_client(),
        configs=configs if configs is not None else {a: config_for(a) for a in AGENTS},
    ))
    return packet.model_dump(mode="json")
