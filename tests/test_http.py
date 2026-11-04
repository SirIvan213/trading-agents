"""Tests for tools.http.get_json. Fake openers only; headers carry a sentinel secret."""
import email.message
import io
import json
import urllib.error
import urllib.request
import urllib.response

import pytest

from tools.http import _NoRedirect, get_json

ORIG_OPEN = urllib.request.OpenerDirector.open  # captured before conftest patches it

SENTINEL = "SENTINEL-KEY-123"
HEADERS = {"APCA-API-KEY-ID": SENTINEL, "User-Agent": SENTINEL}
URL = "https://data.example.test/v2/things?apikey=" + SENTINEL + "&x=1"


class Resp:
    def __init__(self, body):
        self._b = body if isinstance(body, bytes) else json.dumps(body).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def opener_returning(body):
    seen = []

    def op(req, timeout=None):
        seen.append((req, timeout))
        return Resp(body)

    op.seen = seen
    return op


def opener_raising(exc):
    def op(req, timeout=None):
        raise exc

    return op


def call(op, **kw):
    return get_json(URL, HEADERS, opener=op, **kw)


def assert_safe(exc_info):
    msg = str(exc_info.value)
    assert SENTINEL not in msg
    assert "apikey" not in msg and "x=1" not in msg  # query string never shown
    assert "/v2/things" in msg  # path is
    assert exc_info.value.__cause__ is None  # original exception (may hold URL) not chained


def test_success_dict_and_request_details():
    op = opener_returning({"a": 1})
    assert call(op, timeout=7) == {"a": 1}
    req, timeout = op.seen[0]
    assert timeout == 7
    assert req.full_url == URL
    assert {k.lower(): v for k, v in req.header_items()}["apca-api-key-id"] == SENTINEL


def test_success_list_body():
    assert call(opener_returning([1, 2])) == [1, 2]


def test_http_error():
    err = urllib.error.HTTPError(URL, 403, "Forbidden", {}, io.BytesIO(SENTINEL.encode()))
    with pytest.raises(RuntimeError, match="HTTP 403") as ei:
        call(opener_raising(err))
    assert_safe(ei)


def test_url_error():
    with pytest.raises(RuntimeError, match="network error") as ei:
        call(opener_raising(urllib.error.URLError(ConnectionRefusedError(SENTINEL))))
    assert_safe(ei)
    assert "ConnectionRefusedError" in str(ei.value)


def test_timeout_error():
    with pytest.raises(RuntimeError, match="timed out") as ei:
        call(opener_raising(TimeoutError(SENTINEL)))
    assert_safe(ei)


def test_url_error_wrapping_timeout():
    with pytest.raises(RuntimeError, match="timed out") as ei:
        call(opener_raising(urllib.error.URLError(TimeoutError())))
    assert_safe(ei)


def test_plain_oserror():
    with pytest.raises(RuntimeError, match="network error") as ei:
        call(opener_raising(ConnectionResetError(SENTINEL)))
    assert_safe(ei)


@pytest.mark.parametrize("body", [b"<html>oops</html>", b"", b"\xff\xfe\x00bad"])
def test_non_json_body(body):
    with pytest.raises(RuntimeError, match="non-JSON") as ei:
        call(opener_returning(body))
    assert_safe(ei)


def test_error_payload_message_and_error_keys():
    with pytest.raises(RuntimeError, match="error payload") as ei:
        call(opener_returning({"message": "forbidden"}))
    assert "forbidden" in str(ei.value)
    with pytest.raises(RuntimeError, match="error payload"):
        call(opener_returning({"error": "bad"}))


def test_error_payload_message_truncated():
    with pytest.raises(RuntimeError) as ei:
        call(opener_returning({"message": "x" * 1000}))
    assert len(str(ei.value)) < 400


def test_expect_keys_allows_dict_with_message():
    body = {"message": "note", "news": []}
    assert call(opener_returning(body), expect_keys=["news"]) == body


def test_expect_keys_missing_still_errors():
    with pytest.raises(RuntimeError, match="error payload"):
        call(opener_returning({"message": "nope"}), expect_keys=["news"])


def test_dict_without_error_keys_passes_without_expect_keys():
    assert call(opener_returning({"snapshots": {}})) == {"snapshots": {}}


# ---------- network guard (conftest) ----------

def test_default_opener_hits_network_guard():
    # The default path builds an OpenerDirector (no redirects); conftest patches
    # OpenerDirector.open, so this must raise our guard error, never reach DNS.
    with pytest.raises(RuntimeError, match="network access in tests"):
        get_json(URL, HEADERS)


def test_getaddrinfo_and_create_connection_are_blocked():
    import socket

    with pytest.raises(RuntimeError, match="network access in tests"):
        socket.getaddrinfo("example.invalid", 443)
    with pytest.raises(RuntimeError, match="network access in tests"):
        socket.create_connection(("example.invalid", 443))


def test_opener_director_open_is_blocked():
    d = urllib.request.build_opener()
    with pytest.raises(RuntimeError, match="network access in tests"):
        d.open("https://example.invalid/")


def test_default_open_builds_a_no_redirect_opener(monkeypatch):
    built = []

    class FakeDirector:
        def open(self, req, timeout=None):
            return Resp({"ok": True})

    def fake_build_opener(*handlers):
        built.extend(handlers)
        return FakeDirector()

    monkeypatch.setattr(urllib.request, "build_opener", fake_build_opener)
    assert get_json(URL, HEADERS) == {"ok": True}
    assert len(built) == 1 and isinstance(built[0], urllib.request.HTTPRedirectHandler)
    assert type(built[0]).__name__ == "_NoRedirect"


# ---------- redirects (issue #6) ----------

