"""Shared HTTP helper: fetch a URL and return parsed JSON, with safe errors.

Exception messages never contain header values (API keys, user agent) and show
only the URL path, never the query string.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable


def _path(url: str) -> str:
    return urllib.parse.urlsplit(url).path or "/"


def get_json(
    url: str,
    headers: dict,
    *,
    opener: Callable | None = None,
    timeout: float = 10,
    expect_keys: Iterable[str] | None = None,
) -> dict | list:
    """GET `url` and return the decoded JSON body (a dict or a list).

    `opener` defaults to urllib.request.urlopen, resolved at call time; it is
    called as opener(request, timeout=timeout) and must return a context manager
    whose value has .read().

    Raises RuntimeError on: HTTP error status, URL/network error or timeout, a
    body that is not valid JSON, and a JSON error payload, meaning a dict that
    has "message" or "error" and none of `expect_keys`.
    """
    open_fn = opener or urllib.request.urlopen
    path = _path(url)
    req = urllib.request.Request(url, headers=dict(headers))
    try:
        with open_fn(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} from {path}") from None
    except TimeoutError:
        raise RuntimeError(f"request to {path} timed out") from None
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, TimeoutError):
            raise RuntimeError(f"request to {path} timed out") from None
        raise RuntimeError(f"network error for {path}: {type(reason).__name__}") from None
    except OSError as exc:
        raise RuntimeError(f"network error for {path}: {type(exc).__name__}") from None
    try:
        text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
        body = json.loads(text)
    except ValueError:  # includes JSONDecodeError and UnicodeDecodeError
        raise RuntimeError(f"non-JSON response from {path}") from None
    if isinstance(body, dict) and ("message" in body or "error" in body):
        expected = set(expect_keys or ())
        if not expected.intersection(body):
            detail = str(body.get("message", body.get("error")))[:200]
            raise RuntimeError(f"error payload from {path}: {detail}")
    return body
