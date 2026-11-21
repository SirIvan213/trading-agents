"""Metering for run_day's billed calls (#157, #156): every Claude, FMP and Alpha Vantage
request goes through the day's DailyBudget (pipeline/budget.py) BEFORE it is made.

Claude (`MeteredClient`): wraps an Anthropic client built with the SDK's own retries OFF
(max_retries=0), so no request can happen out of the meter's sight. For each call:
  1. reserve the call's worst case on disk (`worst_case_tokens`): its max_tokens plus a
     deliberately high input estimate (the whole serialized request, non-ASCII escaped, / 2),
     times FALLBACK_FACTOR when a server-side fallback may re-run it. A reservation that
     would pass the day's cap is refused (CapReached): the call is not made;
  2. make the call;
  3. settle the reservation to the call's actual tokens, CallRecord.billed_usage() (which
     sums usage.iterations, #58: a declined fallback hop counts).
On a failure:
  - the server answered with an error status (APIStatusError): nothing was billed, the
    reservation settles to 0;
  - no answer (a connection error or a timeout): the outcome is unknown, so the reservation
    is never settled and keeps counting (fail closed);
  - a retryable failure (no answer, or HTTP 408, 409, 429 or 5xx) is retried ONCE, here,
    through the meter: the retry reserves its own worst case (Ivan's decision on #156).
So the day's tokens used plus reserved never pass the cap.

FMP and Alpha Vantage (`metered_fetch`): count the request on disk, then make it. At the cap
the fetch raises CapReached instead of requesting: the calendar caches turn that into a
failed fetch (Alpha Vantage: the gate says UNKNOWN and blocks entries; FMP: block-only, its
input is unavailable and nothing is cleared).
"""
from __future__ import annotations

import json
import math
import time
from collections.abc import Callable
from typing import Any

from agents.runner import call_record
from pipeline.budget import CapReached, DailyBudget

RETRY_STATUSES = frozenset({408, 409, 429}) | frozenset(range(500, 600))
RETRY_WAIT_S = 2.0
CHARS_PER_TOKEN_BOUND = 2  # a token is rarely under ~3 characters: /2 over-estimates input
# A declined request runs "the next model" in a chain (Claude API docs, refusals and
# fallback). Today's supported fallback targets are claude-opus-4-8 and claude-opus-5, so one
# call can run up to 3 attempts (each billed up to its output). Raise this if the docs add
# more targets.
FALLBACK_FACTOR = 3


def worst_case_tokens(kwargs: dict[str, Any]) -> int:
    """An upper bound for one call's tokens (input, output, cache reads and writes): the
    serialized request / CHARS_PER_TOKEN_BOUND plus max_tokens, times FALLBACK_FACTOR when
    the request allows a server-side fallback. Every key is serialized (only the model name,
    the beta list and max_tokens are left out), so a new field can only raise the estimate.
    Non-ASCII text is escaped (an emoji becomes 6 characters, about 3 tokens): it never
    counts for less than it costs."""
    request = {k: v for k, v in kwargs.items() if k not in ("model", "betas", "max_tokens")}
    chars = len(json.dumps(request, default=str, ensure_ascii=True, sort_keys=True))
    out = kwargs.get("max_tokens")
    out = out if isinstance(out, int) and not isinstance(out, bool) and out > 0 else 128_000
    total = math.ceil(chars / CHARS_PER_TOKEN_BOUND) + out
    return total * (FALLBACK_FACTOR if kwargs.get("fallbacks") else 1)


def _billed(response: Any, model: str) -> int:
    u = call_record(response, model).billed_usage()
    return (u.input_tokens + u.output_tokens + u.cache_creation_input_tokens
            + u.cache_read_input_tokens)


def _status(exc: BaseException) -> int | None:
    code = getattr(exc, "status_code", None)
    return code if isinstance(code, int) else None


