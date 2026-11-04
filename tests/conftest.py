"""Global test guard: no test may reach the network.

Every layer a real request could go through is patched to raise, so that code
which builds its own opener (http.get_json) or opens sockets directly is caught
too, not just code that calls urllib.request.urlopen.
"""
import socket
import urllib.request

import pytest


def _blocked(*args, **kwargs):
    raise RuntimeError("network access in tests")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", _blocked)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", _blocked)
    monkeypatch.setattr(socket, "getaddrinfo", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)
