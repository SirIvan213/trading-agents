"""Global test guard: no test may reach the network."""
import socket
import urllib.request

import pytest


def _blocked(*args, **kwargs):
    raise RuntimeError("network access in tests")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    # fetch_bars resolves urllib.request.urlopen at call time, so patching the
    # module attribute is enough; socket.connect is the backstop.
    monkeypatch.setattr(urllib.request, "urlopen", _blocked)
    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)