def _no_answer(exc: BaseException) -> bool:
    try:
        import anthropic
    except ImportError:  # pragma: no cover - the SDK is a dependency
        return False
    return isinstance(exc, anthropic.APIConnectionError)


class _Messages:
    def __init__(self, owner: MeteredClient, inner_messages: Any) -> None:
        self._owner = owner
        self._inner = inner_messages

    def create(self, **kwargs: Any) -> Any:
        return self._owner._metered_create(self._inner, kwargs)


class _Beta:
    def __init__(self, owner: MeteredClient, inner_beta: Any) -> None:
        self.messages = _Messages(owner, inner_beta.messages)


class MeteredClient:
    """An Anthropic client whose `beta.messages.create` goes through the day's budget. Only
    the surface the agents use is exposed: `beta.messages.create` and `with_options`."""

    def __init__(self, inner: Any, budget: DailyBudget, *,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._inner = inner
        self._budget = budget
        self._sleep = sleep
        self.beta = _Beta(self, inner.beta)

    def with_options(self, **options: Any) -> MeteredClient:
        """The inner client's with_options (e.g. a timeout), still metered. max_retries is
        always forced to 0: a retry inside the SDK would bypass the meter."""
        inner = self._inner.with_options(**{**options, "max_retries": 0}) \
            if hasattr(self._inner, "with_options") else self._inner
        return MeteredClient(inner, self._budget, sleep=self._sleep)

    def _once(self, messages: Any, kwargs: dict[str, Any]) -> Any:
        rid = self._budget.reserve_tokens(worst_case_tokens(kwargs))  # CapReached: no call
        try:
            response = messages.create(**kwargs)
        except Exception as exc:
            if _status(exc) is not None:
                self._budget.settle_tokens(rid, 0)  # the server answered: nothing billed
            raise  # no answer: the reservation stays counted (the outcome is unknown)
        if getattr(response, "usage", None) is None:
            return response  # no usage to read: the worst case stays counted (fail closed)
        try:
            actual = _billed(response, str(kwargs.get("model", "")))
        except Exception:  # noqa: BLE001 - an odd usage block: keep the worst case counted
            return response
        self._budget.settle_tokens(rid, actual)
        return response

    def _metered_create(self, messages: Any, kwargs: dict[str, Any]) -> Any:
        try:
            return self._once(messages, kwargs)
        except CapReached:
            raise
        except Exception as exc:
            status = _status(exc)
            if not (_no_answer(exc) or (status is not None and status in RETRY_STATUSES)):
                raise
        self._sleep(RETRY_WAIT_S)
        return self._once(messages, kwargs)  # the one retry, with its own reservation


def metered_fetch(budget: DailyBudget, source: str, fetch: Callable[..., Any]) -> Callable:
    """`fetch`, with every call counted in the day's budget for `source` ("fmp" or "av")
    BEFORE the request. At the cap it raises CapReached and makes no request."""
    def counted(*args: Any, **kwargs: Any) -> Any:
        budget.count_request(source)
        return fetch(*args, **kwargs)
    return counted


def metered_live_data(budget: DailyBudget, cache_dir: Any = None) -> Any:
    """pipeline.data.LiveData whose FMP and Alpha Vantage calendar caches count every request
    in `budget` first. The caches keep a valid calendar on disk for the day (reused, never
    fetched again). Without `cache_dir`, the usual journal/cache/."""
    from pipeline.data import LiveData
    from pipeline.events import CACHE_DIR, AvCalendarCache, FmpCalendarCache
    from tools.earnings_calendar import (
        fetch_av_earnings_calendar_typed,
        fetch_fmp_earnings_calendar,
    )
    where = cache_dir if cache_dir is not None else CACHE_DIR
    return LiveData(
        calendar_cache=FmpCalendarCache(
            where, fetch=metered_fetch(budget, "fmp", fetch_fmp_earnings_calendar)),
        av_cache=AvCalendarCache(
            where, fetch=metered_fetch(budget, "av", fetch_av_earnings_calendar_typed)),
    )

