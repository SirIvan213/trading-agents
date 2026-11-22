"""GET /api/explainer: the How it works page.

Static content (the 8 steps with their Code / AI / You tags, the facts, the screens, the
glossary and the phase progress) comes from ui/api/content/explainer.json. The safety limits
and the daily caps come from the config, through the pipeline's own loaders
(ui/api/readers/config.py). Values are shown as recorded: a money limit is the Decimal string
from tools.config (e.g. "50.00"), a time is "HH:MM" New York time; nothing is computed.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from ui.api.paths import Paths
from ui.api.readers.config import ceilings, read_limits, read_run_day_config

CONTENT = Path(__file__).resolve().parent / "content" / "explainer.json"
ACTORS = ("code", "ai", "you")
PHASE_STATES = ("done", "in_progress", "next")

# (key on tools.config.Limits, label, unit)
LIMITS = (
    ("max_loss_per_trade", "most a single trade can lose", "usd"),
    ("daily_loss_limit", "most a day can lose", "usd"),
    ("max_trades_per_day", "trades per day, at most", "count"),
    ("max_open_positions", "open positions at a time, at most", "count"),
    ("flat_by", "planned close-out time (New York), earlier on short days", "time"),
    ("max_position_value", "most one position can be worth", "usd"),
    ("max_plan_age_s", "how long a plan stays valid, approval included", "seconds"),
)
# (key on pipeline.budget.Caps and CEILINGS, label)
CAPS = (
    ("max_decisions", "AI decisions per day, at most"),
    ("max_tokens", "AI tokens per day, at most"),
    ("max_fmp_requests", "FMP data requests per day"),
    ("max_av_calls", "Alpha Vantage calls per day"),
)


def load_content(path: Path = CONTENT) -> dict:
    """The static content, checked for the shape the page relies on. Raises ValueError."""
    data = json.loads(path.read_text(encoding="utf-8"))
    steps = data.get("steps")
    if not isinstance(steps, list) or [s.get("n") for s in steps] != list(range(1, 9)):
        raise ValueError("explainer content needs the 8 steps, numbered 1-8")
    if any(s.get("actor") not in ACTORS for s in steps):
        raise ValueError("every step's actor must be code, ai or you")
    if any(p.get("state") not in PHASE_STATES for p in data.get("phases", [])):
        raise ValueError("every phase's state must be done, in_progress or next")
    for key in ("hero", "facts", "screens", "glossary", "phases"):
        if key not in data:
            raise ValueError(f"explainer content has no {key}")
    return data


def _show(value: object) -> str:
    if hasattr(value, "strftime"):
        return value.strftime("%H:%M")  # type: ignore[union-attr]
    return str(value)


def explainer(paths: Paths) -> dict:
    content = load_content()
    problems: list[dict] = []
    limits = read_limits(paths.config_dir)
    if limits.error:
        problems.append({"level": "error", "text": limits.error})
    if limits.value is not None and not limits.value.paper:
        problems.append({"level": "error", "text": "limits say paper: false. This project "
                         "trades paper only, and run_day refuses to start"})
    run_day = read_run_day_config(paths.config_dir)
    if run_day.error:
        problems.append({"level": "error", "text": run_day.error})
    if limits.value is not None and not limits.value.trading_enabled:
        problems.append({"level": "warn", "text": "the kill switch is on (trading_enabled: "
                         "false): no new orders"})
    ceiling = ceilings()
    # dataclass fields by name (asdict), never a getattr with a computed name
    lim = asdict(limits.value) if limits.value is not None else None
    caps = asdict(run_day.value.caps) if run_day.value is not None else None
    return {
        **content,
        "limits": [{"key": k, "label": label, "unit": unit,
                    "value": _show(lim[k]) if lim is not None else None}
                   for k, label, unit in LIMITS],
        "limits_source": limits.view(),
        "paper": bool(lim["paper"]) if lim is not None else None,
        "trading_enabled": bool(lim["trading_enabled"]) if lim is not None else None,
        "caps": [{"key": k, "label": label, "ceiling": ceiling[k],
                  "value": caps[k] if caps is not None else None}
                 for k, label in CAPS],
        "caps_source": run_day.view(),
        "problems": problems,
    }
