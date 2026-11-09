"""Shared HTTP helper: fetch a URL and return parsed JSON, with safe errors.

Exception messages never contain header values (API keys, user agent) and show
only the URL path, never the query string.
"""
from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect so API-key headers are never forwarded to another URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None  # urllib then raises HTTPError for the 3xx response


def _default_open(req, timeout):  # noqa: ANN001
    """Default opener, built at call time: urllib's OpenerDirector without redirects."""
    return urllib.request.build_opener(_NoRedirect()).open(req, timeout=timeout)


_DEFAULT_PORTS = {"https": 443, "http": 80}


def _origin(url: str) -> tuple[str, str, int | None]:
    """(scheme, hostname, port) with the scheme's default port filled in."""
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme.lower()
    try:
        port = parts.port
    except ValueError:
        port = -1
    return scheme, (parts.hostname or "").lower(), port or _DEFAULT_PORTS.get(scheme)


def _path(url: str) -> str:
    return urllib.parse.urlsplit(url).path or "/"


def get_bytes(
    url: str,
    headers: dict,
    *,
    opener: Callable | None = None,
    timeout: float = 10,
    max_bytes: int | None = None,
) -> bytes:
    """GET `url` and return the raw body bytes.

    `opener` defaults to an OpenerDirector that refuses redirects, built at call time; it is
    called as opener(request, timeout=timeout) and must return a context manager
    whose value has .read().

    Raises RuntimeError on: HTTP error status, a refused redirect, a response from another
    origin, and a URL/network error or timeout. Messages name the URL path only (never the
    query string, which may hold a key).

    Proxies: the default opener comes from urllib.request.build_opener, which honours the
    HTTPS_PROXY / HTTP_PROXY / NO_PROXY environment variables, as urlopen did. A proxy set
    there sees every request: the full URL (so the query string, e.g. an FMP key) and the
    request headers (the Alpaca key headers, the SEC user agent). Only set them to a proxy
    you trust. An injected `opener` bypasses this. With `max_bytes`, at most max_bytes + 1 bytes are
    read, so a caller can refuse an oversized body without holding all of it in memory.

    A body shorter than the response's Content-Length is refused ("incomplete response"):
    http.client's read(n) returns a cut-short body silently when the connection drops, and a
    cut CSV can still parse (#167 review). A chunked reply that ends early (IncompleteRead) is
    refused the same way.
    """
    open_fn = opener or _default_open
    path = _path(url)
    req = urllib.request.Request(url, headers=dict(headers))
    try:
        with open_fn(req, timeout=timeout) as resp:
            final = resp.geturl() if hasattr(resp, "geturl") else None
            if isinstance(final, str) and _origin(final) != _origin(url):
                raise RuntimeError(f"response came from a different origin (requested {path})")
            raw = resp.read() if max_bytes is None else resp.read(max_bytes + 1)
            length = _content_length(resp)
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            raise RuntimeError(f"redirect refused (HTTP {exc.code}) from {path}") from None
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
    except http.client.HTTPException:  # IncompleteRead and friends: not an OSError
        raise RuntimeError(f"incomplete response from {path}") from None
    body = raw if isinstance(raw, (bytes, bytearray)) else str(raw).encode("utf-8")
    # A body shorter than announced was cut in transit. (A longer read can only happen with
    # max_bytes, where the caller refuses the oversized body itself.)
    if length is not None and len(body) < length and (max_bytes is None or len(body) <= max_bytes):
        raise RuntimeError(f"incomplete response from {path} ({len(body)} of {length} bytes)")
    return body


def _content_length(resp: object) -> int | None:
    """The response's Content-Length as an int, or None when absent or unreadable."""
    headers = getattr(resp, "headers", None)
    getter = getattr(headers, "get", None)
    value = getter("Content-Length") if callable(getter) else None
    if not isinstance(value, str) or not value.strip().isdigit():
        return None
    return int(value.strip())


def get_json(
    url: str,
    headers: dict,
    *,
    opener: Callable | None = None,
    timeout: float = 10,
    expect_keys: Iterable[str] | None = None,
) -> dict | list:
    """GET `url` and return the decoded JSON body (a dict or a list).

    Transport as get_bytes. Also raises RuntimeError on a body that is not valid JSON, and a
    JSON error payload, meaning a dict that has "message" or "error" and none of
    `expect_keys`.
    """
    path = _path(url)
    raw = get_bytes(url, headers, opener=opener, timeout=timeout)
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
