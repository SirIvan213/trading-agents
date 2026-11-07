"""Exit plan: stop, target and time exit for a long entry.

The stop sits just below structure (opening-range low or a named level),
optionally padded by a fraction of ATR. Agents never compute these numbers;
this function is the source of truth. Stops round down, targets round up.
"""
from __future__ import annotations

from datetime import date, time
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from tools.config import Limits
from tools.market_calendar import CalendarError, day_times, session_hours
from tools.size import Plan

CENT = Decimal("0.01")
MIN_R = Decimal("1.0")
SETUPS = ("orb", "level")


def _refuse(reason: str) -> dict:
    return {"ok": False, "reason": reason}


def _dec(value: object, name: str) -> tuple[Decimal | None, str | None]:
    """Convert to a finite Decimal, or return (None, refusal reason naming `name`)."""
    if isinstance(value, bool):
        return None, f"{name} is not a number"
    try:
        d = Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None, f"{name} is not a number"
    if not d.is_finite():
        return None, f"{name} must be a finite number"
    return d, None


def exit_plan(
    setup: str,
    entry: Decimal,
    limits: Limits,
    *,
    opening_range: dict | None = None,
    level: Decimal | None = None,
    atr: Decimal | None = None,
    reward_multiple: Decimal | None = None,
    buffer_atr: Decimal = Decimal("0.10"),
    session_day: date | None = None,
) -> dict:
    """Return {"ok", "setup", "entry", "stop", "target", "risk_per_share",
    "reward_to_risk", "time_exit", "notes"} or {"ok": False, "reason"}.

    Long only. "orb": structure is opening_range["low"] and the range must be
    complete (opening_range["complete"] is True) and missing at most
    limits.max_or_missing_minutes minutes of bars (opening_range["missing_minutes"], which
    must be present; issue #14); "level": structure is `level`.
    time_exit: limits.flat_by, or with `session_day` (ET date) the NYSE calendar's flat-by for
    that day (12:55 on an early close, issue #11). With `session_day`, a holiday, weekend or
    a year the calendar does not cover is refused.
    Entry must be a whole number of cents. The ATR timeframe should match the
    setup (e.g. 1-minute ATR for an opening-range breakout).
    Stop = floor_cent(structure - buffer_atr * atr). Target = ceil_cent(entry + R * risk),
    where R = max(reward_multiple or min_reward_to_risk, min_reward_to_risk, 1.0).
    reward_multiple must be positive and at most limits.max_reward_multiple.

    Minimum stop distance: a stop a few cents from entry is a noise stop that
    sizing then pushes to the position cap. After the other checks, the plan is
    refused when risk per share is below limits.min_risk_per_share, or, when an
    ATR is given and limits.min_risk_atr_multiple > 0, below that multiple of ATR.
    """
    if setup not in SETUPS:
        return _refuse(f"unknown setup {setup!r}")

    if setup == "orb":
        if not opening_range or "low" not in opening_range:
            return _refuse("orb setup needs opening_range with a low")
        if opening_range.get("complete") is not True:
            return _refuse("opening range incomplete")
        structure, err = _dec(opening_range["low"], "opening_range low")
        if err:
            return _refuse(err)
        missing = opening_range.get("missing_minutes")
        if isinstance(missing, bool) or not isinstance(missing, int) or missing < 0:
            return _refuse("opening range has no valid missing_minutes count")
        if missing > limits.max_or_missing_minutes:
            return _refuse(
                f"opening range is missing {missing} minutes of bars "
                f"(max {limits.max_or_missing_minutes})"
            )
    else:
        if level is None:
            return _refuse("level setup needs a level")
        structure, err = _dec(level, "level")
        if err:
            return _refuse(err)

    entry, err = _dec(entry, "entry")
    if err:
        return _refuse(err)
    if entry <= 0:
        return _refuse("entry must be positive")
    if entry % CENT != 0:
        return _refuse("entry must be a whole number of cents")
    if entry <= structure:
        return _refuse("entry must be above structure (long only)")
    atr_d: Decimal | None = None
    if atr is not None:
        atr_d, err = _dec(atr, "atr")
        if err:
            return _refuse(err)
        if atr_d < 0:
            return _refuse("atr must not be negative")
    buffer_atr, err = _dec(buffer_atr, "buffer_atr")
    if err:
        return _refuse(err)
    if buffer_atr < 0:
        return _refuse("buffer_atr must not be negative")

    notes: list[str] = []
    buffer = buffer_atr * atr_d if atr_d is not None else Decimal("0")
    stop = (structure - buffer).quantize(CENT, rounding=ROUND_FLOOR)
    if stop <= 0:
        return _refuse("stop would be zero or negative")
    if stop >= entry:  # unreachable given checks above, kept as a guard
        return _refuse("stop must be below entry")

    if reward_multiple is None:
        r = limits.min_reward_to_risk
    else:
        r, err = _dec(reward_multiple, "reward_multiple")
        if err:
            return _refuse(err)
        if r <= 0:
            return _refuse("reward_multiple must be positive")
        if r > limits.max_reward_multiple:
            return _refuse(
                f"reward_multiple {r} exceeds max_reward_multiple {limits.max_reward_multiple}"
            )
    if r < limits.min_reward_to_risk:
        notes.append(
            f"reward_multiple {r} below limit; using min_reward_to_risk {limits.min_reward_to_risk}"
        )
        r = limits.min_reward_to_risk
    if r < MIN_R:
        notes.append(f"reward multiple {r} floored to {MIN_R}")
        r = MIN_R
    if r > limits.max_reward_multiple:
        return _refuse(
            f"config error: effective reward multiple {r} exceeds "
            f"max_reward_multiple {limits.max_reward_multiple}"
        )

    risk = entry - stop
    if risk < limits.min_risk_per_share:
        return _refuse(
            f"risk per share {risk} is below min_risk_per_share {limits.min_risk_per_share}"
        )
    if (
        atr_d is not None
        and limits.min_risk_atr_multiple > 0
        and risk < limits.min_risk_atr_multiple * atr_d
    ):
        return _refuse(
            f"risk per share {risk} is below {limits.min_risk_atr_multiple} x ATR {atr_d}"
        )
    target = (entry + r * risk).quantize(CENT, rounding=ROUND_CEILING)
    # Rounded down so the reported ratio never overstates the real one.
    rr = ((target - entry) / risk).quantize(CENT, rounding=ROUND_FLOOR)

    if atr is None and setup == "orb":
        notes.append("no atr given; stop has no volatility buffer")
    notes.append(
        f"stop = {setup} structure {structure} minus buffer {buffer}, rounded down to the cent"
    )

    flat_by: time = limits.flat_by
    if session_day is not None:
        try:
            if session_hours(session_day) is None:
                return _refuse(f"market is closed on {session_day}")
            flat_by = day_times(session_day, limits)[2]
        except CalendarError:
            return _refuse("market calendar does not cover this date")
    return {
        "ok": True,
        "setup": setup,
        "entry": str(entry),
        "stop": str(stop),
        "target": str(target),
        "risk_per_share": str(risk),
        "reward_to_risk": str(rr),
        "time_exit": f"{flat_by:%H:%M}",
        "notes": notes,
    }


def to_plan(result: dict, symbol: str) -> Plan:
    """Convert a successful exit_plan result into a tools.size.Plan."""
    if not result.get("ok"):
        raise ValueError(f"cannot build a Plan from a refused exit plan: {result.get('reason')}")
    return Plan(
        symbol=symbol.upper(),
        entry=Decimal(result["entry"]),
        stop=Decimal(result["stop"]),
        target=Decimal(result["target"]),
    )
