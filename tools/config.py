"""Load risk limits from config/limits.json (falls back to the example file)."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import time
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"


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


def _hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def limits_from_dict(d: dict) -> Limits:
    start, end = d["entry_window"]
    return Limits(
        paper=bool(d["paper"]),
        account_cash=Decimal(str(d["account_cash"])),
        cash_buffer_pct=Decimal(str(d["cash_buffer_pct"])),
        max_loss_per_trade=Decimal(str(d["max_loss_per_trade"])),
        max_position_value=Decimal(str(d["max_position_value"])),
        daily_loss_limit=Decimal(str(d["daily_loss_limit"])),
        max_trades_per_day=int(d["max_trades_per_day"]),
        min_reward_to_risk=Decimal(str(d["min_reward_to_risk"])),
        entry_start=_hhmm(start),
        entry_end=_hhmm(end),
        flat_by=_hhmm(d["flat_by"]),
        blocked_symbols=frozenset(s.upper() for s in d.get("blocked_symbols", [])),
    )


def load_limits(path: Path | None = None) -> Limits:
    """Read config/limits.json, or config/limits.example.json if it doesn't exist."""
    if path is None:
        path = CONFIG_DIR / "limits.json"
        if not path.exists():
            path = CONFIG_DIR / "limits.example.json"
    return limits_from_dict(json.loads(path.read_text()))
