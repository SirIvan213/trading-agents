"""Load risk limits from config/limits.json (falls back to the example file)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import time
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"

# Regular session bounds (US/Eastern). Defined here so risk_check can import them
# without a circular import.
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)


@dataclass(frozen=True)
class Limits:
    paper: bool
    account_cash: Decimal
    cash_buffer_pct: Decimal
    max_loss_per_trade: Decimal
    max_position_value: Decimal
    daily_loss_limit: Decimal
    max_trades_per_day: int
    min_reward_to_risk: Decimal
    entry_start: time
    entry_end: time
    flat_by: time
    blocked_symbols: frozenset[str] = field(default_factory=frozenset)
    max_reward_multiple: Decimal = Decimal("5")
    # Minimum stop distance (#66): a stop a few cents from entry is a noise stop
    # that sizing pushes to the position cap. ATR part is off when the multiple is 0.
    min_risk_per_share: Decimal = Decimal("0.10")
    min_risk_atr_multiple: Decimal = Decimal("1.0")
    # Maximum age of an exit plan at order time (#67), in seconds.
    max_plan_age_s: int = 300


def _hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def _dec(d: dict, key: str, default: str | None = None) -> Decimal:
    raw = d[key] if default is None else d.get(key, default)
    try:
        return Decimal(str(raw))
    except (ArithmeticError, ValueError):
        raise ValueError(f"{key}: not a number: {raw!r}") from None


def _time(value: object, name: str) -> time:
    try:
        return _hhmm(value)  # type: ignore[arg-type]
    except (ValueError, TypeError, AttributeError):
        raise ValueError(f"{name}: expected 'HH:MM', got {value!r}") from None


def _blocked(raw: object) -> frozenset[str]:
    if not isinstance(raw, (list, tuple)):
        raise ValueError("blocked_symbols: must be a list of strings")
    out: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            raise ValueError(f"blocked_symbols: entries must be strings, got {item!r}")
        sym = item.strip().upper()
        if sym:
            out.add(sym)
    return frozenset(out)


def validate_limits(limits: Limits) -> None:
    """Raise one ValueError listing every problem with `limits`; return None if valid.

    Called by limits_from_dict (not by Limits.__post_init__, so tests can still build
    deliberately bad Limits). Checks only ever tighten: nothing here relaxes a limit.
    """
    problems: list[str] = []

    def positive(name: str) -> None:
        v = getattr(limits, name)
        if not isinstance(v, Decimal) or not v.is_finite():
            problems.append(f"{name} must be a finite number, got {v!r}")
        elif v <= 0:
            problems.append(f"{name} must be greater than zero, got {v}")

    for name in (
        "account_cash", "max_loss_per_trade", "max_position_value",
        "daily_loss_limit", "min_reward_to_risk", "max_reward_multiple",
        "min_risk_per_share",
    ):
        positive(name)
    atr_mult = limits.min_risk_atr_multiple
    if not isinstance(atr_mult, Decimal) or not atr_mult.is_finite():
        problems.append(f"min_risk_atr_multiple must be a finite number, got {atr_mult!r}")
    elif atr_mult < 0:
        problems.append(f"min_risk_atr_multiple must be at least 0, got {atr_mult}")
    age = limits.max_plan_age_s
    if isinstance(age, bool) or not isinstance(age, int) or age < 1:
        problems.append(f"max_plan_age_s must be an integer of at least 1, got {age!r}")
    buf = limits.cash_buffer_pct
    if not isinstance(buf, Decimal) or not buf.is_finite():
        problems.append(f"cash_buffer_pct must be a finite number, got {buf!r}")
    elif not (0 <= buf < 100):
        problems.append(f"cash_buffer_pct must be at least 0 and below 100, got {buf}")
    bs = limits.blocked_symbols
    if not isinstance(bs, frozenset) or not all(
        isinstance(x, str) and x and x == x.strip().upper() for x in bs
    ):
        problems.append("blocked_symbols must be a set of stripped upper-case symbols")
    n = limits.max_trades_per_day
    if isinstance(n, bool) or not isinstance(n, int) or n < 1:
        problems.append(f"max_trades_per_day must be an integer of at least 1, got {n!r}")
    try:
        if limits.max_reward_multiple < limits.min_reward_to_risk:
            problems.append("max_reward_multiple must not be below min_reward_to_risk")
    except (TypeError, ArithmeticError):
        pass  # already reported above
    start, end, flat = limits.entry_start, limits.entry_end, limits.flat_by
    try:
        if start > end:
            problems.append("entry_window start must not be after its end")
        if start < MARKET_OPEN:
            problems.append(
                f"entry_window start {start:%H:%M} is before market open {MARKET_OPEN:%H:%M}"
            )
        if end > MARKET_CLOSE:
            problems.append(
                f"entry_window end {end:%H:%M} is after market close {MARKET_CLOSE:%H:%M}"
            )
        if not (MARKET_OPEN <= flat <= MARKET_CLOSE):
            problems.append(
                f"flat_by {flat:%H:%M} must be within {MARKET_OPEN:%H:%M}-{MARKET_CLOSE:%H:%M}"
            )
        if end > flat:
            problems.append("entry_window end must not be after flat_by")
    except TypeError:
        problems.append("entry_window and flat_by must be times")
    if problems:
        raise ValueError("invalid limits: " + "; ".join(problems))


def limits_from_dict(d: dict) -> Limits:
    """Build and validate Limits. Unparseable values raise ValueError naming the field."""
    try:
        start, end = d["entry_window"]
    except (TypeError, ValueError):
        raise ValueError("entry_window: expected [start, end] as 'HH:MM' strings") from None
    raw_trades = d["max_trades_per_day"]
    if isinstance(raw_trades, bool):
        max_trades = None
    elif isinstance(raw_trades, int):
        max_trades = raw_trades
    elif isinstance(raw_trades, float):
        max_trades = int(raw_trades) if raw_trades.is_integer() else None
    elif isinstance(raw_trades, str) and re.fullmatch(r"[0-9]+", raw_trades.strip()):
        max_trades = int(raw_trades.strip())
    else:
        max_trades = None
    if max_trades is None:
        raise ValueError(f"max_trades_per_day: not an integer: {raw_trades!r}")
    raw_age = d.get("max_plan_age_s", 300)
    if isinstance(raw_age, bool):
        max_age = None
    elif isinstance(raw_age, int):
        max_age = raw_age
    elif isinstance(raw_age, float):
        max_age = int(raw_age) if raw_age.is_integer() else None
    elif isinstance(raw_age, str) and re.fullmatch(r"[0-9]+", raw_age.strip()):
        max_age = int(raw_age.strip())
    else:
        max_age = None
    if max_age is None:
        raise ValueError(f"max_plan_age_s: not an integer: {raw_age!r}")
    limits = Limits(
        paper=bool(d["paper"]),
        account_cash=_dec(d, "account_cash"),
        cash_buffer_pct=_dec(d, "cash_buffer_pct"),
        max_loss_per_trade=_dec(d, "max_loss_per_trade"),
        max_position_value=_dec(d, "max_position_value"),
        daily_loss_limit=_dec(d, "daily_loss_limit"),
        max_trades_per_day=max_trades,
        min_reward_to_risk=_dec(d, "min_reward_to_risk"),
        entry_start=_time(start, "entry_window start"),
        entry_end=_time(end, "entry_window end"),
        flat_by=_time(d["flat_by"], "flat_by"),
        blocked_symbols=_blocked(d.get("blocked_symbols", [])),
        max_reward_multiple=_dec(d, "max_reward_multiple", "5"),
        min_risk_per_share=_dec(d, "min_risk_per_share", "0.10"),
        min_risk_atr_multiple=_dec(d, "min_risk_atr_multiple", "1.0"),
        max_plan_age_s=max_age,
    )
    validate_limits(limits)
    return limits


def load_limits(path: Path | None = None) -> Limits:
    """Read config/limits.json, or config/limits.example.json if it doesn't exist."""
    if path is None:
        path = CONFIG_DIR / "limits.json"
        if not path.exists():
            path = CONFIG_DIR / "limits.example.json"
    return limits_from_dict(json.loads(path.read_text()))
