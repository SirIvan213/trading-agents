"""Bull researcher: argues for the trade from the packet only. Proposes; never sizes."""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from agents.debate_common import BullOut, build_payload, make_validator, to_case
from agents.llm import AgentConfig, config_for
from agents.runner import StructuredRun, run_structured
from agents.schemas import BearCase, BullCase, DecisionPacket

PROMPT = (Path(__file__).parent / "prompts" / "bull.md").read_text(encoding="utf-8")


def run_bull(
    packet: DecisionPacket,
    *,
    client: Any,
    config: AgentConfig | None = None,
    opponent: BearCase | None = None,
    round: int = 1,  # noqa: A002 - name fixed by the task spec
    cancel: threading.Event | None = None,
) -> StructuredRun:
    """Make the bull case. A failure gives value None and ok False (the caller decides)."""
    cfg = config if config is not None else config_for("bull")
    try:
        payload = build_payload(packet, opponent, round)
        result = run_structured(
            "bull", PROMPT, payload, BullOut, make_validator(packet, BullCase),
            client=client, config=cfg, cancel=cancel,
        )
        if result.ok and result.value is not None:
            result.value = to_case(result.value, packet.symbol)  # "" -> None, upper symbol
    except Exception as exc:  # noqa: BLE001
        return StructuredRun(None, False, type(exc).__name__, "exception", [], 0)
    return result
