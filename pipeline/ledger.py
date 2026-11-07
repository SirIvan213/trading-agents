"""The pipeline's plan ledger: the only source of plans the order path will size (issue #64).

One ledger per pipeline run (a random `run_id`). The plan_exit executor records each plan the
moment exit_plan produces it. After the analysts finish, the orchestrator seals the ledger:
accepted if the technical run validated, emptied otherwise (the same rule that decides which
plans reach the packet).

The order path looks plans up HERE by plan_id and never reads `DecisionPacket.exit_plans`.
A packet is just data: anyone holding it can append a plan, `model_copy(update=...)` one or
rebuild it from JSON, and a self-consistent forgery would pass every price check. A ledger
lookup returns a freshly validated copy of what plan_exit recorded, so such a forgery is
never sized.
"""
from __future__ import annotations

import secrets
import threading

from agents.schemas import ExitPlanRecord


class LedgerError(ValueError):
    """A plan could not be recorded (sealed ledger, wrong symbol or duplicate id)."""


class PlanLedger:
    """Plans recorded by plan_exit during one pipeline run, for one symbol. Thread-safe."""

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol.strip().upper()
        self.run_id = secrets.token_hex(8)
        self._plans: dict[str, dict] = {}
        self._sealed = False
        self._accepted = False
        self._lock = threading.Lock()

    def record(self, plan: ExitPlanRecord) -> None:
        """Store a plan as plan_exit produced it. Raises LedgerError once sealed, for another
        symbol, or for a plan_id already recorded."""
        data = ExitPlanRecord.model_validate(plan.model_dump()).model_dump()  # validated copy
        with self._lock:
            if self._sealed:
                raise LedgerError("ledger is sealed")
            if data["symbol"] != self.symbol:
                raise LedgerError("plan symbol does not match the ledger")
            if data["plan_id"] in self._plans:
                raise LedgerError("plan_id already recorded")
            self._plans[data["plan_id"]] = data

    def seal(self, *, accept: bool) -> None:
        """Close the ledger. With accept=False every plan is dropped (technical run failed).
        Later records are refused; sealing twice keeps the first outcome."""
        with self._lock:
            if self._sealed:
                return
            self._sealed = True
            self._accepted = accept
            if not accept:
                self._plans.clear()

    @property
    def sealed(self) -> bool:
        return self._sealed

    def plan_ids(self) -> list[str]:
        """Ids of the plans the order path may use (empty until sealed and accepted)."""
        with self._lock:
            return list(self._plans) if self._sealed and self._accepted else []

    def get(self, plan_id: str) -> ExitPlanRecord:
        """A fresh, validated copy of the recorded plan. KeyError("unknown plan_id") if it is
        absent, or if the ledger is not sealed and accepted."""
        with self._lock:
            if not (self._sealed and self._accepted) or not isinstance(plan_id, str):
                raise KeyError("unknown plan_id")
            data = self._plans.get(plan_id)
        if data is None:
            raise KeyError("unknown plan_id")
        return ExitPlanRecord.model_validate(data)
