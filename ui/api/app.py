"""The read-only API app: GET /api/health (UI-1), /api/explainer (UI-2), /api/decisions
(UI-3, UI-4), /api/live (UI-5), and the built frontend (ui/web/dist) on the same origin.

Rules (CLAUDE.md, UI track): GET only; served on 127.0.0.1 only (ui/api/__main__.py); Host
must be 127.0.0.1 or localhost (blocks DNS rebinding from a web page); no docs or OpenAPI
routes; no network calls; never a lock or a write under journal/ or config/; money values are
shown as recorded, never computed.

Every response carries a Content-Security-Policy that lets the page load and fetch only from
its own origin: no CDN fonts, analytics or telemetry can load even if a dependency tried.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi import Path as PathParam
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.staticfiles import StaticFiles

from journal.log import JournalError
from ui.api.detail import RUN_ID_PATTERN, decision_detail, records_dir
from ui.api.explainer import explainer
from ui.api.history import FILTERS, decisions, load_reasons
from ui.api.live import live
from ui.api.paths import Paths
from ui.api.readers.config import read_limits, read_run_day_config
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
OUTCOME_PATTERN = "^(" + "|".join(FILTERS) + ")$"  # module level: annotations resolve here
WEB_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; font-src 'self'; "
       "img-src 'self'; connect-src 'self'; object-src 'none'; frame-src 'none'; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
SECURITY_HEADERS = {"Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff",
                    "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY"}
NOT_BUILT = ("The frontend is not built. Build it with:\n"
             "  cd ui/web && npm ci && npm run build\n"
             "The API is at /api/health and /api/explainer.\n")


def etag(body: dict) -> str:
    """A strong ETag over the body without its request time and the heartbeat's age (both
    change every second; a change that matters, such as turning stale, changes the rest)."""
    stable = {k: v for k, v in body.items() if k != "as_of"}
    stable["run_day"] = {k: v for k, v in body["run_day"].items() if k != "age_s"}
    digest = hashlib.sha256(json.dumps(stable, sort_keys=True, ensure_ascii=False)
                            .encode("utf-8")).hexdigest()
    return f'"{digest[:32]}"'


def _now() -> datetime:
    return datetime.now(NY)


def create_app(paths: Paths | None = None, *,
               clock: Callable[[], datetime] = _now,
               web_dir: Path | None = WEB_DIST) -> FastAPI:
    """The app. `web_dir` is the built frontend (None: serve none), mounted at / after the
    API routes, so /api/* is never shadowed."""
    paths = paths or Paths.default()
    app = FastAPI(title="trading-agents UI (read-only)", docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(ALLOWED_HOSTS))
    reader = JournalReader(paths.journal)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):  # noqa: ANN202
        response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        return response

    @app.get("/api/explainer")
    def get_explainer() -> dict:
        return explainer(paths)

    reasons = load_reasons()

    @app.get("/api/decisions")
    def get_decisions(
        outcome: Annotated[str, Query(pattern=OUTCOME_PATTERN)] = "all",
        day_from: Annotated[date | None, Query(alias="from")] = None,
        day_to: Annotated[date | None, Query(alias="to")] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: Annotated[str, Query(pattern="^[0-9]{1,9}$")] = "0",
    ) -> dict:
        """The History page: decisions grouped by run_id, newest first (ui/api/history.py)."""
        if day_from is not None and day_to is not None and day_from > day_to:
            raise HTTPException(status_code=422, detail="'from' is after 'to'")
        return decisions(reader.read(), clock().astimezone(NY), outcome=outcome,
                         day_from=day_from, day_to=day_to, limit=limit, cursor=int(cursor),
                         reasons=reasons)

    @app.get("/api/decisions/{run_id}")
    def get_decision(run_id: Annotated[str, PathParam(pattern=RUN_ID_PATTERN)]) -> dict:
        """The Decision detail page (ui/api/detail.py). 404 when nothing knows run_id."""
        body = decision_detail(reader.read(), clock().astimezone(NY), run_id,
                               records_dir(paths.journal), reasons)
        if body is None:
            raise HTTPException(status_code=404, detail="no such decision")
        return body

    @app.get("/api/live")
    def get_live(request: Request) -> Response:
        """The Live run page (ui/api/live.py), polled every 2 s. The ETag covers everything
        but the request time and the heartbeat's age, so an unchanged run answers 304."""
        body = live(paths, reader.read(), clock().astimezone(NY),
                    limits=read_limits(paths.config_dir),
                    run_cfg=read_run_day_config(paths.config_dir), reasons=reasons)
        tag = etag(body)
        headers = {"ETag": tag, "Cache-Control": "no-cache"}
        if request.headers.get("if-none-match") == tag:
            return Response(status_code=304, headers=headers)
        return JSONResponse(body, headers=headers)

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
        if limits.value is not None and not limits.value.paper:
            problems.append({"level": "error", "text": "limits say paper: false. This project "
                             "trades paper only, and run_day refuses to start"})
        if limits.value is not None and not limits.value.trading_enabled:
            problems.append({"level": "warn", "text": "the kill switch is on "
                             "(trading_enabled: false): no new orders"})
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
            # the header's PAPER badge and kill-switch note (None: the limits are unknown)
            "paper": bool(limits.value.paper) if limits.value is not None else None,
            "trading_enabled": (bool(limits.value.trading_enabled)
                                if limits.value is not None else None),
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

    if web_dir is not None and (Path(web_dir) / "index.html").is_file():
        app.mount("/", StaticFiles(directory=Path(web_dir), html=True), name="web")
    else:
        @app.get("/", response_class=PlainTextResponse)
        def not_built() -> str:
            return NOT_BUILT

    return app
