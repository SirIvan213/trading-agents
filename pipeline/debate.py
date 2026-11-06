"""Run the bull/bear debate for one DecisionPacket with live defaults."""
from __future__ import annotations

from typing import Any

from agents.debate import resolve_rounds, run_debate
from agents.llm import AgentConfig, config_for, make_client
from agents.schemas import DecisionPacket


def run_debate_sync(
    packet: DecisionPacket,
    *,
    client: Any = None,
    configs: dict[str, AgentConfig] | None = None,
    rounds: int | None = None,
) -> dict:
    """Return the DebateRecord as a JSON-serializable dict.

    Defaults: make_client() and config_for("bull") / config_for("bear"). Raises ValueError for
    bad `rounds` before any request is made.
    """
    planned = resolve_rounds(rounds)  # ValueError before make_client() (no key needed)
    record = run_debate(
        packet,
        clients=client if client is not None else make_client(),
        configs=configs if configs is not None else {s: config_for(s) for s in ("bull", "bear")},
        rounds=planned,
    )
    return record.model_dump(mode="json")
