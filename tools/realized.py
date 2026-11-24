"""A closed trade's realized profit or loss (Phase 5 PR 4, #155). Pure, Decimal only.

From the broker's recorded fills: the entry's filled quantity and average price, and each
exit's quantity and average price (the take-profit leg, the stop leg, a flatten's close).
    realized = sum(exit_qty * (exit_avg - entry_avg))
rounded DOWN to the cent (ROUND_FLOOR): a loss is never understated, so the daily loss stop
(#188) never under-counts. Nothing here comes from an agent (CLAUDE.md rule 2).
"""
from __future__ import annotations

from decimal import ROUND_FLOOR, Decimal, InvalidOperation

CENT = Decimal("0.01")


class RealizedError(ValueError):
    """The fills do not describe one closed trade."""


def _positive(value: object, what: str) -> Decimal:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise RealizedError(f"{what} is not a number") from None
    if not d.is_finite() or d <= 0:
        raise RealizedError(f"{what} must be positive")
    return d


def realized_pnl(entry_qty: object, entry_avg: object,
                 exits: list[tuple[object, object]]) -> Decimal:
    """The trade's realized P&L, negative for a loss. Raises RealizedError unless the exits'
    quantities add up to exactly the entry's filled quantity."""
    qty = _positive(entry_qty, "entry quantity")
    avg = _positive(entry_avg, "entry average price")
    if not exits:
        raise RealizedError("no exit fills")
    total, pnl = Decimal("0"), Decimal("0")
    for n, (q, a) in enumerate(exits, 1):
        eq, ea = _positive(q, f"exit {n} quantity"), _positive(a, f"exit {n} average price")
        total += eq
        pnl += eq * (ea - avg)
    if total != qty:
        raise RealizedError(f"the exits cover {total} of {qty} shares")
    return pnl.quantize(CENT, rounding=ROUND_FLOOR)
