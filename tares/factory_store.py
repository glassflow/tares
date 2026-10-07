"""Storage for the software factory on Tares (P-TR-176, M2 onward), mixed into Store.

Validation lives in tares/factory.py; these methods trust what they are handed. Every method takes
the store's lock, like the rest of Store. Tables are created after Store's own schema and
migrations (FACTORY_SCHEMA), so they can refer to columns those add.
"""
from __future__ import annotations

import json
import re
import uuid

from .envelope import now_utc

FACTORY_SCHEMA = [
    # A factory project's milestones (TR-411): each ships something a person can check, its
    # acceptance checks ([{check, expect}]). owner 'linear' when it mirrors a Linear project
    # milestone (external_id), written by the sync; the checks stay Tares's either way.
    """CREATE TABLE IF NOT EXISTS milestones (
      id          TEXT PRIMARY KEY,
      project     TEXT,
      owner       TEXT,
      external_id TEXT,
      name        TEXT,
      goal        TEXT,
      checks      JSON,
      position    DOUBLE,
      created_at  TIMESTAMPTZ,
      updated_at  TIMESTAMPTZ
    )""",
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
    # The last ticket number a project gave out: numbers are never reused, so T5 keeps meaning
    # the same ticket in branch names and messages after a ticket is deleted.
    """CREATE TABLE IF NOT EXISTS ticket_numbers (
      project     TEXT PRIMARY KEY,
      last        INTEGER
    )""",
    # Every change to a ticket's holder or stage (TR-427), with who made it (a station, or the
    # caller's name when unlabeled, or "github" when Tares read a merge off the pull request).
    """CREATE TABLE IF NOT EXISTS ticket_history (
      ticket      TEXT,
      project     TEXT,
      changed_at  TIMESTAMPTZ,
      field       TEXT,
      value       TEXT,
      reason      TEXT,
      by_station  TEXT,
      by_role     TEXT
    )""",
    # A factory project handed to the crew at the end of its spec (TR-425): when, from where.
    """CREATE TABLE IF NOT EXISTS handovers (
      project     TEXT PRIMARY KEY,
      repo        TEXT,
      handed_by   TEXT,
      handed_at   TIMESTAMPTZ
    )""",
]


