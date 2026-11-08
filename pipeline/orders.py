"""The one order path: from a stored exit plan to a sized order.

Order prices come only from tool-produced ExitPlanRecords. Analyst text and analyst scores
never feed sizing or risk_check (CLAUDE.md rule 2; issues #40, #50).
Nothing but the plan, the risk limits and the clock is an input here; a stale plan is refused
(issue #67). Plans are looked up in the pipeline's PlanLedger, never in a DecisionPacket
(issue #64).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

from agents.schemas import ExitPlanRecord
from pipeline.ledger import PlanLedger
from pipeline.market import NY
from tools.config import Limits
from tools.scan import validate_symbols
from tools.size import Plan, size_position
from tools.slippage import slippage_from_quote

_CENT = Decimal("0.01")
_SKEW = timedelta(seconds=5)  # tolerated clock skew for a plan dated slightly ahead of now


def select_plan(ledger: PlanLedger, plan_id: str) -> ExitPlanRecord:
    """Return the plan the pipeline recorded under this id (issue #64).

    Reads ONLY the pipeline's PlanLedger, never DecisionPacket.exit_plans: a plan carried in a
    packet (appended, model_copy'd, model_construct'ed or rebuilt from JSON) is never sized.
    Raises KeyError("unknown plan_id") if the ledger has no such plan, or is not sealed and
    accepted.
    """
    if not isinstance(ledger, PlanLedger):
        raise KeyError("unknown plan_id")
    return ledger.get(plan_id)


def _cents(text: object) -> Decimal | None:
    """A finite, positive, whole-cent Decimal, or None."""
    if not isinstance(text, str):
        return None
    try:
        d = Decimal(text.strip())
        if not d.is_finite() or d <= 0 or d % _CENT != 0:
            return None
    except (InvalidOperation, ValueError, ArithmeticError):
        return None
    return d


def _freshness_error(plan: ExitPlanRecord, limits: Limits, now: datetime) -> str | None:
    """Why the plan may not be ordered at `now`, or None if it is fresh. Arithmetic in UTC."""
    if now.tzinfo is None or now.utcoffset() is None:
        return "now must be timezone-aware"
    try:
        made = datetime.fromisoformat(plan.as_of)
    except (ValueError, TypeError):
        return "plan as_of is invalid"
    if made.tzinfo is None or made.utcoffset() is None:
        return "plan as_of is invalid"
    made_utc = made.astimezone(UTC)
    now_utc = now.astimezone(UTC)
    if made_utc.astimezone(NY).date() != now_utc.astimezone(NY).date():
        return "plan is from another session day"
    if made_utc - now_utc > _SKEW:
        return "plan is dated in the future"
    age = int((now_utc - made_utc).total_seconds())  # whole seconds, rounded toward zero
    if age > limits.max_plan_age_s:
        return f"plan is {age}s old (max {limits.max_plan_age_s}s)"
    return None


def build_order(
    plan: ExitPlanRecord, limits: Limits, now: datetime, quote: object = None
) -> dict:
    """Re-validate the plan, check it is fresh at `now`, then size it (size_position result).

    The plan is refused unless its entry is its last_close (entry_level "last_close" and the
    recorded last_close equal to entry): orders are market orders (issue #118).
    `now` must be timezone-aware. After the price re-validation the plan is refused if `now`
    is naive, plan.as_of is not an aware ISO datetime, the plan is from another New York
    session day, it is dated more than 5 s in the future, or it is older than
    limits.max_plan_age_s. Never raises for a bad plan: returns {"ok": False, "reason": ...}.
    `quote` is the symbol's live Alpaca latestQuote, read by the caller at `now` (#68). Without
    a fresh, uncrossed quote, or when its ask is above entry + slippage allowance, there is no
    order. The order is sized with that allowance (tools.slippage, tools.size); the result
    gains "quote" (bid, ask, spread, quote_t, age_s) for the ticket and the journal.
    """
    try:
        validate_symbols([plan.symbol])
    except (ValueError, AttributeError, TypeError):
        return {"ok": False, "reason": "plan symbol is not a valid ticker"}
    entry, stop, target = _cents(plan.entry), _cents(plan.stop), _cents(plan.target)
    if entry is None or stop is None or target is None:
        return {"ok": False, "reason": "plan prices must be finite whole cents"}
    if not stop < entry < target:
        return {"ok": False, "reason": "plan must have stop < entry < target"}
    try:
        risk = Decimal(plan.risk_per_share)
        matches = risk.is_finite() and risk == entry - stop
    except (InvalidOperation, ValueError, ArithmeticError):
        matches = False
    if not matches:
        return {"ok": False, "reason": "plan risk_per_share does not match entry - stop"}
    # #78: the minimum stop distance before sizing, so a self-consistent forged record with a
    # noise stop is never sized (risk_check would block it later; refuse it here first)
    if risk < limits.min_risk_per_share:
        return {"ok": False, "reason": (
            f"risk per share {risk} is below min_risk_per_share {limits.min_risk_per_share}")}
    if risk < entry * limits.min_risk_pct / 100:
        return {"ok": False, "reason": (
            f"risk per share {risk} is below {limits.min_risk_pct}% of entry")}
    # Issue #118: the order is a MARKET order, so it fills near the last close. A plan whose
    # entry is any other price would be sized on a risk it does not carry.
    if plan.entry_level != "last_close" or _cents(plan.last_close) != entry:
        return {"ok": False, "reason": "market order: plan entry must be its last_close"}
    stale = _freshness_error(plan, limits, now)
    if stale is not None:
        return {"ok": False, "reason": stale}
    slip = slippage_from_quote(entry, quote, limits, now)
    if slip.get("ok") is not True:
        return {"ok": False, "reason": str(slip.get("reason", "no live quote"))}
    sized = size_position(Plan(plan.symbol, entry, stop, target), limits,
                          slippage=Decimal(slip["allowance"]))
    if sized.get("ok") is True:
        sized["quote"] = {k: slip[k] for k in ("bid", "ask", "spread", "quote_t", "age_s")}
    return sized
