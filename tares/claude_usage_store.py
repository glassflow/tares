"""Storage for what Claude Code sessions spend, mixed into Store.

Three things arrive through the claude_code source (tares/connectors/claude_code.py):
- the token usage every model reply carries in the transcript, one row per reply;
- the status line's report (a station's context window and the plan's limits), the newest per session;
- a /context reading (the window by category) and the plan's limits from /usage, which the factory
  CLI reads on the crew's machine.
Cost is priced at write time at API list prices (tares/pricing.py): on a subscription it is what the
tokens would cost on the API, a way to compare projects, not a bill.
The usage and meter rows live in the session store (tares/session_store), read and written with
SQLAlchemy Core on `self.sessions_db`.
"""
from __future__ import annotations

import json
from datetime import date

from sqlalchemy import (BigInteger, bindparam, case, distinct, false, func, literal, select, union,
                        union_all, update)

from . import claude_usage_hourly as hourly
from . import session_ingest
from .pricing import cost_usd
from .request_memo import kept
from .session_store.tables import (project_sessions, session_meters, session_stations,
                                   session_usage, session_usage_hourly)

PLAN_SETTING = "claude_plan"          # the plan's limits as /usage last reported them
SYNTHETIC = "<synthetic>"             # the model of Claude Code's own notices: no model call, no tokens
CREW_ROLES = ("orchestrator", "reviewer", "releaser", "comms")
TICKET_ROLES = ("builder", "helper", "reviewer")   # whose replies a ticket's cost can hold


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
    """Methods Store gains for Claude Code usage. Expects self.sessions_db."""

    # ── upgrade and clean-up ──────────────────────────────────────────────────
    def _claude_usage_reprice(self) -> None:
        """At every start: the SYNTHETIC rows are removed, and the rows of a model that had no price
        when they were written are priced now (a newer price list). Every usage row is written as its
        reply is stored (`record_reply_usage`); nothing here reads the session lines."""
        u = session_usage

        def price(model, *tokens):
            return cost_usd(model or "", *(t or 0 for t in tokens))
        with self.sessions_db.begin() as conn:
            hourly.hold(self, conn)
            # every session this transaction changes, found before it writes a row, so that their
            # locks are taken in one ordered pass (TR-1661); the kept hourly totals follow (TR-1632)
            unpriced = conn.execute(select(
                u.c.session, u.c.message, u.c.model, u.c.input_tokens, u.c.output_tokens,
                u.c.cache_write_tokens, u.c.cache_read_tokens, u.c.cache_write_1h_tokens
            ).where(u.c.cost_usd.is_(None), u.c.model.is_not(None))).all()
            priced = [{"b_session": r[0], "b_message": r[1], "b_cost": cost} for r in unpriced
                      if (cost := price(*r[2:])) is not None]
            touched = {p["b_session"] for p in priced}
            touched.update(r[0] for r in conn.execute(
                select(u.c.session).where(u.c.model == SYNTHETIC).distinct()))
            covered = hourly.lock_covered(self, conn, touched)
            conn.execute(u.delete().where(u.c.model == SYNTHETIC))
            if priced:
                conn.execute(update(u).where(u.c.session == bindparam("b_session"),
                                             u.c.message == bindparam("b_message"))
                             .values(cost_usd=bindparam("b_cost")), priced)
            hourly.after_write(self, conn, covered)

    def _usage_forget_project(self, uid: str, private: bool = False) -> None:
        """A project is deleted. A team project's replies stay, counted as the session's own. A
        private one's stay its own, so that no total counts them (TR-1697): those that counted
        for it through the session's link are stamped with it."""
        u, ss, ps = session_usage, session_stations, project_sessions
        if private:
            # as _replies_by_project picked it until now: the lowest project the session is
            # linked to, an earlier deleted private one left out
            earlier = sorted(set(self.deleted_private_projects()) - {uid})
            by_link = select(ps.c.session).where(ps.c.project.not_in(earlier)).group_by(
                ps.c.session).having(func.min(ps.c.project) == uid)
            # a station with a project, and a crew station, do not go by the link
            by_station = select(ss.c.session).where(
                ss.c.project.is_not(None) | ss.c.role.in_(CREW_ROLES))
            change = update(u).where(u.c.project.is_(None), u.c.session.in_(by_link),
                                     u.c.session.not_in(by_station)).values(project=uid)
        else:
            change = update(u).where(u.c.project == uid).values(project=None)
        with self.sessions_db.begin() as conn:
            hourly.hold(self, conn)
            # the sessions it changes, locked first, so that their kept hourly totals follow
            theirs = select(u.c.session).where(change.whereclause).distinct()
            covered = hourly.lock_covered(self, conn, conn.execute(theirs).scalars().all())
            conn.execute(change)
            hourly.after_write(self, conn, covered)

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
        with db.begin() as conn:                     # the reply and its hour's total, one transaction
            hourly.hold(self, conn)
            covered = hourly.lock_covered(self, conn, [session])
            conn.execute(stmt)
            if covered:
                hourly.after_reply(self, conn, session, message)

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
        with self.sessions_db.begin() as conn:       # the reply and its hour's total, one transaction
            hourly.hold(self, conn)
            covered = hourly.lock_covered(self, conn, [session])
            conn.execute(stmt.on_conflict_do_nothing(index_elements=["session", "message"]))
            if covered:
                hourly.after_reply(self, conn, session, message)

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
        with self.sessions_db.read() as conn:
            rows = conn.execute(select(m.c.session).where(
                m.c.context_asked_at >= since,
                m.c.context_at.is_(None) | (m.c.context_at < m.c.context_asked_at)
            ).order_by(m.c.context_asked_at)).all()
        return [r[0] for r in rows]

    @kept
    def session_meters(self, sessions: list[str]) -> dict[str, dict]:
        if not sessions:
            return {}
        m = session_meters
        out: dict[str, dict] = {}
        for i in range(0, len(sessions), 500):
            with self.sessions_db.read() as conn:
                rows = conn.execute(select(
                    m.c.session, m.c.status, m.c.status_at, m.c.context, m.c.context_at,
                    m.c.context_asked_at).where(m.c.session.in_(list(sessions[i:i + 500])))).all()
            for r in rows:
                out[r[0]] = {"status": _json(r[1]), "status_at": r[2], "context": _json(r[3]),
                             "context_at": r[4], "context_asked_at": r[5]}
        return out

    @kept
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
            with self.sessions_db.read() as conn:
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

    def usage_by_session(self, sessions: list[str], hidden=frozenset()) -> dict[str, dict]:
        """What each session's replies add up to. A reply that counts for a project in `hidden`
        (the ones the caller cannot see) is not in it; a session with no other is left out."""
        if not sessions:
            return {}
        u = session_usage
        q = select(u.c.session, *self._total_cols(u)).group_by(u.c.session)
        if hidden:
            replies, seen = self._replies_outside(hidden)
            q = q.select_from(replies).where(seen)
        out: dict[str, dict] = {}
        for i in range(0, len(sessions), 500):
            with self.sessions_db.read() as conn:
                rows = conn.execute(q.where(u.c.session.in_(list(sessions[i:i + 500])))).all()
            out.update({r[0]: self._totals(r[1:]) for r in rows})
        return out

    def sessions_counting_for(self, sessions: list[str], projects) -> set[str]:
        """Those of `sessions` with a reply that counts for one of `projects`."""
        u = session_usage
        replies, project, _ = self._replies_by_project()
        q = select(u.c.session).select_from(replies).where(project.in_(sorted(projects))).distinct()
        out: set[str] = set()
        for i in range(0, len(sessions), 500):
            with self.sessions_db.read() as conn:
                out.update(conn.execute(q.where(u.c.session.in_(list(sessions[i:i + 500])))).scalars())
        return out

    def _sessions_in(self, projects):
        """A query of the sessions that worked in one of `projects` (TR-1704): the station's
        project, any project the session is linked to, or the project one of its replies counts
        for (`_replies_by_project`). Inside another query it goes in as a CTE: it reads the
        station table itself, so as a plain subquery it would be correlated with the outer one."""
        ss, ps, u = session_stations, project_sessions, session_usage
        ids = sorted(projects)
        replies, project, _ = self._replies_by_project()
        return union(select(ss.c.session).where(ss.c.project.in_(ids)),
                     select(ps.c.session).where(ps.c.project.in_(ids)),
                     select(u.c.session).select_from(replies).where(project.in_(ids)))

    def sessions_in(self, sessions: list[str], projects) -> set[str]:
        """Those of `sessions` that worked in one of `projects` (`_sessions_in`). An id the
        sessions database cannot hold (a NUL, on Postgres) worked in none: it has no row there, and
        in the IN() below it would fail the whole read."""
        if not projects:
            return set()
        sessions = [s for s in sessions if session_ingest.holds_session(self, s)]
        found = self._sessions_in(projects).cte("worked_in")
        out: set[str] = set()
        for i in range(0, len(sessions), 500):
            chunk = list(sessions[i:i + 500])
            with self.sessions_db.read() as conn:
                out.update(conn.execute(select(found.c.session).where(
                    found.c.session.in_(chunk))).scalars())
        return out

    def _replies_by_project(self, rows=None):
        """(the replies (`rows`: anything with a session and a stamped project, session_usage
        when None) joined to their station and the project their session worked in, the
        project each reply counts for, whether it is a crew station's). The one definition of
        which project a reply belongs to: the one it was stamped with, else the station's project,
        else, for a session that is no crew station, the (lowest) one it worked in, else none.
        A deleted private project keeps its links, for what they hide, and no new reply counts
        for it through one."""
        u, ss, ps = session_usage if rows is None else rows, session_stations, project_sessions
        deleted = sorted(self.deleted_private_projects())
        worked = select(ps.c.session, func.min(ps.c.project).label("project")).where(
            ps.c.project.not_in(deleted)).group_by(ps.c.session).subquery()
        is_crew = ss.c.role.in_(CREW_ROLES)
        project = func.coalesce(u.c.project, ss.c.project,
                                case((is_crew, None), else_=worked.c.project))
        replies = (u.outerjoin(ss, ss.c.session == u.c.session)
                   .outerjoin(worked, worked.c.session == u.c.session))
        return replies, project, is_crew

    def _replies_outside(self, projects):
        """(`_replies_by_project`'s join, the condition that keeps the replies that count for no
        one of `projects`)."""
        replies, project, _ = self._replies_by_project()
        return replies, self._outside(project, projects)

    @staticmethod
    def _outside(project, projects):
        return project.is_(None) | project.not_in(sorted(projects))

    def _dated_replies(self):
        """(`_replies_by_project`'s join and project, the conditions a reply meets to be counted in
        the report's figures): it has a time, and `usage_by_week`'s exclusion holds for it."""
        replies, project, _ = self._replies_by_project()
        counted = [session_usage.c.replied_at.is_not(None)]
        deleted = self.deleted_private_projects()
        if deleted:
            counted.append(self._outside(project, deleted))
        return replies, project, counted

    def ticket_replies(self) -> tuple[list, set]:
        """What the ticket costs are worked out from (report_cost.py). First, every reply
        `usage_by_week` counts that a builder, helper or reviewer station made, one row each:
        `proj` the project it counts for (None when none), `role`, `station`, `parent` (the
        station a helper works for), `home` (the station's project), `at` and `cost` (None with no
        price). Second, the (station, project) pairs that are builders."""
        u, ss = session_usage, session_stations
        replies, project, counted = self._dated_replies()
        q = select(project.label("proj"), ss.c.role, ss.c.station, ss.c.parent,
                   ss.c.project.label("home"), u.c.replied_at.label("at"),
                   u.c.cost_usd.label("cost")
                   ).select_from(replies).where(ss.c.role.in_(TICKET_ROLES), *counted)
        builders = select(ss.c.station, ss.c.project).where(ss.c.role == "builder").distinct()
        with self.sessions_db.begin() as conn:
            rows = conn.execute(q).all()
            held = conn.execute(builders).all()
        return rows, {(r[0], r[1]) for r in held}

    def usage_by_week(self, since=None, until=None) -> list[dict]:
        """Replies in [since, until) (all when None) grouped by the project they count for (as
        usage_by_project does; None when none), the role of the session's station (None: a
        person's own session) and the ISO week of the reply in UTC (`week`, its Monday as a date).
        `cost_usd` adds the priced replies; `unpriced` counts the rest. A reply with no time is
        in no week, and one of a deleted private project is not read (TR-1697)."""
        u, ss = session_usage, session_stations
        replies, project, counted = self._dated_replies()
        if self.sessions_db.dialect == "postgresql":
            monday = func.to_char(func.date_trunc("week", func.timezone("UTC", u.c.replied_at)),
                                  "YYYY-MM-DD")
        else:   # SQLite keeps the UTC wall time: forward to Sunday, back six days
            monday = func.date(u.c.replied_at, "weekday 0", "-6 days")
        q = select(project.label("proj"), ss.c.role.label("role"), monday.label("week"),
                   u.c.cost_usd).select_from(replies).where(*counted)
        if since is not None:
            q = q.where(u.c.replied_at >= since)
        if until is not None:
            q = q.where(u.c.replied_at < until)
        r = q.cte("w")
        final = select(r.c.proj, r.c.role, r.c.week, func.count(), func.sum(r.c.cost_usd),
                       func.count().filter(r.c.cost_usd.is_(None))
                       ).group_by(r.c.proj, r.c.role, r.c.week)
        with self.sessions_db.read() as conn:
            rows = conn.execute(final).all()
        return [{"project": x[0], "role": x[1], "week": date.fromisoformat(x[2]),
                 "replies": int(x[3]), "cost_usd": float(x[4] or 0), "unpriced": int(x[5])}
                for x in rows]

    def usage_by_project(self, since=None) -> list[dict]:
        """Replies since `since` (all when None) by the project they count for: the project the
        reply was stamped with, else the station's project (a builder), else the project the
        session worked in. The crew's own stations serve every project, so their unstamped replies
        are the crew's (`kind` crew); anything else unclaimed is `other`."""
        u, h = session_usage, session_usage_hourly
        if hourly.ready(self):
            # the kept hourly totals from the first whole hour of the window on, and the replies of
            # the partial hour before it from the rows (TR-1632); one query, one snapshot
            parts = []
            first = hourly.first_whole_hour(since) if since is not None else None
            b = select(h.c.session, h.c.replies, h.c.input_tokens, h.c.output_tokens,
                       h.c.cache_write_tokens, h.c.cache_read_tokens, h.c.cost_usd, h.c.unpriced,
                       func.nullif(h.c.project, "").label("project"))
            parts.append(b if first is None else b.where(h.c.hour >= first))
            if since is not None and first > since:
                parts.append(select(
                    u.c.session, literal(1, BigInteger), u.c.input_tokens, u.c.output_tokens,
                    u.c.cache_write_tokens, u.c.cache_read_tokens, u.c.cost_usd,
                    case((u.c.cost_usd.is_(None), 1), else_=0), u.c.project)
                    .where(u.c.replied_at >= since, u.c.replied_at < first))
            src = (parts[0] if len(parts) == 1 else union_all(*parts)).subquery("src")
            replies, project, is_crew = self._replies_by_project(src)
            q = select(src.c.session, src.c.replies, src.c.input_tokens, src.c.output_tokens,
                       src.c.cache_write_tokens, src.c.cache_read_tokens, src.c.cost_usd,
                       src.c.unpriced, project.label("proj"),
                       is_crew.label("crew")).select_from(replies)
            r = q.cte("r")
            n_replies, n_unpriced = func.sum(r.c.replies), func.sum(r.c.unpriced)
        else:
            replies, project, is_crew = self._replies_by_project()
            q = select(u.c.session, u.c.input_tokens, u.c.output_tokens, u.c.cache_write_tokens,
                       u.c.cache_read_tokens, u.c.cost_usd, project.label("proj"),
                       is_crew.label("crew")).select_from(replies)
            if since is not None:
                q = q.where(u.c.replied_at >= since)
            r = q.cte("r")
            n_replies, n_unpriced = func.count(), func.count().filter(r.c.cost_usd.is_(None))
        kind = case((r.c.proj.is_not(None), "project"),
                    (func.coalesce(r.c.crew, false()), "crew"), else_="other")
        totals = [n_replies, func.sum(r.c.input_tokens), func.sum(r.c.output_tokens),
                  func.sum(r.c.cache_write_tokens), func.sum(r.c.cache_read_tokens),
                  func.sum(r.c.cost_usd), n_unpriced]
        final = (select(r.c.proj, kind.label("kind"), func.count(distinct(r.c.session)), *totals)
                 .group_by(r.c.proj, kind)
                 .order_by(func.sum(r.c.cost_usd).desc().nulls_last()))
        with self.sessions_db.read() as conn:
            rows = conn.execute(final).all()
        return [{"project": x[0], "kind": x[1], "sessions": int(x[2] or 0), **self._totals(x[3:])}
                for x in rows]
