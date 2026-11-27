"""Storage for the software factory on Tares (P-TR-176, M2 onward), mixed into Store.

Validation lives in tares/factory.py; these methods trust what they are handed. Every method takes
the store's lock, like the rest of Store. Tables are created after Store's own schema and
migrations (FACTORY_SCHEMA), so they can refer to columns those add.
"""
from __future__ import annotations

import json
import logging
import re
import uuid

from sqlalchemy import (Integer, Text, and_, bindparam, case, cast, delete, false, func, insert,
                        literal, null, or_, select, type_coerce, update)
from sqlalchemy import text as sql_text

from . import claude_usage_hourly as hourly
from . import metrics, session_ingest
from .envelope import now_utc
from .integrations import SLOTS
from .request_memo import kept
from .session_store.pr_columns import pr_columns
from .session_store.tables import (project_sessions, session_commands, session_messages,
                                   session_states, session_stations, session_turns, session_usage,
                                   session_usage_hourly)
from .session_store.tables import sessions as sessions_table
from .session_store.tables import usecases as projects_table
from .session_store.tables import people as people_table
from .session_store.tables import ticket_history as history_table
from .session_store.tables import ticket_numbers as numbers_table
from .session_store.tables import tickets as tickets_table
from .session_store.tables import milestones as milestones_table
from .session_store.tables import milestone_check_results as milestone_results_table
from .session_store.tables import milestone_acceptances as acceptances_table
from .session_store.tables import ticket_checks as checks_table
from .session_store.tables import challenges as challenges_table
from .session_store.tables import reviews as reviews_table
from .session_store.tables import review_findings as findings_table
from .session_store.tables import rule_proposals as proposals_table
from .session_store.tables import assumptions as assumptions_table
from .session_store.tables import desk_items as desk_table
from .session_store.tables import decisions as decisions_table
from .session_store.tables import handovers as handovers_table
from .session_store.tables import handover_overrides as overrides_table
from .session_store.tables import grants as grants_table
from .session_store.tables import pending_memory as pending_table
from .session_store.tables import crews as crews_table
from .session_store.tables import spec_questions as questions_table
from .session_store.tables import signoffs as signoffs_table
from .session_store.tables import answers as answers_table
from .session_store.tables import waiting_told as told_table
from .session_store.tables import guardrail_rules as rules_table
from .session_store.tables import guardrail_pairs as pairs_table
from .session_store.tables import integration_choices as choices_table
from .session_store.tables import integration_connections as connections_table
from .session_store.tables import project_integrations as picks_table
from .session_store.tables import pending_writeback as writeback_table
from .session_store.tables import project_secrets as secrets_table
from .session_store.tables import project_required_checks as required_table
from .session_store.tables import merge_queue as queue_table
from .session_store.tables import project_merge_method as method_table
from .session_store.tables import repo_maps as maps_table
from .session_store.tables import repo_map_history as map_history_table
from .session_store.tables import brain_repos as brain_table
from .session_store.tables import brain_repo_folders as brain_folders_table
from .session_store.tables import brain_repo_updates as brain_updates_table
from .session_store.tables import brain_calls as brain_calls_table
from .session_store.types import UTCDateTime

log = logging.getLogger("tares.factory_store")


def _loggable(x) -> str:
    """A stored name or key for a log line: control characters written out, so a newline in a
    stored row cannot start a forged line."""
    return re.sub(r"[\x00-\x1f\x7f]", lambda m: repr(m.group())[1:-1], str(x))


def _no_nul(v):
    """Free text for a text column: Postgres takes no NUL byte in text, so a string is kept without
    it, on both engines. Anything else comes back as it is."""
    return v.replace("\x00", "") if isinstance(v, str) else v


# A real challenger line nests a few levels. The bound is far under where the parser, a copy or the
# response encoder raises RecursionError, which varies with the Python version and the call stack.
CHALLENGE_MAX_DEPTH = 100


def challenge_json(text):
    """`json.loads` for a challenger line's payload. One nested more than CHALLENGE_MAX_DEPTH
    objects and lists deep raises ValueError, as text that is not JSON does (TR-1712)."""
    try:
        o = json.loads(text)
    except RecursionError:
        raise ValueError("nested too deeply") from None
    level = [o]
    for _ in range(CHALLENGE_MAX_DEPTH + 1):     # level by level: no recursion here
        level = [x for x in level if isinstance(x, (dict, list))]
        if not level:
            return o
        level = [v for x in level for v in (x.values() if isinstance(x, dict) else x)]
    raise ValueError("nested too deeply")


FACTORY_SCHEMA = [
    # Which station a Claude Code session plays (TR-418, TR-420), from the plugin's
    # `session_station` line: the station's stable name, its role, the station that started it
    # (builders and helpers), and the project it was started for (TARES_PROJECT, builders).
    # A station is played over time by a chain of sessions; the newest live one is current.
    """CREATE TABLE IF NOT EXISTS session_stations (
      session     TEXT PRIMARY KEY,
      station     TEXT,
      role        TEXT,
      parent      TEXT,
      project     TEXT,
      seen_at     TIMESTAMPTZ
    )""",
    # The crew (TR-628) the station line belongs to: a row of `crews` (session_store/tables.py).
    "ALTER TABLE session_stations ADD COLUMN IF NOT EXISTS crew_id TEXT",
]

# The slots a project may choose for itself; the others are the factory's alone (TR-839).
PROJECT_SLOTS = ("tickets", "review", "ci")

# a station row lists this many of its earlier sessions; the rest are read a page at a time (TR-1500)
EARLIER_KEPT = 5
# the monitoring integrations kept in the tracing settings (TR-866)
MONITORING_NAMES = ("off", "rius", "otlp")

# Words that say "a session" and not a station: shown without parentheses next to a person's name.
GENERIC_BY = ("", "a session", "claude code session", "github")


def by_label(person: dict | None, by: str | None) -> str | None:
    """How a record of who did it reads: "Kiran (crew-builder-1)" for a person acting through a
    station, the person's name alone when nothing names a station, and what was stored (the
    station, the caller's name) when no person stood behind the call."""
    name = (person or {}).get("name")
    by = (by or "").strip()
    if not name:
        return by or None
    return name if by in GENERIC_BY or by == name else f"{name} ({by})"


class MapChanged(Exception):
    """A repo map was written since the writer read it; `.current` is the map now (None: none)."""

    def __init__(self, current: dict | None):
        super().__init__("the repo map changed since it was read")
        self.current = current


