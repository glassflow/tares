"""The store: every read and write of the cell, in the session store (P-TR-295, Tares 2.0).

One database holds it all: `tares.sqlite` in the data directory, or the cell's Postgres when
TARES_STATE_DSN is set (`tares.session_store`). Its tables are SQLAlchemy Core definitions
(`session_store/tables.py`) made by its Alembic revisions. All reads and writes go through here; the
MCP server never touches the database directly.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import threading
import time
import uuid
from datetime import timedelta

from .envelope import Envelope, now_utc
from .claude_usage_store import ClaudeUsageStore
from .factory import DONE_STATUSES, OPEN_STAGES, state_of
from .factory_store import FactoryStore
from .request_memo import kept

from sqlalchemy import (Date, LargeBinary, Text, and_, case, cast, delete, func, insert, literal, null,
                        or_, select, text, union_all, update)

from . import session_ingest, session_read
from .session_store import SessionStore
from .session_store.engine import MEMORY, SQLITE_FILE
from .session_store.pr_columns import fill_pr_columns, pr_columns
from .session_store.tables import (merge_records, project_sessions, report_texts,
                                   report_weeks, session_stations, session_states)
from .session_store.tables import settings as settings_table
from .session_store.tables import api_keys as api_keys_table
from .session_store.tables import people as people_table
from .session_store.tables import project_members as members_table
from .session_store.tables import usecases as projects_table
from .session_store.tables import usecase_objects as objects_table
from .session_store.tables import usecase_log as plog_table
from .session_store.tables import docs as docs_table
from .session_store.tables import skills as skills_table
from .session_store.tables import shared_skills as shared_skills_table
from .session_store.tables import github_credentials as github_table
from .session_store.tables import model_usage as usage_table
from .session_store.tables import tickets as tickets_table
from .session_store.tables import pending_writeback as writeback_table
from .session_store.tables import session_archive_parts, session_files, session_said
from .session_store.tables import sessions as sessions_table

# the setting that remembers the deleted private projects (Store.deleted_private_projects)
_DELETED_PRIVATE = "deleted_private_projects"
_DELETED_PRIVATE_LOCK = threading.Lock()   # two deletes at once must not lose one


def _name_digest(name: str) -> str:
    return hashlib.sha256((name or "").encode()).hexdigest()


def _digest_of(record) -> str:
    """The name's digest in one deleted private project's record (Store._deleted_records)."""
    return record["name"] if isinstance(record, dict) else record


class ReadExclude(dict):
    """One request's `exclude` ({"ids": [hidden project ids], "names": [their names], "shared":
    [...]}): the same dict, with room for what a reader works out from it, so a request that reads
    several times looks each thing up once. `records` is
    the deleted private projects' records as read when the dict was built (None: look them up). It
    belongs to the request that built it: nothing here is kept for the next one."""

    def __init__(self, exclude: dict, records: dict | None = None):
        super().__init__(exclude)
        self.records = records
        self.built: dict = {}


def _file_size(path: str) -> int:
    """Bytes on disk, 0 if the file isn't there (an in-memory db, or a WAL that's checkpointed)."""
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September",
           "October", "November", "December")


def month_name(when) -> str:
    """"October" for a date or datetime in October (English whatever the machine's locale)."""
    return _MONTHS[when.month - 1]


def day_month_words(when) -> str:
    """"1 Nov": the day a monthly budget starts again."""
    return f"{when.day} {_MONTHS[when.month - 1][:3]}"


class StoreUnavailable(RuntimeError):
    """The database could not be opened or initialized: a Postgres that does not answer or refuses
    the login, a file that cannot be opened, a full disk. Raised instead of the raw error so taresd
    can start in degraded mode (serve the console and explain itself) rather than exit with a
    traceback."""

    def __init__(self, reason: str, path: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.path = path


# How long one measure of the store's size (`Store.disk_bytes`) is used again: /health, every
# ingest and every scrape ask for it, and on Postgres it is a query
SIZE_FOR = 5.0


class Store(FactoryStore, ClaudeUsageStore):
    def __init__(self, data_dir: str = ".", state_dsn: str | None = None):
        # Everything is in the session store: `tares.sqlite` in `data_dir`, or the Postgres of
        # `state_dsn`. A read is the session store's own read transaction and takes no lock
        # (`_sread`). A write takes `_swrite`, one lock in this process, so a step that reads and
        # then writes, in one transaction or several, is not run in between another one's; between
        # processes the database decides (BEGIN IMMEDIATE on SQLite, the numbering locks on Postgres).
        self._sread = contextlib.nullcontext()
        self._swrite = threading.Lock()
        # held by append across a batch's write to the session store, so a refused batch is taken
        # back before the next one is numbered
        self._sess_lock = threading.Lock()
        self._challenges_unreadable: set = set()    # challenger lines already logged as unreadable
        self._size: tuple | None = None             # (when, the sizes) of the last measure
        self.data_dir = data_dir                    # ":memory:" for a store with no file
        self.state_dsn = state_dsn
        where = "the state database" if state_dsn else (
            "memory" if data_dir == MEMORY else os.path.join(os.path.abspath(data_dir), SQLITE_FILE))
        try:
            if data_dir != MEMORY and not state_dsn:
                os.makedirs(data_dir, exist_ok=True)
            self.sessions_db = SessionStore(
                MEMORY if data_dir == MEMORY else os.path.abspath(data_dir), state_dsn or None)
            self.sessions_db.upgrade()
            self._upgrade()
        except Exception as e:
            raise StoreUnavailable(f"cannot open the database in {where}: {e}", where) from e

    def close(self) -> None:
        """Let go of the session store's connections, after the ten minute pass has stopped."""
        stop = getattr(self, "_reconcile_stop", None)
        if stop is not None:
            stop.set()          # the ten minute pass (session_ingest._cycle) ends
        th = getattr(self, "_reconcile_thread", None)
        if th is not None and th is not threading.current_thread():
            th.join(60)
        sessions_db = getattr(self, "sessions_db", None)
        if sessions_db is not None:
            sessions_db.close()

    def _upgrade(self) -> None:
        """The data upgrades every open runs; each is idempotent."""
        self._skills_upgrade()
        self._factory_kind_upgrade()
        self._factory_upgrade()
        self._ticket_pr_columns()
        self._people_upgrade()
        self._claude_usage_reprice()

    def _ticket_pr_columns(self) -> None:
        """tickets.pr_repo and pr_number say what `pr` says (TR-1614). A release before them writes
        `pr` and knows nothing of the two columns (a cell rolled back and up again), so every start
        repairs the rows that differ."""
        with self._swrite, self.sessions_db.begin() as conn:
            fill_pr_columns(conn)

    def _people_upgrade(self) -> None:
        """The person `local` always exists: the one person of a cell with auth off, and the one
        an unbound key acts as while nobody else is on the cell."""
        with self.sessions_db.begin() as conn:
            conn.execute(self.sessions_db.upsert(people_table).values(
                id="local", name="You", email=None, emails=[], github=None, is_admin=True,
                created_at=now_utc(), created_via="boot").on_conflict_do_nothing(
                index_elements=["id"]))

    def _skills_upgrade(self) -> None:
        """Once per database: the skills each project kept become skills of the cell that those
        projects use. Two projects' skills of the same name and text are one skill; the same name
        with different text keeps both, the second named name-2 (and so on). All of it is in the
        session store, in one transaction."""
        if self.get_setting("skills_shared") is not None:
            return
        k, s, o = skills_table, shared_skills_table, objects_table
        with self.sessions_db.begin() as conn:
            for project, name, desc, body, c, u in conn.execute(
                    select(k.c.project, k.c.name, k.c.description, k.c.body, k.c.created_at,
                           k.c.updated_at).order_by(k.c.created_at, k.c.project)).all():
                use, n = name, 2
                while True:
                    have = conn.execute(select(s.c.body).where(s.c.name == use)).first()
                    if have is None or have[0] == body:
                        break
                    use, n = f"{name}-{n}", n + 1
                if have is None:
                    conn.execute(s.insert().values(name=use, description=desc, body=body,
                                                   made_by=project, created_at=c, updated_at=u))
                if use != name:   # the project's own row follows the new name
                    conn.execute(o.update().where(
                        (o.c.usecase_id == project) & (o.c.kind == "skill") & (o.c.name == name)
                    ).values(name=use))
                if conn.execute(select(o.c.name).where(
                        (o.c.usecase_id == project) & (o.c.kind == "skill") & (o.c.name == use))).first() is None:
                    conn.execute(o.insert().values(usecase_id=project, kind="skill", key=f"skill:{use}",
                                                   name=use, customized=False, created_at=c or now_utc()))
        self._set_setting_once("skills_shared", "1")      # after the commit: see _wiring_upgrade

    def projects_using(self, kind: str, name: str) -> list[str]:
        """The projects whose object list has this part (any kind), oldest membership first."""
        o = objects_table
        with self.sessions_db.read() as conn:
            return list(conn.execute(
                select(o.c.usecase_id).where(o.c.kind == kind, o.c.name == name)
                .group_by(o.c.usecase_id).order_by(func.min(o.c.created_at))).scalars())

    def ping(self) -> None:
        """Cheapest possible liveness probe for /health: one SELECT 1 in the session store. Raises
        whatever the database raises when it has gone away underneath us."""
        with self.sessions_db.read() as conn:
            conn.execute(text("SELECT 1")).scalar()

    def _sizes(self) -> dict:
        """{"db": the store's own bytes (the SQLite file, or the Postgres database), "wal": the
        SQLite WAL, "archive": the bytes of the archived parts, "lines": the lines the sessions
        hold}. Measured at most once every SIZE_FOR seconds; nothing here scans `session_lines`."""
        now = time.monotonic()
        if self._size is not None and now - self._size[0] < SIZE_FOR:
            return self._size[1]
        db = self.sessions_db
        with db.read() as conn:
            if db.dialect == "postgresql":
                own = int(conn.execute(text("SELECT pg_database_size(current_database())")).scalar_one())
                wal = 0
            elif self.data_dir == MEMORY:
                own = wal = 0
            else:
                f = os.path.join(os.path.abspath(self.data_dir), SQLITE_FILE)
                own, wal = _file_size(f), _file_size(f + "-wal")
            archive = int(conn.execute(select(func.coalesce(func.sum(
                session_archive_parts.c.bytes), 0))).scalar_one())
            lines = int(conn.execute(select(func.coalesce(func.sum(
                sessions_table.c.line_count), 0))).scalar_one())
        out = {"db": own, "wal": wal, "archive": archive, "lines": lines}
        self._size = (now, out)
        return out

    def disk_bytes(self) -> int:
        """What the cell stores, measured against its limit: the session store (the SQLite file and
        its WAL, or the Postgres database) plus the archive."""
        z = self._sizes()
        return z["db"] + z["wal"] + z["archive"]

    # ── ingest ──────────────────────────────────────────────────────────────
    def append(self, envelopes: list[Envelope]) -> None:
        """Store a batch: its claude_code lines in the session store (`session_ingest.record_first`,
        which raises `session_ingest.LinesNotStored` when it took none of them), and the
        challenger's lines in `challenges`. A line of no session is not kept."""
        if not envelopes:
            return
        cc = session_ingest.claude_code_lines(self, envelopes)
        if cc:
            with self._sess_lock:
                try:
                    session_ingest.record_first(self, cc)
                except session_ingest.LinesNotStored:
                    # a challenger line is sent once, with no retry, so its verdict is still
                    # written (a repeat is skipped). While the store takes no write it is lost,
                    # said in the log and counted: accepted (decision A9)
                    self._keep_challenges(envelopes)
                    raise
        self._keep_challenges(envelopes)

    def start_session_reconcile(self):
        """Start the ten minute pass over the session store (`session_ingest.start_reconcile`)."""
        return session_ingest.start_reconcile(self)

    def _keep_challenges(self, envelopes) -> None:
        # the challenger's lines go to `challenges`: after the batch's lines were stored, or when
        # the batch was refused. One that cannot be written is lost: the challenger sends a line
        # once, so it is said in the log and counted (`tares_challenges_lost_total`).
        ch = [(e.source, e.key_value or "", e.event_time, e.event_type,
               str((e.labels or {}).get("branch") or ""),
               None if (e.labels or {}).get("tares_project") is None
               else str(e.labels["tares_project"]),
               json.dumps(e.payload)) for e in envelopes if e.event_type in self.CHALLENGE_TYPES]
        if ch:
            try:
                with self._swrite:
                    self._record_challenges(ch)
            except Exception as ex:   # never fail an ingest, or hide why one was refused, for this
                self._challenges_not_kept(ch, ex)

    def projects_of_sessions(self, sessions: list[str]) -> set[str]:
        """The projects any of the sessions is linked to."""
        out: set[str] = set()
        for i in range(0, len(sessions), 500):
            with self.sessions_db.read() as conn:
                out.update(r[0] for r in conn.execute(
                    select(project_sessions.c.project).where(
                        project_sessions.c.session.in_(sessions[i:i + 500]))))
        return out

    # ── model-usage ledger (the cell's Anthropic spend meter) ─────────────────
    def record_model_usage(self, surface: str, agent: str, run_id: str, model: str, calls: int,
                           input_tokens: int, output_tokens: int,
                           cache_creation_input_tokens: int = 0,
                           cache_read_input_tokens: int = 0,
                           cost_usd: float | None = None,
                           key_source: str | None = None,
                           provider: str | None = None) -> None:
        with self._swrite, self.sessions_db.begin() as conn:
            conn.execute(insert(usage_table).values(
                id="mu_" + uuid.uuid4().hex[:12], ts=now_utc(), surface=surface, agent=agent,
                run_id=run_id, model=model, calls=calls, input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_creation_input_tokens=cache_creation_input_tokens,
                cache_read_input_tokens=cache_read_input_tokens, cost_usd=cost_usd,
                key_source=key_source or None, provider=provider or None))
        from . import metrics
        metrics.model_usage(provider, surface, calls, input_tokens, output_tokens, cost_usd)

    def model_usage_summary(self, days: int = 30) -> dict:
        """All-time totals plus a per-day tail over the ledger. The shape a credits poller needs:
        totals to enforce against, days to draw a burn-down. `uncosted_calls` counts rows whose
        model had no known price, so the cost total is understood as a floor."""
        u = usage_table
        agg = [func.coalesce(func.sum(u.c.calls), 0), func.coalesce(func.sum(u.c.input_tokens), 0),
               func.coalesce(func.sum(u.c.output_tokens), 0),
               func.coalesce(func.sum(u.c.cache_creation_input_tokens), 0),
               func.coalesce(func.sum(u.c.cache_read_input_tokens), 0), func.sum(u.c.cost_usd),
               func.coalesce(func.sum(u.c.calls).filter(u.c.cost_usd.is_(None)), 0)]

        def shape(r) -> dict:
            return {"calls": int(r[0]), "input_tokens": int(r[1]), "output_tokens": int(r[2]),
                    "cache_creation_input_tokens": int(r[3]),
                    "cache_read_input_tokens": int(r[4]),
                    "cost_usd": float(r[5]) if r[5] is not None else None,
                    "uncosted_calls": int(r[6])}

        key_source = func.coalesce(u.c.key_source, "unknown")
        provider = func.coalesce(u.c.provider, "anthropic")
        # the day a row was written, in UTC
        day = (cast(func.timezone("UTC", u.c.ts), Date) if self.sessions_db.dialect == "postgresql"
               else func.date(u.c.ts))
        since = now_utc() - timedelta(days=int(days))
        with self.sessions_db.read() as conn:
            total = conn.execute(select(*agg)).one()
            surfaces = conn.execute(select(u.c.surface, *agg).group_by(u.c.surface)).all()
            # Which key paid: the boundary a hosted trial is enforced against ('unknown' holds
            # rows from before attribution existed).
            key_sources = conn.execute(select(key_source, *agg).group_by(key_source)).all()
            # Which provider served it: rows from before providers existed were all Anthropic.
            providers = conn.execute(select(provider, *agg).group_by(provider)).all()
            daily = conn.execute(select(day.label("day"), *agg).where(u.c.ts > since)
                                 .group_by(day).order_by(day)).all()
        return {
            "total": shape(total),
            "by_surface": {r[0]: shape(r[1:]) for r in surfaces},
            "by_key_source": {r[0]: shape(r[1:]) for r in key_sources},
            "by_provider": {r[0]: shape(r[1:]) for r in providers},
            "days": [{"day": str(r[0]), **shape(r[1:])} for r in daily],
            "window_days": int(days),
        }

    # ── settings (instance config set from the console) ───────────────────────
    # `settings` lives in the session store (TR-1480). These methods take no lock, so they are safe
    # from inside one.
    @kept
    def get_setting(self, key: str) -> str | None:
        with self.sessions_db.read() as conn:
            return conn.execute(select(settings_table.c.value).where(
                settings_table.c.key == key)).scalar_one_or_none()

    def set_setting(self, key: str, value: str | None) -> None:
        with self.sessions_db.begin() as conn:
            if value is None:
                conn.execute(delete(settings_table).where(settings_table.c.key == key))
                return
            stmt = self.sessions_db.upsert(settings_table).values(
                key=key, value=value, updated_at=now_utc())
            conn.execute(stmt.on_conflict_do_update(
                index_elements=["key"],
                set_={"value": stmt.excluded.value, "updated_at": stmt.excluded.updated_at}))

    def _set_setting_once(self, key: str, value: str) -> None:
        """Set `key` unless it already has a value: the marker of a one-time upgrade."""
        with self.sessions_db.begin() as conn:
            conn.execute(self.sessions_db.upsert(settings_table).values(
                key=key, value=value, updated_at=now_utc()).on_conflict_do_nothing(
                index_elements=["key"]))

    # ── projects ──
    def create_project(self, uid: str, template: str, name: str, params: dict,
                       status: str = "active", goal: str | None = None,
                       lead_person_id: str | None = None, icon: str | None = None) -> None:
        ts = now_utc()
        with self._swrite, self.sessions_db.begin() as conn:
            conn.execute(insert(projects_table).values(
                id=uid, recipe=template, name=name, params=params, status=status, created_at=ts,
                updated_at=ts, last_error=None, goal=goal or None,
                lead_person_id=lead_person_id or None, scope="team", icon=icon or None))
        # an id made again (a setup that was undone puts its draft back) is no deleted project
        self._change_deleted_private(lambda d: d.pop(uid, None))

    def update_project(self, uid: str, params: dict | None = None, status: str | None = None,
                       last_error: str | None = "", name: str | None = None,
                       goal: str | None = "") -> None:
        """last_error and goal: "" (default) leaves it unchanged; None clears it; a string sets
        it."""
        vals: dict = {"updated_at": now_utc()}
        if goal != "":
            vals["goal"] = goal
        if params is not None:
            vals["params"] = params
        if status is not None:
            vals["status"] = status
        if name is not None:
            vals["name"] = name
        if last_error != "":
            vals["last_error"] = last_error
        with self._swrite, self.sessions_db.begin() as conn:
            conn.execute(update(projects_table).where(projects_table.c.id == uid).values(**vals))

    @staticmethod
    def _project_row(r) -> dict:
        return {"id": r[0], "template": r[1], "name": r[2], "params": r[3] or {},
                "status": r[4], "created_at": r[5], "updated_at": r[6], "last_error": r[7],
                "goal": r[8], "kind": r[9], "lead_person_id": r[10],
                "scope": r[11] or "team", "icon": r[12]}

    @staticmethod
    def _project_cols():
        t = projects_table
        return (t.c.id, t.c.recipe, t.c.name, t.c.params, t.c.status, t.c.created_at,
                t.c.updated_at, t.c.last_error, t.c.goal, t.c.kind, t.c.lead_person_id, t.c.scope,
                t.c.icon)

    @kept
    def list_projects(self, *, kind=None, status_in=None, status_not_in=None, ids=None,
                      exclude_ids=None) -> list[dict]:
        """Every project, oldest first, or only those that match: `kind`, a status in or not in a
        list (a project with no status is not in any list), `ids` (an empty list gives none) and
        `exclude_ids`. With no argument it is every project."""
        if ids is not None and not list(ids):
            return []
        t = projects_table
        where = []
        if kind is not None:
            where.append(t.c.kind == kind)
        if status_in is not None:
            vals = list(status_in)
            if not vals:
                return []
            where.append(t.c.status.in_(vals))
        if status_not_in:
            where.append(or_(t.c.status.is_(None), t.c.status.not_in(list(status_not_in))))
        if ids is not None:
            where.append(t.c.id.in_(list(ids)))
        if exclude_ids:
            where.append(t.c.id.not_in(list(exclude_ids)))
        with self.sessions_db.read() as conn:
            rows = conn.execute(select(*self._project_cols()).where(*where).order_by(
                t.c.created_at, t.c.id)).all()
        return [self._project_row(r) for r in rows]

    @kept
    def project_names(self) -> dict[str, str]:
        """{project id: name} for every project, in one query."""
        with self.sessions_db.read() as conn:
            rows = conn.execute(select(projects_table.c.id, projects_table.c.name)).all()
        return {r[0]: r[1] for r in rows}

    # Licence-independent by design (TR-915): never consult licence or team here.
    @kept
    def private_projects(self) -> list[dict]:
        """The private projects, as {id, name, scope}: the one small query every cell-wide read
        runs to learn whether it has anything to hide (usually it has not)."""
        with self.sessions_db.read() as conn:
            rows = conn.execute(select(projects_table.c.id, projects_table.c.name).where(
                projects_table.c.scope == "private")).all()
        return [{"id": r[0], "name": r[1], "scope": "private"} for r in rows]

    # ── private projects that were deleted (TR-1697) ──────────────────────────
    # Their cost counts in no total and what they recorded stays unread (TR-1708), so Tares has
    # to remember which they were: {id: {name: a digest of the name, at: when it was deleted,
    # sources: the ones only it had}}. A record from before that is the digest alone. The digest
    # is only there to know a line stamped with the name.
    def _deleted_records(self) -> dict:
        return json.loads(self.get_setting(_DELETED_PRIVATE) or "{}")

    def deleted_private_projects(self) -> dict[str, str]:
        """{id: the digest of the name} of the deleted private projects."""
        return {uid: _digest_of(r) for uid, r in self._deleted_records().items()}

    def _change_deleted_private(self, change) -> None:
        with _DELETED_PRIVATE_LOCK:
            have = self._deleted_records()
            new = dict(have)
            change(new)
            if new != have:
                self.set_setting(_DELETED_PRIVATE, json.dumps(new, sort_keys=True) if new else None)

    # Licence-independent by design (TR-915): never consult licence or team here.
    def private_and_deleted(self) -> tuple[list[dict], set[str]]:
        """`private_projects()` and the ids of the deleted private projects, in one query."""
        private, deleted, _ = self.private_and_records()
        return private, deleted

    # Licence-independent by design (TR-915): never consult licence or team here.
    @kept
    def private_and_records(self) -> tuple[list[dict], set[str], dict]:
        """`private_and_deleted()` and the records it read them from (`_deleted_records`)."""
        q = union_all(
            select(projects_table.c.id, projects_table.c.name).where(
                projects_table.c.scope == "private"),
            select(null(), settings_table.c.value).where(settings_table.c.key == _DELETED_PRIVATE))
        with self.sessions_db.read() as conn:
            rows = conn.execute(q).all()
        private = [{"id": r[0], "name": r[1], "scope": "private"} for r in rows if r[0] is not None]
        records = next((json.loads(r[1]) for r in rows if r[0] is None), {})
        # one that is still there (its delete stopped half way) is still its members' to see
        return private, set(records) - {p["id"] for p in private}, records

    @staticmethod
    def deleted_private_ref(ref: str, deleted: dict[str, str]) -> str | None:
        """The one of `deleted` (`deleted_private_projects`) that `ref`, an id or a name, stood
        for."""
        if ref in deleted:
            return ref
        digest = _name_digest(ref)
        return next((uid for uid, d in deleted.items() if d == digest), None)

    def usage_project(self, ref: str, session: str = "") -> str | None:
        """The project a recorded line's `tares_project` (an id or a name) stands for: the one
        with that id; else the deleted private one of that name the line's `session` is tied to;
        else the one of that name there is; else the deleted private one it named."""
        p = self.get_project(ref)
        if p:
            return p["id"]
        deleted = self.deleted_private_projects()
        digest = _name_digest(ref)
        named = {uid for uid, d in deleted.items() if d == digest}
        tied = named & set(self.session_projects(session)) if named and session else set()
        if tied:
            return min(tied)
        p = self.get_project_by_name(ref)
        return p["id"] if p else self.deleted_private_ref(ref, deleted)

    @kept
    def get_project(self, uid: str) -> dict | None:
        with self.sessions_db.read() as conn:
            r = conn.execute(select(*self._project_cols()).where(
                projects_table.c.id == uid)).first()
        return self._project_row(r) if r else None

    # ── who a project belongs to: its lead, its scope and its members (TR-623) ──
    def set_project_lead(self, uid: str, person_id: str | None) -> None:
        with self._swrite, self.sessions_db.begin() as conn:
            conn.execute(update(projects_table).where(projects_table.c.id == uid).values(
                lead_person_id=person_id or None))

    def set_project_icon(self, uid: str, icon: str | None) -> None:
        """Set or clear a project's icon. Not `updated_at`: an icon is not activity on the project,
        and the Projects list sorts by activity."""
        with self._swrite, self.sessions_db.begin() as conn:
            conn.execute(update(projects_table).where(projects_table.c.id == uid).values(
                icon=icon or None))

    def set_project_scope(self, uid: str, scope: str) -> None:
        if scope not in ("team", "private"):
            raise ValueError("scope is team or private")
        with self._swrite, self.sessions_db.begin() as conn:
            conn.execute(update(projects_table).where(projects_table.c.id == uid).values(
                scope=scope))

    # The member list lives in the session store (TR-1608), like settings: no lock
    # is taken, so these are safe from inside one.
    def add_project_member(self, uid: str, person_id: str, added_by: str | None = None,
                           reason: str = "manual") -> bool:
        """Put a person on the project. True when the row was written: a new member, or a person
        who had joined by themselves and is now added (`manual` or `ticket`). Otherwise False,
        and a second add changes nothing: the first reason stays."""
        if reason not in ("manual", "ticket", "self"):
            raise ValueError("reason is manual, ticket or self")
        t = members_table
        row = {"added_by": added_by or None, "added_at": now_utc(), "reason": reason}
        stmt = self.sessions_db.upsert(t).values(project=uid, person_id=person_id, **row)
        key = [t.c.project, t.c.person_id]
        if reason == "self":
            stmt = stmt.on_conflict_do_nothing(index_elements=key)
        else:       # one statement, so an add racing a join still ends as an added member
            stmt = stmt.on_conflict_do_update(index_elements=key, set_=row, where=t.c.reason == "self")
        with self.sessions_db.begin() as conn:
            # RETURNING, not rowcount: Postgres reports -1 for an insert that skipped on conflict
            return conn.execute(stmt.returning(t.c.person_id)).first() is not None

    def remove_project_member(self, uid: str, person_id: str) -> bool:
        """True when they were a member (a stored one; the lead is not)."""
        with self.sessions_db.begin() as conn:
            done = conn.execute(delete(members_table).where(
                members_table.c.project == uid, members_table.c.person_id == person_id))
        return done.rowcount > 0

    @staticmethod
    def _member_row(r) -> dict:
        return {"person_id": r[0], "added_by": r[1], "added_at": r[2], "reason": r[3]}

    # Which rows of project_members count, for every read of them: someone who joined by
    # themselves is a member while the project is a team project, and not once it is private.
    _MEMBER_ROW = or_(func.coalesce(members_table.c.reason, "") != "self",
                      ~select(projects_table.c.id).where(
                          projects_table.c.id == members_table.c.project,
                          projects_table.c.scope == "private").exists())

    def list_project_members(self, uid: str) -> list[dict]:
        """The people on the project besides its lead, oldest first: {person_id, added_by,
        added_at, reason}."""
        t = members_table
        with self.sessions_db.read() as conn:
            rows = conn.execute(select(t.c.person_id, t.c.added_by, t.c.added_at, t.c.reason).where(
                t.c.project == uid, self._MEMBER_ROW).order_by(t.c.added_at, t.c.person_id)).all()
        return [self._member_row(r) for r in rows]

    def members_by_project(self, uids: list[str]) -> dict[str, list[dict]]:
        """{project: its members as list_project_members gives them} for `uids`, in one query."""
        ids = sorted({u for u in uids if u})
        if not ids:
            return {}
        t = members_table
        with self.sessions_db.read() as conn:
            rows = conn.execute(select(t.c.project, t.c.person_id, t.c.added_by, t.c.added_at,
                                       t.c.reason).where(t.c.project.in_(ids), self._MEMBER_ROW)
                                .order_by(t.c.added_at, t.c.person_id)).all()
        out: dict[str, list[dict]] = {}
        for r in rows:
            out.setdefault(r[0], []).append(self._member_row(r[1:]))
        return out

    # Licence-independent by design (TR-915): never consult licence or team here.
    def is_project_member(self, uid: str, person_id: str | None) -> bool:
        """The project's lead, or a person on its member list. Someone who joined by themselves
        counts while the project is a team project."""
        return self._leads_or_on(uid, person_id, self._MEMBER_ROW)

    # Licence-independent too: what a person who joined by themselves does not get.
    def is_added_member(self, uid: str, person_id: str | None) -> bool:
        """The project's lead, or a member someone put on it (`manual` or `ticket`), as opposed to
        one who joined by themselves. This is who gets the project's secrets."""
        return self._leads_or_on(uid, person_id, func.coalesce(members_table.c.reason, "") != "self")

    def _leads_or_on(self, uid: str, person_id: str | None, counts) -> bool:
        """The person is the project's lead, or has a member row that `counts`."""
        if not person_id:
            return False
        with self.sessions_db.read() as conn:
            lead = conn.execute(select(projects_table.c.lead_person_id).where(
                projects_table.c.id == uid)).first()
            if lead and lead[0] == person_id:
                return True
            return conn.execute(select(members_table.c.person_id).where(
                members_table.c.project == uid, members_table.c.person_id == person_id, counts)
            ).first() is not None

    def set_project_kind(self, uid: str, kind: str | None) -> None:
        with self._swrite, self.sessions_db.begin() as conn:
            conn.execute(update(projects_table).where(projects_table.c.id == uid).values(
                kind=kind or None))

    def _factory_kind_upgrade(self) -> None:
        """Once: a project from before project kinds that holds docs and tickets and no agent was
        made by a spec session; it is a software factory project. Lock not needed (init)."""
        if self.get_setting("factory_kind_filled") is not None:
            return
        t, o = projects_table, objects_table
        with self.sessions_db.read() as conn:
            custom = set(conn.execute(select(t.c.id).where(
                t.c.kind.is_(None), t.c.recipe == "custom")).scalars())
            with_docs = set(conn.execute(select(o.c.usecase_id).where(o.c.kind == "doc")).scalars())
            with_agents = set(conn.execute(select(o.c.usecase_id).where(
                o.c.kind == "agent")).scalars())
            ticketed = (set(conn.execute(select(tickets_table.c.project).distinct()).scalars())
                        if custom else set())
        if custom:
            hit = sorted((custom & ticketed & with_docs) - with_agents)
            if hit:
                with self.sessions_db.begin() as conn:
                    conn.execute(update(t).where(t.c.id.in_(hit), t.c.kind.is_(None)).values(
                        kind="software_factory"))
        self._set_setting_once("factory_kind_filled", "1")

    def get_project_by_name(self, name: str) -> dict | None:
        return next((u for u in self.list_projects() if u["name"] == name), None)

    def delete_project(self, uid: str) -> None:
        milestones = [m["id"] for m in self.list_milestones(uid)]
        gone = self.get_project(uid)
        private = bool(gone) and gone["scope"] == "private"
        if private:   # remembered before anything goes, so its cost is never counted in between
            at = now_utc().isoformat()

            def remember(d):
                had = d.get(uid)   # an earlier delete (before TR-1811) may have named sources
                had = had.get("sources", []) if isinstance(had, dict) else []
                d[uid] = {"name": _name_digest(gone["name"]), "at": at, "sources": sorted(had)}
            self._change_deleted_private(remember)
        with self._swrite:
            # the state rows first: a delete that stops half way leaves session rows with no
            # project, which every reader ignores
            # its keys stop working (TR-335)
            # (the keys, members and the project's own rows are in the session store, a second
            # database: a crash between the two leaves a project whose keys are revoked and whose
            # rows remain, and a second delete finishes it; the project row goes last)
            with self.sessions_db.begin() as conn:
                conn.execute(update(api_keys_table).where(
                    api_keys_table.c.project == uid, api_keys_table.c.revoked_at.is_(None)
                ).values(revoked_at=now_utc()))
            with self.sessions_db.begin() as conn:
                conn.execute(delete(objects_table).where(objects_table.c.usecase_id == uid))
            with self.sessions_db.begin() as conn:
                conn.execute(delete(members_table).where(members_table.c.project == uid))
            self._forget_unused_skills()   # a skill another project uses stays
            self._forget_unused_docs()     # so does a doc
            with self.sessions_db.begin() as conn:
                # the write-backs waiting for its tickets go before the tickets that name them
                conn.execute(delete(writeback_table).where(writeback_table.c.ticket.in_(
                    select(tickets_table.c.id).where(tickets_table.c.project == uid))))
                conn.execute(delete(tickets_table).where(tickets_table.c.project == uid))
            self._factory_forget_project(uid)
            with self.sessions_db.begin() as conn:
                conn.execute(delete(plog_table).where(plog_table.c.usecase_id == uid))
                conn.execute(delete(projects_table).where(projects_table.c.id == uid))
        self._usage_forget_project(uid, private)   # before a team project's links go, below
        over = sorted({uid, *self.deleted_private_projects()})   # their links keep no state
        with self.sessions_db.begin() as conn:
            # a private project's session links stay (TR-1708): they mark what its sessions
            # recorded as not to be read, by the same tests as while it was there
            if not private:
                conn.execute(delete(project_sessions).where(project_sessions.c.project == uid))
            # its weekly figures (report.py); a pass running now sees the project gone, above
            conn.execute(delete(report_weeks).where(report_weeks.c.project == uid))
            conn.execute(delete(merge_records).where(merge_records.c.project == uid))
            # a crew station's session serves every project: its state stays
            linked = select(project_sessions.c.session).where(project_sessions.c.project.not_in(over))
            conn.execute(delete(session_states).where(
                session_states.c.session.not_in(linked),
                session_states.c.session.not_in(select(session_stations.c.session))))
            conn.execute(delete(report_texts).where(report_texts.c.kind == "milestone",
                                                    report_texts.c.key.in_(milestones)))

    # ── texts Tares writes from its figures (TR-1645, report_words.py) ──
    def get_report_text(self, kind: str, key: str) -> dict | None:
        t = report_texts
        with self.sessions_db.read() as conn:
            r = conn.execute(select(t.c.text, t.c.source, t.c.figures, t.c.written_at).where(
                t.c.kind == kind, t.c.key == key)).first()
        return ({"kind": kind, "key": key, "text": r[0], "source": r[1],
                 "figures": json.loads(r[2]) if r[2] else {}, "written_at": r[3]} if r else None)

    def put_report_text(self, kind: str, key: str, text: str, source: str, figures: dict,
                        written_at, replace: bool = False) -> None:
        """Keep a text. The first one written for a kind and key stays, unless `replace`."""
        values = {"text": text, "source": source,
                  "figures": json.dumps(figures, ensure_ascii=False), "written_at": written_at}
        stmt = self.sessions_db.upsert(report_texts).values(kind=kind, key=key, **values)
        on = [report_texts.c.kind, report_texts.c.key]
        stmt = (stmt.on_conflict_do_update(index_elements=on, set_=values) if replace
                else stmt.on_conflict_do_nothing(index_elements=on))
        with self.sessions_db.begin() as conn:
            conn.execute(stmt)

    def get_project_setup(self, uid: str) -> dict | None:
        """What the guided setup kept on a project ({plan, step, practice_run, ...}), or None. The
        guided setup went with the source catalog (TR-1811); a project made with it keeps the row."""
        with self.sessions_db.read() as conn:
            r = conn.execute(select(projects_table.c.setup).where(
                projects_table.c.id == uid)).first()
        return r[0] if r and r[0] else None

    def set_project_setup(self, uid: str, setup: dict | None) -> None:
        # default=str: a value JSON cannot hold (a datetime) is kept as its text, as before
        clean = json.loads(json.dumps(setup, default=str)) if setup is not None else None
        with self._swrite, self.sessions_db.begin() as conn:
            conn.execute(update(projects_table).where(projects_table.c.id == uid).values(
                setup=clean, updated_at=now_utc()))

    def upsert_project_object(self, uid: str, kind: str, key: str, name: str) -> None:
        t = objects_table
        with self._swrite, self.sessions_db.begin() as conn:
            conn.execute(self.sessions_db.upsert(t).values(
                usecase_id=uid, kind=kind, key=key, name=name, customized=False,
                created_at=now_utc()).on_conflict_do_update(
                index_elements=["usecase_id", "kind", "key"], set_={"name": name}))

    def list_project_objects(self, uid: str) -> list[dict]:
        t = objects_table
        with self.sessions_db.read() as conn:
            rows = conn.execute(select(t.c.kind, t.c.key, t.c.name, t.c.customized,
                                       t.c.created_at).where(t.c.usecase_id == uid).order_by(
                t.c.created_at, t.c.kind, t.c.key)).all()
        return [{"kind": r[0], "key": r[1], "name": r[2], "customized": bool(r[3]),
                 "created_at": r[4]} for r in rows]

    # ── skills (TR-332): parts of the cell (P-TR-216), used by projects; validated by
    # tares/skills.py before they get here ──
    def delete_project_object(self, uid: str, kind: str, key: str) -> None:
        t = objects_table
        with self._swrite, self.sessions_db.begin() as conn:
            conn.execute(delete(t).where(t.c.usecase_id == uid, t.c.kind == kind, t.c.key == key))

    # ── skills (TR-332): parts of the cell (P-TR-216), used by projects; validated by
    # tares/skills.py before they get here ──
    def _body_size(self, col):
        """The size of a text column in bytes (UTF-8), the same on both engines."""
        if self.sessions_db.dialect == "postgresql":
            return func.octet_length(col)
        return func.length(cast(col, LargeBinary))

    def list_skills(self, project: str) -> list[dict]:
        """The skills a project uses, without their bodies: name, description, updated_at, size
        (bytes of the body)."""
        s, o = shared_skills_table, objects_table
        with self.sessions_db.read() as conn:
            rows = conn.execute(
                select(s.c.name, s.c.description, s.c.updated_at, self._body_size(s.c.body))
                .select_from(s.join(o, and_(o.c.kind == "skill", o.c.name == s.c.name)))
                .where(o.c.usecase_id == project).distinct().order_by(s.c.name)).all()
        return [{"name": r[0], "description": r[1], "updated_at": r[2], "size": int(r[3] or 0)}
                for r in rows]

    def list_all_skills(self) -> list[dict]:
        """Every skill on the cell with its body, the project that made it (`project`) and the
        projects that use it (`projects`)."""
        s, o = shared_skills_table, objects_table
        with self.sessions_db.read() as conn:
            rows = conn.execute(select(s.c.name, s.c.description, s.c.body, s.c.made_by)
                                .order_by(s.c.name)).all()
            users = conn.execute(select(o.c.name, o.c.usecase_id).where(o.c.kind == "skill")
                                 .group_by(o.c.name, o.c.usecase_id)).all()
        by: dict[str, list] = {}
        for n, u in users:
            by.setdefault(n, []).append(u)
        return [{"project": r[3], "name": r[0], "description": r[1], "body": r[2],
                 "projects": sorted(by.get(r[0], []))} for r in rows]

    def get_skill(self, project: str | None, name: str) -> dict | None:
        """A skill the project uses (any project, with None)."""
        s, o = shared_skills_table, objects_table
        with self.sessions_db.read() as conn:
            if project is not None and conn.execute(select(o.c.name).where(
                    o.c.usecase_id == project, o.c.kind == "skill", o.c.name == name)
                    .limit(1)).first() is None:
                return None
            r = conn.execute(select(s.c.name, s.c.description, s.c.body, s.c.created_at,
                                    s.c.updated_at).where(s.c.name == name)).first()
        return ({"name": r[0], "description": r[1], "body": r[2], "created_at": r[3],
                 "updated_at": r[4]} if r else None)

    def _use_skill(self, conn, project: str, name: str) -> bool:
        """The project uses the skill; True when it did not before. Runs in `conn`, a session
        store transaction."""
        o = objects_table
        if conn.execute(select(o.c.name).where(
                o.c.usecase_id == project, o.c.kind == "skill", o.c.name == name)
                .limit(1)).first() is not None:
            return False
        conn.execute(self.sessions_db.upsert(o).values(
            usecase_id=project, kind="skill", key=f"skill:{name}", name=name, customized=False,
            created_at=now_utc()).on_conflict_do_nothing())
        return True

    def use_skill(self, project: str, name: str) -> bool:
        """A skill already on the cell, used by one more project. False when there is no such
        skill; the project already using it is fine."""
        with self.sessions_db.begin() as conn:
            if conn.execute(select(shared_skills_table.c.name).where(
                    shared_skills_table.c.name == name)).first() is None:
                return False
            self._use_skill(conn, project, name)
        return True

    def upsert_skill(self, project: str, name: str, description: str, body: str,
                     updated_by: str | None = None) -> bool:
        """Create or replace the skill (shared: every project that uses it sees the change), and
        have the project use it. Returns True when the project did not have it before.
        `updated_by` is who changed it, when a person did (a page, TR-753); NULL otherwise."""
        ts = now_utc()
        t = shared_skills_table
        # the write lock: on Postgres a delete_skill beside this could drop the skill just written
        with self._swrite, self.sessions_db.begin() as conn:
            ins = self.sessions_db.upsert(t).values(
                name=name, description=description, body=body, made_by=project, created_at=ts,
                updated_at=ts, updated_by=updated_by)
            conn.execute(ins.on_conflict_do_update(index_elements=["name"], set_={
                "description": ins.excluded.description, "body": ins.excluded.body,
                "updated_at": ins.excluded.updated_at, "updated_by": ins.excluded.updated_by}))
            return self._use_skill(conn, project, name)

    def delete_skill(self, project: str, name: str) -> bool:
        """The project stops using the skill; the skill goes when no project uses it any more.
        Returns whether the project had it."""
        o = objects_table
        with self._swrite, self.sessions_db.begin() as conn:
            had = conn.execute(select(o.c.name).where(
                o.c.usecase_id == project, o.c.kind == "skill", o.c.name == name)
                .limit(1)).first()
            conn.execute(delete(o).where(
                o.c.usecase_id == project, o.c.kind == "skill", o.c.name == name))
            self._forget_unused_skills(conn)
        return had is not None

    def _forget_unused_skills(self, conn=None) -> None:
        """Skills no project uses any more go. `conn`: the session store transaction to run in."""
        s, o = shared_skills_table, objects_table
        stmt = delete(s).where(s.c.name.not_in(select(o.c.name).where(o.c.kind == "skill")))
        if conn is not None:
            conn.execute(stmt)
            return
        with self.sessions_db.begin() as c:
            c.execute(stmt)

    def skill_users(self, name: str) -> list[str]:
        return self.projects_using("skill", name)

    def mark_skill_customized(self, project: str, name: str) -> None:
        """A skill a template planned was edited by hand: a re-plan keeps the edit."""
        o = objects_table
        with self._swrite, self.sessions_db.begin() as conn:
            conn.execute(update(o).where(
                o.c.usecase_id == project, o.c.kind == "skill", o.c.name == name
            ).values(customized=True))

    # ── docs and tickets (TR-403): validated by tares/docs.py before they get here ──
    @staticmethod
    def _doc_cols():
        d = docs_table
        return (d.c.id, d.c.kind, d.c.title, d.c.made_by, d.c.updated_by, d.c.created_at,
                d.c.updated_at, d.c.owner)

    @staticmethod
    def _doc_row(r, body: str | None = None) -> dict:
        out = {"id": r[0], "kind": r[1], "title": r[2], "made_by": r[3], "updated_by": r[4],
               "created_at": r[5], "updated_at": r[6], "owner": r[7]}
        if body is not None:
            out["body"] = body
        return out

    def create_doc(self, project: str | None, kind: str, title: str, body: str,
                   by: str = "", owner: str | None = None) -> str:
        """A new doc on the cell, included in `project` (None: on the cell only, for every
        project to include). `owner` (a people.id) makes it that person's own: never included in
        a project, listed only to them. Returns its id."""
        doc_id = "doc_" + uuid.uuid4().hex[:10]
        ts = now_utc()
        with self._swrite, self.sessions_db.begin() as conn:
            conn.execute(insert(docs_table).values(
                id=doc_id, kind=kind, title=title, body=body, made_by=project, updated_by=by or None,
                created_at=ts, updated_at=ts, owner=owner))
            if project:
                self._use_doc(conn, project, doc_id)
        return doc_id

    def update_doc(self, doc_id: str, kind: str | None = None, title: str | None = None,
                   body: str | None = None, by: str = "") -> None:
        vals: dict = {"updated_at": now_utc(), "updated_by": by or None}
        for col, v in (("kind", kind), ("title", title), ("body", body)):
            if v is not None:
                vals[col] = v
        with self._swrite, self.sessions_db.begin() as conn:
            conn.execute(update(docs_table).where(docs_table.c.id == doc_id).values(**vals))

    def get_doc(self, project: str | None, doc_id: str) -> dict | None:
        """A doc the project includes (any doc, with None), with its body and the projects that
        include it."""
        o = objects_table
        with self.sessions_db.read() as conn:
            if project is not None and conn.execute(select(o.c.name).where(
                    o.c.usecase_id == project, o.c.kind == "doc", o.c.name == doc_id)
                    .limit(1)).first() is None:
                return None
            r = conn.execute(select(*self._doc_cols(), docs_table.c.body).where(
                docs_table.c.id == doc_id)).first()
            if not r:
                return None
            users = list(conn.execute(select(o.c.usecase_id).where(
                o.c.kind == "doc", o.c.name == doc_id).order_by(o.c.usecase_id)).scalars())
        return {**self._doc_row(r, r[8]), "projects": users}

    def personal_doc(self, person_id: str, kind: str) -> dict | None:
        """The doc of `kind` that belongs to this person, with its body, or None until they
        write the first line."""
        d = docs_table
        with self.sessions_db.read() as conn:
            r = conn.execute(select(*self._doc_cols(), d.c.body).where(
                d.c.owner == person_id, d.c.kind == kind).order_by(d.c.created_at, d.c.id)
                .limit(1)).first()
        return self._doc_row(r, r[8]) if r else None

    def doc_titles(self, project: str, doc_ids: list[str]) -> dict[str, str]:
        """The titles of those docs the project includes, in one query (TR-657); a doc the
        project does not include is absent, as get_doc gives None for it."""
        if not doc_ids:
            return {}
        d, o = docs_table, objects_table
        with self.sessions_db.read() as conn:
            rows = conn.execute(
                select(d.c.id, d.c.title).select_from(
                    d.join(o, and_(o.c.kind == "doc", o.c.name == d.c.id)))
                .where(o.c.usecase_id == project, d.c.id.in_(list(doc_ids))).distinct()).all()
        return {r[0]: r[1] for r in rows}

    def list_docs(self, project: str, kind: str | None = None) -> list[dict]:
        """The docs a project includes, without bodies: size in bytes instead."""
        d, o = docs_table, objects_table
        q = (select(*self._doc_cols(), self._body_size(d.c.body))
             .select_from(d.join(o, and_(o.c.kind == "doc", o.c.name == d.c.id)))
             .where(o.c.usecase_id == project, d.c.owner.is_(None)))
        if kind:
            q = q.where(d.c.kind == kind)
        with self.sessions_db.read() as conn:
            rows = conn.execute(q.distinct().order_by(d.c.created_at, d.c.id)).all()
        return [{**self._doc_row(r), "size": int(r[8] or 0)} for r in rows]

    def list_cell_docs(self, viewer: str | None = None) -> list[dict]:
        """Every doc on the cell (no bodies), with the projects that include it. A personal doc
        is listed only to its person (`viewer`, a people.id): nobody else sees it, whatever their
        rights."""
        d, o = docs_table, objects_table
        with self.sessions_db.read() as conn:
            rows = conn.execute(
                select(*self._doc_cols(), self._body_size(d.c.body))
                .where(or_(d.c.owner.is_(None), d.c.owner == (viewer or "")))
                .order_by(d.c.title, d.c.id)).all()
            users = conn.execute(select(o.c.name, o.c.usecase_id).where(o.c.kind == "doc")
                                 .group_by(o.c.name, o.c.usecase_id)).all()
        by: dict[str, list] = {}
        for n, u in users:
            by.setdefault(n, []).append(u)
        return [{**self._doc_row(r), "size": int(r[8] or 0), "projects": sorted(by.get(r[0], []))}
                for r in rows]

    def _use_doc(self, conn, project: str, doc_id: str) -> None:
        """Runs in `conn`, a session store transaction."""
        conn.execute(self.sessions_db.upsert(objects_table).values(
            usecase_id=project, kind="doc", key=f"doc:{doc_id}", name=doc_id, customized=False,
            created_at=now_utc()).on_conflict_do_nothing())

    def use_doc(self, project: str, doc_id: str) -> bool:
        """A doc already on the cell, included in one more project. False when there is no such
        doc."""
        d = docs_table
        with self.sessions_db.begin() as conn:
            if conn.execute(select(d.c.id).where(d.c.id == doc_id, d.c.owner.is_(None))
                            ).first() is None:
                return False        # no such doc, or a person's own: those join no project
            self._use_doc(conn, project, doc_id)
        return True

    def remove_doc(self, project: str, doc_id: str) -> bool:
        """The project stops including the doc; the doc is deleted when no project includes it
        any more and it was made by a project (a doc made on the cell stays). A ticket of this
        project whose working doc it was loses the link. Returns whether the project had it."""
        o, t = objects_table, tickets_table
        with self._swrite, self.sessions_db.begin() as conn:
            conn.execute(update(t).where(t.c.project == project, t.c.working_doc == doc_id)
                         .values(working_doc=None))
            had = conn.execute(select(o.c.name).where(
                o.c.usecase_id == project, o.c.kind == "doc", o.c.name == doc_id).limit(1)).first()
            conn.execute(delete(o).where(
                o.c.usecase_id == project, o.c.kind == "doc", o.c.name == doc_id))
            self._forget_unused_docs(conn)
        return had is not None

    def _forget_unused_docs(self, conn=None) -> None:
        """Docs a project made that no project includes any more go. `conn`: the session store
        transaction to run in."""
        d, o = docs_table, objects_table
        stmt = delete(d).where(d.c.made_by.is_not(None),
                               d.c.id.not_in(select(o.c.name).where(o.c.kind == "doc")))
        if conn is not None:
            conn.execute(stmt)
            return
        with self.sessions_db.begin() as c:
            c.execute(stmt)

    @staticmethod
    def _ticket_row(r) -> dict:
        """A row of `select(tickets_table)` as callers read a ticket."""
        out = dict(r._mapping)
        del out["pr_repo"], out["pr_number"]        # the index of `pr`, not part of the ticket
        out["depends_on"] = out["depends_on"] or []
        return out

    def create_ticket(self, project: str, owner: str, title: str, status: str = "todo",
                      position: float | None = None, external_id: str | None = None,
                      identifier: str | None = None, url: str | None = None,
                      working_doc: str | None = None, milestone: str | None = None,
                      depends_on: list[str] | None = None, repo: str | None = None,
                      assignee_person_id: str | None = None,
                      assignee_raw: str | None = None) -> str:
        """A ticket at the end of the project's list unless `position` says where. It gets the
        project's next number (T1, T2, ...)."""
        with self._swrite, self.sessions_db.begin() as conn:
            return self._insert_ticket(conn, project, owner, title, status, position, external_id,
                                       identifier, url, working_doc, milestone, depends_on, repo,
                                       assignee_person_id, assignee_raw)

    def _insert_ticket(self, conn, project, owner, title, status, position, external_id,
                       identifier, url, working_doc, milestone, depends_on, repo,
                       assignee_person_id, assignee_raw) -> str:
        """The INSERT of create_ticket, in the caller's transaction."""
        t = tickets_table
        tid = "tk_" + uuid.uuid4().hex[:10]
        ts = now_utc()
        # the number first: it holds the project's row until the transaction ends, so a second
        # writer on Postgres waits here and then sees this ticket's position
        num = self._next_ticket_number(conn, project)
        if position is None:
            last = conn.execute(select(func.max(t.c.position)).where(
                t.c.project == project)).scalar()
            position = (last or 0) + 1
        conn.execute(insert(t).values(
            id=tid, project=project, owner=owner, external_id=external_id, identifier=identifier,
            url=url, title=title, status=status, position=float(position),
            working_doc=working_doc, created_at=ts, updated_at=ts, milestone=milestone,
            depends_on=depends_on or None, number=num, repo=repo,
            assignee_person_id=assignee_person_id or None, assignee_raw=assignee_raw or None))
        return tid

    def adopt_ticket(self, project: str, owner: str, external_id: str, title: str, status: str,
                     identifier: str, url: str, position: float | None = None,
                     working_doc: str | None = None, milestone: str | None = None,
                     depends_on: list[str] | None = None, repo: str | None = None,
                     assignee_person_id: str | None = None,
                     assignee_raw: str | None = None) -> str:
        """The Tares row of a ticket that was just made in Linear or GitHub (TR-851): the row of
        this project with this owner and external id when there is one (a retry, or a sync that got
        there first), else a new one. Look and write are one locked step, so the two never make
        two rows. A row found takes the identifier, the url and the fields given (a field left as
        None keeps its value). The assignee is set only on a new row. The syncs insert through here
        too, so a sync and a write_ticket never leave two rows for one issue. Returns the row's id."""
        t = tickets_table
        with self._swrite, self.sessions_db.begin() as conn:
            # no external id names no row (`== None` would read as IS NULL and match any)
            r = None if external_id is None else conn.execute(select(t.c.id).where(
                t.c.project == project, t.c.owner == owner, t.c.external_id == external_id
            ).order_by(t.c.created_at.nulls_last()).limit(1)).first()
            if r is None:
                return self._insert_ticket(conn, project, owner, title, status, position,
                                           external_id, identifier, url, working_doc, milestone,
                                           depends_on, repo, assignee_person_id, assignee_raw)
            tid = r[0]
            values = {"identifier": identifier, "url": url, "updated_at": now_utc()}
            for col, v in (("working_doc", working_doc), ("milestone", milestone),
                           ("repo", repo), ("position", position)):
                if v is not None:
                    values[col] = float(v) if col == "position" else v
            if depends_on is not None:
                values["depends_on"] = depends_on or None
            conn.execute(update(t).where(t.c.id == tid).values(values))
        return tid

    def update_ticket(self, tid: str, touch: bool = True, why: str | None = None,
                      unless_pending: bool = False, **fields) -> bool:
        """Set the given columns (title, status, position, working_doc, identifier, url,
        external_id, milestone, depends_on); None clears working_doc or milestone only when passed
        explicitly. `touch=False` leaves updated_at alone: a re-read that found nothing new is
        not activity.

        A status of done or canceled closes the ticket everywhere (TR-1008): a stage still in the
        work (todo, doing, review, changes, blocked) is cleared in the same write, and the history
        says why (`why`, e.g. "Linear marked it done"). Merged and shipped stay: they are true.

        `unless_pending` (TR-845) leaves the ticket alone when a status change of it is waiting to
        reach the ticket system (a pending_writeback row), and returns False. The row is looked
        for under the same lock as the write, so one added a moment ago is seen. Returns True
        when the ticket was written."""
        allowed = {"title", "status", "position", "working_doc", "identifier", "url",
                   "external_id", "milestone", "depends_on", "holder", "stage", "stage_reason",
                   "pr", "repo", "assignee_person_id", "assignee_raw"}
        t = tickets_table
        values = {"updated_at": now_utc()} if touch else {}
        for col, v in fields.items():
            if col == "depends_on":
                v = v or None
            if col == "pr":
                # default=str: a value JSON cannot hold (a datetime) is kept as its text
                v = json.loads(json.dumps(v, default=str)) if v else None
                values["pr_repo"], values["pr_number"] = pr_columns(v)
            if col in allowed:
                values[col] = v
        closes = fields.get("status") in DONE_STATUSES and "stage" not in fields
        if closes:
            values["stage"] = case((t.c.stage.in_(("merged", "shipped")), t.c.stage), else_=None)
        with self._swrite:
            with self.sessions_db.begin() as conn:
                if unless_pending and conn.execute(select(writeback_table.c.ticket).where(
                        writeback_table.c.ticket == tid)).first() is not None:
                    return False
                was = (conn.execute(select(t.c.project, t.c.stage).where(t.c.id == tid)).first()
                       if closes else None)
                if values:
                    conn.execute(update(t).where(t.c.id == tid).values(values))
        if was and was[1] in OPEN_STAGES:
            self.add_ticket_history(tid, was[0], "stage", None,
                                    why or f"the ticket was marked {fields['status']}",
                                    "tares", "tares", now_utc())
        return True

    def update_ticket_owner(self, tid: str, owner: str) -> None:
        """A project stopped using Linear or GitHub: its tickets become Tares's (TR-408)."""
        t = tickets_table
        with self._swrite, self.sessions_db.begin() as conn:
            conn.execute(update(t).where(t.c.id == tid).values(owner=owner, updated_at=now_utc()))

    @staticmethod
    def _ticket_order():
        """The order of a project's list. NULLs last on SQLite too, as Postgres sorts."""
        return (tickets_table.c.position.nulls_last(), tickets_table.c.created_at.nulls_last(),
                tickets_table.c.number.nulls_last(), tickets_table.c.id)

    def list_tickets(self, project: str) -> list[dict]:
        t = tickets_table
        with self.sessions_db.read() as conn:
            rows = conn.execute(select(t).where(t.c.project == project)
                                .order_by(*self._ticket_order())).all()
        return [self._ticket_row(r) for r in rows]

    @kept
    def tickets_by_project(self, projects: list[str]) -> dict[str, list[dict]]:
        """{project: its tickets as list_tickets gives them} in one query (TR-659)."""
        if not projects:
            return {}
        t = tickets_table
        with self.sessions_db.read() as conn:
            rows = conn.execute(select(t).where(t.c.project.in_(list(projects)))
                                .order_by(*self._ticket_order())).all()
        out: dict[str, list[dict]] = {p: [] for p in projects}
        for r in rows:
            out[r.project].append(self._ticket_row(r))
        return out

    def done_ticket_facts(self, project_ids: list[str]) -> dict[str, dict]:
        """{project: {merged, closed, queued, stuck, total, repos, repo_from_pr, assignees, exact}}
        from two grouped queries, so a finished project's row needs no ticket rows. merged, closed,
        queued, stuck and total are what factory.progress gives for the same keys; repos are the full repo
        names of the tickets in the order the ticket list shows them; repo_from_pr counts the
        tickets with no repo whose pull request may name one. exact is False when groups cannot
        give progress: a ticket that would read "to do" but lists dependencies (whether it is
        queued depends on those tickets), or one still open that has a pull request (it counts as
        merged when that pull request is), so the caller loads that project's tickets. assignees is
        {person id or None: how many tickets that are not canceled}, what a team cell's "tickets
        yours" line reads."""
        if not project_ids:
            return {}
        out = {p: {"merged": 0, "closed": 0, "queued": 0, "stuck": 0, "total": 0, "repos": [],
                   "repo_from_pr": 0, "assignees": {}, "exact": True} for p in project_ids}
        t = tickets_table
        mine = t.c.project.in_(list(project_ids))
        has_pr = and_(t.c.pr.is_not(None), cast(t.c.pr, Text).not_in(("", "null", "{}")))
        no_deps = or_(t.c.depends_on.is_(None),
                      cast(t.c.depends_on, Text).in_(("", "null", "[]")))
        no_repo = or_(t.c.repo.is_(None), t.c.repo == "")
        # the flags are worked out in a subquery, so the grouping names its columns: Postgres does
        # not take two copies of an expression with bound values for the same one
        flags = select(
            t.c.project, t.c.stage, t.c.status,
            case((no_deps, 0), else_=1).label("has_deps"),
            case((and_(no_repo, has_pr), 1), else_=0).label("from_pr"),
            case((has_pr, 1), else_=0).label("has_pr"),
            t.c.assignee_person_id).where(mine).subquery()
        ranked = select(
            t.c.project, t.c.repo, t.c.position, t.c.created_at,
            func.row_number().over(partition_by=(t.c.project, t.c.repo),
                                   order_by=self._ticket_order()).label("rn")
        ).where(mine, t.c.repo.is_not(None), t.c.repo != "").subquery()
        with self.sessions_db.consistent_read() as conn:
            groups = conn.execute(select(*flags.c, func.count()).group_by(*flags.c)).all()
            first = conn.execute(
                select(ranked.c.project, ranked.c.repo).where(ranked.c.rn == 1)
                .order_by(ranked.c.project, ranked.c.position.nulls_last(),
                          ranked.c.created_at.nulls_last())).all()
        for project, stage, status, has_deps, from_pr, has_pr, who, n in groups:
            facts = out[project]
            if status != "canceled":
                facts["assignees"][who] = facts["assignees"].get(who, 0) + n
            state = state_of({"stage": stage, "status": status})
            merged = stage in ("merged", "shipped")
            if state == "to do" and has_deps:
                facts["exact"] = False
            if has_pr and not merged and state not in ("merged", "canceled"):
                facts["exact"] = False      # its pull request may be merged: factory.turn_of
            if from_pr:
                facts["repo_from_pr"] += n
            if state == "canceled":
                continue
            facts["total"] += n
            if merged:                      # factory.turn_of: merged only for a merge
                facts["merged"] += n
            elif state == "merged":
                facts["closed"] += n        # a done status with no merge
            elif state == "blocked":
                facts["stuck"] += n
        for project, repo in first:
            out[project]["repos"].append(repo)
        return out

    def get_ticket(self, project: str, ref: str) -> dict | None:
        """A ticket of the project by its id, Linear identifier (ENG-12, any case), Linear
        issue id, or number (T3, or 3)."""
        if project is None or ref is None:       # `== None` would read as IS NULL and match
            return None
        m = re.match(r"^[Tt]?(\d{1,6})$", ref.strip())
        num = int(m.group(1)) if m else -1
        t = tickets_table
        # a match by name is preferred to a match by number
        found = union_all(
            select(t, literal(0).label("preference")).where(
                t.c.project == project,
                or_(t.c.id == ref, func.upper(t.c.identifier) == func.upper(ref),
                    t.c.external_id == ref)),
            select(t, literal(1).label("preference")).where(
                t.c.project == project, t.c.number == num)).subquery()
        with self.sessions_db.read() as conn:
            r = conn.execute(select(*(found.c[c.name] for c in t.c))
                             .order_by(found.c.preference).limit(1)).first()
        return self._ticket_row(r) if r else None

    def ticket_for_pr(self, repo: str, number: int) -> dict | None:
        """The ticket whose pull request is `repo`#`number` (the ticket row, which names its
        project), or None. A pull request linked to several tickets gives the one still open,
        else the first."""
        try:
            number = int(number)
        except (TypeError, ValueError, OverflowError):
            return None
        # read as the columns were filled: lower case, and None for a repo or number they never hold
        want, number = pr_columns({"repo": str(repo or "").strip(), "number": number})
        if want is None or number is None:
            return None
        t = tickets_table
        with self.sessions_db.read() as conn:
            rows = conn.execute(select(t).where(t.c.pr_repo == want, t.c.pr_number == number)
                                .order_by(*self._ticket_order())).all()
        found = [self._ticket_row(r) for r in rows]
        return next((x for x in found if x["stage"] not in ("merged", "shipped")), found[0] if found else None)

    def delete_ticket(self, tid: str) -> None:
        """The ticket goes, and with it the other tickets' dependency on it."""
        t = tickets_table
        with self._swrite:
            with self.sessions_db.begin() as conn:
                conn.execute(delete(writeback_table).where(writeback_table.c.ticket == tid))
                row = conn.execute(select(t.c.project).where(t.c.id == tid)).first()
                conn.execute(delete(t).where(t.c.id == tid))
                if row:
                    for oid, deps in conn.execute(select(t.c.id, t.c.depends_on).where(
                            t.c.project == row[0], t.c.depends_on.is_not(None))).all():
                        if tid in (deps or []):
                            conn.execute(update(t).where(t.c.id == oid).values(
                                depends_on=[d for d in deps if d != tid] or None))

    def link_session(self, project: str, session: str, repo: str | None = None) -> bool:
        """The Claude Code session works in the project. True when it was not linked before."""
        stmt = self.sessions_db.upsert(project_sessions).values(
            project=project, session=session, repo=repo, linked_at=now_utc()
        ).on_conflict_do_nothing(index_elements=["project", "session"])
        with self.sessions_db.begin() as conn:
            return conn.execute(stmt).rowcount == 1

    def sessions_of_projects(self, projects: list[str]) -> list[str]:
        """The sessions linked to any of the projects."""
        out: set[str] = set()
        for i in range(0, len(projects), 500):
            with self.sessions_db.read() as conn:
                out.update(r[0] for r in conn.execute(
                    select(project_sessions.c.session).where(
                        project_sessions.c.project.in_(projects[i:i + 500]))))
        return sorted(out)

    def project_sessions(self, project: str) -> list[dict]:
        """The sessions that worked in the project, newest activity first, with when they started
        and last did something, how many lines they have, and their state (working, idle, waiting,
        ended; None before the plugin reported one) with its reason and time."""
        ps, ss, st, sx = project_sessions, session_stations, session_states, sessions_table
        # the lines come from the sessions table in the same read (TR-1499)
        j = (ps.outerjoin(ss, ss.c.session == ps.c.session)
             .outerjoin(st, st.c.session == ps.c.session)
             .outerjoin(sx, and_(sx.c.session == ps.c.session,
                                 sx.c.source == "claude_code", sx.c.line_count > 0)))
        q = (select(ps.c.session, ps.c.repo, ps.c.linked_at, st.c.state, st.c.reason,
                    st.c.state_at, ss.c.station, sx.c.started_at, sx.c.last_line_at, sx.c.line_count)
             .select_from(j)
             .where(ps.c.project == project,
                    # the crew's own stations serve every project: they show on the Crew page and
                    # never count as the project's build session (a pick-up must not replace them)
                    ps.c.session.not_in(select(ss.c.session).where(
                        ss.c.role.in_(("orchestrator", "reviewer", "releaser", "comms"))))))
        with self.sessions_db.read() as conn:
            rows = conn.execute(q).all()
        seen, uniq = set(), []
        for r in rows:
            if r[0] not in seen:
                seen.add(r[0])
                uniq.append(r)
        act = {r[0]: (r[7], r[8], int(r[9] or 0)) for r in uniq}
        out = [{"session": r[0], "repo": r[1], "linked_at": r[2],
                "started_at": act.get(r[0], (None,))[0], "last_at": act.get(r[0], (None, None))[1],
                "lines": act.get(r[0], (None, None, 0))[2], "state": r[3], "state_reason": r[4],
                "state_at": r[5], "station": r[6]} for r in uniq]
        # newest activity first, sessions with no lines last
        out.sort(key=lambda s: (s["last_at"] is None, -(s["last_at"].timestamp())
                                if s["last_at"] else 0))
        return out

    def session_activity(self, source: str, sessions: list[str]) -> dict:
        """{session: (first line time, last line time, line count)} for the sessions that have
        lines on `source`, from the sessions table (TR-1471 keeps it)."""
        out = {}
        t = sessions_table
        for i in range(0, len(sessions), 500):
            chunk = sessions[i:i + 500]
            if not chunk:
                continue
            q = select(t.c.session, t.c.started_at, t.c.last_line_at, t.c.line_count).where(
                t.c.source == source, t.c.session.in_(chunk), t.c.line_count > 0)
            with self.sessions_db.read() as conn:
                for sid, first, last, n in conn.execute(q):
                    out[sid] = (first, last, int(n))
        return out

    def set_session_state(self, session: str, state: str, reason: str | None, at,
                          force: bool = False) -> None:
        """The session's state now; an older line than the one stored does not win, unless
        `force` (a take-over is a decision, not a report)."""
        stmt = self.sessions_db.upsert(session_states).values(
            session=session, state=state, reason=reason or None, state_at=at)
        stmt = stmt.on_conflict_do_update(
            index_elements=["session"],
            set_={"state": stmt.excluded.state, "reason": stmt.excluded.reason,
                  "state_at": stmt.excluded.state_at},
            where=None if force else stmt.excluded.state_at >= session_states.c.state_at)
        with self.sessions_db.begin() as conn:
            conn.execute(stmt)

    def session_state(self, session: str) -> dict | None:
        """The session's stored state {state, reason, state_at}, as reported (not as shown), or
        None when it never reported one."""
        with self.sessions_db.read() as conn:
            row = conn.execute(select(session_states.c.state, session_states.c.reason,
                                      session_states.c.state_at).where(
                session_states.c.session == session)).first()
        return {"state": row[0], "reason": row[1], "state_at": row[2]} if row else None

    def said_lately(self, source: str, session: str, n: int = 3) -> list[dict]:
        """The session's last `n` assistant messages (not tool calls, not empty), oldest first.
        Read from `session_said`, which keeps the last three of each session (session_ingest,
        TR-1505), so `n` above 3 gives 3."""
        t = session_said.c
        with self.sessions_db.read() as conn:
            rows = conn.execute(select(t.text, t.at).where(
                t.source == source, t.session == session).order_by(
                t.at.desc(), t.line_key.desc()).limit(max(n, 0))).fetchall()
        return [{"text": r[0], "at": r[1]} for r in reversed(rows)]

    def files_touched(self, source: str, session: str, limit: int = 20) -> list[str]:
        """The files the session last edited or wrote (its Edit / Write / MultiEdit / NotebookEdit
        calls), most recent first, each once. Read from `session_files` (session_ingest, TR-1505)."""
        t = session_files.c
        with self.sessions_db.read() as conn:
            return [r[0] for r in conn.execute(select(t.path).where(
                t.source == source, t.session == session).order_by(
                t.last_at.desc(), t.path).limit(max(limit, 0)))]

    def last_said(self, source: str, session: str) -> str | None:
        """The last thing the session's assistant wrote (not a tool call), for a waiting
        session: what it stopped on."""
        said = self.said_lately(source, session, 1)
        return said[0]["text"] if said else None

    def session_projects(self, session: str) -> list[str]:
        with self.sessions_db.read() as conn:
            return [r[0] for r in conn.execute(select(project_sessions.c.project).where(
                project_sessions.c.session == session))]

    def get_project_linear(self, uid: str) -> dict | None:
        """The Linear project a project's tickets live in, with the last sync's state, or None."""
        with self.sessions_db.read() as conn:
            r = conn.execute(select(projects_table.c.linear).where(
                projects_table.c.id == uid)).first()
        return r[0] if r and r[0] else None

    def set_project_linear(self, uid: str, linear: dict | None) -> None:
        # default=str: a value JSON cannot hold (a datetime) is kept as its text, as before
        clean = json.loads(json.dumps(linear, default=str)) if linear is not None else None
        with self._swrite, self.sessions_db.begin() as conn:
            conn.execute(update(projects_table).where(projects_table.c.id == uid).values(
                linear=clean))

    def linear_projects(self) -> list[tuple[str, dict]]:
        """(project id, its Linear link) for every project that uses Linear."""
        with self.sessions_db.read() as conn:
            rows = conn.execute(select(projects_table.c.id, projects_table.c.linear).where(
                projects_table.c.linear.is_not(None))).all()
        return [(r[0], r[1]) for r in rows if r[1]]

    def get_project_github(self, uid: str) -> dict | None:
        """The GitHub repo and issue label a project's tickets live in, with the last sync's
        state, or None."""
        with self.sessions_db.read() as conn:
            r = conn.execute(select(projects_table.c.github_issues).where(
                projects_table.c.id == uid)).first()
        return r[0] if r and r[0] else None

    def set_project_github(self, uid: str, link: dict | None) -> None:
        clean = json.loads(json.dumps(link, default=str)) if link is not None else None
        with self._swrite, self.sessions_db.begin() as conn:
            conn.execute(update(projects_table).where(projects_table.c.id == uid).values(
                github_issues=clean))

    def github_projects(self) -> list[tuple[str, dict]]:
        """(project id, its GitHub link) for every project whose tickets live in GitHub Issues."""
        with self.sessions_db.read() as conn:
            rows = conn.execute(select(projects_table.c.id, projects_table.c.github_issues).where(
                projects_table.c.github_issues.is_not(None))).all()
        return [(r[0], r[1]) for r in rows if r[1]]

    def project_ticket_home(self, uid: str) -> str | None:
        """'linear' or 'github' when the project's tickets live there, None when they are Tares's."""
        if self.get_project_linear(uid):
            return "linear"
        if self.get_project_github(uid):
            return "github"
        return None

    # ── the default project: made the first time something has no other project ──
    DEFAULT_PROJECT_NAME = "Default"

    def default_project_id(self) -> str:
        with self._swrite:
            return self._ensure_default_project()

    def default_project_id_if_any(self) -> str | None:
        with self._swrite:
            return self._default_project_if_any()

    def _default_project_if_any(self) -> str | None:
        """The default project's id when the cell has one, never creating it. Called with the
        lock held."""
        t = projects_table
        saved = self.get_setting("default_project")
        with self.sessions_db.read() as conn:
            if saved and conn.execute(select(t.c.id).where(t.c.id == saved)).first() is not None:
                return saved
            found = conn.execute(select(t.c.id).where(t.c.recipe == "default").order_by(
                t.c.created_at, t.c.id).limit(1)).first()
        return found[0] if found else None

    def _ensure_default_project(self) -> str:
        """The default project's id, creating the project if the cell has none yet. Called with
        the lock held."""
        found = self._default_project_if_any()
        if found:
            uid = found
        else:
            t = projects_table
            uid = "uc_" + uuid.uuid4().hex[:10]
            with self.sessions_db.begin() as conn:
                taken = set(conn.execute(select(t.c.name)).scalars())
                name = self.DEFAULT_PROJECT_NAME if self.DEFAULT_PROJECT_NAME not in taken \
                    else "Default project"
                ts = now_utc()
                conn.execute(insert(t).values(
                    id=uid, recipe="default", name=name, params={}, status="active",
                    created_at=ts, updated_at=ts, last_error=None))
        self.set_setting("default_project", uid)
        return uid

    def log_project(self, uid: str, action: str, detail: str = "") -> None:
        with self._swrite, self.sessions_db.begin() as conn:
            conn.execute(insert(plog_table).values(
                usecase_id=uid, logged_at=now_utc(), action=action, detail=detail[:2000]))

    def list_project_log(self, uid: str, limit: int = 100) -> list[dict]:
        t = plog_table
        with self.sessions_db.read() as conn:
            rows = conn.execute(select(t.c.logged_at, t.c.action, t.c.detail).where(
                t.c.usecase_id == uid).order_by(t.c.logged_at.desc()).limit(limit)).all()
        return [{"at": r[0], "action": r[1], "detail": r[2]} for r in rows]

    # ── GitHub credentials: a token or an App stored once ──
    # The token (and an App's private key) is held verbatim like every other connector secret;
    # redaction is the API's job. `config` holds the kind-specific fields: for `app` the app id,
    # private key, webhook secret and installations; for `app_broker` the broker URL and secret.
    @kept
    def list_github_credentials(self) -> list[dict]:
        t = github_table
        with self.sessions_db.read() as conn:
            rows = conn.execute(select(
                t.c.name, t.c.kind, t.c.token, t.c.api_url, t.c.account, t.c.created_at,
                t.c.updated_at, t.c.config).order_by(t.c.name)).all()
        out = []
        for r in rows:
            try:
                cfg = json.loads(r[7]) if r[7] else {}
            except (TypeError, ValueError):
                cfg = {}
            out.append({"name": r[0], "kind": r[1] or "token", "token": r[2] or "",
                        "api_url": r[3] or "", "account": r[4] or "",
                        "created_at": r[5], "updated_at": r[6],
                        "config": cfg if isinstance(cfg, dict) else {}})
        return out

    def get_github_credential(self, name: str) -> dict | None:
        return next((c for c in self.list_github_credentials() if c["name"] == name), None)

    def upsert_github_credential(self, name: str, token: str, kind: str = "token",
                                 api_url: str = "", account: str = "",
                                 config: dict | None = None) -> None:
        """Create or replace a credential. `config` None keeps the stored one (a token rotation
        does not wipe an App's installations); pass {} to clear it."""
        ts = now_utc()
        t = github_table
        # the write lock: a config kept from a read must not undo a change made after it
        with self._swrite, self.sessions_db.begin() as conn:
            if config is None:
                row = conn.execute(select(t.c.config).where(t.c.name == name)).first()
                cfg_json = row[0] if row and row[0] else None
            else:
                cfg_json = json.dumps(config) if config else None
            conn.execute(self.sessions_db.upsert(t).values(
                name=name, kind=kind, token=token or "", api_url=api_url or "",
                account=account or "", created_at=ts, updated_at=ts,
                config=cfg_json).on_conflict_do_update(
                index_elements=["name"], set_={
                    "kind": kind, "token": token or "", "api_url": api_url or "",
                    "account": account or "", "updated_at": ts, "config": cfg_json}))

    def update_github_credential_config(self, name: str, config: dict) -> None:
        """Replace only the kind-specific fields (an installation added or removed by a webhook)."""
        with self._swrite, self.sessions_db.begin() as conn:
            conn.execute(update(github_table).where(github_table.c.name == name).values(
                config=json.dumps(config) if config else None, updated_at=now_utc()))

    def delete_github_credential(self, name: str) -> None:
        with self._swrite, self.sessions_db.begin() as conn:
            conn.execute(delete(github_table).where(github_table.c.name == name))

    # ── metering ──────────────────────────────────────────────────────────────
    def usage(self) -> dict:
        """What this instance is costing on disk, for the metering endpoint (`storage_usage`). The
        run and delivery counts are 0 since agents and dispatch went (TR-1810); kept for old
        readers."""
        return {**self.storage_usage(), "agent_runs": 0, "dispatch_deliveries": 0}

    def storage_usage(self) -> dict:
        """The disk half of `usage()`, measured as `disk_bytes` measures it: `db_bytes` the session
        store (the SQLite file, or the Postgres database), `wal_bytes` its SQLite WAL,
        `archive_bytes` the archived parts, `events` the lines the sessions hold. /metrics reads
        this on every scrape: sizes and two sums, no table scan."""
        z = self._sizes()
        return {
            "db_bytes": z["db"],
            "wal_bytes": z["wal"],
            "archive_bytes": z["archive"],
            "events": z["lines"],
            # per source counts went with the source catalog's counter (TR-1811)
            "sources": [],
        }

    def key_events(self, source: str, key: str, limit: int = 200,
                   offset: int = 0) -> list[dict]:
        """One entity's events on one source, oldest first (a session read top to bottom)."""
        return self.key_events_from(source, key, limit, offset)[0]

    def key_events_from(self, source: str, key: str, limit: int = 200, offset: int = 0,
                        tail: dict | None = None) -> tuple[list[dict], str]:
        """`key_events` and where the lines came from: `table`, `archive` or `both` (the session's
        own table rows and archive, TR-1507). `tail`: the page is the session's end
        (`session_read._fetch`)."""
        rows, where = session_read.lines(self, source, key, limit, offset, tail)
        return [{"event_type": r["type"], "text": r["text"], "event_time": r["at"],
                 "labels": r["labels"]} for r in rows], where

    def key_payload_rows(self, source: str, key: str, limit: int = 200, offset: int = 0) -> list[dict]:
        """One entity's events with their lossless payload, oldest first (TR-1177: a session as a
        conversation). The payload is the transcript line as it came in."""
        return self.key_payload_rows_from(source, key, limit, offset)[0]

    def key_payload_rows_from(self, source: str, key: str, limit: int = 200, offset: int = 0,
                              tail: dict | None = None) -> tuple[list[dict], str]:
        """`key_payload_rows` and where the lines came from (see `key_events_from`)."""
        rows, where = session_read.lines(self, source, key, limit, offset, tail)
        return [{"event_type": r["type"], "text": r["text"], "event_time": r["at"],
                 "payload": r["payload"]} for r in rows], where

    def key_tool_result(self, source: str, key: str, tool_use_id: str) -> dict | None:
        """The stored line that holds one tool call's result in this entity's events, or None."""
        return self.key_tool_result_from(source, key, tool_use_id)[0]

    def key_tool_result_from(self, source: str, key: str,
                             tool_use_id: str) -> tuple[dict | None, str]:
        """`key_tool_result` and where the lines came from (see `key_events_from`)."""
        return session_read.tool_result(self, source, key, tool_use_id)

    # ── API keys (scoped credentials; only the SHA-256 of the secret is stored) ─
    # `api_keys` lives in the session store (TR-1608): these methods take no lock,
    # so find_api_key, which every authenticated request calls, no longer queues behind them.
    def insert_api_key(self, kid: str, name: str, prefix: str, hash_: str, scopes: list[str],
                       project: str | None = None, person_id: str | None = None) -> None:
        """`project` makes it a project key: it reads that project only (TR-335). `person_id`
        is the person it belongs to."""
        with self.sessions_db.begin() as conn:
            conn.execute(api_keys_table.insert().values(
                id=kid, name=name, prefix=prefix, hash=hash_, scopes=list(scopes),
                created_at=now_utc(), last_used_at=None, revoked_at=None,
                project=project or None, person_id=person_id or None))

    def list_api_keys(self, project: str | None = None) -> list[dict]:
        """Every key, newest first; with `project`, only that project's keys."""
        t = api_keys_table
        q = select(t.c.id, t.c.name, t.c.prefix, t.c.scopes, t.c.created_at, t.c.last_used_at,
                   t.c.revoked_at, t.c.project, t.c.person_id)
        if project is not None:
            q = q.where(t.c.project == project)
        with self.sessions_db.read() as conn:
            rows = conn.execute(q.order_by(t.c.created_at.desc(), t.c.id)).all()
        # a key with no scopes gets none (least privilege), never a crash in the key list
        return [{"id": r[0], "name": r[1], "prefix": r[2], "scopes": r[3] or [],
                 "created_at": r[4], "last_used_at": r[5], "revoked_at": r[6],
                 "project": r[7], "person_id": r[8]} for r in rows]

    def get_api_key(self, kid: str) -> dict | None:
        t = api_keys_table
        with self.sessions_db.read() as conn:
            r = conn.execute(select(t.c.id, t.c.name, t.c.prefix, t.c.scopes, t.c.created_at,
                                    t.c.last_used_at, t.c.revoked_at, t.c.project,
                                    t.c.person_id).where(t.c.id == kid)).first()
        if not r:
            return None
        return {"id": r[0], "name": r[1], "prefix": r[2], "scopes": r[3] or [],
                "created_at": r[4], "last_used_at": r[5], "revoked_at": r[6],
                "project": r[7], "person_id": r[8]}

    # Licence-independent by design (TR-915): never consult licence or team here.
    def find_api_key(self, hash_: str) -> dict | None:
        """Active (non-revoked) key by secret hash, or None."""
        t = api_keys_table
        with self.sessions_db.read() as conn:
            r = conn.execute(select(t.c.id, t.c.name, t.c.scopes, t.c.last_used_at, t.c.project,
                                    t.c.person_id).where(
                t.c.hash == hash_, t.c.revoked_at.is_(None))).first()
        if not r:
            return None
        return {"id": r[0], "name": r[1], "scopes": r[2] or [], "last_used_at": r[3],
                "project": r[4], "person_id": r[5]}

    # Licence-independent by design (TR-915): never consult licence or team here.
    def key_person_id(self, kid: str) -> str | None:
        """The person an active key is bound to, None for an unbound or unknown key (unlike a
        credential's person, this never falls back to `local`)."""
        t = api_keys_table
        with self.sessions_db.read() as conn:
            return conn.execute(select(t.c.person_id).where(
                t.c.id == kid, t.c.revoked_at.is_(None))).scalar_one_or_none()

    def set_key_person(self, kid: str, person_id: str | None) -> bool:
        """Bind an active key to a person (None unbinds it). False when there is no such key."""
        t = api_keys_table
        with self.sessions_db.begin() as conn:
            done = conn.execute(update(t).where(t.c.id == kid, t.c.revoked_at.is_(None)).values(
                person_id=person_id))
        return done.rowcount > 0

    # ── People: who is on the cell. A key belongs to one; the credential names the person ─
    _PERSON_COLS = tuple(people_table.c[n] for n in (
        "id", "name", "email", "emails", "github", "is_admin", "created_at", "created_via",
        "picture", "removed_at"))
    PICTURE_MAX = 2048

    @staticmethod
    def _person_row(r) -> dict:
        return {"id": r[0], "name": r[1], "email": r[2],
                "emails": list(r[3] or []), "github": r[4],
                "is_admin": bool(r[5]), "created_at": r[6], "created_via": r[7],
                "picture": r[8] or None, "removed_at": r[9]}

    @classmethod
    def check_picture(cls, picture) -> str | None:
        """A picture address as stored: None for none (None or empty), ValueError when it is not
        one https address of at most PICTURE_MAX characters. The cell never fetches it; the
        console shows it in an img (TR-1016)."""
        if picture is None:
            return None
        if not isinstance(picture, str):
            raise ValueError("picture must be an https address")
        p = picture.strip()
        if not p:
            return None
        if len(p) > cls.PICTURE_MAX:
            raise ValueError(f"picture is at most {cls.PICTURE_MAX} characters")
        if not re.match(r"^https://[^\s/?#]+[^\s]*$", p) or any(c in p for c in "\"'<>"):
            raise ValueError("picture must be an https address")
        return p

    @staticmethod
    def _norm_email(e) -> str | None:
        e = str(e or "").strip().lower()
        return e or None

    @classmethod
    def check_email(cls, email) -> str | None:
        """The main email, lowercased: None for none (None or empty), ValueError when it is not
        a string holding an @."""
        if email is None:
            return None
        if not isinstance(email, str):
            raise ValueError("email must be an email address")
        e = cls._norm_email(email)
        if e and "@" not in e:
            raise ValueError(f"{e!r} is not an email address")
        return e

    @classmethod
    def _norm_emails(cls, emails) -> list[str]:
        """A list of addresses, lowercased, without repeats. Anything else (a bare string would
        be split into letters) or an entry without an @ raises ValueError."""
        if emails is None:
            return []
        if not isinstance(emails, list) or not all(isinstance(x, str) for x in emails):
            raise ValueError("emails must be a list of email addresses")
        out = [e for e in dict.fromkeys(cls._norm_email(x) for x in emails) if e]
        bad = next((e for e in out if "@" not in e), None)
        if bad:
            raise ValueError(f"{bad!r} is not an email address")
        return out

    def _taken_emails(self, emails: list[str], besides: str | None = None, conn=None) -> str | None:
        """The first of `emails` another person already has (as the main or an extra email)."""
        people = self._people(conn)
        for e in emails:
            for p in people:
                if p["id"] != besides and e in ([p["email"]] + p["emails"]):
                    return e
        return None

    def create_person(self, name: str, email: str | None = None, emails: list[str] | None = None,
                      github: str | None = None, is_admin: bool = False,
                      via: str = "admin") -> dict:
        """Add a person. Emails and the GitHub login are kept lowercased and are unique across
        people; a clash raises ValueError naming it."""
        name = str(name or "").strip()
        if not name:
            raise ValueError("name is required")
        email = self.check_email(email)
        extra = self._norm_emails(emails)
        github = str(github or "").strip().lstrip("@").lower() or None
        pid = "p_" + uuid.uuid4().hex[:10]
        # the check and the insert are one transaction (one writer at a time on SQLite) and sit
        # under the write lock so two callers in this process cannot both pass the check on Postgres
        with self._swrite, self.sessions_db.begin() as conn:
            clash = self._taken_emails(([email] if email else []) + extra, conn=conn)
            if clash:
                raise ValueError(f"{clash} already belongs to another person")
            if github and any(p["github"] == github for p in self._people(conn)):
                raise ValueError(f"the GitHub login {github} already belongs to another person")
            conn.execute(people_table.insert().values(
                id=pid, name=name, email=email, emails=extra, github=github,
                is_admin=bool(is_admin), created_at=now_utc(), created_via=via))
        return self.get_person(pid)

    @kept
    def get_person(self, pid: str) -> dict | None:
        with self.sessions_db.read() as conn:
            r = conn.execute(select(*self._PERSON_COLS).where(people_table.c.id == pid)).first()
        return self._person_row(r) if r else None

    def _people(self, conn=None) -> list[dict]:
        """list_people; `conn` is the caller's open session store transaction, if it has one."""
        q = select(*self._PERSON_COLS).where(people_table.c.removed_at.is_(None)).order_by(
            people_table.c.created_at, people_table.c.id)
        if conn is None:
            with self.sessions_db.read() as c:
                rows = c.execute(q).all()
        else:
            rows = conn.execute(q).all()
        return [self._person_row(r) for r in rows]

    @kept
    def list_people(self) -> list[dict]:
        """Everyone on the cell, oldest first (`local` first on a cell that has it)."""
        return self._people()

    def find_person_by_email(self, email: str) -> dict | None:
        """The person with this main or extra email, ignoring case."""
        e = self._norm_email(email)
        if not e:
            return None
        return next((p for p in self.list_people() if e in ([p["email"]] + p["emails"])), None)

    def find_person_by_github(self, login: str) -> dict | None:
        g = str(login or "").strip().lstrip("@").lower()
        if not g:
            return None
        return next((p for p in self.list_people() if p["github"] == g), None)

    def update_person(self, pid: str, **fields) -> dict | None:
        """Change name, email, emails, github, is_admin or picture (anything else is ignored). None
        when there is no such person; ValueError when an email or login belongs to someone else,
        or the picture is not an https address."""
        cur = self.get_person(pid)
        if not cur:
            return None
        new = dict(cur)
        if "name" in fields:
            new["name"] = str(fields["name"] or "").strip()
            if not new["name"]:
                raise ValueError("name cannot be empty")
        if "email" in fields:
            new["email"] = self.check_email(fields["email"])
        if "emails" in fields:
            new["emails"] = self._norm_emails(fields["emails"])
        if "github" in fields:
            new["github"] = str(fields["github"] or "").strip().lstrip("@").lower() or None
        if "is_admin" in fields:
            new["is_admin"] = bool(fields["is_admin"])
        if "picture" in fields:
            new["picture"] = self.check_picture(fields["picture"])
        with self._swrite, self.sessions_db.begin() as conn:
            clash = self._taken_emails(([new["email"]] if new["email"] else []) + new["emails"],
                                       besides=pid, conn=conn)
            if clash:
                raise ValueError(f"{clash} already belongs to another person")
            if new["github"] and any(p["github"] == new["github"] and p["id"] != pid
                                     for p in self._people(conn)):
                raise ValueError(f"the GitHub login {new['github']} already belongs to another person")
            conn.execute(update(people_table).where(people_table.c.id == pid).values(
                name=new["name"], email=new["email"], emails=new["emails"], github=new["github"],
                is_admin=new["is_admin"], picture=new["picture"]))
        return self.get_person(pid)

    def remove_person(self, pid: str) -> bool:
        """They left: off the list of people and the seat count, every key bound to them revoked, no
        longer lead of a project or assignee of an open ticket. The row stays so what they did keeps their
        name, and coming back makes a new person.
        False when there is no such person on the cell. `local` owns what has no owner: ValueError."""
        if pid == "local":
            raise ValueError("this person cannot be removed")
        cur = self.get_person(pid)
        if not cur or cur.get("removed_at"):
            return False
        with self.sessions_db.read() as conn:
            keys = list(conn.execute(select(api_keys_table.c.id).where(
                api_keys_table.c.person_id == pid, api_keys_table.c.revoked_at.is_(None))).scalars())
        for kid in keys:
            self.revoke_api_key(kid)
        t = tickets_table
        # one transaction: the guard above answers False once removed_at is set, so a crash must
        # not leave it set with the tickets still theirs
        with self._swrite, self.sessions_db.begin() as conn:
            # open tickets only: a finished one keeps the name of who did it. A NULL status or
            # stage is open (NOT IN is not true for NULL, TR-1692)
            conn.execute(update(t).where(
                t.c.assignee_person_id == pid,
                or_(t.c.status.is_(None), t.c.status.not_in(("done", "canceled"))),
                or_(t.c.stage.is_(None), t.c.stage.not_in(("merged", "shipped"))))
                .values(assignee_person_id=None))
            # what they led or held is nobody's now: it falls to the next owner in line
            # (factory_owners), and the project says it has no lead
            conn.execute(update(projects_table).where(
                projects_table.c.lead_person_id == pid).values(lead_person_id=None))
            conn.execute(update(people_table).where(people_table.c.id == pid).values(
                removed_at=now_utc()))
        return True

    def claim_local(self, email: str, name: str | None = None) -> dict | None:
        """The cell's owner arrives: `local` becomes them (their name and email) instead of a new
        person being made, so what `local` owns (projects without a lead, desk items addressed to
        no one, keys bound to no one) stays theirs. Only while `local` is unclaimed (no email)
        and nobody else has this email; None otherwise."""
        e = self.check_email(email)
        if not e:
            return None
        with self._swrite, self.sessions_db.begin() as conn:
            local = next((p for p in self._people(conn) if p["id"] == "local"), None)
            if not local or local["email"] or local["emails"]:
                return None
            if self._taken_emails([e], conn=conn):
                return None
            conn.execute(update(people_table).where(people_table.c.id == "local").values(
                name=str(name or "").strip() or e.split("@")[0], email=e))
        return self.get_person("local")

    def person_or_create(self, email: str, name: str | None = None, via: str = "key",
                         owner: bool = False) -> dict:
        """The person with this email, made first when there is none (named by `name`, else by
        the part of the email before the @). Asking twice for one email gives one person. With
        `owner` (the cell's owner, as the control plane says) an unclaimed `local` becomes them."""
        e = self.check_email(email)
        if not e:
            raise ValueError("an email is required")
        found = self.find_person_by_email(e)
        if found:
            return found
        if owner:
            claimed = self.claim_local(e, name)
            if claimed:
                return claimed
        try:
            return self.create_person(str(name or "").strip() or e.split("@")[0], email=e, via=via)
        except ValueError:
            # two callers asked for one new email at once: the other one made the person first
            found = self.find_person_by_email(e)
            if found:
                return found
            raise

    @kept
    def only_local_person(self) -> bool:
        """True while `local` is the only person on the cell: a key nobody has bound still acts
        as them, so a single-user cell keeps working."""
        with self.sessions_db.read() as conn:
            rows = list(conn.execute(select(people_table.c.id).where(
                people_table.c.removed_at.is_(None)).limit(2)).scalars())
        return rows == ["local"]

    def touch_api_key(self, kid: str) -> None:
        with self.sessions_db.begin() as conn:
            conn.execute(update(api_keys_table).where(api_keys_table.c.id == kid).values(
                last_used_at=now_utc()))

    def revoke_api_key(self, kid: str) -> bool:
        """Revoke the key. False when there is no active key `kid`."""
        t = api_keys_table
        with self._swrite:
            with self.sessions_db.read() as conn:
                hit = conn.execute(select(t.c.id).where(
                    t.c.id == kid, t.c.revoked_at.is_(None))).first()
            if not hit:
                return False
            with self.sessions_db.begin() as conn:
                conn.execute(update(t).where(t.c.id == kid, t.c.revoked_at.is_(None)).values(
                    revoked_at=now_utc()))
        return True
