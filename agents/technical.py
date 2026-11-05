"""Technical Analyst: a spec for the generic runner plus two thin wrappers.

The model may only call the tools in agents/tool_registry.py. Any failure ends in a neutral
zero-confidence report; the agent never raises except for a naive `now` or a bad symbol.
"""
from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from agents.llm import AgentConfig, config_for
from agents.runner import AnalystRun, AnalystSpec, output_schema, run_analyst
from agents.schemas import AnalystReport, CallRecord
from agents.tool_registry import TOOLS, ToolSession
from pipeline.data import MarketData
from pipeline.market import ny_today
from tools.config import Limits, load_limits
from tools.scan import ScanCriteria, validate_symbols

PROMPT = (Path(__file__).parent / "prompts" / "technical.md").read_text(encoding="utf-8")
OUTPUT_SCHEMA: dict[str, Any] = output_schema("technical")
SPEC = AnalystSpec(name="technical", prompt=PROMPT, tools=TOOLS, output_schema=OUTPUT_SCHEMA)


def run_technical(
    symbol: str,
    now: datetime,
    data: MarketData,
    *,
    client: Any,
    config: AgentConfig | None = None,
    limits: Limits | None = None,
    criteria: ScanCriteria | None = None,
    calls_sink: list[CallRecord] | None = None,
    cancel: threading.Event | None = None,
) -> AnalystRun:
    """Analyse one symbol and return the full AnalystRun (report, ok flag, call records).

    Raises ValueError for a naive `now`, an invalid symbol, or an unreadable/invalid limits
    config ("limits config is invalid"). The last is the one deliberate loud failure: a
    broken risk config must not be silently ignored.
    """
    ny_today(now)  # ValueError for naive now, before any config is read
    try:
        validate_symbols([symbol])
    except (ValueError, AttributeError):
        raise ValueError("symbol must be a valid ticker") from None
    if limits is None:
        try:
            limits = load_limits()
        except Exception:  # noqa: BLE001 - no file contents in the message
            raise ValueError("limits config is invalid") from None
    session = ToolSession(
        symbol=symbol.strip().upper(),
        now=now,
        data=data,
        limits=limits,
        criteria=criteria or ScanCriteria(),
    )
    cfg = config if config is not None else config_for("technical")
    return run_analyst(
        SPEC, symbol, now, session, client=client, config=cfg, calls_sink=calls_sink,
        cancel=cancel,
    )


def run_technical_analyst(
    symbol: str,
    now: datetime,
    data: MarketData,
    *,
    client: Any,
    config: AgentConfig | None = None,
    limits: Limits | None = None,
    criteria: ScanCriteria | None = None,
) -> AnalystReport:
    """Analyse one symbol and return an AnalystReport (neutral on any failure).

    Raises ValueError only for a naive `now` or an invalid symbol.
    """
    return run_technical(
        symbol, now, data, client=client, config=config, limits=limits, criteria=criteria
    ).report
