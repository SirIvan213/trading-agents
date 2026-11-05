"""Sentiment Analyst: news, 8-K filings and red flags for one symbol."""
from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from agents.llm import AgentConfig, config_for
from agents.runner import AnalystRun, AnalystSpec, output_schema, run_analyst
from agents.schemas import CallRecord
from agents.sentiment_tools import TOOLS, SentimentSession

PROMPT = (Path(__file__).parent / "prompts" / "sentiment.md").read_text(encoding="utf-8")
OUTPUT_SCHEMA: dict[str, Any] = output_schema("sentiment")
SPEC = AnalystSpec(name="sentiment", prompt=PROMPT, tools=TOOLS, output_schema=OUTPUT_SCHEMA)


def run_sentiment(
    symbol: str,
    now: datetime,
    data: Any,
    *,
    client: Any,
    config: AgentConfig | None = None,
    calls_sink: list[CallRecord] | None = None,
    cancel: threading.Event | None = None,
) -> AnalystRun:
    """Analyse news and filings for one symbol; `data` provides news() and submissions().

    Raises ValueError only for a naive `now` or an invalid symbol.
    """
    session = SentimentSession(symbol if isinstance(symbol, str) else "", now, data)
    cfg = config if config is not None else config_for("sentiment")
    return run_analyst(
        SPEC, symbol, now, session, client=client, config=cfg, calls_sink=calls_sink,
        cancel=cancel,
    )
