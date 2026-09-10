"""Runtime — the daemon's live state: the DB-backed catalog plus one poll task per source.

This replaces the static start-everything-at-boot loop: sources are started, stopped, paused and
reconfigured at runtime as the catalog changes (no restart). Health (last poll, last error, counts)
is tracked in memory; per-source event totals come from the events table.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from .config import Catalog, SourceCfg, catalog_from_db
from .connectors import REGISTRY, SPECS, build_connector
from .envelope import now_utc
from .triggers import eval_triggers


@dataclass
class SourceHealth:
    status: str = "starting"        # starting | ok | error | paused | push
    last_poll_at: object = None
    last_ok_at: object = None
    last_error: str | None = None
    consecutive_errors: int = 0
    polls: int = 0
    events_since_start: int = 0


@dataclass
class SourceRuntime:
    cfg: SourceCfg
    task: asyncio.Task | None = None
    health: SourceHealth = field(default_factory=SourceHealth)


class Runtime:
    def __init__(self, store, dispatcher):
        self.store = store
        # Set by the daemon: returns a reason while ingest is paused for storage, else None.
        # Poll connectors skip their fetch while it is set, so a full disk stops growth from
        # every direction, not only the push endpoints.
        self.storage_full = None
        self.dispatcher = dispatcher
        self.catalog: Catalog = catalog_from_db(store)
        self.sources: dict[str, SourceRuntime] = {}
        # {trigger_name: last_eval_datetime} — debounces trigger re-evaluation across ingest ticks.
        self._trigger_eval_at: dict = {}

    # ── lifecycle ─────────────────────────────────────────────────────────────
    def start_all(self) -> None:
        for cfg in self.catalog.sources.values():
            self._start(cfg)

    def shutdown(self) -> None:
        from .triggers import cancel_catchups
        cancel_catchups()
        for rt in self.sources.values():
            if rt.task:
                rt.task.cancel()
        self.sources.clear()

    def _start(self, cfg: SourceCfg) -> None:
        rt = SourceRuntime(cfg=cfg)
        mode = SPECS.get(cfg.connector, {}).get("mode")
        if cfg.paused:
            rt.health.status = "paused"
        elif mode == "reference":
            self._materialize(rt)      # declarative: rows mirror the config, no poll loop
        elif mode == "push":
            rt.health.status = "push"  # no poll loop; envelopes arrive via /ingest
        else:
            rt.task = asyncio.create_task(self._loop(rt))
        self.sources[cfg.name] = rt

    def _materialize(self, rt: SourceRuntime) -> None:
        """(Re)build a declarative source's rows from its config, replacing whatever was there."""
        rt.health.polls += 1
        rt.health.last_poll_at = now_utc()
        try:
            envelopes = build_connector(rt.cfg, self.store).materialize()
            self.store.replace_source_events(rt.cfg.name, envelopes)
            rt.health.events_since_start = len(envelopes)
            rt.health.last_ok_at = now_utc()
            rt.health.last_error, rt.health.consecutive_errors, rt.health.status = None, 0, "ok"
        except Exception as e:
            rt.health.last_error = f"{type(e).__name__}: {str(e) or repr(e)}"
            rt.health.consecutive_errors += 1
            rt.health.status = "error"
            print(f"[connector {rt.cfg.name}] {rt.health.last_error}")

    def _stop(self, name: str) -> None:
        rt = self.sources.pop(name, None)
        if rt and rt.task:
            rt.task.cancel()

    async def _loop(self, rt: SourceRuntime) -> None:
        conn = build_connector(rt.cfg, self.store)
        while True:
            h = rt.health
            h.last_poll_at = now_utc()
            h.polls += 1
            reason = self.storage_full() if self.storage_full else None
            if reason:
                h.last_error, h.status = reason, "paused"
                await asyncio.sleep(rt.cfg.poll_seconds)
                continue
            try:
                envelopes = await conn.poll()
                self.store.append(envelopes)
                h.events_since_start += len(envelopes)
                h.last_ok_at = now_utc()
                h.last_error, h.consecutive_errors, h.status = None, 0, "ok"
                if envelopes:
                    await eval_triggers(self.store, self.catalog, self.dispatcher,
                                        affected_sources={rt.cfg.name},
                                        eval_state=self._trigger_eval_at)
            except Exception as e:  # never let one source kill the loop
                # some exceptions (timeouts, a few connection errors) have an empty str() — always
                # surface the type (and repr as a fallback) so the error is never a blank line.
                detail = str(e) or repr(e)
                h.last_error = f"{type(e).__name__}: {detail}"
                h.consecutive_errors += 1
                h.status = "error"
                print(f"[connector {rt.cfg.name}] {h.last_error}")
            await asyncio.sleep(rt.cfg.poll_seconds)

    # ── catalog mutations (already persisted to the store by the caller) ─────
    def reload_catalog(self) -> None:
        """Re-read views/triggers (and source defs) from the store; restart changed sources."""
        new = catalog_from_db(self.store)
        old_sources = self.catalog.sources
        self.catalog = new

        for name in set(old_sources) - set(new.sources):
            self._stop(name)
        for name, cfg in new.sources.items():
            if name not in self.sources:
                self._start(cfg)
            elif old_sources.get(name) != cfg:
                self._stop(name)
                self._start(cfg)

    # ── push ingestion (webhook / memory / otlp sources) ──────────────────────
    def _push_cfg(self, token: str):
        # /ingest/<token>: match the stable ingest_key first, then the source name (back-compat for
        # name-based URLs and YAML sources without a key).
        cfg = next((c for c in self.catalog.sources.values()
                    if c.ingest_key and c.ingest_key == token), None)
        if cfg is None:
            cfg = self.catalog.sources.get(token)
        if cfg is None and token == "claude_code":
            # zero-setup, like the OTLP source: the Claude Code plugin ships with only the ingest
            # token, and on a hosted cell it can't call the (auth-token-guarded) management API to
            # create its source — so provision it on first ingest instead.
            from .connectors import normalize_config, source_type_for
            self.store.upsert_catalog_source(
                "claude_code", source_type_for("claude_code"), "claude_code", "10s",
                normalize_config("claude_code", {"push": True, "labels": [
                    {"name": "session", "field": "session", "primary": True},
                    {"name": "repo", "field": "repo"},
                    {"name": "branch", "field": "branch"},
                    {"name": "model", "field": "model"}]}))
            self.reload_catalog()
            cfg = self.catalog.sources.get("claude_code")
            print("taresd: auto-provisioned Claude Code source 'claude_code'")
        if cfg is None:
            raise KeyError(f"unknown source {token!r}")
        # push sources, plus poll connectors that opt into accepting pushes too (claude_code can be
        # tailed locally *or* fed by its Claude Code plugin via /ingest).
        is_push = SPECS.get(cfg.connector, {}).get("mode") == "push"
        accepts_push = getattr(REGISTRY.get(cfg.connector), "ACCEPTS_PUSH", False)
        if not (is_push or accepts_push):
            raise ValueError(f"source {cfg.name!r} is not a push source")
        if cfg.paused:
            raise ValueError(f"source {cfg.name!r} is paused")
        return cfg

    async def _store_envelopes(self, source_name: str, envelopes: list) -> int:
        """Append envelopes, update health, fire triggers — the common ingest tail."""
        self.store.append(envelopes)
        rt = self.sources.get(source_name)
        if rt:
            rt.health.events_since_start += len(envelopes)
            rt.health.last_ok_at = now_utc()
        if envelopes:
            await eval_triggers(self.store, self.catalog, self.dispatcher,
                                affected_sources={source_name},
                                eval_state=self._trigger_eval_at)
        return len(envelopes)

    def _ensure_push_wins(self, cfg) -> None:
        """Push wins: the first pushed event to a poll-mode source that also accepts pushes
        (claude_code — tailable locally *or* fed by its Claude Code plugin) flips it to push mode,
        so the daemon stops tailing files the plugin is now feeding. Without this, a source created
        as a local tail (e.g. via the console's Connect card) and *also* fed by the plugin ingests
        every event twice. Persisted + reloaded so the running poller actually stops. No-op for
        native push connectors (no poll() to conflict with) and for sources already in push mode."""
        if SPECS.get(cfg.connector, {}).get("mode") == "push" or cfg.config.get("push"):
            return
        self.store.upsert_catalog_source(cfg.name, cfg.type, cfg.connector, cfg.poll,
                                         {**cfg.config, "push": True},
                                         paused=cfg.paused, ingest_key=cfg.ingest_key)
        self.reload_catalog()

    async def ingest(self, token: str, payload) -> int:
        cfg = self._push_cfg(token)   # token may be the ingest_key or the source name
        self._ensure_push_wins(cfg)   # first push flips a tail source to push mode (no double-ingest)
        conn = build_connector(cfg, self.store)
        return await self._store_envelopes(cfg.name, conn.map_payload(payload))

    async def ingest_otlp(self, source_name: str, signal: str, body) -> int:
        cfg = self._push_cfg(source_name)
        if cfg.connector != "otlp":
            raise ValueError(f"source {cfg.name!r} is not an OTLP source")
        conn = build_connector(cfg, self.store)
        return await self._store_envelopes(cfg.name, conn.map_otlp(signal, body))

    # ── health/introspection ──────────────────────────────────────────────────
    def health_snapshot(self) -> dict:
        """{source: health dict} merged with persisted event totals."""
        totals = {s["source"]: s for s in self.store.event_stats()}
        out = {}
        for name, rt in self.sources.items():
            h = rt.health
            t = totals.get(name, {})
            out[name] = {
                "status": h.status,
                "last_poll_at": h.last_poll_at,
                "last_ok_at": h.last_ok_at,
                "last_error": h.last_error,
                "consecutive_errors": h.consecutive_errors,
                "polls": h.polls,
                "events_since_start": h.events_since_start,
                "events_total": t.get("events", 0),
                "last_ingest": t.get("last_ingest"),
            }
        return out

    async def test_source(self, cfg: SourceCfg) -> dict:
        """One poll with the given config; cursor is restored so the real loop misses nothing."""
        if SPECS.get(cfg.connector, {}).get("mode") == "push":
            return {"ok": True, "events": 0,
                    "note": f"push source: POST JSON to /ingest/{cfg.name} to ingest"}
        saved_cursor = self.store.get_cursor(cfg.name)
        try:
            conn = build_connector(cfg, self.store)
            envelopes = await asyncio.wait_for(conn.poll(), timeout=15)
            sample = [e.text for e in envelopes[:3]]
            return {"ok": True, "events": len(envelopes), "sample": sample}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        finally:
            if saved_cursor is not None:
                self.store.set_cursor(cfg.name, saved_cursor)
            else:
                self.store.delete_cursor(cfg.name)
