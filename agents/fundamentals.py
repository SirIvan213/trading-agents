"""Fundamentals Analyst: quarterly SEC facts for one symbol."""
from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from agents.fundamentals_tools import TOOLS, FundamentalsSession
from agents.llm import AgentConfig, config_for
from agents.runner import AnalystRun, AnalystSpec, output_schema, run_analyst
from agents.schemas import CallRecord

PROMPT = (Path(__file__).parent / "prompts" / "fundamentals.md").read_text(encoding="utf-8")
OUTPUT_SCHEMA: dict[str, Any] = output_schema("fundamentals")
SPEC = AnalystSpec(name="fundamentals", prompt=PROMPT, tools=TOOLS, output_schema=OUTPUT_SCHEMA)


def run_fundamentals(
    symbol: str,
    now: datetime,
    data: Any,
    *,
    client: Any,
    config: AgentConfig | None = None,
    calls_sink: list[CallRecord] | None = None,
    cancel: threading.Event | None = None,
) -> AnalystRun:
    """Analyse one symbol's SEC facts; `data` provides company_facts().

    Raises ValueError only for a naive `now` or an invalid symbol.
    """
    session = FundamentalsSession(symbol if isinstance(symbol, str) else "", now, data)
    cfg = config if config is not None else config_for("fundamentals")
    return run_analyst(
        SPEC, symbol, now, session, client=client, config=cfg, calls_sink=calls_sink,
        cancel=cancel,
    )
