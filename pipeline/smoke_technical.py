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

from agents.llm import EFFORTS, AgentConfig, config_for, make_client
from agents.schemas import CallRecord
from agents.technical import run_technical
from agents.tool_registry import ToolSession
from tools.bars import parse_bars

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
BARS_FIXTURE = FIXTURES / "alpaca_bars_tsla.json"
SNAPSHOT_FIXTURE = FIXTURES / "alpaca_snapshot_tsla.json"
SYMBOL = "TSLA"
NY = ZoneInfo("America/New_York")
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


def fallback_notes(response: Any) -> list[str]:
    """Human-readable fallback hops from the `fallback` content blocks of one response."""
    notes = []
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", "") == "fallback":
            src = getattr(getattr(block, "from_", None), "model", "?")
            dst = getattr(getattr(block, "to", None), "model", "?")
            notes.append(f"{src} declined -> {dst} continued")
    return notes


TOKEN_KEYS = ("input_tokens", "output_tokens", "cache_creation_input_tokens",
              "cache_read_input_tokens")


def token_totals(calls: list[CallRecord]) -> dict[str, int]:
    """Sum of the top-level usage fields across all call records."""
    totals = dict.fromkeys(TOKEN_KEYS, 0)
    for c in calls:
        for k in TOKEN_KEYS:
            totals[k] += getattr(c.usage, k)
    return totals


def main(argv: list[str] | None = None, *, client: Any = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the Technical Analyst once on saved TSLA fixtures against the real "
                    "Claude API (billed; no Alpaca calls)."
    )
    if client is None:  # .env first: it may hold TRADING_AGENTS_* defaults
        try:
            load_dotenv(ROOT / ".env")
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    try:
        defaults = config_for("technical")
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    parser.add_argument("--model", default=defaults.model,
                        help=f"Claude model ID (default: {defaults.model})")
    parser.add_argument("--effort", default=defaults.effort, choices=sorted(EFFORTS))
    parser.add_argument("--no-fallbacks", action="store_true",
                        help="turn off server-side refusal fallback")
    args = parser.parse_args(argv)

    if client is None:
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
        run = run_technical(SYMBOL, now, data, client=RecordingClient(client, rec),
                            config=config)
    finally:
        ToolSession.execute = original_execute  # type: ignore[method-assign]
    report = run.report

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

    print(f"\n-- API calls ({len(run.calls)}) --")
    for i, c in enumerate(run.calls, 1):
        fb = fallback_notes(rec.responses[i - 1]) if i <= len(rec.responses) else []
        line = f"{i}. served by {c.serving_model}  stop_reason={c.stop_reason}"
        if c.refusal_category:
            line += f"  refusal_category={c.refusal_category}"
        print(line + (f"  fallback: {'; '.join(fb)}" if fb else ""))
        for it in c.iterations:
            print(f"   iteration {it.type} model={it.model} "
                  f"in={it.usage.input_tokens} out={it.usage.output_tokens}")
    served = sorted({c.serving_model for c in run.calls})
    print(f"serving model(s): {', '.join(served) or 'none (no API response)'}")
    print(f"fallback ran: {'yes' if any(c.fallback_ran for c in run.calls) else 'no'}")

    t = token_totals(run.calls)
    print("\n-- Tokens (sum over all API calls) --")
    print(f"input {t['input_tokens']}  output {t['output_tokens']}  "
          f"cache write {t['cache_creation_input_tokens']}  "
          f"cache read {t['cache_read_input_tokens']}  total processed {sum(t.values())}")

    if not run.calls or not run.ok:
        print(f"\nsmoke run FAILED: {run.reason or 'no API response'}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
