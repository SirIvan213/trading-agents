"""The read-only API app. UI-1 has one route, GET /api/health; the pages' routes come in
UI-2 to UI-7 (issues #215-#220).

Rules (CLAUDE.md, UI track): GET only; served on 127.0.0.1 only (ui/api/__main__.py); Host
must be 127.0.0.1 or localhost (blocks DNS rebinding from a web page); no docs or OpenAPI
routes; no network calls; never a lock or a write under journal/ or config/; money values are
shown as recorded, never computed.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from starlette.middleware.trustedhost import TrustedHostMiddleware

from journal.log import JournalError
from ui.api.paths import Paths
from ui.api.readers.config import read_limits
from ui.api.readers.journal_snapshot import JournalReader, SnapshotJournal
from ui.api.readers.state import (
    HEARTBEAT_FUTURE_S,
    HEARTBEAT_STALE_S,
    budget_exists,
    heartbeat_age_s,
    heartbeat_path,
    read_state_file,
)

NY = ZoneInfo("America/New_York")
ALLOWED_HOSTS = ("127.0.0.1", "localhost")


def _now() -> datetime:
    return datetime.now(NY)


def create_app(paths: Paths | None = None, *,
               clock: Callable[[], datetime] = _now) -> FastAPI:
    paths = paths or Paths.default()
    app = FastAPI(title="trading-agents UI (read-only)", docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(ALLOWED_HOSTS))
    reader = JournalReader(paths.journal)

    @app.get("/api/health")
    def health() -> dict:
        now = clock().astimezone(NY)
        snap = reader.read()
        problems = snap.problems()
        orders_ok: bool | None = None
        if snap.exists and snap.trusted:
            try:
                SnapshotJournal(snap.entries).orders()
                orders_ok = True
            except JournalError as exc:
                orders_ok = False
                problems.append({"level": "error", "text": f"{exc}: day totals cannot be "
                                 "trusted, and run_day refuses orders until it is fixed"})
        limits = read_limits(paths.config_dir)
        if limits.error:
            problems.append({"level": "error", "text": limits.error})
        hb = read_state_file(heartbeat_path(paths.state_dir))
        age = heartbeat_age_s(hb, now)
        if hb.error:
            problems.append({"level": "error", "text": f"{hb.name} is {hb.error}"})
        future = age is not None and age < -HEARTBEAT_FUTURE_S
        if future:
            problems.append({"level": "error", "text": f"{hb.name} is dated in the future: "
                             "check the PC's clock"})
        budget_found, budget_error = budget_exists(paths.state_dir, now.date())
        if budget_error:
            problems.append({"level": "error", "text": budget_error})
        return {
            "ok": not any(p["level"] == "error" for p in problems),
            "read_only": True,
            "as_of": now.isoformat(),
            "journal": {"file": paths.journal.name, "exists": snap.exists,
                        "entries": len(snap.entries), "tail_incomplete": snap.tail_bytes > 0,
                        "error": snap.error, "orders_ok": orders_ok},
            "limits": limits.view(),
            "state": {
                # existence only, from the folder's listing; any read of the budget's
                # contents goes through open_shared (readers/state.py)
                "budget": {"file": f"budget-{now.date().isoformat()}.json",
                           "exists": budget_found, "error": budget_error},
                "heartbeat": {**hb.view(), "age_s": age,
                              "stale": age is None or age > HEARTBEAT_STALE_S or future},
            },
            "problems": problems,
        }

    return app
