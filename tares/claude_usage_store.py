"""Storage for what Claude Code sessions spend, mixed into Store.

Three things arrive through the claude_code source (tares/connectors/claude_code.py):
- the token usage every model reply carries in the transcript, one row per reply;
- the status line's report (a station's context window and the plan's limits), the newest per session;
- a /context reading (the window by category) and the plan's limits from /usage, which the factory
  CLI reads on the crew's machine.
Cost is priced at write time at API list prices (tares/pricing.py): on a subscription it is what the
tokens would cost on the API, a way to compare projects, not a bill.
The usage and meter rows live in the session store (tares/session_store), read and written with
SQLAlchemy Core on `self.sessions_db`; the DDL below stays, because on Postgres it makes the same tables.
"""
from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy import bindparam, case, distinct, false, func, select, update

from .pricing import cost_usd
from .session_store.tables import (project_sessions, session_meters, session_stations,
                                   session_usage)

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
    # the person of the credential that sent the status line, so the plan it carries has an owner
    "ALTER TABLE session_meters ADD COLUMN IF NOT EXISTS status_person TEXT",
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
    """Methods Store gains for Claude Code usage. Expects self.con, self._lock and self.sessions_db."""

    # ── upgrade and clean-up ──────────────────────────────────────────────────
    def _claude_usage_fill(self) -> None:
        """Replies stored as claude_code events without a usage row get one: every one on the
        first start, then those from a day before the newest row on (a version that did not count
        them may have run in between). Lock not needed (init). The usage rows are in the session
        store and the replies are events, so they are read from DuckDB and written there."""
        u = session_usage
        with self.sessions_db.begin() as conn:
            newest = conn.execute(select(func.max(u.c.replied_at))).scalar_one()
        since = newest - timedelta(days=1) if newest is not None else None
        sources = [r[0] for r in self.sc.execute(
            "SELECT name FROM catalog_sources WHERE connector = 'claude_code'").fetchall()]
        projects = {}
        for pid, pname in self.sc.execute("SELECT id, name FROM usecases").fetchall():
            projects.setdefault(pname, pid)
            projects[pid] = pid                            # an id wins over a same-named project
        j = "json_extract(e.payload, '$.message.usage')"
        rows = self.con.execute(
            "SELECT e.key_value, json_extract_string(e.payload, '$.message.id'), min(e.event_time), "
            "any_value(json_extract_string(e.payload, '$.message.model')), "
            "any_value(json_extract_string(e.payload, '$.tares_project')), "
            "any_value(json_extract_string(e.payload, '$.subagent')), "
            f"max(CAST(json_extract({j}, '$.input_tokens') AS BIGINT)), "
            f"max(CAST(json_extract({j}, '$.output_tokens') AS BIGINT)), "
            f"max(CAST(json_extract({j}, '$.cache_creation_input_tokens') AS BIGINT)), "
            f"max(CAST(json_extract({j}, '$.cache_read_input_tokens') AS BIGINT)), "
            f"max(CAST(json_extract({j}, '$.cache_creation.ephemeral_1h_input_tokens') AS BIGINT)) "
            "FROM events e "
            f"WHERE e.source IN ({', '.join('?' * len(sources)) or 'NULL'}) "
            f"AND {j} IS NOT NULL AND json_extract_string(e.payload, '$.message.id') IS NOT NULL "
            "AND coalesce(json_extract_string(e.payload, '$.message.model'), '') <> ? "
            "AND (CAST(? AS TIMESTAMPTZ) IS NULL OR e.event_time >= ?) "
            "GROUP BY 1, 2", [*sources, SYNTHETIC, since, since]).fetchall()
        cols = ("session", "message", "replied_at", "model", "project", "subagent", "input_tokens",
                "output_tokens", "cache_write_tokens", "cache_read_tokens", "cache_write_1h_tokens")
        rows = [dict(zip(cols, (*r[:4], projects.get(r[4]), *r[5:]))) for r in rows]
        stmt = self.sessions_db.upsert(u).on_conflict_do_nothing(
            index_elements=["session", "message"])
        with self.sessions_db.begin() as conn:
            for i in range(0, len(rows), 1000):
                conn.execute(stmt, rows[i:i + 1000])
            conn.execute(u.delete().where(u.c.model == SYNTHETIC))
            # priced by family as pricing.cost_usd prices them (longest prefix first, a model with
            # another version number after the prefix is left unpriced)
            todo = conn.execute(select(
                u.c.session, u.c.message, u.c.model, u.c.input_tokens, u.c.output_tokens,
                u.c.cache_write_tokens, u.c.cache_read_tokens, u.c.cache_write_1h_tokens
            ).where(u.c.cost_usd.is_(None), u.c.model.is_not(None))).all()
            priced = []
            for r in todo:
                cost = cost_usd(r[2], r[3] or 0, r[4] or 0, r[5] or 0, r[6] or 0, r[7] or 0)
                if cost is not None:
                    priced.append({"b_session": r[0], "b_message": r[1], "b_cost": cost})
            if priced:
                conn.execute(update(u).where(u.c.session == bindparam("b_session"),
                                             u.c.message == bindparam("b_message"))
                             .values(cost_usd=bindparam("b_cost")), priced)

    def _usage_forget_project(self, uid: str) -> None:
        """A project is deleted: its replies stay, counted as the session's own."""
        with self.sessions_db.begin() as conn:
            conn.execute(update(session_usage).where(session_usage.c.project == uid)
                         .values(project=None))

    # ── writes, from the claude_code source ───────────────────────────────────
    def record_reply_usage(self, session: str, message: str, at, model: str | None,
                           project: str | None, subagent: str | None, usage: dict) -> None:
        """One line of a model reply: keep the largest count seen for the reply, and its cost."""
        i, o, cw, cr, hour = reply_tokens(usage)
        cost = cost_usd(model or "", i, o, cw, cr, hour)
        db, u = self.sessions_db, session_usage
        stmt = db.upsert(u).values(
            session=session, message=message, replied_at=at, model=model or None,
            project=project or None, subagent=subagent or None, input_tokens=i, output_tokens=o,
            cache_write_tokens=cw, cache_read_tokens=cr, cache_write_1h_tokens=hour, cost_usd=cost)
        x = stmt.excluded
        stmt = stmt.on_conflict_do_update(index_elements=["session", "message"], set_={
            "input_tokens": db.greatest(u.c.input_tokens, x.input_tokens),
            "output_tokens": db.greatest(u.c.output_tokens, x.output_tokens),
            "cache_write_tokens": db.greatest(u.c.cache_write_tokens, x.cache_write_tokens),
            "cache_read_tokens": db.greatest(u.c.cache_read_tokens, x.cache_read_tokens),
            "cache_write_1h_tokens": db.greatest(u.c.cache_write_1h_tokens, x.cache_write_1h_tokens),
            "cost_usd": db.greatest(u.c.cost_usd, x.cost_usd),
            "project": func.coalesce(u.c.project, x.project),
            "model": func.coalesce(u.c.model, x.model)})
        with db.begin() as conn:
            conn.execute(stmt)

    def record_reported_usage(self, session: str, message: str, at, model: str | None,
                              project: str | None, counts: dict) -> None:
        """One reply's tokens as a runtime reported them once (a Codex station's, TR-1595): `counts` holds
        input_tokens, output_tokens, cache_creation_input_tokens and cache_read_input_tokens, each only
        when it was reported. A count that is not there is stored as unknown (NULL), never as 0, and the
        cost is the price of the model when Tares has one and every count is known, else unknown. A reply
        already stored is left as it is: its counts were final when they were written."""
        def n(key):
            v = counts.get(key)
            return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None
        i, o = n("input_tokens"), n("output_tokens")
        cw, cr = n("cache_creation_input_tokens"), n("cache_read_input_tokens")
        cost = None if None in (i, o, cw, cr) else cost_usd(model or "", i, o, cw, cr, 0)
        stmt = self.sessions_db.upsert(session_usage).values(
            session=session, message=message, replied_at=at, model=model or None, project=project or None,
            subagent=None, input_tokens=i, output_tokens=o, cache_write_tokens=cw, cache_read_tokens=cr,
            cache_write_1h_tokens=None if cw is None else 0, cost_usd=cost)
        with self.sessions_db.begin() as conn:
            conn.execute(stmt.on_conflict_do_nothing(index_elements=["session", "message"]))

    def set_session_status(self, session: str, status: dict, at, person_id: str | None = None) -> None:
        """The status line's report and the person of the credential that sent it; an older one
        than the one stored does not win."""
        m = session_meters
        stmt = self.sessions_db.upsert(m).values(
            session=session, status=json.dumps(status), status_at=at, status_person=person_id)
        x = stmt.excluded
        stmt = stmt.on_conflict_do_update(
            index_elements=["session"],
            set_={"status": x.status, "status_at": x.status_at, "status_person": x.status_person},
            where=(m.c.status_at.is_(None)) | (x.status_at >= m.c.status_at))
        with self.sessions_db.begin() as conn:
            conn.execute(stmt)

    def set_session_context(self, session: str, context: dict, at) -> None:
        m = session_meters
        stmt = self.sessions_db.upsert(m).values(
            session=session, context=json.dumps(context), context_at=at)
        x = stmt.excluded
        stmt = stmt.on_conflict_do_update(
            index_elements=["session"], set_={"context": x.context, "context_at": x.context_at},
            where=(m.c.context_at.is_(None)) | (x.context_at >= m.c.context_at))
        with self.sessions_db.begin() as conn:
            conn.execute(stmt)

    def ask_session_context(self, session: str, at) -> None:
        """A person asked for a new /context reading of the session."""
        stmt = self.sessions_db.upsert(session_meters).values(session=session, context_asked_at=at)
        stmt = stmt.on_conflict_do_update(
            index_elements=["session"], set_={"context_asked_at": stmt.excluded.context_asked_at})
        with self.sessions_db.begin() as conn:
            conn.execute(stmt)

    # ── reads ─────────────────────────────────────────────────────────────────
    def context_asks(self, since) -> list[str]:
        """Sessions a person asked a reading of, since `since`, that have none newer yet."""
        m = session_meters
        with self.sessions_db.begin() as conn:
            rows = conn.execute(select(m.c.session).where(
                m.c.context_asked_at >= since,
                m.c.context_at.is_(None) | (m.c.context_at < m.c.context_asked_at)
            ).order_by(m.c.context_asked_at)).all()
        return [r[0] for r in rows]

    def session_meters(self, sessions: list[str]) -> dict[str, dict]:
        if not sessions:
            return {}
        m = session_meters
        out: dict[str, dict] = {}
        for i in range(0, len(sessions), 500):
            with self.sessions_db.begin() as conn:
                rows = conn.execute(select(
                    m.c.session, m.c.status, m.c.status_at, m.c.context, m.c.context_at,
                    m.c.context_asked_at).where(m.c.session.in_(list(sessions[i:i + 500])))).all()
            for r in rows:
                out[r[0]] = {"status": _json(r[1]), "status_at": r[2], "context": _json(r[3]),
                             "context_at": r[4], "context_asked_at": r[5]}
        return out

    def latest_rate_limits(self) -> tuple | None:
        """(when, rate_limits, person id or None) from the newest status line report that carried
        the plan's limits."""
        m = session_meters
        q = (select(m.c.status_at, m.c.status, m.c.status_person)
             .where(m.c.status.like('%"rate_limits"%'))
             .order_by(m.c.status_at.desc().nulls_last()))
        # the JSON is read here, not in SQL (the two databases spell JSON paths differently); the
        # newest report nearly always carries the limits, so the first batch is the only one read
        for offset in range(0, 10_000, 20):
            with self.sessions_db.begin() as conn:
                rows = conn.execute(q.limit(20).offset(offset)).all()
            for at, status, person in rows:
                limits = (_json(status) or {}).get("rate_limits") if isinstance(_json(status), dict) else None
                if isinstance(limits, dict):
                    return (at, limits, person)
            if len(rows) < 20:
                break
        return None

    @staticmethod
    def _total_cols(t) -> list:
        return [func.count(), func.sum(t.c.input_tokens), func.sum(t.c.output_tokens),
                func.sum(t.c.cache_write_tokens), func.sum(t.c.cache_read_tokens),
                func.sum(t.c.cost_usd), func.count().filter(t.c.cost_usd.is_(None))]

    @staticmethod
    def _totals(r) -> dict:
        return {"replies": int(r[0] or 0), "input_tokens": int(r[1] or 0),
                "output_tokens": int(r[2] or 0), "cache_write_tokens": int(r[3] or 0),
                "cache_read_tokens": int(r[4] or 0), "cost_usd": float(r[5] or 0),
                "unpriced_replies": int(r[6] or 0)}

    def usage_by_session(self, sessions: list[str]) -> dict[str, dict]:
        if not sessions:
            return {}
        u = session_usage
        out: dict[str, dict] = {}
        for i in range(0, len(sessions), 500):
            with self.sessions_db.begin() as conn:
                rows = conn.execute(select(u.c.session, *self._total_cols(u)).where(
                    u.c.session.in_(list(sessions[i:i + 500]))).group_by(u.c.session)).all()
            out.update({r[0]: self._totals(r[1:]) for r in rows})
        return out

    def usage_by_project(self, since=None) -> list[dict]:
        """Replies since `since` (all when None) by the project they count for: the project the
        reply was stamped with, else the station's project (a builder), else the project the
        session worked in. The crew's own stations serve every project, so their unstamped replies
        are the crew's (`kind` crew); anything else unclaimed is `other`."""
        u, ss, ps = session_usage, session_stations, project_sessions
        worked = select(ps.c.session, func.min(ps.c.project).label("project")).group_by(
            ps.c.session).subquery()
        is_crew = ss.c.role.in_(CREW_ROLES)
        q = (select(u.c.session, u.c.input_tokens, u.c.output_tokens, u.c.cache_write_tokens,
                    u.c.cache_read_tokens, u.c.cost_usd,
                    func.coalesce(u.c.project, ss.c.project,
                                  case((is_crew, None), else_=worked.c.project)).label("proj"),
                    is_crew.label("crew"))
             .select_from(u.outerjoin(ss, ss.c.session == u.c.session)
                          .outerjoin(worked, worked.c.session == u.c.session)))
        if since is not None:
            q = q.where(u.c.replied_at >= since)
        r = q.cte("r")
        kind = case((r.c.proj.is_not(None), "project"),
                    (func.coalesce(r.c.crew, false()), "crew"), else_="other")
        totals = [func.count(), func.sum(r.c.input_tokens), func.sum(r.c.output_tokens),
                  func.sum(r.c.cache_write_tokens), func.sum(r.c.cache_read_tokens),
                  func.sum(r.c.cost_usd), func.count().filter(r.c.cost_usd.is_(None))]
        final = (select(r.c.proj, kind.label("kind"), func.count(distinct(r.c.session)), *totals)
                 .group_by(r.c.proj, kind)
                 .order_by(func.sum(r.c.cost_usd).desc().nulls_last()))
        with self.sessions_db.begin() as conn:
            rows = conn.execute(final).all()
        return [{"project": x[0], "kind": x[1], "sessions": int(x[2] or 0), **self._totals(x[3:])}
                for x in rows]
