"""UI tests: the repo's network guard (tests/conftest.py) applies here too, and every test
gets journal fixtures written by the journal's own code into a temporary folder."""
from __future__ import annotations

import _socket
import socket

import pytest

from journal.log import Journal
from tests.conftest import _no_network  # noqa: F401 - autouse: no test reaches the network
from ui.api.paths import Paths
from ui.tests.journal_fixtures import write_day


@pytest.fixture(autouse=True)
def loopback_only(_no_network, monkeypatch):  # noqa: F811 - runs after the guard
    """asyncio (FastAPI's TestClient) needs a loopback socketpair on Windows: allow
    127.0.0.1/::1 and nothing else, like tests/test_orchestrator.py. The global guard stays in
    force for every other address."""
    guard_connect, guard_connect_ex = socket.socket.connect, socket.socket.connect_ex

    def is_loopback(address):
        return isinstance(address, tuple) and address and address[0] in ("127.0.0.1", "::1")

    def connect(self, address):
        if is_loopback(address):
            return _socket.socket.connect(self, address)
        return guard_connect(self, address)

    def connect_ex(self, address):
        if is_loopback(address):
            return _socket.socket.connect_ex(self, address)
        return guard_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)


@pytest.fixture
def paths(tmp_path) -> Paths:
    p = Paths(journal=tmp_path / "journal" / "decisions.jsonl",
              state_dir=tmp_path / "journal" / "state", config_dir=tmp_path / "config")
    p.config_dir.mkdir(parents=True)
    return p


@pytest.fixture
def journal(paths) -> Journal:
    j = Journal(paths.journal)
    write_day(j)
    return j
