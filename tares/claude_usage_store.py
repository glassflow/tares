"""Storage for what Claude Code sessions spend, mixed into Store.

Three things arrive through the claude_code source (tares/connectors/claude_code.py):
- the token usage every model reply carries in the transcript, one row per reply;
- the status line's report (a station's context window and the plan's limits), the newest per session;
- a /context reading (the window by category) and the plan's limits from /usage, which the factory
  CLI reads on the crew's machine.
Cost is priced at write time at API list prices (tares/pricing.py): on a subscription it is what the
tokens would cost on the API, a way to compare projects, not a bill.
"""
from __future__ import annotations

import json

from .envelope import now_utc
from .pricing import (CACHE_WRITE_1H_MULT, CACHE_WRITE_MULT, MODEL_PRICING, NEXT_VERSION_SQL,
                      cost_usd)

CLAUDE_USAGE_SCHEMA = [
    # One row per model reply in a Claude Code session. Claude Code repeats a reply's usage on each
    # of its content lines, so a reply is one row (session, message id) holding the largest count
    # seen. `project` is the project the line was stamped with, when the plugin knew one.
    """CREATE TABLE IF NOT EXISTS session_usage (
      session            TEXT,
      message            TEXT,
      replied_at         TIMESTAMPTZ,
      model              TEXT,
      project            TEXT,
      subagent           TEXT,
      input_tokens       BIGINT,
      output_tokens      BIGINT,
      cache_write_tokens BIGINT,
      cache_read_tokens  BIGINT,
      cost_usd           DOUBLE,
      PRIMARY KEY (session, message)
    )""",
    # of the cache writes, the ones kept an hour (they bill at twice the 5-minute rate)
    "ALTER TABLE session_usage ADD COLUMN IF NOT EXISTS cache_write_1h_tokens BIGINT",
    # Per session: the status line's latest report, the latest /context reading, and when a person
    # last asked for a new reading.
    """CREATE TABLE IF NOT EXISTS session_meters (
      session          TEXT PRIMARY KEY,
      status           JSON,
      status_at        TIMESTAMPTZ,
      context          JSON,
      context_at       TIMESTAMPTZ,
      context_asked_at TIMESTAMPTZ
    )""",
]

PLAN_SETTING = "claude_plan"          # the plan's limits as /usage last reported them
SYNTHETIC = "<synthetic>"             # the model of Claude Code's own notices: no model call, no tokens
CREW_ROLES = ("orchestrator", "reviewer", "releaser", "comms")


def reply_tokens(usage: dict) -> tuple[int, int, int, int, int]:
    """(input, output, cache write, cache read, of the writes the ones kept an hour)."""
    def n(v) -> int:
        try:
            return max(int(v or 0), 0)
        except (TypeError, ValueError):
            return 0
    cc = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else {}
    return (n(usage.get("input_tokens")), n(usage.get("output_tokens")),
            n(usage.get("cache_creation_input_tokens")), n(usage.get("cache_read_input_tokens")),
            n(cc.get("ephemeral_1h_input_tokens")))


