"""Tests for tools.http.get_json. Fake openers only; headers carry a sentinel secret."""
import io
import json
import urllib.error

import pytest

from tools.http import get_json

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


def test_default_opener_is_guarded_urlopen():
    # conftest patches urllib.request.urlopen; get_json resolves it at call time.
    with pytest.raises(RuntimeError, match="network access in tests"):
        get_json(URL, HEADERS)
