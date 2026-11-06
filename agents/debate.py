"""Bull/Bear debate: sequential rounds over one DecisionPacket. Advisory text only.

Nothing here sizes, limits or trades, and nothing in this module is imported by
pipeline/orders.py or tools/ (CLAUDE.md rule 2).
"""
from __future__ import annotations

import os
import threading
from typing import Any

from agents.bear import run_bear, unavailable_bear
from agents.bull import run_bull
from agents.llm import AgentConfig, config_for
from agents.runner import StructuredRun
from agents.schemas import (
    AgentRunRecord,
    BearCase,
    BullCase,
    DebateRecord,
    DebateTurn,
    DecisionPacket,
)

DEFAULT_ROUNDS = 2
MAX_ROUNDS = 5
__all__ = ["run_debate"]


def _client_for(clients: dict[str, Any] | Any, side: str) -> Any:
    return clients.get(side) if isinstance(clients, dict) else clients


def resolve_rounds(rounds: int | None) -> int:
    """Rounds argument, else TRADING_AGENTS_DEBATE_ROUNDS, else 2. ValueError unless 1..5."""
    value: Any = rounds
    if value is None:
        raw = os.environ.get("TRADING_AGENTS_DEBATE_ROUNDS")
        if raw is None:
            return DEFAULT_ROUNDS
        try:
            value = int(raw.strip())
        except ValueError:
            raise ValueError("TRADING_AGENTS_DEBATE_ROUNDS must be an int from 1 to 5") from None
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_ROUNDS:
        raise ValueError("debate rounds must be an int from 1 to 5")
    return value


def _unavailable_bull(symbol: str, reason_code: str | None) -> BullCase:
    """Placeholder for a failed Bull turn (never passed to the Bear as a real case).

    Generic text from `reason_code` only: validator detail stays in the run record.
    """
    return BullCase(
        symbol=symbol, plan_id=None,
        argument=f"bull case unavailable ({reason_code or 'unknown'})",
        key_points=["bull case unavailable"],
    )


def _record(side: str, cfg: AgentConfig, run: StructuredRun) -> AgentRunRecord:
    return AgentRunRecord(
        agent=side, requested_model=cfg.model, effort=cfg.effort, ok=run.ok,
        reason=run.reason, reason_code=run.reason_code, duration_ms=run.duration_ms,
        calls=run.calls,
    )


def run_debate(
    packet: DecisionPacket,
    *,
    clients: dict[str, Any] | Any,
    configs: dict[str, AgentConfig] | None = None,
    rounds: int | None = None,
    cancel: threading.Event | None = None,
) -> DebateRecord:
    """Run up to `rounds` Bull-then-Bear rounds and return the DebateRecord.

    `clients` is one client or a dict keyed "bull"/"bear". Each round the Bull sees the Bear's
    latest case (None in round 1); then the Bear sees the Bull's case from this round, or the
    Bull's last valid case (or None) if the Bull failed. The debate stops as soon as a Bear
    case is fatal (including a failed, fail-closed Bear). Sequential on purpose: each side
    needs the other's latest case. Raises ValueError only for bad `rounds` / config, up front;
    after that exceptions inside a turn become that turn's failure.
    """
    planned = resolve_rounds(rounds)
    cfgs = {s: (configs or {}).get(s) or config_for(s) for s in ("bull", "bear")}
    sym = packet.symbol
    turns: list[DebateTurn] = []
    last_bull: BullCase | None = None  # last VALID bull case
    last_bear: BearCase | None = None
    rounds_run = 0
    fatal = False

    for rnd in range(1, planned + 1):
        rounds_run = rnd
        try:
            bull = run_bull(
                packet, client=_client_for(clients, "bull"), config=cfgs["bull"],
                opponent=last_bear, round=rnd, cancel=cancel,
            )
        except Exception as exc:  # noqa: BLE001 - a turn must not raise
            bull = StructuredRun(None, False, type(exc).__name__, "exception", [], 0)
        if bull.ok and isinstance(bull.value, BullCase):
            last_bull = bull.value
            bull_case = bull.value
        else:
            bull.ok = False
            bull_case = _unavailable_bull(sym, bull.reason_code)
        turns.append(DebateTurn(
            round=rnd, side="bull", case=bull_case, run=_record("bull", cfgs["bull"], bull)
        ))

        try:
            bear = run_bear(
                packet, client=_client_for(clients, "bear"), config=cfgs["bear"],
                opponent=last_bull, round=rnd, cancel=cancel,
            )
        except Exception as exc:  # noqa: BLE001 - fail closed
            bear = StructuredRun(None, False, type(exc).__name__, "exception", [], 0)
        if not isinstance(bear.value, BearCase):
            bear.ok = False
            bear.value = unavailable_bear(sym, bear.reason_code)
        last_bear = bear.value
        turns.append(DebateTurn(
            round=rnd, side="bear", case=bear.value, run=_record("bear", cfgs["bear"], bear)
        ))
        if last_bear.fatal:
            fatal = True
            break

    return DebateRecord(
        symbol=sym,
        as_of=packet.as_of,
        rounds_planned=planned,
        rounds_run=rounds_run,
        turns=turns,
        stopped_early=fatal and rounds_run < planned,
        fatal=fatal,
        same_model=cfgs["bull"].model == cfgs["bear"].model,
        final_bull=last_bull,
        final_bear=last_bear,
        all_ok=all(t.run.ok for t in turns),
    )
