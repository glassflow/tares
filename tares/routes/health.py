"""The probes: /metrics, /health and the metering reads (/api/usage, /api/usage/model)."""
from __future__ import annotations

import hmac
import os
import shutil
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from .. import metrics as _metrics
from ..deps import Deps


def router(deps: Deps) -> APIRouter:
    r = APIRouter()
    store = deps.store
    _err = deps.err

    # ── agent surface (unchanged contract; queries now logged) ───────────────
    # A scrape token for /metrics (TR-653), separate from the cell's own auth token on purpose: the
    # scraper gets only the counters. Read here, not at import, so each app built reads the env.
    metrics_token = os.getenv("TARES_METRICS_TOKEN", "").strip()

    @r.get("/metrics", include_in_schema=False)
    def metrics_endpoint(request: Request):
        """The daemon's own counters in the Prometheus text format (TR-308): events per source,
        model calls and spend by provider, storage against its limit, event loop lag. Counts only. Public like /health
        unless TARES_METRICS_TOKEN is set; then it wants `Authorization: Bearer <that token>`
        (TR-653). The auth guard lets /metrics through either way, so this check decides."""
        if metrics_token and not hmac.compare_digest(
                request.headers.get("authorization", "").encode(),
                f"Bearer {metrics_token}".encode()):
            return JSONResponse({"detail": "this endpoint needs the metrics scrape token"},
                                status_code=401, headers={"WWW-Authenticate": "Bearer"})
        try:
            body, ctype = _metrics.render()
        except RuntimeError:
            _err(KeyError("metrics need the prometheus-client package"), 404)
        return Response(content=body, media_type=ctype)

    @r.get("/health")
    def health():
        """Liveness that actually touches the store, so a database that stopped answering can't read
        as healthy to a k8s probe or the control plane's uptime check. `ok` / `degraded` (running, but nearly out of
        the configured storage) / `down` (the store stopped answering — restart needed). Always
        HTTP 200: the status and `detail` say what is wrong, which a bare 503 could not.
        `pct_used` is on /api/usage's 0-100 scale and is null when no limit is configured — unknown,
        never 0. The probe is a SELECT 1 in the session store plus its measured size (the SQLite
        file and WAL, or the Postgres database, and the archive); no table is scanned."""
        status, detail, pct = "ok", None, None
        try:
            store.ping()
        except Exception as e:
            status, detail = "down", f"database unavailable: {e}"
        if status == "ok":
            st = deps.storage()
            pct = st["pct_used"]
            if st["detail"]:
                status, detail = "degraded", st["detail"]
        body = {"status": status, "auth_required": bool(deps.auth_token), "sources": [],
                "pct_used": pct, "version": deps.version(),
                "capabilities": list(deps.capabilities),
                "uptime_seconds": int(time.monotonic() - deps.started_mono)}
        if detail:
            body["detail"] = detail
        body.update(deps.cloud_links())
        return body

    # ── metering ──────────────────────────────────────────────────────────────
    @r.get("/api/usage")
    def usage():
        """What this instance is using: the session store's bytes (`db_bytes`: the SQLite file, or
        the Postgres database; `wal_bytes` the SQLite WAL), the archive's (`archive_bytes`), the
        volume the data directory sits on, and the lines stored (`events`).
        `max_bytes` is the operator's TARES_MAX_DB_SIZE, else that volume (`max_bytes_source` says
        which); null only when neither is known. At INGEST_PAUSE_PCT of it ingest is refused
        (`ingest_paused`), so the disk never fills.
        `pct_used` is (db + wal + archive) over max_bytes on a 0-100 scale, NOT a 0-1 fraction, so
        a "warn at 80%" consumer compares against 80, not 0.8. Null whenever max_bytes is null.
        Cheap by construction: file stats and two sums, no table scan, so the cost does not grow
        with the lines stored."""
        u = store.usage()
        try:
            du = shutil.disk_usage(os.path.abspath(deps.data_dir or "."))
            disk_total, disk_free = du.total, du.free
        except OSError:
            disk_total = disk_free = None
        st = deps.storage()
        return {**u, "disk_total": disk_total, "disk_free": disk_free,
                "max_bytes": st["max_bytes"], "max_bytes_source": st["max_bytes_source"],
                "pct_used": st["pct_used"], "ingest_paused": st["paused"]}

    @r.get("/api/usage/model")
    def usage_model(days: int = 30):
        """The cell's Anthropic spend meter: all-time totals plus a per-day tail, split by surface
        (every summary and setup plan is under "ask", the name it had before agents went; old
        agent runs stay under "agent"). Generic data only — a hosted control plane polls this to enforce
        credits, a self-hoster reads their own bill coming; the cell attaches no meaning to it.
        `cost_usd` is a floor: `uncosted_calls` counts rows whose model had no known price, and
        runs from before metering existed are absent entirely."""
        return store.model_usage_summary(days=max(1, min(int(days), 366)))

    return r
