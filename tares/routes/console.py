"""The console: the built SPA's catch-all, which make_app() mounts last so every API route wins,
and the few routes of the degraded app that serves it when the store could not be opened."""
from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from ..deps import Deps


def router(deps: Deps, serve_ui: Callable) -> APIRouter:
    r = APIRouter()

    # ── console UI (built SPA; catch-all registered last so API routes win) ──
    @r.get("/{path:path}", include_in_schema=False)
    def ui(path: str):
        # An unmatched /api/* or /ingest/* path must never answer with the SPA's HTML and a 200 —
        # a client checking the status reads that as success and fails to parse (TR-226). The
        # console never requests these prefixes, so nothing legitimate is lost.
        if path == "api" or path.startswith(("api/", "ingest/")):
            deps.err(KeyError(f"unknown API path /{path}"), 404)
        return serve_ui(path)

    return r


def degraded_router(reason: str, retry: bool, auth_required: bool, login_url: str,
                    serve_ui: Callable) -> APIRouter:
    """/health with the reason, a 503 for every /api and /ingest path, and the console."""
    r = APIRouter()

    @r.get("/health", include_in_schema=False)
    def health():
        # HTTP 200 with status "down": a probe that can read the body learns WHAT is wrong, and the
        # console (which only ever reaches this daemon) can render it. Keys AuthGate depends on stay.
        body = {"status": "down", "detail": reason, "auth_required": auth_required,
                "sources": [], "pct_used": None, "capabilities": []}
        if login_url:
            body["login_url"] = login_url
        return JSONResponse(body, status_code=503) if retry else body

    async def unavailable():
        return JSONResponse({"detail": f"database unavailable; {reason}", "status": "down"},
                            status_code=503)

    # Everything that needs the store answers 503 (not a bare 500, and not the SPA's index.html).
    for _p in ("/api/{rest:path}", "/ingest/{rest:path}"):
        r.add_api_route(_p, unavailable, include_in_schema=False,
                        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"])

    @r.get("/{path:path}", include_in_schema=False)
    def ui(path: str):
        # An unmatched /api/* or /ingest/* path must never answer with the SPA's HTML and a 200 —
        # a client checking the status reads that as success and fails to parse (TR-226). The
        # console never requests these prefixes, so nothing legitimate is lost.
        if path == "api" or path.startswith(("api/", "ingest/")):
            raise HTTPException(status_code=404, detail=f"unknown API path /{path}")
        return serve_ui(path)

    return r