def _json(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return None
    return v


class ClaudeUsageStore:
    """Methods Store gains for Claude Code usage. Expects self.con and self._lock."""

    # ── upgrade and clean-up ──────────────────────────────────────────────────
    def _claude_usage_fill(self) -> None:
        """Replies stored as claude_code events without a usage row get one: every one on the
        first start, then those from a day before the newest row on (a version that did not count
        them may have run in between). Lock not needed (init). The usage rows are state and the
        replies are events, so they are read from DuckDB and written where the state lives."""
        since = self.sc.execute("SELECT max(replied_at) - INTERVAL 1 DAY FROM session_usage"
                                ).fetchone()[0]
        sources = [r[0] for r in self.sc.execute(
            "SELECT name FROM catalog_sources WHERE connector = 'claude_code'").fetchall()]
        projects = {}
        for pid, pname in self.sc.execute("SELECT id, name FROM usecases").fetchall():
            projects.setdefault(pname, pid)
            projects[pid] = pid                            # an id wins over a same-named project
        u = "json_extract(e.payload, '$.message.usage')"
        rows = self.con.execute(
            "SELECT e.key_value, json_extract_string(e.payload, '$.message.id'), min(e.event_time), "
            "any_value(json_extract_string(e.payload, '$.message.model')), "
            "any_value(json_extract_string(e.payload, '$.tares_project')), "
            "any_value(json_extract_string(e.payload, '$.subagent')), "
            f"max(CAST(json_extract({u}, '$.input_tokens') AS BIGINT)), "
            f"max(CAST(json_extract({u}, '$.output_tokens') AS BIGINT)), "
            f"max(CAST(json_extract({u}, '$.cache_creation_input_tokens') AS BIGINT)), "
            f"max(CAST(json_extract({u}, '$.cache_read_input_tokens') AS BIGINT)), "
            f"max(CAST(json_extract({u}, '$.cache_creation.ephemeral_1h_input_tokens') AS BIGINT)) "
            "FROM events e "
            f"WHERE e.source IN ({', '.join('?' * len(sources)) or 'NULL'}) "
            f"AND {u} IS NOT NULL AND json_extract_string(e.payload, '$.message.id') IS NOT NULL "
            "AND coalesce(json_extract_string(e.payload, '$.message.model'), '') <> ? "
            "AND (CAST(? AS TIMESTAMPTZ) IS NULL OR e.event_time >= ?) "
            "GROUP BY 1, 2", [*sources, SYNTHETIC, since, since]).fetchall()
        rows = [(*r[:4], projects.get(r[4]), *r[5:]) for r in rows]
        # DuckDB binds an executemany row by row; one big statement is much faster there
        many = (self.sc.executemany if self.sc is not self.con else
                lambda sql, chunk: self.con.execute(
                    sql.split(" VALUES ")[0] + " SELECT * FROM (VALUES " + ", ".join(
                        ["(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"] * len(chunk)) + ") ON CONFLICT DO NOTHING",
                    [v for r in chunk for v in r]))
        for i in range(0, len(rows), 1000):
            many(
                "INSERT INTO session_usage (session, message, replied_at, model, project, "
                "subagent, input_tokens, output_tokens, cache_write_tokens, cache_read_tokens, "
                "cache_write_1h_tokens) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT DO NOTHING", rows[i:i + 1000])
        self.sc.execute("DELETE FROM session_usage WHERE model = ?", [SYNTHETIC])
        # priced by family as pricing.cost_usd prices them; MODEL_PRICING is longest prefix first, so a
        # row a longer prefix already priced is not touched again by a shorter one
        for prefix, p_in, p_out, p_read in MODEL_PRICING:
            self.sc.execute(
                "UPDATE session_usage SET cost_usd = (coalesce(input_tokens, 0) * ? "
                "+ coalesce(output_tokens, 0) * ? "
                "+ (coalesce(cache_write_tokens, 0) - coalesce(cache_write_1h_tokens, 0)) * ? "
                "+ coalesce(cache_write_1h_tokens, 0) * ? "
                "+ coalesce(cache_read_tokens, 0) * ?) / 1e6 "
                "WHERE cost_usd IS NULL AND starts_with(model, ?) "
                "AND NOT regexp_matches(substr(model, ?), ?)",
                [p_in, p_out, p_in * CACHE_WRITE_MULT, p_in * CACHE_WRITE_1H_MULT,
                 p_read, prefix, len(prefix) + 1, NEXT_VERSION_SQL])

    def _usage_forget_project(self, uid: str) -> None:
        """A project is deleted: its replies stay, counted as the session's own. Lock held."""
        self.sc.execute("UPDATE session_usage SET project = NULL WHERE project = ?", [uid])

    # ── writes, from the claude_code source ───────────────────────────────────
    def record_reply_usage(self, session: str, message: str, at, model: str | None,
                           project: str | None, subagent: str | None, usage: dict) -> None:
        """One line of a model reply: keep the largest count seen for the reply, and its cost."""
        i, o, cw, cr, hour = reply_tokens(usage)
        cost = cost_usd(model or "", i, o, cw, cr, hour)
        with self._swrite:
            self.sc.execute(
                "INSERT INTO session_usage (session, message, replied_at, model, project, subagent, "
                "input_tokens, output_tokens, cache_write_tokens, cache_read_tokens, "
                "cache_write_1h_tokens, cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (session, message) DO UPDATE SET "
                "input_tokens = greatest(session_usage.input_tokens, excluded.input_tokens), "
                "output_tokens = greatest(session_usage.output_tokens, excluded.output_tokens), "
                "cache_write_tokens = greatest(session_usage.cache_write_tokens, "
                "excluded.cache_write_tokens), "
                "cache_read_tokens = greatest(session_usage.cache_read_tokens, "
                "excluded.cache_read_tokens), "
                "cache_write_1h_tokens = greatest(session_usage.cache_write_1h_tokens, "
                "excluded.cache_write_1h_tokens), "
                "cost_usd = greatest(session_usage.cost_usd, excluded.cost_usd), "
                "project = coalesce(session_usage.project, excluded.project), "
                "model = coalesce(session_usage.model, excluded.model)",
                [session, message, at, model or None, project or None, subagent or None,
                 i, o, cw, cr, hour, cost])

    def set_session_status(self, session: str, status: dict, at) -> None:
        """The status line's report; an older one than the one stored does not win."""
        with self._swrite:
            self.sc.execute(
                "INSERT INTO session_meters (session, status, status_at) VALUES (?, ?, ?) "
                "ON CONFLICT (session) DO UPDATE SET status = excluded.status, "
                "status_at = excluded.status_at WHERE session_meters.status_at IS NULL "
                "OR excluded.status_at >= session_meters.status_at",
                [session, json.dumps(status), at])

    def set_session_context(self, session: str, context: dict, at) -> None:
        with self._swrite:
            self.sc.execute(
                "INSERT INTO session_meters (session, context, context_at) VALUES (?, ?, ?) "
                "ON CONFLICT (session) DO UPDATE SET context = excluded.context, "
                "context_at = excluded.context_at WHERE session_meters.context_at IS NULL "
                "OR excluded.context_at >= session_meters.context_at",
                [session, json.dumps(context), at])

    def ask_session_context(self, session: str, at) -> None:
        """A person asked for a new /context reading of the session."""
        with self._swrite:
            self.sc.execute(
                "INSERT INTO session_meters (session, context_asked_at) VALUES (?, ?) "
                "ON CONFLICT (session) DO UPDATE SET context_asked_at = excluded.context_asked_at",
                [session, at])

    # ── reads ─────────────────────────────────────────────────────────────────
    def context_asks(self, since) -> list[str]:
        """Sessions a person asked a reading of, since `since`, that have none newer yet."""
        with self._sread:
            rows = self.sc.execute(
                "SELECT session FROM session_meters WHERE context_asked_at >= ? AND "
                "(context_at IS NULL OR context_at < context_asked_at) ORDER BY context_asked_at",
                [since]).fetchall()
        return [r[0] for r in rows]

    def session_meters(self, sessions: list[str]) -> dict[str, dict]:
        if not sessions:
            return {}
        marks = ", ".join("?" for _ in sessions)
        with self._sread:
            rows = self.sc.execute(
                "SELECT session, status, status_at, context, context_at, context_asked_at "
                f"FROM session_meters WHERE session IN ({marks})", list(sessions)).fetchall()
        return {r[0]: {"status": _json(r[1]), "status_at": r[2], "context": _json(r[3]),
                       "context_at": r[4], "context_asked_at": r[5]} for r in rows}

    def latest_rate_limits(self) -> tuple | None:
        """(when, rate_limits) from the newest status line report that carried the plan's limits."""
        with self._sread:
            row = self.sc.execute(
                "SELECT status_at, json_extract(status, '$.rate_limits') FROM session_meters "
                "WHERE json_extract(status, '$.rate_limits') IS NOT NULL "
                "AND json_type(json_extract(status, '$.rate_limits')) = 'OBJECT' "
                "ORDER BY status_at DESC LIMIT 1").fetchone()
        return (row[0], _json(row[1])) if row else None

    _TOTALS = ("count(*), sum(input_tokens), sum(output_tokens), sum(cache_write_tokens), "
               "sum(cache_read_tokens), sum(cost_usd), count(*) FILTER (WHERE cost_usd IS NULL)")

    @staticmethod
    def _totals(r) -> dict:
        return {"replies": int(r[0] or 0), "input_tokens": int(r[1] or 0),
                "output_tokens": int(r[2] or 0), "cache_write_tokens": int(r[3] or 0),
                "cache_read_tokens": int(r[4] or 0), "cost_usd": float(r[5] or 0),
                "unpriced_replies": int(r[6] or 0)}

    def usage_by_session(self, sessions: list[str]) -> dict[str, dict]:
        if not sessions:
            return {}
        marks = ", ".join("?" for _ in sessions)
        with self._sread:
            rows = self.sc.execute(
                f"SELECT session, {self._TOTALS} FROM session_usage WHERE session IN ({marks}) "
                "GROUP BY session", list(sessions)).fetchall()
        return {r[0]: self._totals(r[1:]) for r in rows}

    def usage_by_project(self, since=None) -> list[dict]:
        """Replies since `since` (all when None) by the project they count for: the project the
        reply was stamped with, else the station's project (a builder), else the project the
        session worked in. The crew's own stations serve every project, so their unstamped replies
        are the crew's (`kind` crew); anything else unclaimed is `other`."""
        crew = ", ".join(f"'{r}'" for r in CREW_ROLES)
        where, args = ("WHERE u.replied_at >= ?", [since]) if since is not None else ("", [])
        with self._sread:
            rows = self.sc.execute(
                "WITH r AS (SELECT u.*, coalesce(u.project, ss.project, "
                f"CASE WHEN ss.role IN ({crew}) THEN NULL ELSE ps.project END) AS proj, "
                f"ss.role IN ({crew}) AS crew FROM session_usage u "
                "LEFT JOIN session_stations ss ON ss.session = u.session "
                "LEFT JOIN (SELECT session, min(project) AS project FROM project_sessions "
                f"GROUP BY session) ps ON ps.session = u.session {where}) "
                "SELECT proj, CASE WHEN proj IS NOT NULL THEN 'project' WHEN coalesce(crew, false) "
                f"THEN 'crew' ELSE 'other' END AS kind, count(DISTINCT session), {self._TOTALS} "
                "FROM r GROUP BY 1, 2 ORDER BY sum(cost_usd) DESC NULLS LAST", args).fetchall()
        return [{"project": r[0], "kind": r[1], "sessions": int(r[2] or 0), **self._totals(r[3:])}
                for r in rows]