class FactoryStore:
    """Methods Store gains for the factory. Expects self.con and self._lock."""

    # ── the person behind a record (TR-619) ───────────────────────────────────
    def _person_refs(self, ids) -> dict[str, dict]:
        """{id: {id, name}} for the people among `ids`. Call without the lock held."""
        ids = sorted({i for i in ids if i})
        if not ids:
            return {}
        with self.sessions_db.read() as conn:
            rows = conn.execute(select(people_table.c.id, people_table.c.name).where(
                people_table.c.id.in_(ids))).all()
        return {r[0]: {"id": r[0], "name": r[1]} for r in rows}

    def _with_people(self, rows: list[dict], by_key: str | None = "by") -> list[dict]:
        """Each row's `person_id` becomes `person` ({id, name} or None), and `by_label` says who
        did it in words (the person, and the station when there is one). `by_key` is the row's
        own name for the station; None adds the person alone."""
        refs = self._person_refs(r.get("person_id") for r in rows)
        for r in rows:
            r["person"] = refs.get(r.pop("person_id", None))
            if by_key:
                r["by_label"] = by_label(r["person"], r.get(by_key))
        return rows

    # ── upgrade and clean-up ──────────────────────────────────────────────────
    def _lock_numbering(self, conn, key: str) -> None:
        """Until the transaction of `conn` ends, a second process doing the same read-then-write
        under `key` waits. SQLite needs nothing: `begin()` already holds the one write lock."""
        if self.sessions_db.dialect == "postgresql":
            conn.execute(sql_text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": key})

    def _merge_split_crews(self) -> None:
        """A person's crew recorded without its machine, before the cell had a licence, and the same
        crew recorded with one since are one crew (TR-1223): its sessions move to the crew with the
        machine and the bare record goes. Only when the person has exactly one crew of that name with
        a machine; with several it can't be told which. One transaction."""
        t = crews_table
        with self.sessions_db.begin() as conn:
            self._lock_numbering(conn, "tares_crews")
            groups: dict = {}
            for r in conn.execute(select(t)).fetchall():
                groups.setdefault((r.person_id, r.name), []).append(r)
            for crews in groups.values():
                bare = [c for c in crews if not c.machine]
                named = [c for c in crews if c.machine]
                if len(named) != 1 or not bare:
                    continue
                keep = named[0]
                firsts = [c.first_seen for c in [keep, *bare] if c.first_seen is not None]
                lasts = [c.last_seen for c in [keep, *bare] if c.last_seen is not None]
                for b in bare:
                    conn.execute(update(session_stations).where(session_stations.c.crew_id == b.id)
                                 .values(crew_id=keep.id))
                    conn.execute(delete(t).where(t.c.id == b.id))
                conn.execute(update(t).where(t.c.id == keep.id).values(
                    first_seen=min(firsts) if firsts else None,
                    last_seen=max(lasts) if lasts else None))

    def _first_override_entries(self) -> None:
        """An override kept before the log existed is its first entry (TR-640). One with no time of
        its own takes the hand-over's, else now, so a log entry always has a time, and the
        hand-over is stamped the same way: get_handover compares the two times to say it was
        overridden. One transaction."""
        h, o = handovers_table, overrides_table
        now = literal(now_utc(), UTCDateTime())
        with self.sessions_db.begin() as conn:
            self._lock_numbering(conn, "tares_override_log")
            conn.execute(insert(o).from_select(
                ["project", "at", "reason", "person_id", "handed_by"],
                select(h.c.project, func.coalesce(h.c.override_at, h.c.handed_at, now),
                       h.c.override_reason, func.coalesce(h.c.override_person_id, h.c.person_id),
                       h.c.handed_by)
                .where(h.c.override_reason.is_not(None), h.c.project.not_in(
                    select(o.c.project).where(o.c.project.is_not(None))))))
            conn.execute(update(h).where(h.c.override_reason.is_not(None), h.c.override_at.is_(None))
                         .values(override_at=func.coalesce(h.c.handed_at, now)))

    def _factory_upgrade(self) -> None:
        """Tickets from before numbers get them, in plan order, per project. Sessions that played
        a station before crews existed (TR-628) join the crew `crew` of the person `local`."""
        with self.sessions_db.read() as conn:
            bare = conn.execute(select(session_stations.c.session).where(
                session_stations.c.crew_id.is_(None)).limit(1)).first()
        if bare:
            cid = self._crew_id("crew", "local", "", now_utc())
            with self.sessions_db.begin() as conn:
                conn.execute(update(session_stations).where(session_stations.c.crew_id.is_(None))
                             .values(crew_id=cid))
        self._merge_split_crews()
        self._first_override_entries()
        t, n = tickets_table, numbers_table
        with self.sessions_db.begin() as conn:
            for project in conn.execute(select(t.c.project).where(
                    t.c.number.is_(None), t.c.project.is_not(None)).distinct()).scalars().all():
                top = conn.execute(select(func.max(t.c.number)).where(
                    t.c.project == project)).scalar() or 0
                todo = conn.execute(select(t.c.id).where(
                    t.c.project == project, t.c.number.is_(None)
                ).order_by(*self._ticket_order())).scalars().all()
                for i, tid in enumerate(todo, top + 1):
                    conn.execute(update(t).where(t.c.id == tid).values(number=i))
                ins = self.sessions_db.upsert(n).values(project=project, last=top + len(todo))
                conn.execute(ins.on_conflict_do_update(index_elements=["project"], set_={
                    "last": self.sessions_db.greatest(n.c.last, ins.excluded["last"])}))
        self._credentials_to_factory()

    def _credentials_to_factory(self) -> None:
        """Credentials live only at the factory (TR-1380). Each step is idempotent, so a start with
        nothing to move changes nothing:

          1. an integration_choices row from before holds the integration settings: they become
             that integration's connection (unless it already has one) and the row keeps only the
             pointer. Monitoring has none: it lives in the tracing settings.
          2. a project's own row keeps its non-secret, non-address fields. The secrets and
             addresses it held move to the factory connection of the same integration, only when
             that integration has none yet; the project's copy goes either way. The integration
             always ends up with a connection (empty if no row held anything to move, made after
             all the rows that did), so the project's pick stays connected. An integration that is not installed has no schema,
             so every key counts as a secret.
          3. a guardrail pair's integration settings move the same way, and the pair keeps none.
             Pairs of one scope and project that are then the same (hook, integration, on_fail)
             are one: the oldest stays.

        Sealed project secrets (TR-775) are not touched. One transaction. Lock held, or no other
        thread yet."""
        from .integrations import registry

        def moves(slot: str, name: str, key: str) -> bool:
            cls = registry.get(slot, name)
            if cls is None:
                return True
            kinds = {f.name: f.kind for f in cls.settings_schema}
            return kinds.get(key, "secret") in ("secret", "url")

        def connect(conn, slot: str, name: str, settings: dict, at, by, origin: str = "") -> None:
            """Make the connection unless there is one. When there is, the settings given are
            dropped: each key whose value differs from the kept one is logged, by integration,
            origin and key name only, never the value."""
            t = connections_table
            kept = conn.execute(select(t.c.settings).where(t.c.slot == slot, t.c.name == name)).first()
            if kept is not None:
                have = self._load_settings(kept[0])
                for k in sorted(k for k, v in settings.items() if have.get(k) != v):
                    log.warning("integration upgrade: %s %s: %s from %s differs from the connection "
                                "kept at the factory and is dropped", slot, _loggable(name), _loggable(k),
                                _loggable(origin or "a row"))
                return
            conn.execute(insert(t).values(slot=slot, name=name, settings=settings, updated_at=at,
                                          updated_by=by))

        at = now_utc()
        bare: list = []
        choices, picks, pairs = choices_table, picks_table, pairs_table
        with self.sessions_db.begin() as conn:
            self._lock_numbering(conn, "tares_integrations")
            for slot, name, raw, up_at, up_by in conn.execute(
                    select(choices.c.slot, choices.c.name, choices.c.settings, choices.c.updated_at,
                           choices.c.updated_by)
                    .where(choices.c.slot != "monitoring").order_by(choices.c.slot)).fetchall():
                have = self._load_settings(raw)
                connect(conn, slot, name, have, up_at or at, up_by or "upgrade",
                        "the factory's earlier choice")
                if have:
                    conn.execute(update(choices).where(choices.c.slot == slot).values(settings={}))
            for uid, slot, name, raw in conn.execute(
                    select(picks.c.project_uid, picks.c.slot, picks.c.name, picks.c.settings)
                    .order_by(picks.c.project_uid, picks.c.slot)).fetchall():
                have = self._load_settings(raw)
                gone = {k: v for k, v in have.items() if moves(slot, name, k)}
                if not gone:
                    # the pick needs a connection to stand on, made below once every row that holds
                    # something to move has had its turn: an empty one made now would win over it
                    bare.append((slot, name))
                    continue
                connect(conn, slot, name, gone, at, "upgrade", f"project {uid}")
                conn.execute(update(picks).where(picks.c.project_uid == uid, picks.c.slot == slot)
                             .values(settings={k: v for k, v in have.items() if k not in gone}))
            seen: set = set()
            for pid, scope, uid, hook, name, raw, on_fail, made in sorted(
                    conn.execute(select(pairs.c.id, pairs.c.scope, pairs.c.project_uid, pairs.c.hook,
                                        pairs.c.integration, pairs.c.settings, pairs.c.on_fail,
                                        pairs.c.created_at)).fetchall(),
                    key=lambda r: int(r[0][2:])):
                have = self._load_settings(raw)
                if have:
                    connect(conn, "guardrails", name, have, made or at, "upgrade", f"guardrail pair {pid}")
                    conn.execute(update(pairs).where(pairs.c.id == pid).values(settings={}))
                key = (scope, uid, hook, name, on_fail or "block")
                if key in seen:
                    conn.execute(delete(pairs).where(pairs.c.id == pid))
                seen.add(key)
            for slot, name in bare:
                connect(conn, slot, name, {}, at, "upgrade")

    def _next_ticket_number(self, conn, project: str) -> int:
        """The project's next ticket number, never one given out before. One statement in the
        caller's transaction, so two writers cannot get the same number: the second waits for the
        project's row until the first commits, and adds to what it finds there."""
        t, n = tickets_table, numbers_table
        after_tickets = select(func.coalesce(func.max(t.c.number), 0) + 1).where(
            t.c.project == project).scalar_subquery()
        ins = self.sessions_db.upsert(n).values(project=project, last=after_tickets)
        return conn.execute(ins.on_conflict_do_update(index_elements=["project"], set_={
            "last": self.sessions_db.greatest(n.c.last + 1, ins.excluded["last"])
        }).returning(n.c.last)).scalar_one()

    def _factory_forget_project(self, uid: str) -> None:
        """A project is deleted: its factory rows go. Lock held."""
        with self.sessions_db.begin() as conn:
            conn.execute(delete(numbers_table).where(numbers_table.c.project == uid))
            for t in (history_table, milestones_table, checks_table, milestone_results_table,
                      acceptances_table, reviews_table, findings_table, assumptions_table,
                      desk_table, grants_table, decisions_table, handovers_table, overrides_table,
                      questions_table, signoffs_table, rules_table, secrets_table,
                      required_table, method_table):
                conn.execute(delete(t).where(t.c.project == uid))
            for t in (pairs_table, picks_table, queue_table):
                conn.execute(delete(t).where(t.c.project_uid == uid))
            conn.execute(delete(pending_table).where(pending_table.c.scope == uid))
        self._secrets_changed()

    # ── milestones (TR-411) ───────────────────────────────────────────────────
    @staticmethod
    def _ms_row(r) -> dict:
        return {**r._mapping, "goal": r.goal or "", "checks": r.checks or []}

    @staticmethod
    def _ms_order():
        t = milestones_table
        return (t.c.position.asc().nulls_last(), t.c.created_at.asc().nulls_last(), t.c.id)

    @staticmethod
    def _insert_milestone(conn, project: str, name: str, goal: str, checks: list[dict] | None,
                          position: float | None, owner: str, external_id: str | None) -> str:
        """A new row, after the project's last when no position is given. In the caller's
        transaction."""
        t = milestones_table
        if position is None:
            last = conn.execute(select(func.max(t.c.position)).where(
                t.c.project == project)).scalar()
            position = (last or 0) + 1
        mid = "ms_" + uuid.uuid4().hex[:10]
        ts = now_utc()
        conn.execute(insert(t).values(
            id=mid, project=project, owner=owner, external_id=external_id, name=name,
            goal=goal or "", checks=checks or [], position=float(position), created_at=ts,
            updated_at=ts))
        return mid

    def create_milestone(self, project: str, name: str, goal: str = "",
                         checks: list[dict] | None = None, position: float | None = None,
                         owner: str = "tares", external_id: str | None = None) -> str:
        with self._swrite, self.sessions_db.begin() as conn:
            return self._insert_milestone(conn, project, name, goal, checks, position, owner,
                                          external_id)

    def adopt_milestone(self, project: str, owner: str, external_id: str, name: str,
                        goal: str | None, checks: list[dict] | None = None,
                        position: float | None = None, new_goal: str = "") -> str:
        """The Tares row of a milestone that was just made in Linear or GitHub (TR-851): the row of
        this project with this owner and external id, else the project's Tares-owned milestone of
        the same name (written before the milestone existed there), else a new one. Look and write
        are one locked step. A row found takes the owner, the external id and the goal and checks
        given (a position only when given). `new_goal` is the goal of a new row when `goal` is None,
        so a sync can fill a new row from the tracker without touching a found one. The syncs
        insert through here too. Returns the row's id."""
        t = milestones_table
        with self._swrite, self.sessions_db.begin() as conn:
            found = None
            if external_id is not None:      # `== None` would match every row without one
                found = conn.execute(select(t.c.id).where(
                    t.c.project == project, t.c.owner == owner,
                    t.c.external_id == external_id).order_by(*self._ms_order()).limit(1)).scalar()
            if found is None:
                # the name is compared here: SQLite's lower() changes ASCII letters only
                low = name.lower()
                found = next((r.id for r in conn.execute(select(t.c.id, t.c.name).where(
                    t.c.project == project, t.c.owner == "tares").order_by(*self._ms_order()))
                    if r.name is not None and r.name.lower() == low), None)
            if found is None:
                return self._insert_milestone(conn, project, name,
                                              goal if goal is not None else new_goal, checks,
                                              position, owner, external_id)
            values: dict = {"owner": owner, "external_id": external_id, "updated_at": now_utc()}
            if goal is not None:
                values["goal"] = goal
            if checks is not None:
                values["checks"] = checks
            if position is not None:
                values["position"] = float(position)
            conn.execute(update(t).where(t.c.id == found).values(values))
            return found

    def update_milestone(self, mid: str, **fields) -> None:
        allowed = {"name", "goal", "checks", "position", "owner", "external_id"}
        values: dict = {"updated_at": now_utc()}
        for col, v in fields.items():
            if col not in allowed:
                continue
            values[col] = (v or []) if col == "checks" else v
        t = milestones_table
        with self._swrite, self.sessions_db.begin() as conn:
            conn.execute(update(t).where(t.c.id == mid).values(values))

    def record_milestone_check(self, project: str, milestone: str, idx: int, result: str,
                               note: str | None, by_kind: str, by_name: str | None,
                               person_id: str | None, at) -> None:
        with self._swrite, self.sessions_db.begin() as conn:
            conn.execute(insert(milestone_results_table).values(
                id="mc_" + uuid.uuid4().hex[:10], project=project, milestone=milestone,
                idx=int(idx), result=result, note=note or None, by_kind=by_kind,
                by_name=by_name or None, person_id=person_id or None, checked_at=at))

    def milestone_check_results(self, milestones: list[str]) -> dict[str, dict[int, dict]]:
        """{milestone: {index: the latest result {result, note, by_kind, by_name, at}}}."""
        if not milestones:
            return {}
        t = milestone_results_table
        with self.sessions_db.read() as conn:
            rows = conn.execute(select(
                t.c.milestone, t.c.idx, t.c.result, t.c.note, t.c.by_kind, t.c.by_name,
                t.c.person_id, t.c.checked_at).where(t.c.milestone.in_(list(milestones)))
                .order_by(t.c.checked_at.asc().nulls_last(), t.c.id)).fetchall()
        out: dict[str, dict[int, dict]] = {}
        for r in rows:
            out.setdefault(r.milestone, {})[int(r.idx)] = {
                "result": r.result, "note": r.note, "by_kind": r.by_kind, "by_name": r.by_name,
                "person_id": r.person_id, "at": r.checked_at}
        return out

    def accept_milestone(self, project: str, milestone: str, by_name: str | None,
                         person_id: str | None, at) -> None:
        t = acceptances_table
        with self._swrite, self.sessions_db.begin() as conn:
            conn.execute(delete(t).where(t.c.milestone == milestone))
            conn.execute(insert(t).values(
                milestone=milestone, project=project, by_name=by_name or None,
                person_id=person_id or None, accepted_at=at))

    def milestone_acceptances(self, milestones: list[str]) -> dict[str, dict]:
        if not milestones:
            return {}
        t = acceptances_table
        with self.sessions_db.read() as conn:
            rows = conn.execute(select(t.c.milestone, t.c.by_name, t.c.person_id, t.c.accepted_at)
                                .where(t.c.milestone.in_(list(milestones)))).fetchall()
        return {r.milestone: {"by": r.by_name, "person_id": r.person_id, "at": r.accepted_at}
                for r in rows}

    def list_milestones(self, project: str) -> list[dict]:
        t = milestones_table
        with self.sessions_db.read() as conn:
            rows = conn.execute(select(t).where(t.c.project == project)
                                .order_by(*self._ms_order())).fetchall()
        return [self._ms_row(r) for r in rows]

    @kept
    def milestones_by_project(self, projects: list[str]) -> dict[str, list[dict]]:
        """{project: its milestones in order} in one query (the home page)."""
        if not projects:
            return {}
        t = milestones_table
        with self.sessions_db.read() as conn:
            rows = conn.execute(select(t).where(t.c.project.in_(list(projects)))
                                .order_by(*self._ms_order())).fetchall()
        out: dict[str, list[dict]] = {p: [] for p in projects}
        for r in rows:
            out[r.project].append(self._ms_row(r))
        return out

    def get_milestone(self, project: str, ref: str) -> dict | None:
        """A milestone of the project by id, Linear id, or name (any case)."""
        ref = (ref or "").strip()
        if not ref:
            return None
        # matched here, not in SQL: SQLite's lower() changes ASCII letters only
        low = ref.lower()
        return next((m for m in self.list_milestones(project)
                     if m["id"] == ref or m["external_id"] == ref
                     or (m["name"] is not None and m["name"].lower() == low)), None)

    # ── crews (TR-628) ────────────────────────────────────────────────────────
    def _crew_id(self, name: str, person_id: str, machine: str, at) -> str:
        """The id of the crew (person, name, machine), made on first sight; last_seen moves up
        to `at`. One transaction, so two writers cannot both make the crew."""
        t = crews_table

        def seen(conn, cid, last) -> None:
            if at is not None and (last is None or at > last):
                conn.execute(update(t).where(t.c.id == cid).values(last_seen=at))

        with self.sessions_db.begin() as conn:
            self._lock_numbering(conn, "tares_crews")
            r = conn.execute(select(t.c.id, t.c.last_seen).where(
                t.c.person_id == person_id, t.c.name == name, t.c.machine == machine)).first()
            if r:
                seen(conn, r.id, r.last_seen)
                return r.id
            # A crew recorded without its machine (a cell with no licence keeps none) and the same
            # crew named with one (once a licence is on, or off again) are one crew (TR-1223): the
            # line joins the person's only other crew of that name, and a crew without a machine
            # takes the one this line names. With several machines it can't be told which one it
            # was: a new crew is made.
            others = conn.execute(select(t.c.id, t.c.last_seen, t.c.machine).where(
                t.c.person_id == person_id, t.c.name == name)).fetchall()
            bare = [o for o in others if not o.machine]
            named = [o for o in others if o.machine]
            pick = None
            if machine and bare and not named:
                pick = bare[0]
                conn.execute(update(t).where(t.c.id == pick.id).values(machine=machine))
            elif not machine and len(named) == 1:
                pick = named[0]
            if pick is not None:
                seen(conn, pick.id, pick.last_seen)
                return pick.id
            cid = "c_" + uuid.uuid4().hex[:10]
            conn.execute(insert(t).values(id=cid, name=name, person_id=person_id, machine=machine,
                                          first_seen=at, last_seen=at))
            return cid

    def resolve_crew(self, name: str | None, person_id: str | None, machine: str | None,
                     at) -> str:
        """The crew a station line belongs to: the crew's name (`crew` when the line names none),
        the person whose recording it is (`local` when it carries none) and the machine ('' when
        unknown). Made on first sight."""
        with self._swrite:
            return self._crew_id((name or "").strip() or "crew", person_id or "local",
                                 (machine or "").strip(), at)

    @kept
    def list_crews(self) -> list[dict]:
        """Every crew the cell has seen, with its person's name, newest activity first."""
        t = crews_table
        with self.sessions_db.read() as conn:
            rows = conn.execute(select(t).order_by(t.c.last_seen.desc().nulls_last(), t.c.id)
                                ).fetchall()
        refs = self._person_refs(r.person_id for r in rows)
        return [{"id": r.id, "name": r.name, "machine": r.machine or "", "first_seen": r.first_seen,
                 "last_seen": r.last_seen, "person_id": r.person_id,
                 "person": refs.get(r.person_id) or {"id": r.person_id, "name": None}}
                for r in rows]

    # ── stations (TR-418, TR-420) ─────────────────────────────────────────────
    def set_session_station(self, session: str, station: str, role: str, parent: str,
                            project: str | None, at, crew_id: str | None = None,
                            runtime: str | None = None, model: str | None = None) -> None:
        """`runtime` and `model` (TR-1600): what a Codex station's lines say it runs on; a Claude
        Code session's lines name neither, and a later line that does not name one keeps the last."""
        t = session_stations
        stmt = self.sessions_db.upsert(t).values(
            session=session, station=station, role=role or None, parent=parent or None,
            project=project, seen_at=at, crew_id=crew_id, runtime=runtime or None, model=model or None)
        x = stmt.excluded
        stmt = stmt.on_conflict_do_update(index_elements=["session"], set_={
            "station": x.station, "role": x.role, "parent": x.parent,
            "project": func.coalesce(x.project, t.c.project),
            "crew_id": func.coalesce(x.crew_id, t.c.crew_id),
            "runtime": func.coalesce(x.runtime, t.c.runtime),
            "model": func.coalesce(x.model, t.c.model)})
        with self.sessions_db.begin() as conn:
            conn.execute(stmt)

    def session_station(self, session: str) -> dict | None:
        t = session_stations
        with self.sessions_db.read() as conn:
            r = conn.execute(select(t.c.station, t.c.role, t.c.parent, t.c.project).where(
                t.c.session == session)).first()
        return {"station": r[0], "role": r[1], "parent": r[2], "project": r[3]} if r else None

    def station_sessions(self, source: str) -> list[dict]:
        """Every session that played a station, with its station, role, parent and project, when
        it started and last did something (from its lines on `source`), its state, and the
        project it last named (project_sessions, a deleted private one left out), newest
        activity first."""
        ss, st, sx = session_stations, session_states, sessions_table
        named = self._named_project(self.deleted_private_projects())
        # once the sessions table has caught up (TR-1520) the lines come from it in the same read
        # (TR-1499); before, from a scan of events below
        table = session_ingest.caught_up(self)
        lines = ((sx.c.started_at, sx.c.last_line_at, sx.c.line_count) if table
                 else (null(), null(), null()))
        j = ss.outerjoin(st, st.c.session == ss.c.session)
        if table:
            j = j.outerjoin(sx, and_(sx.c.session == ss.c.session, sx.c.source == source,
                                     sx.c.line_count > 0))
        q = (select(ss.c.session, ss.c.station, ss.c.role, ss.c.parent, ss.c.project, ss.c.seen_at,
                    st.c.state, st.c.reason, st.c.state_at, named, ss.c.crew_id, *lines,
                    ss.c.runtime, ss.c.model)
             .select_from(j))
        with self.sessions_db.read() as conn:
            rows = conn.execute(q).all()
        act = None if table else self.session_activity(source, [r[0] for r in rows])
        out = []
        for r in rows:
            if act is None:
                first, last, n = r[11], r[12], int(r[13] or 0)
            else:
                first, last, n = act.get(r[0], (None, None, 0))
            out.append({"session": r[0], "station": r[1], "role": r[2], "parent": r[3],
                        "project": r[4] or r[9], "seen_at": r[5], "started_at": first or r[5],
                        "last_at": last or r[5], "lines": n, "state": r[6],
                        "state_reason": r[7], "state_at": r[8], "crew_id": r[10],
                        "runtime": r[14], "model": r[15]})
        out.sort(key=lambda s: s["last_at"].timestamp() if s["last_at"] else float("-inf"),
                 reverse=True)
        return out

    @staticmethod
    def _named_project(left_out=()):
        """The project a session_stations row's session last named, as a subquery of that row,
        the ids in `left_out` passed over."""
        ss, ps = session_stations, project_sessions
        q = select(ps.c.project).where(ps.c.session == ss.c.session)
        if left_out:
            q = q.where(ps.c.project.not_in(sorted(left_out)))
        return q.order_by(ps.c.linked_at.desc()).limit(1).scalar_subquery()

    def _station_projects(self):
        """A query of (crew id, station, project), each once: the station's project of each of its
        sessions, else the one the session last named. A deleted private project stays in it,
        unlike in a session row's own project: what hides a station by these goes on hiding it
        (TR-1708)."""
        ss = session_stations
        return select(ss.c.crew_id, ss.c.station,
                      func.coalesce(ss.c.project, self._named_project())).distinct()

    def sessions_tied_to(self, sessions: list[str], projects) -> set[str]:
        """Of `sessions`, the ones that played a station whose own project, or else the one the
        session last named, is in `projects`, a deleted private one counted (TR-1708)."""
        ss = session_stations
        tied = func.coalesce(ss.c.project, self._named_project()).in_(sorted(projects))
        out: set[str] = set()
        for i in range(0, len(sessions), 500):
            with self.sessions_db.read() as conn:
                out.update(conn.execute(select(ss.c.session).where(
                    ss.c.session.in_(list(sessions[i:i + 500])), tied)).scalars())
        return out

    def _station_join(self, source: str):
        """The sessions a station played, with their state and, once the sessions table has caught
        up, their lines on `source`: (join, the session row's project when the station has none of
        its own, last activity)."""
        ss, st, sx = session_stations, session_states, sessions_table
        named = self._named_project(self.deleted_private_projects())
        j = (ss.outerjoin(st, st.c.session == ss.c.session)
             .outerjoin(sx, and_(sx.c.session == ss.c.session, sx.c.source == source,
                                 sx.c.line_count > 0)))
        return j, named, func.coalesce(sx.c.last_line_at, ss.c.seen_at)

    @staticmethod
    def _joined_row(r) -> dict:
        """A station_sessions row from the columns `station_rollup` and `station_earlier` select."""
        first, last, n = r[11], r[12], int(r[13] or 0)
        return {"session": r[0], "station": r[1], "role": r[2], "parent": r[3],
                "project": r[4] or r[9], "seen_at": r[5], "started_at": first or r[5],
                "last_at": last or r[5], "lines": n, "state": r[6],
                "state_reason": r[7], "state_at": r[8], "crew_id": r[10],
                "runtime": getattr(r, "runtime", None), "model": getattr(r, "model", None)}

    def _hidden_session(self, hidden, own):
        """The condition, on a session_stations row, that the caller must not read its session
        (TR-1704): it worked in a project in `hidden`, the ones the caller cannot see
        (`_sessions_in`), and its crew is not one of `own`, the caller's own crews, whose
        sessions they read whole. A session that also worked elsewhere is hidden whole."""
        if not hidden:
            return false()
        ss = session_stations
        found = self._sessions_in(hidden).cte("hidden_sessions")
        return and_(ss.c.session.in_(select(found.c.session)),
                    ss.c.crew_id.is_(None) | ss.c.crew_id.not_in(sorted(own)))

    def station_rollup(self, source: str, keep: int = EARLIER_KEPT,
                       hidden=frozenset(), own=frozenset(), totals: bool = True) -> list[dict]:
        """One row per station (crew id and name), however many sessions played it (TR-1500):
        `cur` the newest session (as station_sessions gives it), `earlier` the `keep` most recent
        before it, `earlier_count` how many there are in all, and the totals over every session:
        `projects` (sorted), `usage` (None when none of them has any) and `coordination_calls`.
        `usage` leaves out the replies that count for a project in `hidden` (the ones the caller
        cannot see, TR-1696). `earlier`, `earlier_count` and `coordination_calls` leave out the
        sessions the caller must not read (`_hidden_session`, TR-1704); `cur` is the newest
        session all the same, and `cur_hidden` says it is one of those, for the caller of this to
        blank. `projects` covers every session.
        Once the sessions table has caught up (TR-1520) these are grouped queries, so the answer
        stays the size of the stations; before, the same is built from the full list.
        `totals` False reads only who runs (TR-1634): `crew_id`, `station`, `cur` and `projects`,
        no earlier session and no total."""
        if not totals:
            keep = 0
        if not session_ingest.caught_up(self):
            return self._rollup_from_rows(source, keep, hidden, own, totals)
        ss, sx, u = session_stations, sessions_table, session_usage
        j, named, last = self._station_join(source)
        unread = case((self._hidden_session(hidden, own), 1), else_=0)
        every = (select(ss.c.session, ss.c.station, ss.c.role, ss.c.parent, ss.c.project,
                        ss.c.seen_at, session_states.c.state, session_states.c.reason,
                        session_states.c.state_at, named.label("named"), ss.c.crew_id,
                        sx.c.started_at, sx.c.last_line_at, sx.c.line_count,
                        sx.c.coordination_calls, last.label("last"), unread.label("unread"),
                        ss.c.runtime, ss.c.model)
                 .select_from(j).subquery())
        key = (every.c.crew_id, every.c.station)
        newest = (every.c.last.desc().nulls_last(), every.c.session.desc())
        ranked = select(
            every, func.row_number().over(partition_by=key, order_by=newest).label("rank"),
            # the place among the sessions of the same kind, the readable ones or the others
            func.row_number().over(partition_by=(*key, every.c.unread),
                                   order_by=newest).label("kind_rank")).subquery()
        # the newest session, and enough readable ones for `keep` before it
        top = (select(ranked)
               .where(or_(ranked.c.rank == 1,
                          and_(ranked.c.unread == 0, ranked.c.kind_rank <= keep + 1)))
               .order_by(ranked.c.rank))
        read = every.c.unread == 0
        sums = select(*key, func.count().filter(read),
                      func.sum(every.c.coordination_calls).filter(read)).group_by(*key)
        worked = self._station_projects()
        spent = self._station_spend(hidden) if totals else None
        with self.sessions_db.consistent_read() as conn:    # the answers agree on the stations
            top_rows, worked_rows = conn.execute(top).all(), conn.execute(worked).all()
            sum_rows = conn.execute(sums).all() if totals else []
            spent_rows = conn.execute(spent).all() if totals else []
        out: dict[tuple, dict] = {}
        for r in top_rows:   # a station's newest comes first
            g = out.setdefault((r[10], r[1]), {"crew_id": r[10], "station": r[1], "sessions": [],
                                               "cur_hidden": bool(r.unread),
                                               "projects": set(), "usage": None})
            g["sessions"].append(self._joined_row(r))
        for cid, name, n, coord in sum_rows:   # `n` counts the readable sessions
            g = out[(cid, name)]
            g.update(earlier_count=int(n) - (0 if g["cur_hidden"] else 1),
                     coordination_calls=int(coord or 0))
        for cid, name, project in worked_rows:
            if project:
                out[(cid, name)]["projects"].add(project)
        for cid, name, *cols in spent_rows:
            out[(cid, name)]["usage"] = self._totals(cols)
        for g in out.values():
            rows = g.pop("sessions")
            g.update(cur=rows[0], earlier=rows[1:keep + 1], projects=sorted(g["projects"]))
        return list(out.values())

    def projects_by_session(self, sessions: list[str]) -> dict[str, list[str]]:
        """The projects each session worked in (its station's project and every one it is linked
        to), sorted; a session that worked in none is left out."""
        out: dict[str, set[str]] = {}
        ss, ps = session_stations, project_sessions
        for i in range(0, len(sessions), 500):
            chunk = list(sessions[i:i + 500])
            with self.sessions_db.read() as conn:
                for sid, project in conn.execute(
                        select(ss.c.session, ss.c.project).where(ss.c.session.in_(chunk))):
                    if project:
                        out.setdefault(sid, set()).add(project)
                for sid, project in conn.execute(
                        select(ps.c.session, ps.c.project).where(ps.c.session.in_(chunk))):
                    out.setdefault(sid, set()).add(project)
        return {k: sorted(v) for k, v in out.items()}

    @staticmethod
    def _newest_first(rows: list[dict]) -> list[dict]:
        """station_sessions rows by last activity, newest first, ties by session id (the order the
        grouped queries give)."""
        rows = sorted(rows, key=lambda r: r["session"], reverse=True)
        rows.sort(key=lambda r: r["last_at"].timestamp() if r["last_at"] else float("-inf"),
                  reverse=True)
        return rows

    def _station_spend(self, hidden):
        """A query of (crew id, station, then `_total_cols`' seven sums) over the sessions each
        station played, the replies that count for a project in `hidden` left out."""
        ss, u, h = session_stations, session_usage, session_usage_hourly
        if hourly.ready(self):    # the kept hourly totals, not every usage row (TR-1632)
            if hidden:   # an hour holds the project its replies were stamped with, "" for none
                t = select(h.c.session, h.c.replies, h.c.input_tokens, h.c.output_tokens,
                           h.c.cache_write_tokens, h.c.cache_read_tokens, h.c.cost_usd,
                           h.c.unpriced, func.nullif(h.c.project, "").label("project")
                           ).subquery("hours")
            else:
                t = h
            q = select(ss.c.crew_id, ss.c.station, func.sum(t.c.replies),
                       func.sum(t.c.input_tokens), func.sum(t.c.output_tokens),
                       func.sum(t.c.cache_write_tokens), func.sum(t.c.cache_read_tokens),
                       func.sum(t.c.cost_usd), func.sum(t.c.unpriced))
        else:
            t = u
            q = select(ss.c.crew_id, ss.c.station, *self._total_cols(u))
        if hidden:   # that join starts at the replies, so the ones of no station are put out
            replies, project, _ = self._replies_by_project(t)
            q = q.select_from(replies).where(ss.c.session.is_not(None),
                                             self._outside(project, hidden))
        else:
            q = q.select_from(ss.join(t, t.c.session == ss.c.session))
        return q.group_by(ss.c.crew_id, ss.c.station)

    def _unread_sessions(self, rows: list[dict], hidden, own) -> set[str]:
        """`_hidden_session` for station_sessions rows: the sessions of `rows` the caller must
        not read."""
        return self.sessions_in([r["session"] for r in rows if r["crew_id"] not in own], hidden)

    def unread_among(self, sessions: list[str], hidden, own) -> set[str]:
        """`_hidden_session` for session ids, a station's or not (TR-1710): those of `sessions`
        the caller must not read. A session that is no station's has no crew, so none of `own`
        covers it."""
        found = self.sessions_in(sessions, hidden)
        if not found or not own:
            return found
        t, ids = session_stations, sorted(found)
        for i in range(0, len(ids), 500):
            with self.sessions_db.read() as conn:
                found -= set(conn.execute(select(t.c.session).where(
                    t.c.session.in_(ids[i:i + 500]), t.c.crew_id.in_(sorted(own)))).scalars())
        return found

    def _rollup_from_rows(self, source: str, keep: int, hidden=frozenset(),
                          own=frozenset(), totals: bool = True) -> list[dict]:
        """`station_rollup` for a cell whose sessions table has not caught up: from every row."""
        rows = self._newest_first(self.station_sessions(source))
        spent = self.usage_by_session([r["session"] for r in rows], hidden) if totals else {}
        coord = self.coordination_calls(source, [r["session"] for r in rows]) if totals else {}
        unread = self._unread_sessions(rows, hidden, own)
        with self.sessions_db.read() as conn:
            worked = conn.execute(self._station_projects()).all()
        projects: dict[tuple, set] = {}
        for cid, name, project in worked:
            if project:
                projects.setdefault((cid, name), set()).add(project)
        by: dict[tuple, list[dict]] = {}
        for r in rows:   # newest first
            by.setdefault((r["crew_id"], r["station"]), []).append(r)
        out = []
        for (cid, name), ss in by.items():
            mine = [spent[r["session"]] for r in ss if r["session"] in spent]
            read = [r for r in ss if r["session"] not in unread]
            earlier = [r for r in read if r is not ss[0]]
            out.append({"crew_id": cid, "station": name, "cur": ss[0],
                        "cur_hidden": ss[0]["session"] in unread, "earlier": earlier[:keep],
                        "earlier_count": len(earlier),
                        "projects": sorted(projects.get((cid, name), ())),
                        "usage": {k: sum(x[k] for x in mine) for k in mine[0]} if mine else None,
                        "coordination_calls": sum(coord.get(r["session"], 0) for r in read)})
        return out

    def station_earlier(self, source: str, crew_id: str | None, station: str, limit: int = 50,
                        before=None, hidden=frozenset(), own=frozenset()) -> dict | None:
        """The sessions that played one station before its current one, newest first, `limit` at
        a time (TR-1500): {"earlier_count", "projects" (every project the station worked in),
        "sessions" (rows as station_sessions gives them), "next_before"}. `before` (a datetime)
        keeps those last active before it; `next_before` is the last one's time to pass as
        `before` for the next page, None on the last. A page never splits sessions that share one
        time. The sessions the caller must not read (`_hidden_session`, TR-1704) are not counted,
        listed or paged over; `projects` covers every session. None when no such station has
        reported."""
        ss, sx = session_stations, sessions_table
        j, named, last = self._station_join(source)
        mine = and_(ss.c.station == station,
                    ss.c.crew_id.is_(None) if crew_id is None else ss.c.crew_id == crew_id)
        cols = (ss.c.session, ss.c.station, ss.c.role, ss.c.parent, ss.c.project, ss.c.seen_at,
                session_states.c.state, session_states.c.reason, session_states.c.state_at,
                named, ss.c.crew_id, sx.c.started_at, sx.c.last_line_at, sx.c.line_count)
        order = (last.desc().nulls_last(), ss.c.session.desc())
        if not session_ingest.caught_up(self):
            rows = [r for r in self._newest_first(self.station_sessions(source))
                    if r["station"] == station and r["crew_id"] == crew_id]
            if not rows:
                return None
            rows = rows[1:]
            with self.sessions_db.read() as conn:
                projects = sorted(p for *_, p in conn.execute(self._station_projects().where(mine)) if p)
            unread = self._unread_sessions(rows, hidden, own)
            rows = [r for r in rows if r["session"] not in unread]
            total = len(rows)
            if before is not None:
                rows = [r for r in rows if r["last_at"] is not None and r["last_at"] < before]
            page = rows[:limit]
            while len(rows) > len(page) and rows[len(page)]["last_at"] == page[-1]["last_at"]:
                page.append(rows[len(page)])
            return {"earlier_count": total, "projects": projects,
                    "sessions": page,
                    "next_before": page[-1]["last_at"] if len(rows) > len(page) else None}
        with self.sessions_db.read() as conn:
            n = conn.execute(select(func.count()).select_from(ss).where(mine)).scalar_one()
            if not n:
                return None
            cur = conn.execute(select(ss.c.session).select_from(j).where(mine)
                               .order_by(*order).limit(1)).scalar_one()
            projects = sorted(p for *_, p in conn.execute(self._station_projects().where(mine)) if p)
            earlier = (mine, ss.c.session != cur, ~self._hidden_session(hidden, own))
            total = (conn.execute(select(func.count()).select_from(ss).where(*earlier)).scalar_one()
                     if hidden else n - 1)
            q = select(*cols).select_from(j).where(*earlier)
            if before is not None:
                q = q.where(last < before)
            rows = [self._joined_row(r) for r in conn.execute(q.order_by(*order).limit(limit + 1))]
            page, more = rows[:limit], len(rows) > limit
            if more and rows[limit]["last_at"] == page[-1]["last_at"]:
                # sessions that share the last one's time all belong to this page
                tied = conn.execute(q.where(last == page[-1]["last_at"])
                                    .order_by(*order)).all()
                page = [r for r in page if r["last_at"] != page[-1]["last_at"]] + [
                    self._joined_row(r) for r in tied]
        return {"earlier_count": total, "projects": projects, "sessions": page,
                "next_before": page[-1]["last_at"] if more else None}

    # ── the ledger's history (TR-427) ─────────────────────────────────────────
    def add_ticket_history(self, ticket: str, project: str, field: str, value: str | None,
                           reason: str | None, by_station: str | None, by_role: str | None,
                           at, person_id: str | None = None) -> None:
        with self._swrite, self.sessions_db.begin() as conn:
            conn.execute(insert(history_table).values(
                ticket=ticket, project=project, changed_at=at, field=field, value=value,
                reason=reason or None, by_station=by_station or None, by_role=by_role or None,
                person_id=person_id or None))

    def ticket_history(self, ticket: str) -> list[dict]:
        h = history_table
        with self.sessions_db.read() as conn:
            rows = conn.execute(select(
                h.c.changed_at, h.c.field, h.c.value, h.c.reason, h.c.by_station, h.c.by_role,
                h.c.person_id).where(h.c.ticket == ticket)
                .order_by(h.c.changed_at.nulls_last())).all()
        return self._with_people([{"at": r[0], "field": r[1], "value": r[2], "reason": r[3],
                                   "by": r[4], "role": r[5], "person_id": r[6]} for r in rows])

    def entered_stage(self, tickets: list[str], stage: str) -> dict:
        """{ticket: when it last moved to `stage`} in one query (TR-648). Tickets that never
        did are absent."""
        if not tickets:
            return {}
        h = history_table
        with self.sessions_db.read() as conn:
            rows = conn.execute(select(h.c.ticket, func.max(h.c.changed_at)).where(
                h.c.ticket.in_(list(tickets)), h.c.field == "stage", h.c.value == stage
            ).group_by(h.c.ticket)).all()
        return {r[0]: r[1] for r in rows}

    def review_tickets(self) -> list[tuple[str, dict]]:
        """Every ticket in review with its project's name, in the order the projects were made
        and then the list's own order, in one query (TR-648). "In review" is factory.state_of's:
        stage review and a status that is not done or canceled (TR-1008)."""
        t = tickets_table
        with self.sessions_db.consistent_read() as conn:
            rows = conn.execute(select(t).where(t.c.stage == "review", or_(
                t.c.status.is_(None), t.c.status.not_in(("done", "canceled"))))).all()
            made = {r[0]: (r[1], r[2]) for r in conn.execute(select(
                projects_table.c.id, projects_table.c.name, projects_table.c.created_at))}
        rows = [r for r in rows if r.project in made]

        def order(r):
            return tuple((v is None, v) if v is not None else (True, 0) for v in (
                made[r.project][1], r.position, r.created_at))
        rows.sort(key=order)
        return [(made[r.project][0], self._ticket_row(r)) for r in rows]

    def milestone_statuses(self, milestones: list[str]) -> list[dict]:
        """Project, milestone, id and status of every ticket in these milestones (TR-648)."""
        if not milestones:
            return []
        t = tickets_table
        with self.sessions_db.read() as conn:
            rows = conn.execute(select(t.c.project, t.c.milestone, t.c.id, t.c.status).where(
                t.c.milestone.in_(list(milestones)))).all()
        return [{"project": r[0], "milestone": r[1], "id": r[2], "status": r[3]} for r in rows]

    def history_between(self, since, until) -> list[dict]:
        """Every ticket change in [since, until), oldest first, for every project (the standup)."""
        h = history_table
        with self.sessions_db.read() as conn:
            rows = conn.execute(select(
                h.c.ticket, h.c.project, h.c.changed_at, h.c.field, h.c.value, h.c.reason,
                h.c.by_station, h.c.person_id
            ).where(h.c.changed_at >= since, h.c.changed_at < until)
                .order_by(h.c.changed_at)).all()
        return self._with_people([{"ticket": r[0], "project": r[1], "at": r[2], "field": r[3],
                                   "value": r[4], "reason": r[5], "by": r[6], "person_id": r[7]}
                                  for r in rows])

    # ── every project's records, for the weekly figures (report.py, TR-1666) ──
    def all_stage_changes(self) -> list[dict]:
        """Every stage change of every ticket, deleted tickets too, each ticket's together and
        oldest first. `value` None is a stage taken off."""
        t = history_table.c
        with self.sessions_db.read() as conn:
            rows = conn.execute(select(t.ticket, t.project, t.changed_at, t.value)
                                .where(t.field == "stage")
                                .order_by(t.ticket, t.changed_at.asc().nulls_last())).fetchall()
        return [{"ticket": r[0], "project": r[1], "at": r[2], "value": r[3]} for r in rows]

    def all_holder_changes(self) -> list[dict]:
        """Every holder change of every ticket, deleted tickets too, each ticket's together and
        oldest first. `value` is the station that holds the ticket from then on, None for nobody."""
        t = history_table.c
        with self.sessions_db.read() as conn:
            rows = conn.execute(select(t.ticket, t.project, t.changed_at, t.value)
                                .where(t.field == "holder")
                                .order_by(t.ticket, t.changed_at.asc().nulls_last())).fetchall()
        return [{"ticket": r[0], "project": r[1], "at": r[2], "value": r[3]} for r in rows]

    def all_reviews(self) -> list[dict]:
        """Every review round of every ticket, each ticket's together and in round order."""
        t = reviews_table.c
        with self.sessions_db.read() as conn:
            rows = conn.execute(select(t.id, t.project, t.ticket, t.round, t.verdict, t.by_station,
                                       t.reviewed_at)
                                .order_by(t.ticket, t.round.asc().nulls_last(),
                                          t.reviewed_at.asc().nulls_last())).fetchall()
        return [{"id": r[0], "project": r[1], "ticket": r[2], "round": r[3], "verdict": r[4],
                 "by": r[5], "at": r[6]} for r in rows]

    def all_review_findings(self) -> list[dict]:
        """Every finding of every review, with its kind as stored."""
        t = findings_table.c
        with self.sessions_db.read() as conn:
            rows = conn.execute(select(t.project, t.severity, t.blocking, t.kind, t.state,
                                       t.fixed_in, t.found_at)).fetchall()
        return [{"project": r[0], "severity": r[1], "blocking": bool(r[2]), "kind": r[3],
                 "state": r[4], "fixed_in": r[5], "found_at": r[6]} for r in rows]

    def factory_branch_prs(self, repos: set[str]) -> dict[tuple[str, str], tuple[str, int]]:
        """{(repo, ticket ref upper case): (repo, number)} for the pull requests GitHub sources
        saw on a `factory/<ref>` branch in one of `repos`, newest first wins (TR-410)."""
        from .factory_github import branch_ref
        if not repos:
            return {}
        marks = ", ".join("?" for _ in repos)
        with self._lock:
            rows = self.con.execute(
                "SELECT labels FROM events WHERE event_type = 'pull_request' AND "
                f"json_extract_string(labels, '$.repo') IN ({marks}) AND "
                "json_extract_string(labels, '$.branch') LIKE 'factory/%' "
                "ORDER BY event_time DESC LIMIT 1000", list(repos)).fetchall()
        out: dict[tuple[str, str], tuple[str, int]] = {}
        for (lj,) in rows:
            try:
                lab = json.loads(lj) if isinstance(lj, str) else (lj or {})
            except ValueError:
                continue
            ref = branch_ref(lab.get("branch") or "")
            if not ref:
                continue
            try:
                out.setdefault((lab["repo"], ref.upper()), (lab["repo"], int(lab.get("number"))))
            except (TypeError, ValueError):
                continue
        return out

    def factory_ref_elsewhere(self, uid: str, repo: str, ref: str) -> bool:
        """Another project has a ticket called `ref` in the work whose PRs live in `repo`: a
        branch named after the ref could be either's, so it is not linked by name."""
        if repo is None:                 # a pull request with no `repo` is not in "no repo"
            return False
        t = tickets_table
        with self.sessions_db.consistent_read() as conn:
            # the stored text is searched for the name, then each hit's `repo` is compared
            there = {r.project for r in conn.execute(select(t.c.project, t.c.pr).where(
                t.c.project != uid,
                type_coerce(t.c.pr, Text).contains(json.dumps(repo), autoescape=True)))
                if (r.pr or {}).get("repo") == repo}
            rows = conn.execute(select(t.c.project, t.c.number, t.c.identifier, t.c.stage).where(
                t.c.project.in_(sorted(there)))).all() if there else []
        for project, number, ident, stage in rows:
            lab = ident or (f"T{number}" if number else "")
            if lab.upper() == ref.upper() and stage in ("doing", "review", "changes", "blocked"):
                return True
        return False

    def _station_of(self, sessions: set) -> dict:
        """{session: station} for the sessions among `sessions` that played one."""
        sessions = sorted(s for s in sessions if s)
        out = {}
        for i in range(0, len(sessions), 500):
            chunk = sessions[i:i + 500]
            with self.sessions_db.read() as conn:
                out.update((r[0], r[1]) for r in conn.execute(select(
                    session_stations.c.session, session_stations.c.station).where(
                    session_stations.c.session.in_(chunk))))
        return out

    def protocol_messages(self, source: str, limit: int = 3000) -> list[dict]:
        """The crew's recorded protocol messages (TR-429): SendMessage calls whose text starts
        `[TF:<TYPE>]`, oldest first, with the sending station (from its session) and the one it
        was sent to. Read from `session_messages`, where each call was written once as its line was
        stored (session_ingest, TR-1477); the sender is looked up now, so a station renamed later
        shows under its new name."""
        t = session_messages.c
        with self.sessions_db.read() as conn:
            rows = conn.execute(select(t.session, t.at, t.to_station, t.type, t.text).where(
                t.source == source).order_by(t.at.desc(), t.tool_use_id.desc()).limit(limit)
            ).fetchall()
        stations = self._station_of({r[0] for r in rows})
        out = []
        for sid, at, to, typ, text in reversed(rows):
            m = re.match(r"\s*\[TF:([A-Z_]+)\]\s*(.*)", text or "")
            out.append({"at": at, "session": sid, "from": stations.get(sid) or "", "to": to or "",
                        "type": typ, "text": text or "",
                        "first_line": m.group(2).splitlines()[0][:300] if m and m.group(2) else ""})
        return out

    def coordination_calls(self, source: str, sessions: list[str]) -> dict[str, int]:
        """Per session, how many tool calls only coordinate the crew (TR-683, factory.is_coordination),
        read from the sessions table, where each line was counted as it was stored (session_ingest),
        once the table has caught up (TR-1520); before, from a scan of events. Sessions with none
        are left out."""
        if not session_ingest.caught_up(self):
            return self._coordination_calls_scan(source, sessions)
        t = sessions_table
        out: dict[str, int] = {}
        for i in range(0, len(sessions), 500):
            chunk = sessions[i:i + 500]
            if not chunk:
                continue
            q = select(t.c.session, t.c.coordination_calls).where(
                t.c.source == source, t.c.session.in_(chunk), t.c.coordination_calls > 0)
            with self.sessions_db.read() as conn:
                out.update((sid, int(n)) for sid, n in conn.execute(q))
        return out

    def _coordination_calls_scan(self, source: str, sessions: list[str]) -> dict[str, int]:
        """`coordination_calls` from the events table: what a cell not yet caught up has to use, a
        parse of each asked session's calls on every read (nothing is kept between reads)."""
        from .factory import is_coordination
        if not sessions:
            return {}
        marks = ", ".join("?" for _ in sessions)
        with self._lock:
            # the rendered text names each tool as "→ name(": a cheap filter before the parse
            rows = self.con.execute(
                f"SELECT key_value, payload FROM events WHERE source = ? AND key_value IN ({marks}) "
                "AND event_type = 'tool_use' AND (text LIKE '%→ mcp__%' OR text LIKE '%→ SendMessage(%' "
                "OR text LIKE '%→ ListAgents(%' OR text LIKE '%→ Skill(%' OR text LIKE '%→ Cron%')",
                [source, *sessions]).fetchall()
        found: dict[str, int] = {}
        for sid, pj in rows:
            try:
                o = json.loads(pj) if pj else {}
            except (TypeError, ValueError):
                continue
            msg = o.get("message") if isinstance(o.get("message"), dict) else {}
            for b in msg.get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_use" and is_coordination(b.get("name")):
                    found[sid] = found.get(sid, 0) + 1
        return found

    # ── assumptions (TR-433) ──────────────────────────────────────────────────
    @staticmethod
    def _as_row(r) -> dict:
        return {"person_id": r.person_id, "id": r.id, "project": r.project, "number": r.number,
                "label": f"A{r.number}", "question": r.question, "choice": r.choice, "why": r.why,
                "affects": r.affects or "", "tickets": r.tickets or [], "state": r.state,
                "words": r.words, "batch": r.batch, "made_by": r.made_by,
                "made_role": r.made_role, "made_at": r.made_at, "decided_at": r.decided_at}

    def add_assumption(self, project: str, question: str, choice: str, why: str, affects: str,
                       tickets: list[str], made_by: str, made_role: str, at,
                       person_id: str | None = None) -> dict:
        aid = "as_" + uuid.uuid4().hex[:10]
        t = assumptions_table
        with self._swrite, self.sessions_db.begin() as conn:
            self._lock_numbering(conn, f"tares_assumption_number:{project}")
            n = (conn.execute(select(func.max(t.c.number)).where(
                t.c.project == project)).scalar() or 0) + 1
            conn.execute(insert(t).values(
                id=aid, project=project, number=n, question=question, choice=choice, why=why,
                affects=affects or None, tickets=tickets or None, state="open",
                made_by=made_by or None, made_role=made_role or None, made_at=at,
                person_id=person_id or None))
            r = conn.execute(select(t).where(t.c.id == aid)).one()
        return self._with_people([self._as_row(r)], "made_by")[0]

    def list_assumptions(self, project: str, state: str | None = None,
                         ticket: str | None = None) -> list[dict]:
        t = assumptions_table
        q = select(t).where(t.c.project == project)
        if state:
            q = q.where(t.c.state == state)
        with self.sessions_db.read() as conn:
            rows = conn.execute(q.order_by(t.c.number.asc().nulls_last())).fetchall()
        out = [self._as_row(r) for r in rows]
        out = [a for a in out if ticket in a["tickets"]] if ticket else out
        return self._with_people(out, "made_by")

    def open_assumptions(self) -> list[dict]:
        """Every open assumption of every project in one query, oldest first (the home page)."""
        t = assumptions_table
        with self.sessions_db.read() as conn:
            rows = conn.execute(select(t).where(t.c.state == "open").order_by(
                t.c.made_at.asc().nulls_last(), t.c.project.asc().nulls_last(),
                t.c.number.asc().nulls_last())).fetchall()
        return [self._as_row(r) for r in rows]

    def get_assumption(self, project: str, ref: str) -> dict | None:
        m = re.match(r"^[Aa]?(\d{1,6})$", (ref or "").strip())
        t = assumptions_table
        with self.sessions_db.read() as conn:
            r = conn.execute(select(t).where(
                t.c.project == project,
                or_(t.c.id == ref, t.c.number == (int(m.group(1)) if m else -1)))).first()
        return self._with_people([self._as_row(r)], "made_by")[0] if r else None

    def decide_assumption(self, aid: str, state: str, words: str | None, batch: str | None,
                          at) -> None:
        t = assumptions_table
        with self._swrite, self.sessions_db.begin() as conn:
            conn.execute(update(t).where(t.c.id == aid).values(
                state=state, words=words, batch=batch, decided_at=at))

    # ── checks (TR-435) ───────────────────────────────────────────────────────
    def add_check(self, project: str, ticket: str, command: str, result: str, commit: str,
                  broke_test: str, by_station: str, by_session: str, at,
                  person_id: str | None = None) -> str:
        cid = "ck_" + uuid.uuid4().hex[:10]
        with self._swrite, self.sessions_db.begin() as conn:
            conn.execute(insert(checks_table).values(
                id=cid, project=project, ticket=ticket, command=command, result=result,
                commit_sha=commit or None, broke_test=broke_test or None,
                by_station=by_station or None, by_session=by_session or None, checked_at=at,
                person_id=person_id or None))
        return cid

    def ticket_checks(self, ticket: str, limit: int = 50) -> list[dict]:
        t = checks_table
        with self.sessions_db.read() as conn:
            rows = conn.execute(select(
                t.c.id, t.c.command, t.c.result, t.c.commit_sha.label("commit"), t.c.broke_test,
                t.c.by_station.label("by"), t.c.checked_at.label("at"), t.c.person_id)
                .where(t.c.ticket == ticket)
                .order_by(t.c.checked_at.desc().nulls_last(), t.c.id.desc())
                .limit(limit)).fetchall()
        return self._with_people([dict(r._mapping) for r in reversed(rows)])

    # ── reviews (TR-441) ──────────────────────────────────────────────────────
    def add_review(self, project: str, ticket: str, pr: str | None, head: str, verdict: str,
                   verified: str, not_verified: str, by: str, findings: list[dict],
                   resolved: dict[str, str], kind_of, at, person_id: str | None = None) -> dict:
        """A review round: earlier findings marked fixed or still open as `resolved` says
        ({F<n> or id: fixed|open}), the new findings numbered on from the ticket's last."""
        rid = "rv_" + uuid.uuid4().hex[:10]
        r, f = reviews_table, findings_table
        with self._swrite, self.sessions_db.begin() as conn:
            rnd = (conn.execute(select(func.max(r.c.round)).where(
                r.c.ticket == ticket)).scalar() or 0) + 1
            conn.execute(insert(r).values(
                id=rid, project=project, ticket=ticket, round=rnd, pr=pr, head=head,
                verdict=verdict, verified=verified, not_verified=not_verified or None,
                by_station=by or None, reviewed_at=at, person_id=person_id or None))
            for ref, st in (resolved or {}).items():
                m = re.match(r"^[Ff]?(\d{1,6})$", str(ref).strip())
                conn.execute(update(f).where(
                    f.c.ticket == ticket,
                    or_(f.c.id == str(ref), f.c.number == (int(m.group(1)) if m else -1))
                ).values(state=st, fixed_in=rid if st == "fixed" else None))
            n = conn.execute(select(func.max(f.c.number)).where(f.c.ticket == ticket)).scalar() or 0
            new = [{"id": "rf_" + uuid.uuid4().hex[:10], "project": project, "ticket": ticket,
                    "review": rid, "number": n + i, "severity": x["severity"],
                    "file": x["file"] or None, "line": x["line"] or None,
                    "blocking": x["blocking"], "text": x["text"], "kind": kind_of(x["text"]),
                    "state": "open", "fixed_in": None, "found_at": at}
                   for i, x in enumerate(findings, 1)]
            if new:
                conn.execute(insert(f), new)
        return {"id": rid, "round": rnd}

    @staticmethod
    def _review_cols():
        r = reviews_table.c
        return (r.id, r.round, r.pr, r.head, r.verdict, r.verified, r.not_verified,
                r.by_station.label("by"), r.reviewed_at.label("at"))

    @staticmethod
    def _finding_cols():
        f = findings_table.c
        return (f.id, f.review, f.number, f.severity, f.file, f.line, f.blocking, f.text, f.kind,
                f.state, f.fixed_in)

    @staticmethod
    def _finding_row(f) -> dict:
        return {"id": f.id, "review": f.review, "label": f"F{f.number}", "severity": f.severity,
                "file": f.file, "line": f.line, "blocking": bool(f.blocking), "text": f.text,
                "kind": f.kind, "state": f.state, "fixed_in": f.fixed_in}

    def ticket_reviews(self, ticket: str) -> list[dict]:
        r, f = reviews_table, findings_table
        # one snapshot: a round recorded between the two reads must not show half
        with self.sessions_db.consistent_read() as conn:
            revs = conn.execute(select(*self._review_cols(), r.c.person_id)
                                .where(r.c.ticket == ticket)
                                .order_by(r.c.round.asc().nulls_last(), r.c.id)).fetchall()
            fs = conn.execute(select(*self._finding_cols()).where(f.c.ticket == ticket)
                              .order_by(f.c.number.asc().nulls_last(), f.c.id)).fetchall()
        finds = [self._finding_row(x) for x in fs]
        out = []
        for rv in revs:
            mine = [x for x in finds if x["review"] == rv.id]
            fixed_here = [x["label"] for x in finds if x["fixed_in"] == rv.id]
            out.append({**rv._mapping, "findings": mine, "fixed": fixed_here})
        return self._with_people(out)

    def last_reviews(self, tickets: list[str]) -> dict[str, dict]:
        """Each ticket's standing review round (`factory.standing_round`), shaped like an element
        of ticket_reviews(), in two queries for any number of tickets (TR-657). Tickets never
        reviewed are absent."""
        if not tickets:
            return {}
        from .factory import MERGE_QUEUE, standing_round
        r, f = reviews_table, findings_table
        ranked = select(
            *self._review_cols(), r.c.ticket,
            func.row_number().over(
                partition_by=[r.c.ticket, case((r.c.by_station == MERGE_QUEUE, 1), else_=0)],
                order_by=r.c.round.desc().nulls_last()).label("rn")
        ).where(r.c.ticket.in_(list(tickets))).subquery()
        cols = [c for c in ranked.c if c.name != "rn"]
        with self.sessions_db.consistent_read() as conn:
            revs = conn.execute(select(*cols).where(ranked.c.rn == 1)
                                .order_by(ranked.c.round.asc().nulls_last(),
                                          ranked.c.id)).fetchall()
            # per ticket the newest round of the merge queue and the newest of everyone else, so
            # the one that stands is among them
            newest: dict[str, list[dict]] = {}
            for rv in revs:
                newest.setdefault(rv.ticket, []).append(
                    {"id": rv.id, "by": rv.by, "verdict": rv.verdict})
            standing = {standing_round(v)["id"] for v in newest.values()}
            revs = [rv for rv in revs if rv.id in standing]
            ids = [rv.id for rv in revs]
            fs = conn.execute(select(*self._finding_cols())
                              .where(or_(f.c.review.in_(ids), f.c.fixed_in.in_(ids)))
                              .order_by(f.c.number.asc().nulls_last(), f.c.id)
                              ).fetchall() if ids else []
        finds = [self._finding_row(x) for x in fs]
        return {rv.ticket: {**{k: v for k, v in rv._mapping.items() if k != "ticket"},
                            "findings": [x for x in finds if x["review"] == rv.id],
                            "fixed": [x["label"] for x in finds if x["fixed_in"] == rv.id]}
                for rv in revs}

    def open_findings(self, ticket: str) -> list[dict]:
        return [f for r in self.ticket_reviews(ticket) for f in r["findings"] if f["state"] == "open"]

    def repo_findings(self, repo: str, since) -> list[dict]:
        """Every reviewer finding on tickets whose PR lives in `repo` since `since`, with the
        ticket and project (TR-443). Each row also carries the ticket's own project, identifier and
        number (what factory.label needs), so a caller names the tickets without reading each.
        Oldest first."""
        if repo is None:
            return []
        f, t = findings_table, tickets_table
        # the repo is inside the ticket's `pr`. The tickets that may have it are found through the
        # index of `pr_repo` (lower case, NULL for a name it never holds); `pr` itself decides
        want = pr_columns({"repo": repo})[0]
        with self.sessions_db.consistent_read() as conn:
            mine = {tk.id: tk for tk in conn.execute(
                select(t.c.id, t.c.project, t.c.identifier, t.c.number, t.c.pr)
                .where(t.c.pr_repo == want))
                if isinstance(tk.pr, dict) and tk.pr.get("repo") == repo}
            if not mine:
                return []
            rows = conn.execute(select(
                f.c.ticket, f.c.project, f.c.kind, f.c.text, f.c.severity, f.c.found_at)
                .where(f.c.found_at >= since)
                .order_by(f.c.found_at, f.c.ticket, f.c.number)).fetchall()
        return [{"ticket": r.ticket, "project": r.project, "kind": r.kind, "text": r.text,
                 "severity": r.severity, "at": r.found_at,
                 "ticket_project": mine[r.ticket].project,
                 "ticket_identifier": mine[r.ticket].identifier,
                 "ticket_number": mine[r.ticket].number} for r in rows if r.ticket in mine]

    # ── what the recording says (TR-440, TR-442) ──────────────────────────────
    CHALLENGE_TYPES = ("challenge_commit", "challenge_waived", "challenge_plan")

    def challenge_events(self, source: str, project: str, ref: str) -> list[dict]:
        """The Codex challenger's reviews and waivers on a ticket's branch (factory/<ref>...) in
        the project `project` (its id), oldest first. A line stamped with the project's name (any
        case) or id counts; an unstamped one only from a session tied to the project."""
        return self.challenge_events_many(source, [(project, ref)])[(project, ref)]

    def challenge_events_many(self, source: str, tickets: list[tuple[str, str]]
                              ) -> dict[tuple[str, str], list[dict]]:
        """{(project id, ref): challenge_events} for every pair asked, from one read of the
        challenger's lines (and one each for the projects' names and sessions, when a line
        matched), however many tickets are asked. Each list is as challenge_events gives it."""
        pairs = list(dict.fromkeys(tickets))
        out: dict[tuple[str, str], list[dict]] = {p: [] for p in pairs}
        refs = sorted({ref for _, ref in pairs})
        t = challenges_table
        rows = []
        with self.sessions_db.consistent_read() as conn:
            for i in range(0, len(refs), 150):
                rows += conn.execute(select(
                    t.c.event_time, t.c.event_type, t.c.payload, t.c.key_value, t.c.branch,
                    t.c.tares_project).where(self._challenge_branches(source, refs[i:i + 150]))
                    .order_by(t.c.event_time, t.c.key_value, t.c.event_type,
                              t.c.branch)).fetchall()
            if not rows:
                return out
            # a line can match refs in two chunks: keep it once, oldest first as before
            rows = list({(r[0], r[1], r[3], r[4], r[5]): r for r in rows}.values())
            rows.sort(key=lambda r: r[0])
            projects = sorted({p for p, _ in pairs})
            names = dict(conn.execute(select(projects_table.c.id, projects_table.c.name).where(
                projects_table.c.id.in_(projects))).all())
            sessions: dict[str, set] = {}
            for project, session in conn.execute(select(
                    project_sessions.c.project, project_sessions.c.session).where(
                    project_sessions.c.project.in_(projects))):
                sessions.setdefault(project, set()).add(session)
        by_ref: dict[str, list] = {}
        for ref in refs:
            by_ref[ref] = [r for r in rows if r[4] == f"factory/{ref}"
                           or (r[4] or "").startswith(f"factory/{ref}-")]
        for project, ref in pairs:
            stamps = {str(project).lower(), str(names.get(project, project)).lower()}
            for at, typ, payload, sid, _branch, stamped in by_ref[ref]:
                if stamped is not None:
                    if str(stamped).lower() not in stamps:
                        continue
                elif sid not in sessions.get(project, ()):
                    continue
                o = self._challenge_payload(payload, source, sid, at, typ, _branch)
                ch = o.get("challenge") if isinstance(o.get("challenge"), dict) else {}
                out[(project, ref)].append({"at": at, "type": typ, "session": sid, "challenge": ch})
        return out

    def _challenge_branches(self, source: str, refs: list[str]):
        """The lines of `source` on these refs' branches, factory/<ref> and factory/<ref>-...
        It may give more (SQLite) or other-case ones (SQLite's LIKE): the caller keeps the exact
        branches."""
        t = challenges_table
        if self.sessions_db.dialect == "sqlite":
            # a range, one for each ref, with the source in it: SQLite compares bytes, so
            # factory/<ref> up to factory/<ref>. holds every <ref>-... branch, and both columns of
            # ix_challenges_branch are used. Its LIKE ignores case and would use the source alone
            return or_(*(and_(t.c.source == source, t.c.branch >= f"factory/{ref}",
                              t.c.branch < f"factory/{ref}.") for ref in refs))
        # not a range on Postgres: there the database's collation orders text, which need not be
        # by bytes
        wanted = []
        for ref in refs:
            wanted += [t.c.branch == f"factory/{ref}", t.c.branch.like(f"factory/{ref}-%")]
        return and_(t.c.source == source, or_(*wanted))

    def _challenge_payload(self, text, source, session, at, typ, branch) -> dict:
        """A challenger line's stored payload as an object. {} when there is none, when it is JSON
        but not an object, or when it is not JSON or is nested too deeply (`challenge_json`), which
        is also logged, once for each line while this process runs, by its ids alone."""
        if not text or not text.strip():
            return {}
        try:
            o = challenge_json(text)
        except ValueError:
            line = (source, session, at, typ, branch)
            if line not in self._challenges_unreadable:
                self._challenges_unreadable.add(line)
                log.warning("a challenger line's payload is not JSON or is nested too deeply, read "
                            "as empty: source %s session %s type %s branch %s at %s",
                            *(_loggable(x)[:120] for x in
                              (source, session or "unknown", typ, branch, at.isoformat())))
            return {}
        return o if isinstance(o, dict) else {}

    def _record_challenges(self, rows: list[tuple]) -> None:
        """Keep the challenger's lines (source, key, time, type, branch, project, payload as JSON
        text) in `challenges`, the payload as given: one that is not JSON, or is nested too deeply,
        is read as {}.
        Idempotent: a line already kept is skipped."""
        names = ("source", "key_value", "event_time", "event_type", "branch", "tares_project",
                 "payload")
        for i in range(0, len(rows), 500):
            chunk = [dict(zip(names, r)) for r in rows[i:i + 500]]
            with self.sessions_db.begin() as conn:
                conn.execute(self.sessions_db.upsert(challenges_table).on_conflict_do_nothing(),
                             chunk)

    def _challenges_not_kept(self, rows: list[tuple], ex: Exception) -> None:
        """Say that `_record_challenges` could not write `rows` (its shape): session and commit
        ids and the error's type only, never the payload or the error's text, which can quote the
        verdict. On the factory edition they are lost and counted."""
        if not self._factory_writes():
            print(f"taresd: keeping {len(rows)} challenger line(s) failed ({type(ex).__name__}); "
                  "they are copied at the next start", flush=True)
            return
        named = []
        for row in rows[:20]:
            try:
                sha = challenge_json(row[6])["challenge"].get("sha")
            except (ValueError, TypeError, KeyError, AttributeError):
                sha = None
            named.append(f"session {_loggable(row[1] or 'unknown')[:80]} commit "
                         f"{_loggable(sha or 'none')[:40]}")
        metrics.challenges_lost(len(rows))
        log.warning("%d challenger line(s) lost, the sessions database did not take them (%s): %s",
                    len(rows), type(ex).__name__, "; ".join(named))

    def _challenges_fill(self) -> None:
        """Copy challenger lines stored before `challenges` existed, or while a write to it
        failed. One scan of the events at open, then only the lines `append` records. The factory
        edition scans once and records it (`challenges_filled`, TR-1613)."""
        if session_ingest.filled_once(self, session_ingest.CHALLENGES_FILLED):
            return
        rows = self.con.execute(
            "SELECT source, coalesce(key_value, ''), event_time, event_type, "
            "coalesce(json_extract_string(labels, '$.branch'), ''), "
            "json_extract_string(labels, '$.tares_project'), payload FROM events "
            "WHERE event_type IN ('challenge_commit', 'challenge_waived', 'challenge_plan')"
            ).fetchall()
        rows = self._challenges_without_nul(
            [r for r in rows if session_ingest.holds_session(self, r[1])])
        if rows:
            self._record_challenges(rows)
        session_ingest.set_filled(self, session_ingest.CHALLENGES_FILLED)

    def _challenges_without_nul(self, rows: list[tuple]) -> list[tuple]:
        """The fill's rows (`_record_challenges`' shape) with the branch and project a posted line
        has now: without NULs. DuckDB may hold lines from before they were dropped (TR-1707), and
        Postgres takes none in text. On SQLite an older version could keep a line under its branch
        as it was: that one is not kept a second time."""
        db, t = self.sessions_db, challenges_table.c
        out = []
        for source, key, at, typ, branch, project, payload in rows:
            if "\x00" in branch + (project or ""):
                if "\x00" in branch and db.dialect == "sqlite":
                    with db.read() as conn:
                        if conn.execute(select(t.branch).where(
                                t.source == source, t.key_value == key, t.event_time == at,
                                t.event_type == typ, t.branch == branch)).first():
                            continue
                branch = branch.replace("\x00", "")
                project = (project or "").replace("\x00", "") or None
            out.append((source, key, at, typ, branch, project, payload))
        return out

    def recorded_commands(self, source: str, sessions: list[str], limit: int = 6000, ends: bool = False) -> list[dict]:
        """The shell commands the sessions ran (their subagents included), each with what it
        printed and whether it failed, oldest first: what a builder's claimed checks are
        compared with. Read from `session_commands`, where each call was written as its line was
        stored and its result added when that arrived (session_ingest, TR-1477). A command whose
        result has not arrived shows no output and no failure. A Codex station's command (TR-1595)
        also says how it ended (`outcome`), its real exit code and its folder: one that did not
        end (`started`, `unknown`) is told apart from one that ended, and one Codex failed to run
        or declined counts as failed."""
        if not sessions:
            return []
        t = session_commands.c
        rows = []
        for i in range(0, len(sessions), 500):
            with self.sessions_db.read() as conn:
                rows += conn.execute(select(t.at, t.session, t.command, t.output_tail, t.exit_code,
                                            t.outcome, t.cwd, t.result_at)
                                     .where(t.source == source, t.session.in_(sessions[i:i + 500]),
                                            t.command.is_not(None), t.at.is_not(None))
                                     .order_by(t.at.desc(), t.tool_use_id.desc())
                                     .limit(limit // 2)).fetchall()
        rows = sorted(rows, key=lambda r: r[0], reverse=True)[:limit // 2]
        # With `ends`, every row also says whether its result arrived (`ended`) and the code it ended with
        # (`exit_code`; a Claude Code result says only ok or error: 0 or 1): what a step of a chain needs to back
        # a claim (factory.match_check, TR-1598). Without, the rows are what they always were.
        return [{"at": at, "session": sid, "command": command or "", "output": tail or "",
                 "is_error": bool(code) or outcome in ("failed", "declined"),
                 **({"outcome": outcome, "exit_code": code, "cwd": cwd} if outcome else {}),
                 **({"ended": result_at is not None, "exit_code": code} if ends else {})}
                for at, sid, command, tail, code, outcome, cwd, result_at in reversed(rows)]

    # ── repo maps (TR-681) ────────────────────────────────────────────────────
    def get_repo_map(self, repo: str) -> dict | None:
        repo = repo.lower()   # GitHub names a repo the same whatever the case
        t = maps_table
        with self.sessions_db.read() as conn:
            r = conn.execute(select(t.c.body, t.c.updated_at, t.c.updated_by)
                             .where(t.c.repo == repo)).first()
        return {"repo": repo, "body": r[0], "updated_at": r[1], "updated_by": r[2]} if r else None

    def set_repo_map(self, repo: str, body: str, note: str, by: str, project: str | None,
                     at, base: str = "") -> bool:
        """Replace the repo's map and add a line to its history, as one write. `base` is the
        version of the map the writer read (factory.map_version, "" when there was none): another
        version in place raises MapChanged, so a stale copy cannot overwrite a newer map. False,
        and nothing written, when the map is the one already there."""
        from .factory import map_version
        repo = repo.lower()
        body, note, by, project = (_no_nul(v) for v in (body, note, by, project))
        t = maps_table
        with self._swrite, self.sessions_db.begin() as conn:
            # a second process reading the same version waits here, then sees what this one wrote
            self._lock_numbering(conn, f"tares_repo_map:{repo}")
            cur = conn.execute(select(t.c.body, t.c.updated_at, t.c.updated_by)
                               .where(t.c.repo == repo)).first()
            if cur and cur[0] == body:
                return False
            if map_version(cur[0] if cur else None) != (base or ""):
                raise MapChanged({"repo": repo, "body": cur[0], "updated_at": cur[1],
                                  "updated_by": cur[2]} if cur else None)
            stmt = self.sessions_db.upsert(t).values(repo=repo, body=body, updated_at=at,
                                                     updated_by=by)
            conn.execute(stmt.on_conflict_do_update(index_elements=[t.c.repo], set_={
                c: stmt.excluded[c] for c in ("body", "updated_at", "updated_by")}))
            conn.execute(insert(map_history_table).values(repo=repo, changed_at=at, by_station=by,
                                                          project=project, note=note))
        return True

    def repo_map_history(self, repo: str, limit: int = 10) -> list[dict]:
        """Newest first."""
        t = map_history_table
        with self.sessions_db.read() as conn:
            rows = conn.execute(select(t.c.changed_at, t.c.by_station, t.c.project, t.c.note)
                                .where(t.c.repo == repo.lower())
                                .order_by(t.c.changed_at.desc().nulls_last())
                                .limit(limit)).fetchall()
        return [{"at": r[0], "by": r[1], "project": r[2], "note": r[3]} for r in rows]

    # ── rule proposals (TR-443) ───────────────────────────────────────────────
    def upsert_rule_proposal(self, repo: str, kind: str, project: str | None, scope: str,
                             text: str, tickets: list[str], at) -> dict | None:
        """Offer a rule for (repo, kind) unless one was already offered; an open offer gets its
        ticket list refreshed. None when it was decided before (never offered again)."""
        t = proposals_table
        with self._swrite, self.sessions_db.begin() as conn:
            cur = conn.execute(select(t.c.id, t.c.state).where(
                t.c.repo == repo, t.c.kind == kind)
                .order_by(t.c.created_at.asc().nulls_last(), t.c.id)).first()
            if cur and cur.state != "open":
                return None
            if cur:
                conn.execute(update(t).where(t.c.id == cur.id).values(
                    tickets=tickets, scope=scope, project=project, text=text))
                pid = cur.id
            else:
                pid = "rp_" + uuid.uuid4().hex[:10]
                conn.execute(insert(t).values(
                    id=pid, repo=repo, kind=kind, project=project, scope=scope, text=text,
                    tickets=tickets, state="open", created_at=at, decided_at=None))
        return self.get_rule_proposal(pid)

    @staticmethod
    def _proposal_row(r) -> dict:
        return {**r._mapping, "tickets": r.tickets or []}

    def get_rule_proposal(self, pid: str) -> dict | None:
        t = proposals_table
        with self.sessions_db.read() as conn:
            r = conn.execute(select(t).where(t.c.id == pid)).first()
        return self._proposal_row(r) if r else None

    def list_rule_proposals(self, state: str | None = "open") -> list[dict]:
        t = proposals_table
        q = select(t).order_by(t.c.created_at.asc().nulls_last(), t.c.id)
        if state:
            q = q.where(t.c.state == state)
        with self.sessions_db.read() as conn:
            rows = conn.execute(q).fetchall()
        return [self._proposal_row(r) for r in rows]

    def decide_rule_proposal(self, pid: str, state: str, text: str | None, at) -> None:
        t = proposals_table
        values: dict = {"state": state, "decided_at": at}
        if text is not None:
            values["text"] = text
        with self._swrite, self.sessions_db.begin() as conn:
            conn.execute(update(t).where(t.c.id == pid).values(values))

    def station_session_ids(self, station: str) -> list[str]:
        """The sessions that played `station` or a helper it started."""
        t = session_stations
        with self.sessions_db.read() as conn:
            return [r[0] for r in conn.execute(select(t.c.session).where(
                (t.c.station == station) | (t.c.parent == station)))]

    # ── the desk (TR-449..452) ────────────────────────────────────────────────
    def _dk_row(self, r) -> dict:
        # `closed_at` keeps the zone it had in DuckDB, this machine's: /api/desk/answered and the
        # crew's idle wake compare it as text with a time they kept before the move (TR-1616)
        closed = r.closed_at
        if closed is not None and self.sessions_db.dialect == "sqlite":
            closed = closed.astimezone()
        return {"id": r.id, "number": r.number, "label": f"D{r.number}", "project": r.project,
                "question": r.question, "context": r.context or "", "options": r.options or [],
                "recommendation": r.recommendation, "why": r.why, "blocks": r.blocks or "",
                "tickets": r.tickets or [], "blocking": bool(r.blocking),
                "asked_by": r.asked_by, "assumptions": r.assumptions or [],
                "state": r.state, "reason": r.reason, "created_at": r.created_at,
                "closed_at": closed, "station": r.station, "command": r.command,
                "person_id": r.person_id, "to_person_id": r.to_person_id}

    def _dk_people(self, rows: list[dict]) -> list[dict]:
        """Desk rows with `person` (who asked) and `to_person` ({id, name} or None, who it is
        addressed to) filled in; `to_person_id` stays."""
        rows = self._with_people(rows, "asked_by")
        refs = self._person_refs(r.get("to_person_id") for r in rows)
        for r in rows:
            r["to_person"] = refs.get(r["to_person_id"])
        return rows

    def add_desk_item(self, project: str | None, question: str, context: str, options: list[str],
                      recommendation: str, why: str, blocks: str, tickets: list[str],
                      blocking: bool, asked_by: str, assumptions: list[str], at,
                      station: str | None = None, command: str | None = None,
                      person_id: str | None = None, to_person_id: str | None = None) -> dict:
        did = "dk_" + uuid.uuid4().hex[:10]
        t = desk_table
        with self._swrite, self.sessions_db.begin() as conn:
            self._lock_numbering(conn, "tares_desk_number")
            n = (conn.execute(select(func.max(t.c.number))).scalar() or 0) + 1
            conn.execute(insert(t).values(
                id=did, number=n, project=project, question=question, context=context or None,
                options=options or [], recommendation=recommendation, why=why,
                blocks=blocks or None, tickets=tickets or [], blocking=blocking,
                asked_by=asked_by or None, assumptions=assumptions or [], state="open",
                created_at=at, station=station or None, command=command or None,
                person_id=person_id or None, to_person_id=to_person_id or None))
        return self.get_desk_item(did)

    def get_desk_item(self, ref: str) -> dict | None:
        m = re.match(r"^[Dd]?(\d{1,6})$", (ref or "").strip())
        t = desk_table
        with self.sessions_db.read() as conn:
            r = conn.execute(select(t).where(
                or_(t.c.id == ref, t.c.number == (int(m.group(1)) if m else -1)))).first()
        return self._dk_people([self._dk_row(r)])[0] if r else None

    def list_desk(self, state: str | None = "open", project: str | None = None,
                  closed_since=None) -> list[dict]:
        """Desk items by state and project; `closed_since` (a datetime) keeps only the items closed at or after it."""
        t = desk_table
        q = select(t)
        if state:
            q = q.where(t.c.state == state)
        if project:
            q = q.where(t.c.project == project)
        if closed_since is not None:
            q = q.where(t.c.closed_at >= closed_since)
        with self.sessions_db.read() as conn:
            rows = conn.execute(q.order_by(
                t.c.blocking.desc().nulls_last(), t.c.created_at.asc().nulls_last(),
                t.c.number.asc().nulls_last())).fetchall()
        return self._dk_people([self._dk_row(r) for r in rows])

    def close_desk_item(self, did: str, state: str, reason: str | None, at) -> None:
        t = desk_table
        with self._swrite, self.sessions_db.begin() as conn:
            conn.execute(update(t).where(t.c.id == did).values(
                state=state, reason=reason, closed_at=at))

    def add_decision(self, project: str | None, desk_item: str, question: str, words: str,
                     choice: str, standing: bool, scope: str, matched: bool, session: str | None,
                     granted: bool, tickets: list[str], at, via: str | None = None,
                     person_id: str | None = None) -> dict:
        did = "dc_" + uuid.uuid4().hex[:10]
        with self._swrite, self.sessions_db.begin() as conn:
            conn.execute(insert(decisions_table).values(
                id=did, project=project, desk_item=desk_item, question=question, words=words,
                choice=choice or None, standing=standing, scope=scope, matched=matched,
                session=session, granted=granted, tickets=tickets or [], decided_at=at, via=via,
                person_id=person_id or None))
        return next(d for d in self.list_decisions(project, all_=True) if d["id"] == did)

    def list_decisions(self, project: str | None, all_: bool = False) -> list[dict]:
        """A project's decisions, newest first; with `all_`, the cell-wide ones (asked with no
        project) too. `project` None: every decision on the cell."""
        d, k = decisions_table, desk_table
        q = select(d, k.c.number.label("desk_number")).select_from(
            d.outerjoin(k, k.c.id == d.c.desk_item))
        if project and not all_:
            q = q.where(d.c.project == project)
        elif project:
            q = q.where(or_(d.c.project == project, d.c.project.is_(None)))
        with self.sessions_db.read() as conn:
            rows = conn.execute(q.order_by(d.c.decided_at.desc().nulls_last())).fetchall()
        return self._with_people(
            [{"id": r.id, "project": r.project, "desk_item": r.desk_item, "question": r.question,
              "words": r.words, "choice": r.choice, "standing": bool(r.standing),
              "scope": r.scope, "matched": bool(r.matched), "session": r.session,
              "granted": bool(r.granted), "tickets": r.tickets or [], "at": r.decided_at,
              "label": f"D{r.desk_number}" if r.desk_number else None, "via": r.via,
              "person_id": r.person_id}
             for r in rows], None)

    def decisions_between(self, projects: list[str], since, until) -> list[dict]:
        """The decisions of `projects` made from `since` up to, not including, `until`, newest
        first, in one read (the Report). A decision asked with no project is none of theirs."""
        if not projects:
            return []
        d = decisions_table
        q = select(d.c.project, d.c.question, d.c.words, d.c.choice, d.c.tickets, d.c.decided_at,
                   d.c.person_id).where(
            d.c.project.in_(list(projects)), d.c.decided_at >= since, d.c.decided_at < until)
        with self.sessions_db.read() as conn:
            rows = conn.execute(q.order_by(d.c.decided_at.desc(), d.c.id.desc())).fetchall()
        return self._with_people(
            [{"project": r.project, "question": r.question, "words": r.words, "choice": r.choice,
              "tickets": r.tickets or [], "at": r.decided_at, "person_id": r.person_id}
             for r in rows], None)

    ANSWER_VIA =("console", "feed", "session", "message")

    def add_answer(self, item: str, kind: str, project: str | None, ask: str, choice: str | None,
                   said: str, via: str, person_id: str | None, at) -> dict:
        """One answer to what waited for a person (TR-1451). `choice` is yes or no where the item
        has two ways, else None; `said` is the words or the option given, as given."""
        if choice not in (None, "yes", "no"):
            raise ValueError("choice is yes, no or nothing")
        if via not in self.ANSWER_VIA:
            raise ValueError("via is one of " + ", ".join(self.ANSWER_VIA))
        aid = "an_" + uuid.uuid4().hex[:10]
        with self._swrite, self.sessions_db.begin() as conn:
            conn.execute(insert(answers_table).values(
                id=aid, item=item, kind=kind, project=project, ask=ask, choice=choice, said=said,
                via=via, person_id=person_id or None, answered_at=at))
        return {"id": aid, "item": item, "kind": kind, "project": project, "ask": ask,
                "choice": choice, "said": said, "via": via, "at": at,
                "person": self._person_refs([person_id]).get(person_id)}

    def told_items(self, person_id: str) -> set[str]:
        """The waiting items `person_id` was told about, or that were there when telling began
        (TR-1458). Empty for a person whose list was never read; `*` once it was."""
        t = told_table
        with self.sessions_db.read() as conn:
            return set(conn.execute(select(t.c.item).where(t.c.person_id == person_id)).scalars())

    def mark_waiting_told(self, person_id: str, items: list[str], sent: bool, at) -> None:
        """Record `items` as told to `person_id`: `sent` when a message went, False when they were
        only seen (already waiting, or the person had it switched off). An item already recorded
        keeps its first row."""
        if not items:
            return
        with self._swrite, self.sessions_db.begin() as conn:
            for item in dict.fromkeys(items):
                conn.execute(self.sessions_db.upsert(told_table).values(
                    person_id=person_id, item=item, told_at=at, sent=sent).on_conflict_do_nothing())

    def forget_waiting_told(self, person_id: str, items: list[str]) -> None:
        """Undo mark_waiting_told for `items` whose message Slack refused for now (busy, down): the
        next read finds them new and sends them. A row that already names its message stays."""
        if not items:
            return
        t = told_table
        with self._swrite, self.sessions_db.begin() as conn:
            for item in dict.fromkeys(items):
                conn.execute(delete(t).where(t.c.person_id == person_id, t.c.item == item,
                                             t.c.ts.is_(None)))

    def set_told_message(self, person_id: str, items: list[tuple[str, str]], channel: str, ts: str) -> None:
        """The Slack message (`channel`, `ts`) that told `person_id` about `items`, each (id, ask) in
        the order the message shows them (TR-1460), so an answer can edit it later, after a restart too."""
        t = told_table
        with self._swrite, self.sessions_db.begin() as conn:
            for seq, (item, ask) in enumerate(items):
                conn.execute(update(t).where(t.c.person_id == person_id, t.c.item == item)
                             .values(channel=channel, ts=ts, seq=seq, ask=ask))

    def open_told(self, person_id: str) -> list[dict]:
        """The items a message told `person_id` about that it does not yet say were answered."""
        t = told_table
        with self.sessions_db.read() as conn:
            rows = conn.execute(select(t.c.item, t.c.channel, t.c.ts).where(
                t.c.person_id == person_id, t.c.ts.is_not(None), t.c.closed.is_(None))).fetchall()
        return [{"item": r.item, "channel": r.channel, "ts": r.ts} for r in rows]

    def told_message(self, person_id: str, channel: str, ts: str) -> list[dict]:
        """Every item one message told about, in its order, with the line it closed with (or None)."""
        if channel is None or ts is None:     # no message: `== None` would read the unsent rows
            return []
        t = told_table
        with self.sessions_db.read() as conn:
            rows = conn.execute(select(t.c.item, t.c.ask, t.c.closed).where(
                t.c.person_id == person_id, t.c.channel == channel, t.c.ts == ts
            ).order_by(t.c.seq.asc().nulls_last())).fetchall()
        return [{"item": r.item, "ask": r.ask, "closed": r.closed} for r in rows]

    def close_told(self, person_id: str, closed: dict[str, str]) -> None:
        """Record that the message now says `closed[item]` for each item: it is not edited again."""
        t = told_table
        with self._swrite, self.sessions_db.begin() as conn:
            for item, line in closed.items():
                conn.execute(update(t).where(t.c.person_id == person_id, t.c.item == item)
                             .values(closed=line))

    def last_answer(self, items: list[str]) -> dict | None:
        """The newest answer recorded under any of `items` (one item's ids, see answers.item_ids), with
        who gave it ({id, name} or None), or None when it was never answered."""
        if not items:
            return None
        t = answers_table
        with self.sessions_db.read() as conn:
            row = conn.execute(select(t).where(t.c.item.in_(list(items))).order_by(
                t.c.answered_at.desc().nulls_last()).limit(1)).first()
        if not row:
            return None
        return {"item": row.item, "choice": row.choice, "said": row.said, "via": row.via,
                "at": row.answered_at,
                "person": self._person_refs([row.person_id]).get(row.person_id)}

    def list_answers(self, since, hidden: list[str] | None = None, person: str | None = None,
                     own: str | None = None, items: list[str] | None = None) -> list[dict]:
        """The answers given since `since`, newest first, at most 500 (TR-1451). `hidden` leaves out
        those of these projects, except `own`'s grants (a grant's id and ask never name its project, so
        the caller's answer to one over a repo they own stays theirs; any other kind would); `person` keeps
        that person's own; `items` only the answers to these home items. All filter in the query, before
        the limit, so a busy day of other people's answers never pushes the caller's out."""
        t = answers_table
        q = select(t).where(t.c.answered_at >= since)
        if hidden:
            shown = [t.c.project.is_(None), t.c.project.not_in(list(hidden))]
            if own:
                shown.append(and_(t.c.person_id == own, t.c.kind == "grant"))
            q = q.where(or_(*shown))
        if person is not None:
            q = q.where(t.c.person_id == person)
        if items:
            q = q.where(t.c.item.in_(list(items)))
        with self.sessions_db.read() as conn:
            rows = conn.execute(q.order_by(t.c.answered_at.desc().nulls_last()).limit(500)).fetchall()
        return self._with_people(
            [{"id": r.id, "item": r.item, "kind": r.kind, "project": r.project, "ask": r.ask,
              "choice": r.choice, "said": r.said, "via": r.via, "person_id": r.person_id,
              "at": r.answered_at} for r in rows], None)

    def person_turns(self, source: str, sessions: list[str] | None, since,
                     person: str | None = None) -> list[dict]:
        """What a person typed (user turns that are not tool results, not a subagent's) in
        `sessions` (every session when None) since `since`, newest first (TR-450). With `person`,
        only turns whose recording carries that `tares_person` (TR-620), in any session; the
        person `local` also owns the recordings that carry none (the system credential's)
        (TR-634). Read from `session_turns`, where each turn was written once as its line was
        stored (session_ingest, TR-1505)."""
        t = session_turns.c
        q = select(t.session, t.at, t.text).where(t.source == source, t.at >= since)
        if person:
            q = q.where(or_(t.person == person, t.person.is_(None)) if person == "local"
                        else t.person == person)
        if sessions is not None:
            if not sessions:
                return []
            q = q.where(t.session.in_(list(sessions)))
        with self.sessions_db.read() as conn:
            rows = conn.execute(q.order_by(t.at.desc(), t.session.desc(), t.line_key.desc())
                                .limit(2000)).fetchall()
        return [{"session": r[0], "at": r[1], "text": r[2] or ""} for r in rows]

    def role_sessions(self, role: str) -> list[str]:
        with self.sessions_db.read() as conn:
            return [r[0] for r in conn.execute(select(session_stations.c.session).where(
                session_stations.c.role == role))]

    # ── hand-over to the crew (TR-425) ────────────────────────────────────────
    def set_handover(self, project: str, repos: list[dict], autonomy: str | None,
                     by: str | None, at, person_id: str | None = None,
                     crew: str | None = None, override_reason: str | None = None) -> None:
        """Record the hand-over: `repos` is the whole list (the caller merges it with the one
        already kept); `repo` keeps the first path for older readers. `crew` is the name of the
        crew of whoever handed it over (TR-633). `override_reason` is why the project lead handed
        it over without every area owner's sign-off (TR-636); a later hand-over with none keeps it, and
        stays attributed to `person_id` of the hand-over that gave it. Every override is also
        appended to a log (`list_handover_overrides`), so a later one does not replace it."""
        first = next((r["path"] for r in repos if r.get("path")), None)
        t = handovers_table
        ins = self.sessions_db.upsert(t).values(
            project=project, repo=first, handed_by=by or None, handed_at=at, repos=repos,
            autonomy=autonomy or None, person_id=person_id or None, crew=crew or None,
            override_reason=override_reason or None,
            override_at=at if override_reason else None,
            override_person_id=(person_id or None) if override_reason else None)
        new = ins.excluded
        ins = ins.on_conflict_do_update(index_elements=["project"], set_={
            "repo": new.repo, "handed_by": new.handed_by, "person_id": new.person_id,
            "crew": new.crew,
            "override_reason": func.coalesce(new.override_reason, t.c.override_reason),
            "override_at": func.coalesce(new.override_at, t.c.override_at),
            "override_person_id": func.coalesce(new.override_person_id, t.c.override_person_id),
            "finished_person_id": null(), "taken_person_id": null(),
            "handed_at": new.handed_at, "repos": new.repos, "autonomy": new.autonomy,
            "finished_at": null(), "finished_words": null(), "finished_decision": null(),
            "finished_by": null(), "taken_at": null(), "taken_words": null(),
            "taken_by": null(), "released_at": null()})
        # the hand-over and its entry in the override log are saved together or not at all
        with self._swrite, self.sessions_db.begin() as conn:
            conn.execute(ins)
            if override_reason:
                conn.execute(insert(overrides_table).values(
                    project=project, at=at, reason=override_reason, person_id=person_id or None,
                    handed_by=by or None))

    def list_handover_overrides(self, project: str) -> list[dict]:
        """Every hand-over the lead pushed through without all sign-offs, oldest first:
        {at, reason, person, by_label}."""
        return self._handover_overrides([project]).get(project, [])

    def _handover_overrides(self, projects: list[str]) -> dict[str, list[dict]]:
        """{project: its overrides, oldest first} for `projects`, in one query."""
        if not projects:
            return {}
        t = overrides_table
        with self.sessions_db.read() as conn:
            rows = conn.execute(select(t).where(t.c.project.in_(list(projects))).order_by(
                t.c["at"].asc().nulls_last(), t.c.handed_by.asc().nulls_last(),
                t.c.person_id.asc().nulls_last(), t.c.reason.asc().nulls_last())).fetchall()
        refs = self._person_refs(r.person_id for r in rows)
        out: dict[str, list[dict]] = {}
        for r in rows:
            out.setdefault(r.project, []).append(
                {"at": r._mapping["at"], "reason": r.reason, "person": refs.get(r.person_id),
                 "by_label": by_label(refs.get(r.person_id), r.handed_by)})
        return out

    @kept
    def handed_over_projects(self, finished: bool | None = None,
                             taken: bool | None = None) -> list[str]:
        """The projects handed to the crew; `finished` False: only the open ones, True: only the
        ones the person said are done. `taken` False: only the ones the crew still has, True: only
        the ones the person took over (TR-520)."""
        t = handovers_table
        q = select(t.c.project)
        if finished is True:
            q = q.where(t.c.finished_at.is_not(None))
        elif finished is False:
            q = q.where(t.c.finished_at.is_(None))
        if taken is True:
            q = q.where(t.c.taken_at.is_not(None))
        elif taken is False:
            q = q.where(t.c.taken_at.is_(None))
        with self.sessions_db.read() as conn:
            return list(conn.execute(q).scalars())

    def _set_handover_fields(self, project: str, **values) -> None:
        t = handovers_table
        with self._swrite, self.sessions_db.begin() as conn:
            conn.execute(update(t).where(t.c.project == project).values(**values))

    def finish_handover(self, project: str, words: str, decision: str | None, by: str | None,
                        at, person_id: str | None = None) -> None:
        self._set_handover_fields(project, finished_at=at, finished_words=words,
                                  finished_decision=decision, finished_by=by,
                                  finished_person_id=person_id or None)

    def take_over(self, project: str, words: str, by: str | None, at,
                  person_id: str | None = None) -> None:
        """The person takes the project back from the crew (TR-520); released once the
        orchestrator has stopped its builders."""
        self._set_handover_fields(project, taken_at=at, taken_words=words, taken_by=by,
                                  taken_person_id=person_id or None, released_at=None)

    def release_takeover(self, project: str, at) -> None:
        self._set_handover_fields(project, released_at=at)

    def get_handover(self, project: str) -> dict | None:
        return self.handovers_by_project([project]).get(project)

    @kept
    def handovers_by_project(self, projects: list[str] | None = None) -> dict[str, dict]:
        """{project id: hand-over, as get_handover gives it} for the projects handed over, among
        `projects` (all of them for None), in one query plus one for the people and one for the
        overrides, however many there are. A project never handed over is not in the answer."""
        ids = None if projects is None else sorted({p for p in projects if p})
        if ids is not None and not ids:
            return {}
        t = handovers_table
        q = select(t)
        if ids is not None:
            q = q.where(t.c.project.in_(ids))
        with self.sessions_db.read() as conn:
            rows = conn.execute(q).fetchall()
        if not rows:
            return {}
        refs = self._person_refs(x for r in rows for x in (
            r.person_id, r.finished_person_id, r.taken_person_id, r.override_person_id))
        overridden = [r.project for r in rows if r.override_reason]
        overrides = self._handover_overrides(overridden)
        out = {}
        for r in rows:
            project, repo, handed_by, handed_at = r.project, r.repo, r.handed_by, r.handed_at
            autonomy, crew, person_id = r.autonomy, r.crew, r.person_id
            fin_at, fin_words, fin_decision = r.finished_at, r.finished_words, r.finished_decision
            fin_by, fin_person = r.finished_by, r.finished_person_id
            taken_at, taken_words, taken_by = r.taken_at, r.taken_words, r.taken_by
            released_at, taken_person = r.released_at, r.taken_person_id
            o_reason, o_at, o_person = r.override_reason, r.override_at, r.override_person_id
            # an empty list is a list: only a hand-over with no `repos` falls back to `repo`
            repos = (r.repos if r.repos is not None
                     else ([{"path": repo, "name": None}] if repo else []))
            # who pushed the override through: the hand-over that gave it, not a later one; rows
            # from before override_person_id fall back to the handing person
            op, oby = (o_person, None) if o_person else (person_id, handed_by)
            out[project] = {
                "repo": repo, "repos": repos, "autonomy": autonomy, "by": handed_by, "at": handed_at,
                "person": refs.get(person_id), "by_label": by_label(refs.get(person_id), handed_by),
                "handed_by_person": refs.get(person_id), "crew": crew,
                "override": ({"reason": o_reason, "at": o_at, "person": refs.get(op),
                              "by_label": by_label(refs.get(op), oby)} if o_reason else None),
                # true only when the latest hand-over itself carried the override: a later clean
                # one leaves `override` (the latest ever) in place but is not overridden
                "overridden": bool(o_reason and o_at is not None and o_at == handed_at),
                "overrides": overrides.get(project, []) if o_reason else [],
                "finished": ({"at": fin_at, "words": fin_words, "decision": fin_decision, "by": fin_by,
                              "person": refs.get(fin_person), "by_label": by_label(refs.get(fin_person), fin_by)}
                             if fin_at else None),
                "takeover": ({"at": taken_at, "words": taken_words, "by": taken_by, "released_at": released_at,
                              "person": refs.get(taken_person), "by_label": by_label(refs.get(taken_person), taken_by)}
                             if taken_at else None)}
        return out

    # ── questions to an area owner (TR-636) ───────────────────────────────────
    def _sq_rows(self, rows) -> list[dict]:
        out = [dict(r._mapping) for r in rows]
        refs = self._person_refs([x for r in out for x in (r["to_person_id"], r["asked_by"],
                                                           r["answered_by"])])
        for r in out:
            r["to_person"] = refs.get(r["to_person_id"])
            r["asked_by_person"] = refs.get(r["asked_by"])
            r["answered_by_person"] = refs.get(r["answered_by"])
        return out

    def add_spec_question(self, project: str, to_person_id: str, repo: str, text: str,
                          asked_by: str | None, at) -> dict:
        """Q<n>, n one past the project's highest, chosen and inserted in one transaction, so
        parallel asks never share a number."""
        t = questions_table
        with self._swrite, self.sessions_db.begin() as conn:
            self._lock_numbering(conn, f"tares_spec_question_number:{project}")
            top = conn.execute(select(func.max(cast(func.substr(t.c.id, 2), Integer))).where(
                t.c.project == project)).scalar() or 0
            qid = f"Q{top + 1}"
            conn.execute(insert(t).values(
                project=project, id=qid, to_person_id=to_person_id, repo=repo, text=text,
                asked_by=asked_by or None, asked_at=at, state="open"))
        return self.get_spec_question(project, qid)

    def get_spec_question(self, project: str, ref: str) -> dict | None:
        """The project's question by id (Q3, any case, or 3)."""
        m = re.match(r"^[Qq]?(\d{1,6})$", (ref or "").strip())
        if not m:
            return None
        t = questions_table
        with self.sessions_db.read() as conn:
            r = conn.execute(select(t).where(t.c.project == project,
                                             t.c.id == f"Q{int(m.group(1))}")).first()
        return self._sq_rows([r])[0] if r else None

    @kept
    def list_spec_questions(self, project: str | None = None, state: str | None = None,
                            to_person_id: str | None = None) -> list[dict]:
        t = questions_table
        q = select(t)
        for col, v in ((t.c.project, project), (t.c.state, state), (t.c.to_person_id, to_person_id)):
            if v:
                q = q.where(col == v)
        with self.sessions_db.read() as conn:
            rows = conn.execute(q.order_by(
                t.c.asked_at.asc().nulls_last(), t.c.project,
                cast(func.substr(t.c.id, 2), Integer))).fetchall()
        return self._sq_rows(rows)

    def answer_spec_question(self, project: str, qid: str, answer: str, by: str | None,
                             at, via: str | None = None) -> bool:
        """Record the answer of an open question, and where it came from (`via`, "console" for
        the page); False when it was not open."""
        t = questions_table
        with self._swrite, self.sessions_db.begin() as conn:
            hit = conn.execute(update(t).where(
                t.c.project == project, t.c.id == qid, t.c.state == "open").values(
                answer=answer, answered_by=by or None, answered_at=at, state="answered",
                via=via).returning(t.c.id)).first()
        return hit is not None

    # ── the repos connected to the brain (TR-1197) ────────────────────────────
    def _br_rows(self, rows) -> list[dict]:
        out = [dict(r._mapping) for r in rows]
        for r in out:
            for k in ("files_done", "files_total", "sync_failures"):
                r[k] = r[k] or 0
        refs = self._person_refs(r["added_by"] for r in out)
        for r in out:
            r["added_by_name"] = (refs.get(r["added_by"]) or {}).get("name")
        return out

    def list_brain_repos(self) -> list[dict]:
        t = brain_table
        with self.sessions_db.read() as conn:
            rows = conn.execute(select(t).order_by(t.c.added_at.asc().nulls_last(),
                                                   t.c.repo)).fetchall()
        return self._br_rows(rows)

    def get_brain_repo(self, repo: str) -> dict | None:
        t = brain_table
        with self.sessions_db.read() as conn:
            r = conn.execute(select(t).where(t.c.repo == repo)).first()
        return self._br_rows([r])[0] if r else None

    def add_brain_repo(self, repo: str, repo_source_id: str | None, added_by: str | None,
                       at, state: str = "queued") -> bool:
        """Keep a connected repo; False when it was already there (the row is left as it was)."""
        t = brain_table
        with self._swrite, self.sessions_db.begin() as conn:
            hit = conn.execute(self.sessions_db.upsert(t).values(
                repo=repo, repo_source_id=_no_nul(repo_source_id) or None, state=state,
                files_done=0, files_total=0, added_by=added_by or None, added_at=at,
                sync_failures=0).on_conflict_do_nothing().returning(t.c.repo)).first()
        return hit is not None

    def _lock_brain_repo(self, conn, repo: str) -> None:
        """Taken by the writes that read a repo's rows and then change them (its folders, its
        history, its removal), so two processes do them one at a time."""
        self._lock_numbering(conn, f"tares_brain_repo:{repo}")

    def delete_brain_repo(self, repo: str) -> bool:
        """The repo, its chosen folders and its history go in one transaction."""
        t = brain_table
        with self._swrite, self.sessions_db.begin() as conn:
            self._lock_brain_repo(conn, repo)
            hit = conn.execute(delete(t).where(t.c.repo == repo).returning(t.c.repo)).first()
            for child in (brain_folders_table, brain_updates_table):
                conn.execute(delete(child).where(child.c.repo == repo))
        return hit is not None

    BRAIN_UPDATES_KEPT = 50

    def add_brain_repo_update(self, repo: str, kind: str, ok: bool, at, *, commit: str | None = None,
                              files_changed: int = 0, files_deleted: int = 0, files_total: int = 0,
                              error: str | None = None) -> None:
        """Keep one first index (`kind` "index") or later update ("update") of a repo, and drop all but
        the newest BRAIN_UPDATES_KEPT of that repo."""
        t = brain_updates_table
        with self._swrite, self.sessions_db.begin() as conn:
            self._lock_brain_repo(conn, repo)
            conn.execute(insert(t).values(
                id=uuid.uuid4().hex, repo=repo, at=at, kind=kind, ok=1 if ok else 0,
                indexed_commit=_no_nul(commit) or None, files_changed=int(files_changed or 0),
                files_deleted=int(files_deleted or 0), files_total=int(files_total or 0),
                error=_no_nul(error) or None))
            old = (select(t.c.id).where(t.c.repo == repo)
                   .order_by(t.c["at"].desc().nulls_last(), t.c.id.desc())
                   .offset(self.BRAIN_UPDATES_KEPT))
            conn.execute(delete(t).where(t.c.id.in_(old)))

    @staticmethod
    def _bu_row(r) -> dict:
        return {"at": r.at, "kind": r.kind, "ok": bool(r.ok), "indexed_commit": r.indexed_commit,
                "files_changed": r.files_changed or 0, "files_deleted": r.files_deleted or 0,
                "files_total": r.files_total or 0, "error": r.error}

    def list_brain_repo_updates(self, repo: str, limit: int = 20) -> list[dict]:
        """The repo's first index and later updates, newest first."""
        t = brain_updates_table
        with self.sessions_db.read() as conn:
            rows = conn.execute(select(t).where(t.c.repo == repo)
                                .order_by(t.c["at"].desc().nulls_last(), t.c.id.desc())
                                .limit(int(limit))).fetchall()
        return [self._bu_row(r) for r in rows]

    def last_brain_repo_updates(self) -> dict[str, dict]:
        """The newest update of every repo that has one, by repo."""
        t = brain_updates_table
        with self.sessions_db.read() as conn:
            rows = conn.execute(select(t).order_by(
                t.c.repo, t.c["at"].desc().nulls_last(), t.c.id.desc())).fetchall()
        out: dict[str, dict] = {}
        for r in rows:
            out.setdefault(r.repo, self._bu_row(r))
        return out

    def list_brain_folders(self, repo: str) -> list[dict]:
        """The decision folders chosen for `repo`, by path: {path, chosen_by, chosen_by_name, chosen_at}."""
        t = brain_folders_table
        with self.sessions_db.read() as conn:
            rows = conn.execute(select(t.c.path, t.c.chosen_by, t.c.chosen_at)
                                .where(t.c.repo == repo).order_by(t.c.path)).fetchall()
        refs = self._person_refs(r[1] for r in rows)
        return [{"path": r[0], "chosen_by": r[1], "chosen_by_name": (refs.get(r[1]) or {}).get("name"),
                 "chosen_at": r[2]} for r in rows]

    def set_brain_folders(self, repo: str, paths: list[str], by: str | None, at) -> tuple[list[str], list[str]]:
        """Make `paths` the repo's chosen folders. A folder already chosen keeps its chooser and date.
        Returns (added, removed), each sorted. The read, the deletes and the inserts are one
        transaction, and two processes take turns."""
        t = brain_folders_table
        with self._swrite, self.sessions_db.begin() as conn:
            self._lock_brain_repo(conn, repo)
            have = set(conn.execute(select(t.c.path).where(t.c.repo == repo)).scalars())
            want = set(paths)
            added, removed = sorted(want - have), sorted(have - want)
            if removed:
                conn.execute(delete(t).where(t.c.repo == repo, t.c.path.in_(removed)))
            if added:
                conn.execute(self.sessions_db.upsert(t).on_conflict_do_nothing(), [
                    {"repo": repo, "path": p, "chosen_by": by or None, "chosen_at": at}
                    for p in added])
        return added, removed

    def requeue_brain_repo(self, repo: str, from_state: str = "failed") -> bool:
        """Put a repo that is in `from_state` back to queued for a new index job: the error, the
        place, the counts and the job's start are cleared. False when it is not in that state (or
        not there), and then the row is left as it was."""
        t = brain_table
        with self._swrite, self.sessions_db.begin() as conn:
            hit = conn.execute(update(t).where(t.c.repo == repo, t.c.state == from_state).values(
                state="queued", error=None, place=None, files_done=0, files_total=0,
                job_started_at=None, sync_failures=0, sync_error=None).returning(t.c.repo)).first()
        return hit is not None

    _BR_SETTABLE = ("state", "files_done", "files_total", "indexed_commit", "last_synced", "error",
                    "place", "job_started_at", "sync_failures", "sync_error")

    def update_brain_repo(self, repo: str, fields: dict, only_in: tuple[str, ...] | None = None) -> bool:
        """Set some of a repo's columns (state, files_done, files_total, indexed_commit,
        last_synced, error, place, job_started_at, sync_failures, sync_error). With `only_in`, only while the repo is in one
        of those states. False when no row was changed."""
        bad = sorted(set(fields) - set(self._BR_SETTABLE))
        if bad or not fields:
            raise ValueError(f"cannot set {bad or 'nothing'} on a brain repo")
        t = brain_table
        stmt = update(t).where(t.c.repo == repo)
        if only_in:
            stmt = stmt.where(t.c.state.in_(list(only_in)))
        with self._swrite, self.sessions_db.begin() as conn:
            hit = conn.execute(stmt.values({k: _no_nul(v) for k, v in fields.items()})
                               .returning(t.c.repo)).first()
        return hit is not None

    # ── the calls made to the brain (TR-1200) ─────────────────────────────────
    def reserve_brain_call(self, call: dict, cap: int | None = None, since=None) -> bool:
        """Keep a call that is about to start. With `cap`, only while fewer than `cap` calls of the
        same tool and depth have started since `since`; False when the cap is reached (nothing is
        kept). The count and the insert are one transaction, and two calls of one tool and depth
        take turns, in this process or another, so they cannot both take the last place."""
        t = brain_calls_table
        deep = 1 if call.get("deep") else 0
        with self._swrite, self.sessions_db.begin() as conn:
            if cap is not None:
                self._lock_numbering(conn, f"tares_brain_calls:{call['tool']}:{deep}")
                # no `since` compares with NULL and counts nothing
                n = conn.execute(select(func.count()).select_from(t).where(
                    t.c.tool == call["tool"], t.c.deep == deep,
                    t.c["at"] >= bindparam("since", since, type_=UTCDateTime))).scalar_one()
                if n >= cap:
                    return False
            conn.execute(insert(t).values(
                id=call["id"], at=call["at"], tool=call["tool"], deep=deep,
                question=_no_nul(call.get("question")), repos=json.dumps(call.get("repos") or []),
                station=_no_nul(call.get("station")) or None,
                person_id=call.get("person_id") or None, via=call.get("via") or None))
        return True

    def finish_brain_call(self, call_id: str, duration_ms: int, ok: bool, input_tokens: int | None = None,
                          output_tokens: int | None = None, error: str | None = None) -> None:
        t = brain_calls_table
        with self._swrite, self.sessions_db.begin() as conn:
            conn.execute(update(t).where(t.c.id == call_id).values(
                duration_ms=duration_ms, input_tokens=input_tokens, output_tokens=output_tokens,
                ok=1 if ok else 0, error=_no_nul(error)))

    def list_brain_calls(self, since=None, tool: str | None = None, station: str | None = None,
                         limit: int = 1000) -> list[dict]:
        """The calls made to the brain, newest first."""
        t = brain_calls_table
        q = select(t).order_by(t.c["at"].desc().nulls_last(), t.c.id).limit(limit)
        if since is not None:
            q = q.where(t.c["at"] >= since)
        if tool:
            q = q.where(t.c.tool == tool)
        if station:
            q = q.where(t.c.station == station)
        with self.sessions_db.read() as conn:
            rows = conn.execute(q).fetchall()
        out = []
        for r in rows:
            row = dict(r._mapping)
            try:
                row["repos"] = json.loads(row["repos"]) if row["repos"] else []
            except ValueError:
                row["repos"] = []
            row["deep"] = bool(row["deep"])
            row["ok"] = None if row["ok"] is None else bool(row["ok"])
            out.append(row)
        return out

    def _update_waiting_grant(self, gid: str, **values) -> bool:
        """One statement that changes a grant only while it still waits."""
        t = grants_table
        with self._swrite, self.sessions_db.begin() as conn:
            hit = conn.execute(update(t).where(t.c.id == gid, t.c.state == "waiting")
                               .values(**values).returning(t.c.id)).first()
        return hit is not None

    def decline_waiting_grant(self, gid: str, consents: list[dict]) -> bool:
        """End a grant that still waits and keep who said no; False when it was no longer waiting
        (another consent settled it first)."""
        return self._update_waiting_grant(gid, state="revoked", consents=consents)

    def consent_waiting_grant(self, gid: str, consents: list[dict]) -> bool:
        """Keep a consent on a grant that still waits; False when it was no longer waiting (a
        decline or a revoke came first), and then the row is left as it was."""
        return self._update_waiting_grant(gid, consents=consents)

    def activate_waiting_grant(self, gid: str, areas: list[str] | None = None,
                               needs_lead: bool | None = None) -> bool:
        """Put a grant in effect only while it still waits; False when it was declined, revoked
        or put in effect meanwhile. `areas` and `needs_lead`, when given, are written in the
        same statement, so a grant that is no longer waiting keeps what it had."""
        values: dict = {"state": "active"}
        if areas is not None:
            values["areas"] = areas
        if needs_lead is not None:
            values["needs_lead"] = needs_lead
        return self._update_waiting_grant(gid, **values)

    def delete_milestone(self, mid: str) -> None:
        """The milestone goes; its tickets stay, without a milestone."""
        t, m = tickets_table, milestones_table
        with self._swrite, self.sessions_db.begin() as conn:
            conn.execute(update(t).where(t.c.milestone == mid).values(milestone=None))
            conn.execute(delete(m).where(m.c.id == mid))

    # ── grants by kind (TR-630) ───────────────────────────────────────────────
    @staticmethod
    def _grant_row(r) -> dict:
        return {"id": r.id, "kind": r.kind, "project": r.project,
                "holder_person_id": r.holder_person_id, "words": r.words,
                "given_by": r.given_by, "given_at": r.given_at, "areas": r.areas or [],
                "consents": r.consents or [], "state": r.state, "needs_lead": bool(r.needs_lead)}

    def add_grant_row(self, kind: str, words: str, project: str | None = None,
                      holder: str | None = None, given_by: str | None = None,
                      areas: list[str] | None = None, consents: list[dict] | None = None,
                      state: str = "active", given_at=None, needs_lead: bool = False) -> dict:
        """One grant, numbered G1, G2... for the cell; nothing is checked here."""
        t = grants_table
        with self._swrite, self.sessions_db.begin() as conn:
            self._lock_numbering(conn, "tares_grant_number")
            top = conn.execute(select(func.max(cast(func.substr(t.c.id, 2), Integer)))
                               ).scalar() or 0
            gid = f"G{top + 1}"
            conn.execute(insert(t).values(
                id=gid, kind=kind, project=project, holder_person_id=holder, words=words,
                given_by=given_by, given_at=given_at or now_utc(), areas=areas or [],
                consents=consents or [], state=state, needs_lead=needs_lead))
        return self.get_grant_row(gid)

    def get_grant_row(self, gid: str) -> dict | None:
        t = grants_table
        with self.sessions_db.read() as conn:
            r = conn.execute(select(t).where(t.c.id == str(gid or "").strip().upper())).first()
        return self._grant_row(r) if r else None

    def all_grant_rows(self) -> list[dict]:
        """Every grant of every kind and state, oldest first."""
        with self.sessions_db.read() as conn:
            rows = conn.execute(select(grants_table)).fetchall()
        return sorted((self._grant_row(r) for r in rows),
                      key=lambda g: int(g["id"][1:]))

    def update_grant_row(self, gid: str, state: str | None = None,
                         consents: list[dict] | None = None, areas: list[str] | None = None,
                         needs_lead: bool | None = None) -> None:
        values = {k: v for k, v in (("areas", areas), ("needs_lead", needs_lead),
                                    ("state", state), ("consents", consents)) if v is not None}
        if not values:
            return
        t = grants_table
        with self._swrite, self.sessions_db.begin() as conn:
            conn.execute(update(t).where(t.c.id == gid).values(**values))

    # ── your own guardrail rules (TR-862) ─────────────────────────────────────
    @staticmethod
    def _rule_row(r) -> dict:
        return dict(r._mapping)

    def add_guardrail_rule(self, scope: str, project: str | None, kind: str, text: str,
                           by: str | None = None) -> dict:
        """One rule, numbered R1, R2... for the cell; nothing is checked here."""
        t = rules_table
        with self._swrite, self.sessions_db.begin() as conn:
            self._lock_numbering(conn, "tares_guardrail_rules")
            top = conn.execute(select(func.max(cast(func.substr(t.c.id, 2), Integer)))).scalar() or 0
            rid = f"R{top + 1}"
            conn.execute(insert(t).values(id=rid, scope=scope, project=project, kind=kind, text=text,
                                          created_by=by, created_at=now_utc()))
        return self.get_guardrail_rule(rid)

    def get_guardrail_rule(self, rid: str) -> dict | None:
        t = rules_table
        with self.sessions_db.read() as conn:
            r = conn.execute(select(t).where(t.c.id == str(rid or "").strip().upper())).first()
        return self._rule_row(r) if r is not None else None

    def list_guardrail_rules(self) -> list[dict]:
        """Every rule of every scope, oldest first."""
        with self.sessions_db.read() as conn:
            rows = conn.execute(select(rules_table)).fetchall()
        return sorted((self._rule_row(r) for r in rows), key=lambda g: int(g["id"][1:]))

    def set_guardrail_rule_text(self, rid: str, text: str) -> dict | None:
        t = rules_table
        with self._swrite, self.sessions_db.begin() as conn:
            conn.execute(update(t).where(t.c.id == str(rid or "").strip().upper()).values(text=text))
        return self.get_guardrail_rule(rid)

    def delete_guardrail_rule(self, rid: str) -> bool:
        """True when a rule was removed."""
        t = rules_table
        with self._swrite, self.sessions_db.begin() as conn:
            gone = conn.execute(delete(t).where(t.c.id == str(rid or "").strip().upper())
                                .returning(t.c.id)).fetchall()
        return bool(gone)

    # ── guardrail pairs (TR-863) ──────────────────────────────────────────────
    @staticmethod
    def _pair_raw(r) -> dict:
        """A pair row. It holds no integration settings (TR-1380): `settings` is always {}, and the
        runner takes the integration's connection at the factory (`stored_settings`)."""
        return {"id": r.id, "scope": r.scope, "project_uid": r.project_uid, "hook": r.hook,
                "integration": r.integration, "settings": {}, "on_fail": r.on_fail or "block",
                "created_at": r.created_at, "created_by": r.created_by}

    @classmethod
    def _pair_view(cls, raw: dict) -> dict:
        """A pair as a read shows it. `settings` and `secrets` stay in the shape for the console and
        are empty: a pair uses the integration's connection at the factory."""
        from .integrations import registry
        found = registry.get("guardrails", raw["integration"])
        return {"id": raw["id"], "scope": raw["scope"], "project_uid": raw["project_uid"],
                "hook": raw["hook"], "integration": raw["integration"],
                "title": (found.title or raw["integration"]) if found else raw["integration"],
                "settings": {}, "secrets": {}, "on_fail": raw["on_fail"],
                "created_at": raw["created_at"], "created_by": raw["created_by"]}

    def _pair_rows(self, conn) -> list[dict]:
        """Every pair, oldest first, read on `conn`."""
        rows = conn.execute(select(pairs_table)).fetchall()
        return sorted((self._pair_raw(r) for r in rows), key=lambda p: int(p["id"][2:]))

    def list_guardrail_pairs(self, scope: str | None = None, project_uid: str | None = None) -> list[dict]:
        """The pairs of one scope as a read shows them, oldest first: every pair when `scope` is
        None, the factory's for "factory", one project's for "project"."""
        with self.sessions_db.read() as conn:
            rows = self._pair_rows(conn)
        return [self._pair_view(p) for p in rows
                if (scope is None or p["scope"] == scope)
                and (scope != "project" or p["project_uid"] == project_uid)]

    def get_guardrail_pair(self, pid: str) -> dict | None:
        """One pair as a read shows it, or None."""
        raw = self.guardrail_pair_stored(pid)
        return self._pair_view(raw) if raw else None

    def guardrail_pair_stored(self, pid: str) -> dict | None:
        """One pair as the store holds it, or None. For the routes to compare an edit with."""
        t = pairs_table
        with self.sessions_db.read() as conn:
            r = conn.execute(select(t).where(t.c.id == str(pid or "").strip().upper())).first()
        return self._pair_raw(r) if r is not None else None

    def guardrail_pairs_for_run(self, hook: str, project_uid: str | None) -> list[dict]:
        """The pairs to run at `hook`: the factory's first, then the project's own, each oldest
        first. The hook runner reads each one's connection with `stored_settings`."""
        with self.sessions_db.read() as conn:
            rows = self._pair_rows(conn)
        mine = [p for p in rows if p["hook"] == hook]
        return ([p for p in mine if p["scope"] == "factory"]
                + [p for p in mine if p["scope"] == "project" and project_uid
                   and p["project_uid"] == project_uid])

    def save_guardrail_pair(self, scope: str, project_uid: str | None, hook: str, integration: str,
                            on_fail: str, by: str | None = None,
                            pair_id: str | None = None, require_connected: bool = False) -> tuple[dict, str]:
        """Add a pair, or with `pair_id` change one of the same scope and project. Returns
        (the pair as a read shows it, "added" | "updated" | "twin"). A pair holds no integration
        settings (TR-1380): it uses the integration's connection at the factory. An exact twin
        (same scope, project, hook, integration and on_fail) adds nothing. A `pair_id` that names no
        pair of this scope and project raises LookupError. With `require_connected` a guardrail that
        is not connected raises ValueError, checked in the same step as the write (the route asks
        for it). Nothing else is checked here."""
        takes = self._takes_settings("guardrails", integration)   # before the write lock: it loads the registry
        t = pairs_table
        with self._swrite, self.sessions_db.begin() as conn:
            self._lock_numbering(conn, "tares_integrations")
            if require_connected and not self._connected(conn, "guardrails", integration, takes):
                raise ValueError(f"{integration} is not connected: connect it in the factory's "
                                 "integrations first")
            rows = self._pair_rows(conn)
            if pair_id:
                cur = next((p for p in rows if p["id"] == str(pair_id).strip().upper()
                            and p["scope"] == scope and p["project_uid"] == project_uid), None)
                if cur is None:
                    raise LookupError(f"there is no pair {pair_id}")
                conn.execute(update(t).where(t.c.id == cur["id"]).values(
                    hook=hook, integration=integration, settings={}, on_fail=on_fail))
                raw, how = {**cur, "hook": hook, "integration": integration,
                            "on_fail": on_fail}, "updated"
            else:
                twin = next((p for p in rows if (p["scope"], p["project_uid"], p["hook"], p["integration"],
                                                 p["on_fail"]) == (scope, project_uid, hook, integration, on_fail)),
                            None)
                if twin:
                    raw, how = twin, "twin"
                else:
                    top = max((int(p["id"][2:]) for p in rows), default=0)
                    raw, how = {"id": f"GP{top + 1}", "scope": scope, "project_uid": project_uid,
                                "hook": hook, "integration": integration, "settings": {},
                                "on_fail": on_fail, "created_at": now_utc(), "created_by": by}, "added"
                    conn.execute(insert(t).values(**raw))
        return self._pair_view(raw), how

    def delete_guardrail_pair(self, pid: str) -> bool:
        """True when a pair was removed."""
        t = pairs_table
        with self._swrite, self.sessions_db.begin() as conn:
            gone = conn.execute(delete(t).where(t.c.id == str(pid or "").strip().upper())
                                .returning(t.c.id)).fetchall()
        return bool(gone)

    # ── memory lines waiting for a non-Tares memory integration (TR-865) ──────
    def add_pending_memory(self, scope: str, line: str, error: str = "") -> str:
        """Keep a line the integration did not take; returns the row's id. attempts starts at 1:
        the write that just failed."""
        pid = "pm_" + uuid.uuid4().hex[:10]
        at = now_utc()
        with self._swrite, self.sessions_db.begin() as conn:
            conn.execute(insert(pending_table).values(
                id=pid, scope=str(scope), line=str(line), attempts=1,
                last_error=str(error or "")[:500], created_at=at, tried_at=at))
        return pid

    def list_pending_memory(self, scope: str | None = None) -> list[dict]:
        """The waiting lines, oldest first; one scope's when `scope` is given."""
        t = pending_table
        q = select(t.c.id, t.c.scope, t.c.line, t.c.attempts, t.c.last_error, t.c.created_at,
                   t.c.tried_at)
        if scope is not None:
            q = q.where(t.c.scope == str(scope))
        with self.sessions_db.read() as conn:
            rows = conn.execute(q.order_by(t.c.created_at.asc().nulls_last(), t.c.id)).fetchall()
        return [dict(r._mapping) for r in rows]

    def retried_pending_memory(self, pid: str, error: str) -> None:
        """A retry failed again: count it and keep the reason."""
        t = pending_table
        with self._swrite, self.sessions_db.begin() as conn:
            conn.execute(update(t).where(t.c.id == pid).values(
                attempts=t.c.attempts + 1, last_error=str(error or "")[:500], tried_at=now_utc()))

    def delete_pending_memory(self, pid: str) -> bool:
        """True when a row was removed (the integration took the line, or it is no longer needed)."""
        t = pending_table
        with self._swrite, self.sessions_db.begin() as conn:
            gone = conn.execute(delete(t).where(t.c.id == pid).returning(t.c.id)).first()
        return gone is not None

    # ── integration connections, defaults and a project's own choice (TR-839, TR-1380) ──
    # Credentials live only at the factory: each integration the factory connects is one row of
    # integration_connections with its integration settings, secrets included. A slot has one
    # default (integration_choices points at it). A project picks one connected integration and
    # holds only its own non-secret, non-address fields. Secrets are stored as they come, in the
    # state database, exactly as connector secrets are (tares/connectors: redact_config,
    # restore_secrets); there is no new encryption. Every read hands back "" for a secret value
    # and says only whether one is set.

    @staticmethod
    def _integration_slot(slot: str, allowed=SLOTS) -> str:
        if slot not in allowed:
            raise ValueError(f"slot must be one of {', '.join(allowed)} (got {slot!r})")
        return slot

    @staticmethod
    def _secret_fields(slot: str, name: str) -> set | None:
        """The names of the integration's secret fields, from its `settings_schema`; None when
        the integration is not installed here, so the code cannot tell which values are secret."""
        from .integrations import registry
        cls = registry.get(slot, name)
        if cls is None:
            return None
        return {f.name for f in cls.settings_schema if f.kind == "secret"}

    @classmethod
    def _redact_integration(cls, slot: str, name: str, values: dict) -> tuple[dict, dict]:
        """(values with each secret as "", {field: {"has_value": bool}}). An integration that is
        not installed has no schema, so every value is treated as secret. On an installed one, a
        stored key its `settings_schema` does not list (a field a later version dropped, or one
        written by hand) is treated as secret too: nothing reads it back that the schema did not
        declare as plain."""
        from .integrations import registry
        found = registry.get(slot, name)
        if found is None:
            names = set(values)
        else:
            names = ({f.name for f in found.settings_schema if f.kind == "secret"}
                     | (set(values) - {f.name for f in found.settings_schema}))
        out = {k: ("" if k in names else v) for k, v in values.items()}
        flags = {k: {"has_value": values.get(k) not in (None, "")} for k in sorted(names)}
        return out, flags

    @staticmethod
    def _merge_integration_settings(secret: set | None, new: dict | None,
                                    stored: dict | None) -> dict:
        """The settings to store: `new`, with each secret reconciled against `stored`, the row's
        own earlier values (and only when it is the same integration). `secret` is the
        integration's secret field names (`_secret_fields`, read before the write lock is taken),
        None when it is not installed. A secret left out or blank keeps the stored value, JSON null
        clears it, a non-empty value replaces it. An integration that is not installed treats
        every key as a secret, since a read hid all of them."""
        if new is None:
            new = {}
        if not isinstance(new, dict):
            raise ValueError("settings must be an object")
        new = dict(new)
        stored = stored or {}
        names = (set(stored) | set(new)) if secret is None else secret
        out = dict(new)
        for k in names:
            if k in new and new[k] is None:
                out.pop(k, None)                       # null: clear it
            elif new.get(k) in (None, ""):             # missing or blank: keep what was stored
                if stored.get(k) not in (None, ""):
                    out[k] = stored[k]
                else:
                    out.pop(k, None)
        return out

    @staticmethod
    def _load_settings(raw) -> dict:
        v = json.loads(raw) if isinstance(raw, str) else raw
        return v if isinstance(v, dict) else {}

    def _integration_view(self, slot: str, name: str, raw, at, by, **extra) -> dict:
        values, secrets = self._redact_integration(slot, name, self._load_settings(raw))
        return {**extra, "slot": slot, "name": name, "settings": values, "secrets": secrets,
                "updated_at": at, "updated_by": by}

    # the slots that keep exactly one connection: connecting another replaces the old one
    ONE_CONNECTION = ("memory", "monitoring", "runtime")

    def _default_name(self, conn, slot: str) -> str:
        """The name of the slot's default integration: the factory's pointer, else the built-in
        default. Read on `conn`."""
        from .integrations import resolve
        t = choices_table
        r = conn.execute(select(t.c.name).where(t.c.slot == slot)).first()
        return r[0] if r is not None else resolve.DEFAULTS[slot]

    @kept
    def _default_read(self, slot: str) -> str:
        """_default_name in a transaction of its own, for a read: asked once per page read for each
        slot. Not for use inside an open transaction."""
        with self.sessions_db.read() as conn:
            return self._default_name(conn, slot)

    def _connection_view(self, slot: str, name: str, raw, at, by, default: bool) -> dict:
        return {**self._integration_view(slot, name, raw, at, by), "default": default}

    @kept
    def get_choice(self, slot: str) -> dict | None:
        """The factory's default for the slot, secrets redacted: {slot, name, settings, secrets,
        updated_at, updated_by, default}, the settings those of its connection; None when the slot
        has none chosen (it uses its built-in default)."""
        self._integration_slot(slot)
        ch, t = choices_table, connections_table
        with self.sessions_db.consistent_read() as conn:    # the default and its connection agree
            r = conn.execute(select(ch.c.name, ch.c.updated_at, ch.c.updated_by)
                             .where(ch.c.slot == slot)).first()
            if r is None:
                return None
            c = conn.execute(select(t.c.settings, t.c.updated_at, t.c.updated_by)
                             .where(t.c.slot == slot, t.c.name == r.name)).first()
        if c is not None:
            return self._connection_view(slot, r.name, c.settings, c.updated_at, c.updated_by, True)
        return self._connection_view(slot, r.name, {}, r.updated_at, r.updated_by, True)

    def get_connection(self, slot: str, name: str) -> dict | None:
        """One connection of the factory, secrets redacted: {slot, name, settings, secrets,
        updated_at, updated_by, default}; None when it is not connected (a row only: an
        integration with no integration settings has none)."""
        self._integration_slot(slot)
        t = connections_table
        with self.sessions_db.consistent_read() as conn:    # the row and the flag agree
            c = conn.execute(select(t.c.settings, t.c.updated_at, t.c.updated_by)
                             .where(t.c.slot == slot, t.c.name == name)).first()
            default = self._default_name(conn, slot) == name if c is not None else False
        if c is None:
            return None
        return self._connection_view(slot, name, c.settings, c.updated_at, c.updated_by, default)

    @kept
    def list_connections(self, slot: str) -> list[dict]:
        """The slot's connections, secrets redacted, by name, each with `default` true for the one
        the slot uses. Monitoring keeps its one in the tracing settings: it is listed when
        tracing is on."""
        self._integration_slot(slot)
        if slot == "monitoring":
            from . import tracing
            from .integrations.builtin import monitoring_tares as mon
            cfg = tracing.resolve(self)
            if not cfg.active or cfg.provider not in tracing.PROVIDERS:
                return []
            return [self._connection_view(slot, cfg.provider, mon.stored(self, cfg.provider),
                                          None, None, True)]
        from .integrations import resolve
        t, ch = connections_table, choices_table
        # one query, not _default_read: the rows and the default must be from one state
        chosen = select(ch.c.name).where(ch.c.slot == slot).scalar_subquery().label("chosen")
        with self.sessions_db.read() as conn:
            rows = conn.execute(select(t.c.name, t.c.settings, t.c.updated_at, t.c.updated_by, chosen)
                                .where(t.c.slot == slot).order_by(t.c.name)).fetchall()
        return [self._connection_view(slot, r.name, r.settings, r.updated_at, r.updated_by,
                                      r.name == (r.chosen or resolve.DEFAULTS[slot])) for r in rows]

    @staticmethod
    def _has_row(conn, slot: str, name: str) -> bool:
        t = connections_table
        return conn.execute(select(t.c.name).where(t.c.slot == slot,
                                                   t.c.name == name)).first() is not None

    def _connected(self, conn, slot: str, name: str, takes_settings: bool) -> bool:
        """is_connected on `conn`, so a write can check it in the same step as the write.
        `takes_settings` is False for an installed integration whose settings_schema is empty. Read
        the registry before taking the lock."""
        return (not takes_settings or self._has_row(conn, slot, name)
                or self._default_name(conn, slot) == name)

    @staticmethod
    def _takes_settings(slot: str, name: str) -> bool:
        from .integrations import registry
        cls = registry.get(slot, name)       # before the lock: it loads the registry
        return cls is None or bool(cls.settings_schema)

    def is_connected(self, slot: str, name: str) -> bool:
        """The integration is connected: it has a connection row, or it takes no integration
        settings (nothing to connect), or it is the slot's default."""
        self._integration_slot(slot)
        if not self._takes_settings(slot, name):
            return True
        with self.sessions_db.read() as conn:
            if self._has_row(conn, slot, name):
                return True
        return self._default_read(slot) == name

    def set_choice(self, slot: str, name: str, settings: dict | None, by: str,
                   default: bool = True) -> dict:
        """Connect integration `name` for the slot with `settings` and, unless `default` is False,
        make it the slot's default. Returns its connection as get_connection does. Secrets are
        reconciled with this connection's own stored values (see _merge_integration_settings), so no
        secret travels between two integrations. memory, monitoring and runtime keep one connection:
        connecting another replaces the old one, and it is the default."""
        self._integration_slot(slot)
        name = str(name or "").strip()
        if not name:
            raise ValueError("an integration name is required")
        secret = self._secret_fields(slot, name)   # before the write lock: it loads the registry
        if slot == "monitoring" and name in MONITORING_NAMES:
            return self._set_monitoring(name, settings, secret, by)
        t = connections_table
        with self._swrite, self.sessions_db.begin() as conn:
            self._lock_numbering(conn, "tares_integrations")
            cur = conn.execute(select(t.c.settings).where(t.c.slot == slot, t.c.name == name)).first()
            stored = self._load_settings(cur[0]) if cur is not None else {}
            merged = self._merge_integration_settings(secret, settings, stored)
            at = now_utc()
            ins = self.sessions_db.upsert(t).values(slot=slot, name=name, settings=merged,
                                                    updated_at=at, updated_by=by)
            conn.execute(ins.on_conflict_do_update(index_elements=["slot", "name"], set_={
                k: ins.excluded[k] for k in ("settings", "updated_at", "updated_by")}))
            one = slot in self.ONE_CONNECTION
            if one:
                conn.execute(delete(t).where(t.c.slot == slot, t.c.name != name))
            if default or one:
                self._point_default(conn, slot, name, at, by)
            is_default = self._default_name(conn, slot) == name
        return self._connection_view(slot, name, merged, at, by, is_default)

    def _point_default(self, conn, slot: str, name: str, at, by) -> None:
        """Make `name` the slot's default, in the transaction of `conn`."""
        ins = self.sessions_db.upsert(choices_table).values(slot=slot, name=name, settings={},
                                                            updated_at=at, updated_by=by)
        conn.execute(ins.on_conflict_do_update(index_elements=["slot"], set_={
            k: ins.excluded[k] for k in ("name", "settings", "updated_at", "updated_by")}))

    def set_default(self, slot: str, name: str, by: str) -> dict:
        """Make a connected integration the slot's default. Raises ValueError when it is not
        connected, and for monitoring, which keeps one connection in the tracing settings."""
        self._integration_slot(slot)
        name = str(name or "").strip()
        if not name:
            raise ValueError("an integration name is required")
        if slot == "monitoring":
            raise ValueError("monitoring keeps one connection: connect another integration to replace it")
        takes = self._takes_settings(slot, name)
        with self._swrite, self.sessions_db.begin() as conn:
            self._lock_numbering(conn, "tares_integrations")
            # checked in the same step as the write: a connection deleted meanwhile cannot slip by
            if not self._connected(conn, slot, name, takes):
                raise ValueError(f"{name} is not connected: connect it in the factory's integrations first")
            self._point_default(conn, slot, name, now_utc(), by)
        return self.get_connection(slot, name) or self._connection_view(slot, name, {}, now_utc(), by, True)

    def delete_connection(self, slot: str, name: str) -> bool:
        """Remove a connection. False when there is none. Raises ValueError, naming what uses it,
        while it is the slot's default, a project uses it, or a guardrail pair uses it."""
        self._integration_slot(slot)
        t, picks, pt = connections_table, picks_table, pairs_table
        with self._swrite, self.sessions_db.begin() as conn:
            self._lock_numbering(conn, "tares_integrations")
            if conn.execute(select(t.c.name).where(t.c.slot == slot, t.c.name == name)).first() is None:
                return False
            if self._default_name(conn, slot) == name:
                raise ValueError(f"{name} is the default for {slot}: make another connection the "
                                 "default first")
            if slot in PROJECT_SLOTS:
                used = conn.execute(select(picks.c.project_uid).where(
                    picks.c.slot == slot, picks.c.name == name)
                    .order_by(picks.c.project_uid)).scalars().all()
                if used:
                    raise ValueError(f"{name} is used by project {', '.join(used)} for {slot}: "
                                     "change what the project uses first")
            if slot == "guardrails":
                pairs = conn.execute(select(pt.c.id).where(pt.c.integration == name)).scalars().all()
                if pairs:
                    raise ValueError(f"{name} is used by guardrail pair {', '.join(sorted(pairs))}: "
                                     "remove the pair first")
            conn.execute(delete(t).where(t.c.slot == slot, t.c.name == name))
        return True

    def _set_monitoring(self, name: str, settings: dict | None, secret: set | None, by: str) -> dict:
        """Monitoring off, Rius and OTLP live in the tracing settings, the one storage the old
        /api/settings/tracing route also writes (TR-866). A secret left blank keeps the stored one
        while the provider stays the same; another provider starts from nothing. Any choice row an
        earlier version kept for the slot is dropped, so the two never disagree."""
        from .integrations.builtin import monitoring_tares as mon
        merged = self._merge_integration_settings(secret, settings, mon.stored(self, name))
        mon.save(self, name, merged)
        t = choices_table
        with self._swrite, self.sessions_db.begin() as conn:
            conn.execute(delete(t).where(t.c.slot == "monitoring"))
        return self._connection_view("monitoring", name, merged, now_utc(), by, True)

    # ── a project's secrets (TR-775): sealed values; tares/project_secrets.py seals and opens them ──
    _secrets_gen = 0

    def _secrets_changed(self) -> None:
        FactoryStore._secrets_gen += 1

    def secrets_version(self) -> int:
        """A number that changes whenever a secret is set or removed (for a cache of the values)."""
        return FactoryStore._secrets_gen

    def secret_rows(self, uid: str) -> list[dict]:
        """The project's secrets by name, the sealed value included: never return it from a route."""
        t = secrets_table
        with self.sessions_db.read() as conn:
            rows = conn.execute(select(t).where(t.c.project == uid).order_by(t.c.name)).fetchall()
        return [dict(r._mapping) for r in rows]

    def all_secret_rows(self) -> list[dict]:
        with self.sessions_db.read() as conn:
            rows = conn.execute(select(secrets_table)).fetchall()
        return [dict(r._mapping) for r in rows]

    def set_secret(self, uid: str, name: str, sealed: str, by: str) -> None:
        ins = self.sessions_db.upsert(secrets_table).values(
            project=uid, name=name, value=sealed, set_by=by, set_at=now_utc(), used_by=None,
            used_at=None)
        with self._swrite, self.sessions_db.begin() as conn:
            conn.execute(ins.on_conflict_do_update(index_elements=["project", "name"], set_={
                k: ins.excluded[k] for k in ("value", "set_by", "set_at")}))
        self._secrets_changed()

    def delete_secret(self, uid: str, name: str) -> bool:
        t = secrets_table
        with self._swrite, self.sessions_db.begin() as conn:
            gone = conn.execute(delete(t).where(t.c.project == uid, t.c.name == name)
                                .returning(t.c.name)).fetchall()
        self._secrets_changed()
        return bool(gone)

    def secrets_used(self, uid: str, names: list[str], by: str) -> None:
        if not names:
            return
        at = now_utc()
        t = secrets_table
        with self._swrite, self.sessions_db.begin() as conn:
            for n in names:
                conn.execute(update(t).where(t.c.project == uid, t.c.name == n)
                             .values(used_by=by, used_at=at))

    # ── the project's required checks (TR-854) ────────────────────────────────
    @staticmethod
    def clean_commands(commands) -> list[str]:
        """The command strings of a list a person or a station gave: each trimmed, blanks and
        repeats dropped, in the order given. ValueError for anything but a list of strings."""
        if commands is None:
            return []
        if not isinstance(commands, (list, tuple)) or not all(isinstance(c, str) for c in commands):
            raise ValueError("commands is a list of command strings")
        out: list[str] = []
        for c in commands:
            c = " ".join(c.split())
            if c and c not in out:
                out.append(c)
        return out

    def get_required_checks(self, uid: str) -> list[str]:
        """The commands the project requires recorded as passing; [] when it has none."""
        t = required_table
        with self.sessions_db.read() as conn:
            r = conn.execute(select(t.c.commands).where(t.c.project == uid)).first()
        v = r[0] if r is not None else None
        return [c for c in v if isinstance(c, str)] if isinstance(v, list) else []

    def set_required_checks(self, uid: str, commands, by: str) -> list[str]:
        """Replace the project's required checks with `commands` (cleaned as clean_commands);
        an empty list is "no required checks". Returns what was saved."""
        saved = self.clean_commands(commands)
        ins = self.sessions_db.upsert(required_table).values(
            project=uid, commands=saved, updated_at=now_utc(), updated_by=by)
        with self._swrite, self.sessions_db.begin() as conn:
            conn.execute(ins.on_conflict_do_update(index_elements=["project"], set_={
                k: ins.excluded[k] for k in ("commands", "updated_at", "updated_by")}))
        return saved

    # ── merge queue (TR-856) ──────────────────────────────────────────────────
    MERGE_METHODS = ("squash", "merge", "rebase")
    MERGE_OPEN = ("queued", "waiting_ci", "waiting_approval", "merging", "blocked")
    _MQ_FIELDS = ("repo", "base", "pr", "head", "ticket", "project_uid", "state", "reason",
                  "desk_item", "approved_head", "before_files", "unknown_ticks")

    @staticmethod
    def _mq_row(r) -> dict:
        return {**r._mapping, "unknown_ticks": r.unknown_ticks or 0}

    @staticmethod
    def _mq_order():
        t = queue_table
        return (t.c.entered_at.asc().nulls_last(), t.c.id)

    def _mq_rows(self, *where) -> list[dict]:
        """The rows that match, oldest entry first."""
        with self.sessions_db.read() as conn:
            rows = conn.execute(select(queue_table).where(*where).order_by(*self._mq_order())).fetchall()
        return [self._mq_row(r) for r in rows]

    def merge_queue_add(self, repo: str, base: str, pr: int, head: str, ticket: str | None,
                        project_uid: str | None, state: str = "queued",
                        reason: str | None = None) -> dict:
        mid = "mq_" + uuid.uuid4().hex[:10]
        now = now_utc()
        t = queue_table
        with self._swrite, self.sessions_db.begin() as conn:
            self._lock_numbering(conn, "tares_merge_queue")
            # one open row per pull request: two ticks that both saw it ready add it once
            have = conn.execute(select(t).where(
                t.c.repo == repo, t.c.pr == pr, t.c.state.in_(self.MERGE_OPEN))).first()
            if have is not None:
                return self._mq_row(have)
            conn.execute(insert(t).values(
                id=mid, repo=repo, base=base, pr=pr, head=head, ticket=ticket,
                project_uid=project_uid, state=state, reason=reason, desk_item=None,
                approved_head=None, entered_at=now, updated_at=now, before_files=None,
                unknown_ticks=0))
        return self.merge_queue_get(mid)

    def merge_queue_get(self, mid: str) -> dict | None:
        t = queue_table
        with self.sessions_db.read() as conn:
            r = conn.execute(select(t).where(t.c.id == mid)).first()
        return self._mq_row(r) if r is not None else None

    def merge_queue_list(self, repo: str, base: str) -> list[dict]:
        """The rows of one (repo, base), oldest entry first (the queue's own order is the
        caller's)."""
        t = queue_table
        return self._mq_rows(t.c.repo == repo, t.c.base == base)

    def merge_queue_open(self) -> list[dict]:
        """Every row that is not finished: queued, waiting_ci, waiting_approval, merging, blocked."""
        return self._mq_rows(queue_table.c.state.in_(self.MERGE_OPEN))

    def merge_queue_sent_back(self) -> list[dict]:
        """Every row that was sent back to its builder (or ended with the ticket's review)."""
        return self._mq_rows(queue_table.c.state == "sent_back")

    def merge_queue_last_merged(self, repo: str, base: str, not_pr: int | None = None) -> dict | None:
        """The row of the pull request merged most recently into (repo, base), or None. `not_pr`
        leaves one pull request out."""
        t = queue_table
        with self.sessions_db.read() as conn:
            rows = conn.execute(select(t).where(
                t.c.repo == repo, t.c.base == base, t.c.state == "merged")
                .order_by(t.c.updated_at.desc().nulls_last(), t.c.id.desc())).fetchall()
        for r in rows:
            if not_pr is None or r.pr != not_pr:
                return self._mq_row(r)
        return None

    def merge_queue_merged(self, project_uids: list[str]) -> list[dict]:
        """The merged rows of these projects, newest last write first (TR-1672). No project, no
        rows."""
        if not project_uids:
            return []
        t = queue_table
        with self.sessions_db.read() as conn:
            rows = conn.execute(select(t).where(
                t.c.state == "merged", t.c.project_uid.in_(list(project_uids)))
                .order_by(t.c.updated_at.desc().nulls_last(), t.c.id)).fetchall()
        return [self._mq_row(r) for r in rows]

    def merge_approvers(self, person_ids: list[str]) -> dict:
        """{id: {id, name}} of the people who approved merges (TR-1672), one read for all of them."""
        return self._person_refs(person_ids)

    def merge_queue_update(self, mid: str, **fields) -> None:
        bad = [k for k in fields if k not in self._MQ_FIELDS]
        if bad:
            raise ValueError(f"not a merge queue field: {', '.join(bad)}")
        if not fields:
            return
        t = queue_table
        with self._swrite, self.sessions_db.begin() as conn:
            conn.execute(update(t).where(t.c.id == mid).values(**fields, updated_at=now_utc()))

    def merge_queue_delete(self, mid: str) -> None:
        t = queue_table
        with self._swrite, self.sessions_db.begin() as conn:
            conn.execute(delete(t).where(t.c.id == mid))

    def merge_queue_claim(self, mid: str, approved_only: bool = False) -> bool:
        """Take the row for merging: it becomes `merging` only when it is `queued` and no other
        row of its (repo, base) is `merging` (one merge at a time across the cell). With
        `approved_only` (autonomy L3-review) the row also needs approved_head == head. One
        write-locked step, so two ticks cannot both take it."""
        t = queue_table
        with self._swrite, self.sessions_db.begin() as conn:
            self._lock_numbering(conn, "tares_merge_queue")
            r = conn.execute(select(t.c.repo, t.c.base, t.c.state, t.c.head, t.c.approved_head)
                             .where(t.c.id == mid)).first()
            if r is None or r.state != "queued":
                return False
            if approved_only and (not r.approved_head or r.approved_head != r.head):
                return False
            busy = conn.execute(select(t.c.id).where(
                t.c.repo == r.repo, t.c.base == r.base, t.c.state == "merging", t.c.id != mid)).first()
            if busy is not None:
                return False
            conn.execute(update(t).where(t.c.id == mid).values(state="merging", updated_at=now_utc()))
        return True

    def get_merge_method(self, uid: str) -> str:
        """squash, merge or rebase; squash when the project never chose."""
        t = method_table
        with self.sessions_db.read() as conn:
            r = conn.execute(select(t.c.method).where(t.c.project == uid)).first()
        return r[0] if r is not None and r[0] in self.MERGE_METHODS else "squash"

    def set_merge_method(self, uid: str, method: str, by: str) -> str:
        method = (method or "").strip().lower()
        if method not in self.MERGE_METHODS:
            raise ValueError("merge_method is squash, merge or rebase")
        ins = self.sessions_db.upsert(method_table).values(
            project=uid, method=method, updated_at=now_utc(), updated_by=by)
        with self._swrite, self.sessions_db.begin() as conn:
            conn.execute(ins.on_conflict_do_update(index_elements=["project"], set_={
                k: ins.excluded[k] for k in ("method", "updated_at", "updated_by")}))
        return method

    @kept
    def get_project_choice(self, uid: str, slot: str) -> dict | None:
        """The project's own choice for the slot, as get_choice; None when it has none. Its settings
        are the project's own fields only. It never falls back to the factory's default: that is
        the caller's rule."""
        self._integration_slot(slot, PROJECT_SLOTS)
        t = picks_table
        with self.sessions_db.read() as conn:
            r = conn.execute(select(t.c.name, t.c.settings, t.c.updated_at, t.c.updated_by)
                             .where(t.c.project_uid == uid, t.c.slot == slot)).first()
        if r is None:
            return None
        return self._integration_view(slot, r.name, r.settings, r.updated_at, r.updated_by,
                                      project_uid=uid)

    def projects_with_choice(self, slot: str, uids: list[str]) -> set[str]:
        """The projects among `uids` that hold their own choice for the slot, in one query per 500
        projects."""
        return set(self.project_choice_names(slot, uids))

    def project_choice_names(self, slot: str, uids: list[str]) -> dict[str, str]:
        """{project uid: name of its own choice} for the projects among `uids` that hold one for the
        slot, in one query per 500 projects. It is get_project_choice's name for each project, with
        no settings read, and never falls back to the factory's default."""
        self._integration_slot(slot, PROJECT_SLOTS)
        ids = sorted({u for u in uids if u})
        out: dict[str, str] = {}
        t = picks_table
        for i in range(0, len(ids), 500):
            with self.sessions_db.read() as conn:
                rows = conn.execute(select(t.c.project_uid, t.c.name).where(
                    t.c.slot == slot, t.c.project_uid.in_(ids[i:i + 500]))).fetchall()
            out.update({r.project_uid: r.name for r in rows})
        return out

    @classmethod
    def project_field_refusal(cls, slot: str, name: str, key: str) -> str | None:
        """Why a project may not hold field `key` of integration `name`, or None when it may: a
        secret or an address is set where the integration is connected, at the factory. An
        integration that is not installed has no schema, so every key counts as a secret."""
        from .integrations import registry
        found = registry.get(slot, name)
        kind = None
        if found is not None:
            kind = next((f.kind for f in found.settings_schema if f.name == key), "secret")
        if found is None or kind == "secret":
            return (f"{key} is a secret: set it where the integration is connected, in the "
                    "factory's integrations")
        if kind == "url":
            return (f"{key} is an address: set it where the integration is connected, in the "
                    "factory's integrations")
        return None

    def set_project_choice(self, uid: str, slot: str, name: str, settings: dict | None,
                           by: str, require_connected: bool = False) -> dict:
        """Save the project's own choice for the slot: a connected integration's name and the
        project's own fields. A secret or an address in `settings` raises ValueError: those live at
        the factory (TR-1380), so a project never holds one. With `require_connected` an integration
        that is not connected raises ValueError too, checked in the same step as the write (the
        route asks for it). Only tickets, review and ci take one."""
        self._integration_slot(slot, PROJECT_SLOTS)
        name = str(name or "").strip()
        if not name:
            raise ValueError("an integration name is required")
        if settings is None:
            settings = {}
        if not isinstance(settings, dict):
            raise ValueError("settings must be an object")
        for k in settings:
            why = self.project_field_refusal(slot, name, k)   # before the write lock: it loads the registry
            if why:
                raise ValueError(why)
        takes = self._takes_settings(slot, name)
        with self._swrite, self.sessions_db.begin() as conn:
            self._lock_numbering(conn, "tares_integrations")
            if require_connected and not self._connected(conn, slot, name, takes):
                raise ValueError(f"{name} is not connected: connect it in the factory's integrations first")
            at = now_utc()
            ins = self.sessions_db.upsert(picks_table).values(
                project_uid=uid, slot=slot, name=name, settings=settings, updated_at=at, updated_by=by)
            conn.execute(ins.on_conflict_do_update(index_elements=["project_uid", "slot"], set_={
                k: ins.excluded[k] for k in ("name", "settings", "updated_at", "updated_by")}))
        return self._integration_view(slot, name, settings, at, by, project_uid=uid)

    def stored_settings(self, slot: str, name: str, uid: str | None = None) -> dict:
        """The settings integration `name` runs with in the slot, secrets included, as they are
        saved: its connection at the factory, overlaid when `uid` is given with the project's own
        fields if the project chose this integration. {} when it is not connected. For running an
        integration and for merging a form or a connection test over what is saved; never return
        it from a route."""
        self._integration_slot(slot, SLOTS if uid is None else PROJECT_SLOTS)
        if slot == "monitoring" and name in MONITORING_NAMES:
            from .integrations.builtin import monitoring_tares as mon
            return mon.stored(self, name)
        from .integrations import registry
        found = registry.get(slot, name)           # before the lock: it loads the registry
        plain = {f.name for f in found.settings_schema if f.kind not in ("secret", "url")} if found else set()
        t, picks = connections_table, picks_table
        with self.sessions_db.consistent_read() as conn:    # the connection and the pick agree
            c = conn.execute(select(t.c.settings).where(t.c.slot == slot, t.c.name == name)).first()
            own = conn.execute(select(picks.c.name, picks.c.settings).where(
                picks.c.project_uid == uid, picks.c.slot == slot)).first() if uid is not None else None
        out = self._load_settings(c[0]) if c is not None else {}
        if own is not None and own.name == name:
            # a project's own fields; a secret or an address left in a row from before is not read
            out.update({k: v for k, v in self._load_settings(own.settings).items() if k in plain})
        return out

    # ── status changes waiting to reach the ticket system (TR-845) ────────────
    def pending_writeback_fail(self, ticket: str, integration: str, status: str, error: str,
                               at) -> int:
        """A write of `status` to the ticket system failed: add the ticket's row, or update it
        with the status wanted now and the error, one attempt more (the count starts again when the
        status wanted is not the one the row held). Returns the attempts so far.
        Select, then insert or update, in one locked transaction."""
        t = writeback_table
        mine = (t.c.ticket == ticket, t.c.integration == integration)
        with self._swrite, self.sessions_db.begin() as conn:
            self._lock_numbering(conn, "tares_writeback")
            cur = conn.execute(select(t.c.attempts, t.c.status).where(*mine)).first()
            # a different status wanted now is a new write: its attempts start again
            attempts = int(cur.attempts or 0) + 1 if cur is not None and cur.status == status else 1
            values = {"status": status, "attempts": attempts, "last_error": error, "updated_at": at}
            if cur is not None:
                conn.execute(update(t).where(*mine).values(**values))
            else:
                conn.execute(insert(t).values(ticket=ticket, integration=integration, **values))
        return attempts

    def pending_writeback_clear(self, ticket: str, integration: str | None = None) -> bool:
        """The ticket's row goes (the write landed, or the ticket left the system). False when
        there was none."""
        t = writeback_table
        where = [t.c.ticket == ticket] + ([t.c.integration == integration] if integration else [])
        with self._swrite, self.sessions_db.begin() as conn:
            gone = conn.execute(delete(t).where(*where).returning(t.c.ticket)).fetchall()
        return bool(gone)

    def get_pending_writeback(self, ticket: str, integration: str | None = None) -> dict | None:
        t = writeback_table
        where = [t.c.ticket == ticket] + ([t.c.integration == integration] if integration else [])
        with self.sessions_db.read() as conn:
            r = conn.execute(select(t).where(*where)).first()
        return dict(r._mapping) if r is not None else None

    def list_pending_writebacks(self, integration: str | None = None,
                                project: str | None = None) -> list[dict]:
        """The rows waiting, oldest first; `project` keeps those of that project's tickets."""
        t = writeback_table
        q = select(t)
        if integration:
            q = q.where(t.c.integration == integration)
        if project:
            q = q.where(t.c.ticket.in_(
                select(tickets_table.c.id).where(tickets_table.c.project == project)))
        with self.sessions_db.read() as conn:
            rows = conn.execute(q.order_by(t.c.updated_at.asc().nulls_last(), t.c.ticket)).fetchall()
        return [dict(r._mapping) for r in rows]

    def clear_project_choice(self, uid: str, slot: str) -> bool:
        """Drop the project's own choice, so the slot follows the factory's again. False when
        there was none."""
        self._integration_slot(slot, PROJECT_SLOTS)
        t = picks_table
        with self._swrite, self.sessions_db.begin() as conn:
            gone = conn.execute(delete(t).where(t.c.project_uid == uid, t.c.slot == slot)
                                .returning(t.c.slot)).fetchall()
        return bool(gone)
