"""Smoke run: the Technical Analyst once, on saved TSLA fixture data, against the REAL Claude API.

Not part of the test suite (it lives outside tests/ and is never imported by it). It makes real,
billed Claude API calls; it makes NO Alpaca calls - bars and the snapshot come from
tests/fixtures/alpaca_bars_tsla.json and alpaca_snapshot_tsla.json (synthetic data in Alpaca's
response shape for 2026-11-03: a few 08:55-08:59 ET pre-market bars, then 09:30-10:59 ET).

The API key is read from ANTHROPIC_API_KEY, loaded from .env in the repo root if present (a
non-empty exported variable wins). The key is never printed. Model and effort come from the
command-line flags only (TRADING_AGENTS_MODEL / _EFFORT are not used here).

Exit codes: 0 = the model produced a validated report; 1 = the run fell back to the neutral
report or made no successful API call; 2 = no API key or unreadable .env.

Usage:
    python -m pipeline.smoke_technical
    python -m pipeline.smoke_technical --model claude-sonnet-5-5
    python -m pipeline.smoke_technical --effort medium --no-fallbacks
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from agents.llm import EFFORTS, AgentConfig, make_client
from agents.technical import run_technical_analyst
from agents.tool_registry import ToolSession
from tools.bars import parse_bars

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
BARS_FIXTURE = FIXTURES / "alpaca_bars_tsla.json"
SNAPSHOT_FIXTURE = FIXTURES / "alpaca_snapshot_tsla.json"
SYMBOL = "TSLA"
NY = ZoneInfo("America/New_York")
NEUTRAL_SOURCE = "technical_analyst"  # evidence source used by the agent's neutral fallback
PREVIEW = 600


def _parse_value(raw: str) -> str:
    """Value part of KEY=VALUE: quoted values keep everything inside the quotes; unquoted
    values drop a trailing ' # comment'."""
    raw = raw.strip()
    if raw[:1] in ("'", '"'):
        end = raw.find(raw[0], 1)
        if end != -1:
            return raw[1:end]
    if raw.startswith("#"):
        return ""
    return re.split(r"\s#", raw, maxsplit=1)[0].strip()


def load_dotenv(path: Path) -> None:
    """Minimal .env reader. Handles a UTF-8 BOM, `export `, quotes and inline comments.

    Never overrides a non-empty exported variable. Raises ValueError for an unreadable file
    (e.g. UTF-16, which PowerShell's `>` writes). Never includes values in messages.
    """
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path.name} is not UTF-8 (save it as UTF-8)") from exc
    except OSError as exc:
        raise ValueError(f"cannot read {path.name} ({type(exc).__name__})") from exc
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.removeprefix("export ").strip()
        if key and not os.environ.get(key, "").strip():
            os.environ[key] = _parse_value(value)


class FixtureMarketData:
    """MarketData backed by the saved TSLA fixtures. Makes no network calls."""

    def __init__(self) -> None:
        self._bars = json.loads(BARS_FIXTURE.read_text(encoding="utf-8"))
        self._snapshots = json.loads(SNAPSHOT_FIXTURE.read_text(encoding="utf-8"))

    def bars(self, symbol: str, start: datetime, end: datetime) -> dict:
        if symbol.upper() != SYMBOL:
            return {"bars": [], "symbol": symbol.upper(), "next_page_token": None}
        return self._bars

    def snapshots(self, symbols: list[str]) -> dict:
        return {s.upper(): self._snapshots.get(s.upper()) for s in symbols}

    def as_of(self) -> datetime:
        """One minute after the last fixture bar, in New York time."""
        last = max(b.ts for b in parse_bars(self._bars))
        return (last + timedelta(minutes=1)).astimezone(NY)


@dataclass
class Recorder:
    """Records every API response and every tool call the agent executes."""

    responses: list[Any] = field(default_factory=list)
    tool_calls: list[tuple[str, Any, dict, bool]] = field(default_factory=list)


class _RecordingMessages:
    def __init__(self, inner: Any, rec: Recorder) -> None:
        self._inner, self._rec = inner, rec

    def create(self, **kwargs: Any) -> Any:
        response = self._inner.create(**kwargs)
        self._rec.responses.append(response)
        return response


class RecordingClient:
    """Wraps anthropic.Anthropic so the agent's `client.beta.messages.create` calls are recorded."""

    def __init__(self, inner: Any, rec: Recorder) -> None:
        self.beta = type("Beta", (), {})()
        self.beta.messages = _RecordingMessages(inner.beta.messages, rec)


def _iterations(response: Any) -> list[Any]:
    return list(getattr(getattr(response, "usage", None), "iterations", None) or [])


def serving_model(response: Any) -> str:
    """The model that produced the final output of this response.

    On a fallback turn that is the `model` of the last `fallback_message` usage iteration (the
    SDK's documented served-by signal); otherwise `response.model`.
    """
    for it in reversed(_iterations(response)):
        if getattr(it, "type", "") == "fallback_message" and getattr(it, "model", None):
            return str(it.model)
    return str(getattr(response, "model", "?"))


def fallback_notes(response: Any) -> list[str]:
    """Human-readable fallback hops for one response; empty if no fallback ran."""
    notes = []
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", "") == "fallback":
            src = getattr(getattr(block, "from_", None), "model", "?")
            dst = getattr(getattr(block, "to", None), "model", "?")
            notes.append(f"{src} declined -> {dst} continued")
    iters = _iterations(response)
    if not notes and any(getattr(it, "type", "") == "fallback_message" for it in iters):
        notes.append(f"served by fallback model {serving_model(response)}")
    return notes


TOKEN_KEYS = ("input_tokens", "output_tokens", "cache_creation_input_tokens",
              "cache_read_input_tokens")


def token_totals(responses: list[Any]) -> dict[str, int]:
    """Sum of the top-level usage fields across all responses."""
    totals = dict.fromkeys(TOKEN_KEYS, 0)
    for r in responses:
        usage = getattr(r, "usage", None)
        for k in TOKEN_KEYS:
            totals[k] += int(getattr(usage, k, 0) or 0)
    return totals


def _is_neutral_fallback(report: Any) -> bool:
    return report.confidence == 0.0 and any(e.source == NEUTRAL_SOURCE for e in report.evidence)


def main(argv: list[str] | None = None, *, client: Any = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the Technical Analyst once on saved TSLA fixtures against the real "
                    "Claude API (billed; no Alpaca calls)."
    )
    parser.add_argument("--model", default="claude-opus-5-5",
                        help="Claude model ID (default: claude-opus-5-5; e.g. claude-sonnet-5-5)")
    parser.add_argument("--effort", default="high", choices=sorted(EFFORTS))
    parser.add_argument("--no-fallbacks", action="store_true",
                        help="turn off server-side refusal fallback")
    args = parser.parse_args(argv)

    if client is None:
        try:
            load_dotenv(ROOT / ".env")
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        try:
            client = make_client()
        except RuntimeError as exc:
            print(f"error: {exc} (add ANTHROPIC_API_KEY=... to .env at the repo root, "
                  "or export it)", file=sys.stderr)
            return 2

    rec = Recorder()
    data = FixtureMarketData()
    now = data.as_of()
    config = AgentConfig(model=args.model, effort=args.effort, fallbacks=not args.no_fallbacks)

    original_execute = ToolSession.execute

    def recording_execute(self: ToolSession, name: str, tool_input: Any) -> tuple[dict, bool]:
        result, is_error = original_execute(self, name, tool_input)
        rec.tool_calls.append((name, tool_input, result, is_error))
        return result, is_error

    ToolSession.execute = recording_execute  # type: ignore[method-assign]
    try:
        report = run_technical_analyst(SYMBOL, now, data, client=RecordingClient(client, rec),
                                       config=config)
    finally:
        ToolSession.execute = original_execute  # type: ignore[method-assign]

    print(f"== Technical Analyst smoke run: {SYMBOL} as of {now.isoformat()} ==")
    print(f"requested model: {config.model}  effort: {config.effort}  "
          f"fallbacks: {'on' if config.fallbacks else 'off'}")

    print("\n-- AnalystReport --")
    print(report.model_dump_json(indent=2))

    print(f"\n-- Tool calls ({len(rec.tool_calls)}) --")
    for i, (name, tool_input, result, is_error) in enumerate(rec.tool_calls, 1):
        status = "ERROR" if is_error else "ok"
        body = json.dumps(result)
        print(f"{i}. {name}({json.dumps(tool_input)}) -> {status}")
        print("   " + (body if len(body) <= PREVIEW else body[:PREVIEW] + " ...(truncated)"))

    print(f"\n-- API calls ({len(rec.responses)}) --")
    for i, r in enumerate(rec.responses, 1):
        fb = fallback_notes(r)
        line = f"{i}. served by {serving_model(r)}  stop_reason={getattr(r, 'stop_reason', '?')}"
        print(line + (f"  fallback: {'; '.join(fb)}" if fb else ""))
    served = sorted({serving_model(r) for r in rec.responses})
    print(f"serving model(s): {', '.join(served) or 'none (no API response)'}")
    print(f"fallback ran: {'yes' if any(fallback_notes(r) for r in rec.responses) else 'no'}")

    t = token_totals(rec.responses)
    print("\n-- Tokens (sum over all API calls) --")
    print(f"input {t['input_tokens']}  output {t['output_tokens']}  "
          f"cache write {t['cache_creation_input_tokens']}  "
          f"cache read {t['cache_read_input_tokens']}  total processed {sum(t.values())}")

    if not rec.responses or _is_neutral_fallback(report):
        reason = report.evidence[0].point if report.evidence else "no API response"
        print(f"\nsmoke run FAILED: {reason}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