class FactoryStore:
    """Methods Store gains for the factory. Expects self.con and self._lock."""

    # ── upgrade and clean-up ──────────────────────────────────────────────────
    def _factory_upgrade(self) -> None:
        """Tickets from before numbers get them, in plan order, per project."""
        rows = self.con.execute("SELECT DISTINCT project FROM tickets WHERE number IS NULL"
                                ).fetchall()
        for (project,) in rows:
            top = self.con.execute("SELECT max(number) FROM tickets WHERE project = ?",
                                   [project]).fetchone()[0] or 0
            todo = self.con.execute("SELECT id FROM tickets WHERE project = ? AND number IS NULL "
                                    "ORDER BY position, created_at", [project]).fetchall()
            for i, (tid,) in enumerate(todo, top + 1):
                self.con.execute("UPDATE tickets SET number = ? WHERE id = ?", [i, tid])
            self.con.execute("INSERT INTO ticket_numbers (project, last) VALUES (?, ?) ON "
                             "CONFLICT (project) DO UPDATE SET last = greatest(ticket_numbers.last, "
                             "excluded.last)", [project, top + len(todo)])

    def _next_ticket_number(self, project: str) -> int:
        """The project's next ticket number, never one given out before. Lock held."""
        row = self.con.execute("SELECT last FROM ticket_numbers WHERE project = ?",
                               [project]).fetchone()
        top = self.con.execute("SELECT max(number) FROM tickets WHERE project = ?",
                               [project]).fetchone()[0] or 0
        n = max(row[0] if row else 0, top) + 1
        self.con.execute("INSERT INTO ticket_numbers (project, last) VALUES (?, ?) ON CONFLICT "
                         "(project) DO UPDATE SET last = excluded.last", [project, n])
        return n

    def _factory_forget_project(self, uid: str) -> None:
        """A project is deleted: its factory rows go. Lock held."""
        self.con.execute("DELETE FROM milestones WHERE project = ?", [uid])
        self.con.execute("DELETE FROM ticket_numbers WHERE project = ?", [uid])
        self.con.execute("DELETE FROM ticket_history WHERE project = ?", [uid])
        self.con.execute("DELETE FROM handovers WHERE project = ?", [uid])

    # ── milestones (TR-411) ───────────────────────────────────────────────────
    _MS_COLS = "id, project, owner, external_id, name, goal, checks, position, created_at, updated_at"

    @staticmethod
    def _ms_row(r) -> dict:
        return {"id": r[0], "project": r[1], "owner": r[2], "external_id": r[3], "name": r[4],
                "goal": r[5] or "", "checks": json.loads(r[6]) if r[6] else [],
                "position": r[7], "created_at": r[8], "updated_at": r[9]}

    def create_milestone(self, project: str, name: str, goal: str = "",
                         checks: list[dict] | None = None, position: float | None = None,
                         owner: str = "tares", external_id: str | None = None) -> str:
        mid = "ms_" + uuid.uuid4().hex[:10]
        ts = now_utc()
        with self._lock:
            if position is None:
                last = self.con.execute("SELECT max(position) FROM milestones WHERE project = ?",
                                        [project]).fetchone()[0]
                position = (last or 0) + 1
            self.con.execute(
                f"INSERT INTO milestones ({self._MS_COLS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [mid, project, owner, external_id, name, goal or "",
                 json.dumps(checks or []), float(position), ts, ts])
        return mid

    def update_milestone(self, mid: str, **fields) -> None:
        allowed = {"name", "goal", "checks", "position", "owner", "external_id"}
        sets, vals = ["updated_at = ?"], [now_utc()]
        for col, v in fields.items():
            if col not in allowed:
                continue
            if col == "checks":
                v = json.dumps(v or [])
            sets.append(f"{col} = ?"); vals.append(v)
        vals.append(mid)
        with self._lock:
            self.con.execute(f"UPDATE milestones SET {', '.join(sets)} WHERE id = ?", vals)

    def list_milestones(self, project: str) -> list[dict]:
        with self._lock:
            rows = self.con.execute(f"SELECT {self._MS_COLS} FROM milestones WHERE project = ? "
                                    "ORDER BY position, created_at", [project]).fetchall()
        return [self._ms_row(r) for r in rows]

    def get_milestone(self, project: str, ref: str) -> dict | None:
        """A milestone of the project by id, Linear id, or name (any case)."""
        ref = (ref or "").strip()
        if not ref:
            return None
        with self._lock:
            r = self.con.execute(
                f"SELECT {self._MS_COLS} FROM milestones WHERE project = ? AND (id = ? OR "
                "external_id = ? OR lower(name) = lower(?)) ORDER BY position LIMIT 1",
                [project, ref, ref, ref]).fetchone()
        return self._ms_row(r) if r else None

    # ── stations (TR-418, TR-420) ─────────────────────────────────────────────
    def set_session_station(self, session: str, station: str, role: str, parent: str,
                            project: str | None, at) -> None:
        with self._lock:
            self.con.execute(
                "INSERT INTO session_stations (session, station, role, parent, project, seen_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (session) DO UPDATE SET "
                "station = excluded.station, role = excluded.role, parent = excluded.parent, "
                "project = coalesce(excluded.project, session_stations.project)",
                [session, station, role or None, parent or None, project, at])

    def session_station(self, session: str) -> dict | None:
        with self._lock:
            r = self.con.execute("SELECT station, role, parent, project FROM session_stations "
                                 "WHERE session = ?", [session]).fetchone()
        return {"station": r[0], "role": r[1], "parent": r[2], "project": r[3]} if r else None

    def station_sessions(self, source: str) -> list[dict]:
        """Every session that played a station, with its station, role, parent and project, when
        it started and last did something (from its lines on `source`), its state, and the
        project it last named (project_sessions), newest activity first."""
        with self._lock:
            rows = self.con.execute(
                "SELECT ss.session, ss.station, ss.role, ss.parent, ss.project, ss.seen_at, "
                "min(e.event_time), max(e.event_time), count(e.key_value), "
                "st.state, st.reason, st.state_at, "
                "(SELECT p.project FROM project_sessions p WHERE p.session = ss.session "
                " ORDER BY p.linked_at DESC LIMIT 1) "
                "FROM session_stations ss "
                "LEFT JOIN events e ON e.source = ? AND e.key_value = ss.session "
                "LEFT JOIN session_states st ON st.session = ss.session "
                "GROUP BY ss.session, ss.station, ss.role, ss.parent, ss.project, ss.seen_at, "
                "st.state, st.reason, st.state_at "
                "ORDER BY coalesce(max(e.event_time), ss.seen_at) DESC", [source]).fetchall()
        return [{"session": r[0], "station": r[1], "role": r[2], "parent": r[3],
                 "project": r[4] or r[12], "seen_at": r[5], "started_at": r[6] or r[5],
                 "last_at": r[7] or r[5], "lines": int(r[8] or 0), "state": r[9],
                 "state_reason": r[10], "state_at": r[11]} for r in rows]

    # ── the ledger's history (TR-427) ─────────────────────────────────────────
    def add_ticket_history(self, ticket: str, project: str, field: str, value: str | None,
                           reason: str | None, by_station: str | None, by_role: str | None,
                           at) -> None:
        with self._lock:
            self.con.execute(
                "INSERT INTO ticket_history (ticket, project, changed_at, field, value, reason, "
                "by_station, by_role) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [ticket, project, at, field, value, reason or None, by_station or None,
                 by_role or None])

    def ticket_history(self, ticket: str) -> list[dict]:
        with self._lock:
            rows = self.con.execute(
                "SELECT changed_at, field, value, reason, by_station, by_role FROM ticket_history "
                "WHERE ticket = ? ORDER BY changed_at", [ticket]).fetchall()
        return [{"at": r[0], "field": r[1], "value": r[2], "reason": r[3], "by": r[4],
                 "role": r[5]} for r in rows]

    def factory_branch_prs(self, repos: set[str]) -> dict[str, tuple[str, int]]:
        """{ticket ref (upper case): (repo, number)} for the pull requests GitHub sources saw on
        a `factory/<ref>` branch in one of `repos`, newest first wins (TR-410)."""
        from .factory_github import branch_ref
        with self._lock:
            rows = self.con.execute(
                "SELECT labels FROM events WHERE event_type = 'pull_request' "
                "ORDER BY event_time DESC LIMIT 1000").fetchall()
        out: dict[str, tuple[str, int]] = {}
        for (lj,) in rows:
            try:
                lab = json.loads(lj) if isinstance(lj, str) else (lj or {})
            except ValueError:
                continue
            ref = branch_ref(lab.get("branch") or "")
            if not ref or lab.get("repo") not in repos:
                continue
            try:
                out.setdefault(ref.upper(), (lab["repo"], int(lab.get("number"))))
            except (TypeError, ValueError):
                continue
        return out

    def protocol_messages(self, source: str, limit: int = 3000) -> list[dict]:
        """The crew's recorded protocol messages (TR-429): SendMessage calls whose text starts
        `[TF:<TYPE>]`, oldest first, with the sending station (from its session) and the one it
        was sent to."""
        with self._lock:
            rows = self.con.execute(
                "SELECT e.key_value, e.event_time, e.payload, ss.station FROM events e "
                "LEFT JOIN session_stations ss ON ss.session = e.key_value "
                "WHERE e.source = ? AND e.event_type = 'tool_use' "
                "AND CAST(e.payload AS VARCHAR) LIKE '%[TF:%' "
                "ORDER BY e.event_time DESC LIMIT ?", [source, limit]).fetchall()
        out = []
        for sid, at, pj, station in reversed(rows):
            try:
                o = json.loads(pj) if pj else {}
            except (TypeError, ValueError):
                continue
            msg = o.get("message") if isinstance(o.get("message"), dict) else {}
            for b in msg.get("content") or []:
                if not (isinstance(b, dict) and b.get("type") == "tool_use"
                        and str(b.get("name") or "").split("__")[-1] == "SendMessage"):
                    continue
                inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                text = str(inp.get("message") or inp.get("content") or "")
                m = re.match(r"\s*\[TF:([A-Z_]+)\]\s*(.*)", text)
                if not m:
                    continue
                out.append({"at": at, "session": sid, "from": station or "", "to":
                            str(inp.get("to") or ""), "type": m.group(1),
                            "first_line": m.group(2).splitlines()[0][:300] if m.group(2) else "",
                            "text": text[:4000]})
        return out

    # ── hand-over to the crew (TR-425) ────────────────────────────────────────
    def set_handover(self, project: str, repo: str | None, by: str | None, at) -> None:
        with self._lock:
            self.con.execute(
                "INSERT INTO handovers (project, repo, handed_by, handed_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (project) DO UPDATE SET repo = excluded.repo, "
                "handed_by = excluded.handed_by, handed_at = excluded.handed_at",
                [project, repo or None, by or None, at])

    def handed_over_projects(self) -> list[str]:
        with self._lock:
            return [r[0] for r in self.con.execute("SELECT project FROM handovers").fetchall()]

    def get_handover(self, project: str) -> dict | None:
        with self._lock:
            r = self.con.execute("SELECT repo, handed_by, handed_at FROM handovers WHERE "
                                 "project = ?", [project]).fetchone()
        return {"repo": r[0], "by": r[1], "at": r[2]} if r else None

    def delete_milestone(self, mid: str) -> None:
        """The milestone goes; its tickets stay, without a milestone."""
        with self._lock:
            self.con.execute("UPDATE tickets SET milestone = NULL WHERE milestone = ?", [mid])
            self.con.execute("DELETE FROM milestones WHERE id = ?", [mid])
