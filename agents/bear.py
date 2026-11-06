"""Bear researcher: challenges the trade from the packet only. Proposes; never sizes."""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from agents.debate_common import build_payload, make_validator
from agents.llm import AgentConfig, config_for
from agents.runner import StructuredRun, run_structured
from agents.schemas import BearCase, BullCase, DecisionPacket

PROMPT = (Path(__file__).parent / "prompts" / "bear.md").read_text(encoding="utf-8")


def unavailable_bear(symbol: str, reason_code: str | None) -> BearCase:
    """The fail-closed Bear case.

    A Bear that cannot run must never read as "no objections": a missing challenge would let
    an unchallenged trade through. So a failed Bear is fatal=True, which stops the debate and
    tells the downstream trader the trade was not challenged.

    Only the generic `reason_code` goes into the case text. Validator detail (which may list
    rejected money mentions) stays in the run record's `reason`, never in text the Trader reads.
    """
    return BearCase(
        symbol=symbol,
        plan_id=None,
        objections=[
            f"bear case unavailable ({reason_code or 'unknown'})",
            "the trade was not challenged by the bear",
            "debate failed closed",
        ],
        fatal=True,
    )


def run_bear(
    packet: DecisionPacket,
    *,
    client: Any,
    config: AgentConfig | None = None,
    opponent: BullCase | None = None,
    round: int = 1,  # noqa: A002 - name fixed by the task spec
    cancel: threading.Event | None = None,
) -> StructuredRun:
    """Make the bear case. FAILS CLOSED: on any failure value is a fatal BearCase, ok False."""
    cfg = config if config is not None else config_for("bear")
    try:
        payload = build_payload(packet, opponent, round)
        result = run_structured(
            "bear", PROMPT, payload, BearCase, make_validator(packet, BearCase),
            client=client, config=cfg, cancel=cancel,
        )
    except Exception as exc:  # noqa: BLE001
        result = StructuredRun(None, False, type(exc).__name__, "exception", [], 0)
    if result.ok:
        return result
    result.value = unavailable_bear(packet.symbol, result.reason_code)
    return result
