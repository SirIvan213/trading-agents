"""Load risk limits from config/limits.json (falls back to the example file)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import time
from decimal import Decimal
from pathlib import Path

from tools.symbols import normalize_symbol

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"

# The only reward multiples the Technical analyst may request from plan_exit (a menu, not a
# number it supplies). good_setup_multiple must be one of them, so the default target is
# always one the model could also ask for.
REWARD_MULTIPLES = ("1", "1.5", "2", "2.5", "3")

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
    # that sizing pushes to the position cap. The ATR multiple must be > 0 (#76).
    min_risk_per_share: Decimal = Decimal("0.10")
    min_risk_atr_multiple: Decimal = Decimal("1.0")
    # Maximum age of an exit plan at order time (#67), in seconds.
    max_plan_age_s: int = 300
    # Plan labels (tools/plan_labels.py), in 5-minute ATRs. They set the suggested (maximum)
    # reward multiple plan_exit allows; they never change a stop, a size or a risk limit.
    max_extension_atr_5m: Decimal = Decimal("2.0")  # entry - VWAP above this: extended
    max_stop_atr_5m: Decimal = Decimal("2.5")       # risk at or below this: within max width
    good_setup_multiple: Decimal = Decimal("2")     # suggested when the plan qualifies
    # Kill switch (#32): false blocks every order in risk_check, including the approval
    # re-check. A missing key means true, so existing configs keep working.
    trading_enabled: bool = True
    # Gappy opening ranges (#14): exit_plan refuses an orb setup whose 15-minute opening
    # range is missing more than this many minutes of bars (thin names on IEX data).
    max_or_missing_minutes: int = 3
    # Slippage on market entries (#68): sizing adds max(spread, slippage_pct% of entry, 0.01)
    # per share, from a live quote at most max_order_quote_age_s old (tools/slippage.py).
    slippage_pct: Decimal = Decimal("0.10")
    # Minimum stop distance relative to price (#77): a stop closer than this percent of the
    # entry is a noise stop. Enforced in exit_plan, build_order and risk_check.
    min_risk_pct: Decimal = Decimal("0.25")
    max_order_quote_age_s: int = 15


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
        if not item.strip():
            continue
        sym = normalize_symbol(item)
        if sym is None:
            # a typo would silently leave a symbol Ivan meant to block tradeable
            raise ValueError(f"blocked_symbols: not a valid ticker: {item!r}")
        out.add(sym)
    return frozenset(out)


def validate_limits(limits: Limits) -> None:
    """Raise one ValueError listing every problem with `limits`; return None if valid.

    Called by limits_from_dict (not by Limits.__post_init__, so tests can still build
    deliberately bad Limits). Checks only ever tighten: nothing here relaxes a limit.
    """
    problems: list[str] = []
    for flag in ("paper", "trading_enabled"):
        if not isinstance(getattr(limits, flag), bool):
            problems.append(f"{flag} must be a boolean, got {getattr(limits, flag)!r}")

    def positive(name: str) -> None:
        v = getattr(limits, name)
        if not isinstance(v, Decimal) or not v.is_finite():
            problems.append(f"{name} must be a finite number, got {v!r}")
        elif v <= 0:
            problems.append(f"{name} must be greater than zero, got {v}")

    for name in (
        "account_cash", "max_loss_per_trade", "max_position_value",
        "daily_loss_limit", "min_reward_to_risk", "max_reward_multiple",
        "min_risk_per_share", "max_extension_atr_5m", "max_stop_atr_5m", "good_setup_multiple",
    ):
        positive(name)
    try:
        good = limits.good_setup_multiple
        if not (limits.min_reward_to_risk <= good <= limits.max_reward_multiple):
            problems.append(
                "good_setup_multiple must be between min_reward_to_risk and max_reward_multiple"
            )
        if good.is_finite() and not any(good == Decimal(m) for m in REWARD_MULTIPLES):
            problems.append(
                f"good_setup_multiple must be one of {', '.join(REWARD_MULTIPLES)}, got {good}"
            )
    except (TypeError, ArithmeticError):
        pass  # already reported above
    atr_mult = limits.min_risk_atr_multiple
    if not isinstance(atr_mult, Decimal) or not atr_mult.is_finite():
        problems.append(f"min_risk_atr_multiple must be a finite number, got {atr_mult!r}")
    elif atr_mult <= 0:
        # #76: a zero multiple is a disabled limit, not a setting
        problems.append(f"min_risk_atr_multiple must be greater than zero, got {atr_mult}")
    pct = limits.min_risk_pct
    if not isinstance(pct, Decimal) or not pct.is_finite():
        problems.append(f"min_risk_pct must be a finite number, got {pct!r}")
    elif not (0 < pct < 10):
        problems.append(f"min_risk_pct must be above 0 and below 10, got {pct}")
    mrps = limits.min_risk_per_share
    if isinstance(mrps, Decimal) and mrps.is_finite() and mrps > 0:
        if mrps % Decimal("0.01") != 0:
            problems.append(f"min_risk_per_share must be whole cents, got {mrps}")
        mlpt = limits.max_loss_per_trade
        if isinstance(mlpt, Decimal) and mlpt.is_finite() and mrps >= mlpt:
            problems.append("min_risk_per_share must be below max_loss_per_trade "
                            "(otherwise no whole share ever fits)")
    slip = limits.slippage_pct
    if not isinstance(slip, Decimal) or not slip.is_finite():
        problems.append(f"slippage_pct must be a finite number, got {slip!r}")
    elif not (0 <= slip < 100):
        problems.append(f"slippage_pct must be at least 0 and below 100, got {slip}")
    qage = limits.max_order_quote_age_s
    if isinstance(qage, bool) or not isinstance(qage, int) or not 1 <= qage <= 60:
        # #140: an older quote makes the price-moved check and the allowance meaningless
        problems.append(f"max_order_quote_age_s must be an integer from 1 to 60, got {qage!r}")
    gap = limits.max_or_missing_minutes
    if isinstance(gap, bool) or not isinstance(gap, int) or gap < 0:
        problems.append(f"max_or_missing_minutes must be an integer of at least 0, got {gap!r}")
    age = limits.max_plan_age_s
    if isinstance(age, bool) or not isinstance(age, int) or not 1 <= age <= 900:
        # #75: an intraday plan older than 15 minutes is stale
        problems.append(f"max_plan_age_s must be an integer from 1 to 900, got {age!r}")
    buf = limits.cash_buffer_pct
    if not isinstance(buf, Decimal) or not buf.is_finite():
        problems.append(f"cash_buffer_pct must be a finite number, got {buf!r}")
    elif not (0 <= buf < 100):
        problems.append(f"cash_buffer_pct must be at least 0 and below 100, got {buf}")
    bs = limits.blocked_symbols
    if not isinstance(bs, frozenset) or not all(
        isinstance(x, str) and normalize_symbol(x) == x for x in bs
    ):
        problems.append("blocked_symbols must be a set of valid upper-case tickers")
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


def _whole(value: object, name: str) -> int:
    """A JSON whole number (int, integral float or digit string); bool rejected."""
    if isinstance(value, bool):
        raise ValueError(f"{name}: not an integer: {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value.strip()):
        return int(value.strip())
    raise ValueError(f"{name}: not an integer: {value!r}")


def _json_bool(value: object, name: str) -> bool:
    """Exactly a JSON true/false (#30): "false", 0, 1, "" and null are config errors, never
    coerced with bool()."""
    if not isinstance(value, bool):
        raise ValueError(f"{name}: must be a JSON boolean (true or false), got {value!r}")
    return value


def limits_from_dict(d: dict) -> Limits:
    """Build and validate Limits. Unparseable values raise ValueError naming the field.

    `paper` is required and `trading_enabled` defaults to true; both must be JSON booleans."""
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
        paper=_json_bool(d["paper"], "paper"),
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
        max_extension_atr_5m=_dec(d, "max_extension_atr_5m", "2.0"),
        max_stop_atr_5m=_dec(d, "max_stop_atr_5m", "2.5"),
        good_setup_multiple=_dec(d, "good_setup_multiple", "2"),
        trading_enabled=_json_bool(d.get("trading_enabled", True), "trading_enabled"),
        max_or_missing_minutes=_whole(d.get("max_or_missing_minutes", 3), "max_or_missing_minutes"),
        slippage_pct=_dec(d, "slippage_pct", "0.10"),
        min_risk_pct=_dec(d, "min_risk_pct", "0.25"),
        max_order_quote_age_s=_whole(d.get("max_order_quote_age_s", 15), "max_order_quote_age_s"),
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