class RespUrl(Resp):
    def __init__(self, body, url):
        super().__init__(body)
        self._url = url

    def geturl(self):
        return self._url


def opener_with_final_url(final_url):
    return lambda req, timeout=None: RespUrl({"a": 1}, final_url)


def test_response_from_same_host_passes():
    op = opener_with_final_url("https://data.example.test/v2/other")
    assert call(op) == {"a": 1}
    # host comparison is case-insensitive
    assert call(opener_with_final_url("https://DATA.example.test/x")) == {"a": 1}


def test_response_from_another_host_refused_without_secrets():
    op = opener_with_final_url("https://evil.example.test/v2/things?apikey=" + SENTINEL)
    with pytest.raises(RuntimeError, match="different origin") as ei:
        call(op)
    msg = str(ei.value)
    assert SENTINEL not in msg and "apikey" not in msg and "evil" not in msg
    assert "/v2/things" in msg


def test_response_without_geturl_is_accepted():
    assert call(opener_returning({"a": 1})) == {"a": 1}


class FakeHTTPS(urllib.request.BaseHandler):
    """Serves https:// requests in memory: 302 to another host, or a 200."""

    handler_order = 100

    def __init__(self, location=None):
        self.location = location
        self.seen = []

    def https_open(self, req):
        self.seen.append((req.full_url, dict(req.header_items())))
        hdrs = email.message.Message()
        if self.location:
            hdrs["Location"] = self.location
            resp = urllib.response.addinfourl(io.BytesIO(b""), hdrs, req.full_url, 302)
            resp.msg = "Found"
        else:
            body = io.BytesIO(json.dumps({"ok": True}).encode())
            resp = urllib.response.addinfourl(body, hdrs, req.full_url, 200)
            resp.msg = "OK"
        return resp


def real_chain(handler, monkeypatch):
    # Redirect follow-ups call director.open again, so restore the real method here
    # (the fake handler is in-memory, so there is still no network).
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", ORIG_OPEN)
    d = urllib.request.OpenerDirector()
    for h in (_NoRedirect(), urllib.request.HTTPDefaultErrorHandler(),
              urllib.request.HTTPErrorProcessor(), handler):
        d.add_handler(h)
    return lambda req, timeout=None: ORIG_OPEN(d, req, timeout=timeout)


def test_real_handler_chain_refuses_redirect_to_other_host(monkeypatch):
    fake = FakeHTTPS(location="https://evil.example.test/steal?apikey=" + SENTINEL)
    with pytest.raises(RuntimeError, match="redirect refused") as ei:
        call(real_chain(fake, monkeypatch))
    msg = str(ei.value)
    assert "302" in msg and "/v2/things" in msg
    assert SENTINEL not in msg and "apikey" not in msg and "evil" not in msg and "x=1" not in msg
    assert len(fake.seen) == 1  # the redirect target was never requested
    assert fake.seen[0][0].startswith("https://data.example.test/")


def test_real_handler_chain_refuses_redirect_to_same_host_too(monkeypatch):
    fake = FakeHTTPS(location="https://data.example.test/v2/elsewhere")
    with pytest.raises(RuntimeError, match="redirect refused"):
        call(real_chain(fake, monkeypatch))
    assert len(fake.seen) == 1


def test_real_handler_chain_passes_200(monkeypatch):
    fake = FakeHTTPS()
    assert call(real_chain(fake, monkeypatch)) == {"ok": True}
    sent = {k.lower(): v for k, v in fake.seen[0][1].items()}
    assert sent["apca-api-key-id"] == SENTINEL  # the key goes to the requested host only


def test_stock_redirect_handler_would_have_followed(monkeypatch):
    # Control: the stock handler (not ours) does attempt a second request, which
    # is exactly what _NoRedirect prevents.
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", ORIG_OPEN)
    fake = FakeHTTPS(location="https://evil.example.test/steal")
    d = urllib.request.OpenerDirector()
    for h in (urllib.request.HTTPRedirectHandler(), urllib.request.HTTPDefaultErrorHandler(),
              urllib.request.HTTPErrorProcessor(), fake):
        d.add_handler(h)
    try:
        ORIG_OPEN(d, urllib.request.Request(URL, headers=HEADERS), timeout=1)
    except Exception:  # noqa: BLE001 - the fake serves another 302; only the count matters
        pass
    assert len(fake.seen) >= 2
    assert fake.seen[1][0].startswith("https://evil.example.test/")


# ---------- origin check: scheme, host and port (issue #6) ----------

A_URL = "https://a.com/v2/things?x=1"


def fetch_a(final, url=A_URL):
    return get_json(url, HEADERS, opener=opener_with_final_url(final))


@pytest.mark.parametrize(
    "final",
    ["https://a.com/other", "https://a.com:443/other", "https://A.COM/other", "https://a.com/"],
)
def test_same_origin_passes(final):
    assert fetch_a(final) == {"a": 1}


def test_http_default_port_equals_explicit_80():
    url = "http://a.com/v2/things"
    assert fetch_a("http://a.com:80/x", url) == {"a": 1}


@pytest.mark.parametrize(
    "final",
    [
        "http://a.com/v2/things",  # https -> http downgrade, same host
        "https://a.com:8443/v2/things",  # different port
        "http://a.com:443/v2/things",  # same port number, different scheme
        "https://b.com/v2/things",
        "https://a.com.evil.test/v2/things",
        "https://a.com:notaport/v2/things",  # unparsable port never matches
    ],
)
def test_different_origin_refused(final):
    with pytest.raises(RuntimeError, match="different origin") as ei:
        fetch_a(final)
    msg = str(ei.value)
    assert SENTINEL not in msg and "x=1" not in msg and "/v2/things" in msg
