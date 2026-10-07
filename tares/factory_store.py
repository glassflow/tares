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
    # Assumptions a builder made so it did not block (TR-433): numbered per project, never
    # reused; open until the orchestrator confirms them with the person (kept or overturned).
    """CREATE TABLE IF NOT EXISTS assumptions (
      id          TEXT PRIMARY KEY,
      project     TEXT,
      number      INTEGER,
      question    TEXT,
      choice      TEXT,
      why         TEXT,
      affects     TEXT,
      tickets     JSON,
      state       TEXT,
      words       TEXT,
      batch       TEXT,
      made_by     TEXT,
      made_role   TEXT,
      made_at     TIMESTAMPTZ,
      decided_at  TIMESTAMPTZ
    )""",
    # Checks a builder ran on a ticket (TR-435): the command, what it showed, the commit, the test
    # proven to bite. Also a line in the ticket's working doc; kept here so they can be matched
    # against the recording (TR-442).
    """CREATE TABLE IF NOT EXISTS ticket_checks (
      id          TEXT PRIMARY KEY,
      project     TEXT,
      ticket      TEXT,
      command     TEXT,
      result      TEXT,
      commit_sha  TEXT,
      broke_test  TEXT,
      by_station  TEXT,
      by_session  TEXT,
      checked_at  TIMESTAMPTZ
    )""",
    # The reviewer's verdicts (TR-441), one row per round, on the commit it was given on.
    """CREATE TABLE IF NOT EXISTS reviews (
      id           TEXT PRIMARY KEY,
      project      TEXT,
      ticket       TEXT,
      round        INTEGER,
      pr           TEXT,
      head         TEXT,
      verdict      TEXT,
      verified     TEXT,
      not_verified TEXT,
      by_station   TEXT,
      reviewed_at  TIMESTAMPTZ
    )""",
    # Findings of a review (F1, F2... per ticket), each open until a later round marks it fixed.
    """CREATE TABLE IF NOT EXISTS review_findings (
      id          TEXT PRIMARY KEY,
      project     TEXT,
      ticket      TEXT,
      review      TEXT,
      number      INTEGER,
      severity    TEXT,
      file        TEXT,
      line        INTEGER,
      blocking    BOOLEAN,
      text        TEXT,
      kind        TEXT,
      state       TEXT,
      fixed_in    TEXT,
      found_at    TIMESTAMPTZ
    )""",
    # Rules offered from findings that keep coming back (TR-443): nothing is kept until the person
    # accepts one; a rejected one is never offered again (its key stays).
    """CREATE TABLE IF NOT EXISTS rule_proposals (
      id          TEXT PRIMARY KEY,
      repo        TEXT,
      kind        TEXT,
      project     TEXT,
      scope       TEXT,
      text        TEXT,
      tickets     JSON,
      state       TEXT,
      created_at  TIMESTAMPTZ,
      decided_at  TIMESTAMPTZ
    )""",
    # The desk (TR-449): questions for the person, one numbering for the cell (D1, D2...), written
    # only by the orchestrator. A batch of assumptions to confirm is a desk item too (TR-452).
    """CREATE TABLE IF NOT EXISTS desk_items (
      id             TEXT PRIMARY KEY,
      number         INTEGER,
      project        TEXT,
      question       TEXT,
      context        TEXT,
      options        JSON,
      recommendation TEXT,
      why            TEXT,
      blocks         TEXT,
      tickets        JSON,
      blocking       BOOLEAN,
      asked_by       TEXT,
      assumptions    JSON,
      state          TEXT,
      reason         TEXT,
      created_at     TIMESTAMPTZ,
      closed_at      TIMESTAMPTZ
    )""",
    # The person's answers (TR-450, TR-451): their words as typed, whether Tares found those words
    # in what they typed into the orchestrator's session, and whether it stands from now on.
    """CREATE TABLE IF NOT EXISTS decisions (
      id          TEXT PRIMARY KEY,
      project     TEXT,
      desk_item   TEXT,
      question    TEXT,
      words       TEXT,
      choice      TEXT,
      standing    BOOLEAN,
      scope       TEXT,
      matched     BOOLEAN,
      session     TEXT,
      granted     BOOLEAN,
      tickets     JSON,
      decided_at  TIMESTAMPTZ
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
        self.con.execute("DELETE FROM assumptions WHERE project = ?", [uid])
        self.con.execute("DELETE FROM ticket_checks WHERE project = ?", [uid])
        self.con.execute("DELETE FROM reviews WHERE project = ?", [uid])
        self.con.execute("DELETE FROM review_findings WHERE project = ?", [uid])
        self.con.execute("DELETE FROM desk_items WHERE project = ?", [uid])
        self.con.execute("DELETE FROM decisions WHERE project = ?", [uid])
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
        with self._lock:
            rows = self.con.execute(
                "SELECT t.project, t.number, t.identifier, t.stage FROM tickets t WHERE "
                "t.project <> ? AND t.project IN (SELECT project FROM tickets WHERE "
                "json_extract_string(pr, '$.repo') = ?)", [uid, repo]).fetchall()
        for project, number, ident, stage in rows:
            lab = ident or (f"T{number}" if number else "")
            if lab.upper() == ref.upper() and stage in ("doing", "review", "changes", "blocked"):
                return True
        return False

    def protocol_messages(self, source: str, limit: int = 3000) -> list[dict]:
        """The crew's recorded protocol messages (TR-429): SendMessage calls whose text starts
        `[TF:<TYPE>]`, oldest first, with the sending station (from its session) and the one it
        was sent to."""
        # parsed once per new tool call on the source: a cheap count says whether anything came
        with self._lock:
            n = self.con.execute("SELECT count(*) FROM events WHERE source = ? AND "
                                 "event_type = 'tool_use'", [source]).fetchone()[0]
        hit = getattr(self, "_proto_cache", None)
        if hit and hit[0] == (source, n):
            return hit[1]
        with self._lock:
            rows = self.con.execute(
                "SELECT e.key_value, e.event_time, e.payload, ss.station FROM events e "
                "LEFT JOIN session_stations ss ON ss.session = e.key_value "
                "WHERE e.source = ? AND e.event_type = 'tool_use' "
                "AND e.text LIKE '%SendMessage%' "
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
        self._proto_cache = ((source, n), out)
        return out

    # ── assumptions (TR-433) ──────────────────────────────────────────────────
    _AS_COLS = ("id, project, number, question, choice, why, affects, tickets, state, words, batch, "
                "made_by, made_role, made_at, decided_at")

    @staticmethod
    def _as_row(r) -> dict:
        return {"id": r[0], "project": r[1], "number": r[2], "label": f"A{r[2]}",
                "question": r[3], "choice": r[4], "why": r[5], "affects": r[6] or "",
                "tickets": json.loads(r[7]) if r[7] else [], "state": r[8], "words": r[9],
                "batch": r[10], "made_by": r[11], "made_role": r[12], "made_at": r[13],
                "decided_at": r[14]}

    def add_assumption(self, project: str, question: str, choice: str, why: str, affects: str,
                       tickets: list[str], made_by: str, made_role: str, at) -> dict:
        aid = "as_" + uuid.uuid4().hex[:10]
        with self._lock:
            n = (self.con.execute("SELECT max(number) FROM assumptions WHERE project = ?",
                                  [project]).fetchone()[0] or 0) + 1
            self.con.execute(
                f"INSERT INTO assumptions ({self._AS_COLS}) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, 'open', NULL, NULL, ?, ?, ?, NULL)",
                [aid, project, n, question, choice, why, affects or None,
                 json.dumps(tickets) if tickets else None, made_by or None, made_role or None, at])
            r = self.con.execute(f"SELECT {self._AS_COLS} FROM assumptions WHERE id = ?",
                                 [aid]).fetchone()
        return self._as_row(r)

    def list_assumptions(self, project: str, state: str | None = None,
                         ticket: str | None = None) -> list[dict]:
        q = f"SELECT {self._AS_COLS} FROM assumptions WHERE project = ?"
        vals: list = [project]
        if state:
            q += " AND state = ?"; vals.append(state)
        with self._lock:
            rows = self.con.execute(q + " ORDER BY number", vals).fetchall()
        out = [self._as_row(r) for r in rows]
        return [a for a in out if ticket in a["tickets"]] if ticket else out

    def get_assumption(self, project: str, ref: str) -> dict | None:
        m = re.match(r"^[Aa]?(\d{1,6})$", (ref or "").strip())
        with self._lock:
            r = self.con.execute(
                f"SELECT {self._AS_COLS} FROM assumptions WHERE project = ? AND (id = ? OR "
                "number = ?)", [project, ref, int(m.group(1)) if m else -1]).fetchone()
        return self._as_row(r) if r else None

    def decide_assumption(self, aid: str, state: str, words: str | None, batch: str | None,
                          at) -> None:
        with self._lock:
            self.con.execute("UPDATE assumptions SET state = ?, words = ?, batch = ?, "
                             "decided_at = ? WHERE id = ?", [state, words, batch, at, aid])

    # ── checks (TR-435) ───────────────────────────────────────────────────────
    def add_check(self, project: str, ticket: str, command: str, result: str, commit: str,
                  broke_test: str, by_station: str, by_session: str, at) -> str:
        cid = "ck_" + uuid.uuid4().hex[:10]
        with self._lock:
            self.con.execute(
                "INSERT INTO ticket_checks (id, project, ticket, command, result, commit_sha, "
                "broke_test, by_station, by_session, checked_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, "
                "?, ?)", [cid, project, ticket, command, result, commit or None, broke_test or None,
                          by_station or None, by_session or None, at])
        return cid

    def ticket_checks(self, ticket: str, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self.con.execute(
                "SELECT id, command, result, commit_sha, broke_test, by_station, checked_at FROM "
                "ticket_checks WHERE ticket = ? ORDER BY checked_at DESC LIMIT ?",
                [ticket, limit]).fetchall()
        return [{"id": r[0], "command": r[1], "result": r[2], "commit": r[3], "broke_test": r[4],
                 "by": r[5], "at": r[6]} for r in reversed(rows)]

    # ── reviews (TR-441) ──────────────────────────────────────────────────────
    def add_review(self, project: str, ticket: str, pr: str | None, head: str, verdict: str,
                   verified: str, not_verified: str, by: str, findings: list[dict],
                   resolved: dict[str, str], kind_of, at) -> dict:
        """A review round: earlier findings marked fixed or still open as `resolved` says
        ({F<n> or id: fixed|open}), the new findings numbered on from the ticket's last."""
        rid = "rv_" + uuid.uuid4().hex[:10]
        with self._lock:
            rnd = (self.con.execute("SELECT max(round) FROM reviews WHERE ticket = ?",
                                    [ticket]).fetchone()[0] or 0) + 1
            self.con.execute(
                "INSERT INTO reviews (id, project, ticket, round, pr, head, verdict, verified, "
                "not_verified, by_station, reviewed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [rid, project, ticket, rnd, pr, head, verdict, verified, not_verified or None,
                 by or None, at])
            for ref, st in (resolved or {}).items():
                m = re.match(r"^[Ff]?(\d{1,6})$", str(ref).strip())
                self.con.execute(
                    "UPDATE review_findings SET state = ?, fixed_in = ? WHERE ticket = ? AND "
                    "(id = ? OR number = ?)", [st, rid if st == "fixed" else None, ticket, ref,
                                               int(m.group(1)) if m else -1])
            n = self.con.execute("SELECT max(number) FROM review_findings WHERE ticket = ?",
                                 [ticket]).fetchone()[0] or 0
            for f in findings:
                n += 1
                self.con.execute(
                    "INSERT INTO review_findings (id, project, ticket, review, number, severity, "
                    "file, line, blocking, text, kind, state, fixed_in, found_at) VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', NULL, ?)",
                    ["rf_" + uuid.uuid4().hex[:10], project, ticket, rid, n, f["severity"],
                     f["file"] or None, f["line"] or None, f["blocking"], f["text"],
                     kind_of(f["text"]), at])
        return {"id": rid, "round": rnd}

    def ticket_reviews(self, ticket: str) -> list[dict]:
        with self._lock:
            revs = self.con.execute(
                "SELECT id, round, pr, head, verdict, verified, not_verified, by_station, "
                "reviewed_at FROM reviews WHERE ticket = ? ORDER BY round", [ticket]).fetchall()
            fs = self.con.execute(
                "SELECT id, review, number, severity, file, line, blocking, text, kind, state, "
                "fixed_in FROM review_findings WHERE ticket = ? ORDER BY number",
                [ticket]).fetchall()
        finds = [{"id": f[0], "review": f[1], "label": f"F{f[2]}", "severity": f[3], "file": f[4],
                  "line": f[5], "blocking": bool(f[6]), "text": f[7], "kind": f[8],
                  "state": f[9], "fixed_in": f[10]} for f in fs]
        out = []
        for r in revs:
            mine = [f for f in finds if f["review"] == r[0]]
            fixed_here = [f["label"] for f in finds if f["fixed_in"] == r[0]]
            out.append({"id": r[0], "round": r[1], "pr": r[2], "head": r[3], "verdict": r[4],
                        "verified": r[5], "not_verified": r[6], "by": r[7], "at": r[8],
                        "findings": mine, "fixed": fixed_here})
        return out

    def open_findings(self, ticket: str) -> list[dict]:
        return [f for r in self.ticket_reviews(ticket) for f in r["findings"] if f["state"] == "open"]

    def repo_findings(self, repo: str, since) -> list[dict]:
        """Every reviewer finding on tickets whose PR lives in `repo` since `since`, with the
        ticket and project (TR-443)."""
        with self._lock:
            rows = self.con.execute(
                "SELECT f.ticket, f.project, f.kind, f.text, f.severity, f.found_at FROM "
                "review_findings f JOIN tickets t ON t.id = f.ticket WHERE "
                "json_extract_string(t.pr, '$.repo') = ? AND f.found_at >= ?",
                [repo, since]).fetchall()
        return [{"ticket": r[0], "project": r[1], "kind": r[2], "text": r[3], "severity": r[4],
                 "at": r[5]} for r in rows]

    # ── what the recording says (TR-440, TR-442) ──────────────────────────────
    def challenge_events(self, source: str, project_name: str, ref: str) -> list[dict]:
        """The Codex challenger's reviews and waivers on a ticket's branch (factory/<ref>...) in
        sessions working on the project, oldest first."""
        with self._lock:
            rows = self.con.execute(
                "SELECT event_time, event_type, payload, key_value FROM events WHERE source = ? "
                "AND event_type IN ('challenge_commit', 'challenge_waived', 'challenge_plan') "
                "AND (json_extract_string(labels, '$.branch') = ? OR "
                "     json_extract_string(labels, '$.branch') LIKE ?) "
                "AND coalesce(json_extract_string(labels, '$.tares_project'), ?) = ? "
                "ORDER BY event_time", [source, f"factory/{ref}", f"factory/{ref}-%",
                                        project_name, project_name]).fetchall()
        out = []
        for at, typ, pj, sid in rows:
            try:
                o = json.loads(pj) if isinstance(pj, str) else (pj or {})
            except ValueError:
                o = {}
            ch = o.get("challenge") if isinstance(o.get("challenge"), dict) else {}
            out.append({"at": at, "type": typ, "session": sid, "challenge": ch})
        return out

    def recorded_commands(self, source: str, sessions: list[str], limit: int = 6000) -> list[dict]:
        """The shell commands the sessions ran (their subagents included), each with what it
        printed and whether it failed, oldest first: what a builder's claimed checks are
        compared with."""
        if not sessions:
            return []
        marks = ", ".join("?" for _ in sessions)
        with self._lock:
            rows = self.con.execute(
                f"SELECT event_time, event_type, payload, key_value FROM events WHERE source = ? "
                f"AND key_value IN ({marks}) AND event_type IN ('tool_use', 'tool_result') "
                "ORDER BY event_time DESC LIMIT ?", [source, *sessions, limit]).fetchall()
        uses: dict[str, dict] = {}
        results: dict[str, dict] = {}
        for at, typ, pj, sid in reversed(rows):
            try:
                o = json.loads(pj) if isinstance(pj, str) else (pj or {})
            except ValueError:
                continue
            msg = o.get("message") if isinstance(o.get("message"), dict) else {}
            for b in msg.get("content") or []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use" and b.get("name") == "Bash":
                    inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                    uses[str(b.get("id"))] = {"at": at, "session": sid,
                                              "command": str(inp.get("command") or "")}
                elif b.get("type") == "tool_result":
                    c = b.get("content")
                    if isinstance(c, list):
                        c = "\n".join(str(x.get("text") or "") for x in c if isinstance(x, dict))
                    results[str(b.get("tool_use_id"))] = {"output": str(c or "")[:20000],
                                                          "is_error": bool(b.get("is_error"))}
        out = []
        for tid, u in uses.items():
            r = results.get(tid) or {"output": "", "is_error": False}
            out.append({**u, **r})
        return sorted(out, key=lambda x: x["at"])

    # ── rule proposals (TR-443) ───────────────────────────────────────────────
    def upsert_rule_proposal(self, repo: str, kind: str, project: str | None, scope: str,
                             text: str, tickets: list[str], at) -> dict | None:
        """Offer a rule for (repo, kind) unless one was already offered; an open offer gets its
        ticket list refreshed. None when it was decided before (never offered again)."""
        with self._lock:
            cur = self.con.execute("SELECT id, state FROM rule_proposals WHERE repo = ? AND "
                                   "kind = ?", [repo, kind]).fetchone()
            if cur and cur[1] != "open":
                return None
            if cur:
                self.con.execute("UPDATE rule_proposals SET tickets = ?, scope = ?, project = ? "
                                 "WHERE id = ?", [json.dumps(tickets), scope, project, cur[0]])
                pid = cur[0]
            else:
                pid = "rp_" + uuid.uuid4().hex[:10]
                self.con.execute(
                    "INSERT INTO rule_proposals (id, repo, kind, project, scope, text, tickets, "
                    "state, created_at, decided_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, NULL)",
                    [pid, repo, kind, project, scope, text, json.dumps(tickets), at])
        return self.get_rule_proposal(pid)

    def get_rule_proposal(self, pid: str) -> dict | None:
        with self._lock:
            r = self.con.execute("SELECT id, repo, kind, project, scope, text, tickets, state, "
                                 "created_at, decided_at FROM rule_proposals WHERE id = ?",
                                 [pid]).fetchone()
        return ({"id": r[0], "repo": r[1], "kind": r[2], "project": r[3], "scope": r[4],
                 "text": r[5], "tickets": json.loads(r[6]) if r[6] else [], "state": r[7],
                 "created_at": r[8], "decided_at": r[9]} if r else None)

    def list_rule_proposals(self, state: str | None = "open") -> list[dict]:
        q = "SELECT id FROM rule_proposals"
        vals: list = []
        if state:
            q += " WHERE state = ?"; vals.append(state)
        with self._lock:
            ids = [r[0] for r in self.con.execute(q + " ORDER BY created_at", vals).fetchall()]
        return [self.get_rule_proposal(i) for i in ids]

    def decide_rule_proposal(self, pid: str, state: str, text: str | None, at) -> None:
        with self._lock:
            self.con.execute("UPDATE rule_proposals SET state = ?, text = coalesce(?, text), "
                             "decided_at = ? WHERE id = ?", [state, text, at, pid])

    def station_session_ids(self, station: str) -> list[str]:
        """The sessions that played `station` or a helper it started."""
        with self._lock:
            return [r[0] for r in self.con.execute(
                "SELECT session FROM session_stations WHERE station = ? OR parent = ?",
                [station, station]).fetchall()]

    # ── the desk (TR-449..452) ────────────────────────────────────────────────
    _DK_COLS = ("id, number, project, question, context, options, recommendation, why, blocks, "
                "tickets, blocking, asked_by, assumptions, state, reason, created_at, closed_at")

    @staticmethod
    def _dk_row(r) -> dict:
        return {"id": r[0], "number": r[1], "label": f"D{r[1]}", "project": r[2],
                "question": r[3], "context": r[4] or "", "options": json.loads(r[5]) if r[5] else [],
                "recommendation": r[6], "why": r[7], "blocks": r[8] or "",
                "tickets": json.loads(r[9]) if r[9] else [], "blocking": bool(r[10]),
                "asked_by": r[11], "assumptions": json.loads(r[12]) if r[12] else [],
                "state": r[13], "reason": r[14], "created_at": r[15], "closed_at": r[16]}

    def add_desk_item(self, project: str | None, question: str, context: str, options: list[str],
                      recommendation: str, why: str, blocks: str, tickets: list[str],
                      blocking: bool, asked_by: str, assumptions: list[str], at) -> dict:
        did = "dk_" + uuid.uuid4().hex[:10]
        with self._lock:
            n = (self.con.execute("SELECT max(number) FROM desk_items").fetchone()[0] or 0) + 1
            self.con.execute(
                f"INSERT INTO desk_items ({self._DK_COLS}) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', NULL, ?, NULL)",
                [did, n, project, question, context or None, json.dumps(options or []),
                 recommendation, why, blocks or None, json.dumps(tickets or []), blocking,
                 asked_by or None, json.dumps(assumptions or []), at])
        return self.get_desk_item(did)

    def get_desk_item(self, ref: str) -> dict | None:
        m = re.match(r"^[Dd]?(\d{1,6})$", (ref or "").strip())
        with self._lock:
            r = self.con.execute(f"SELECT {self._DK_COLS} FROM desk_items WHERE id = ? OR "
                                 "number = ?", [ref, int(m.group(1)) if m else -1]).fetchone()
        return self._dk_row(r) if r else None

    def list_desk(self, state: str | None = "open", project: str | None = None) -> list[dict]:
        q, vals = f"SELECT {self._DK_COLS} FROM desk_items WHERE 1 = 1", []
        if state:
            q += " AND state = ?"; vals.append(state)
        if project:
            q += " AND project = ?"; vals.append(project)
        with self._lock:
            rows = self.con.execute(q + " ORDER BY blocking DESC, created_at", vals).fetchall()
        return [self._dk_row(r) for r in rows]

    def close_desk_item(self, did: str, state: str, reason: str | None, at) -> None:
        with self._lock:
            self.con.execute("UPDATE desk_items SET state = ?, reason = ?, closed_at = ? WHERE "
                             "id = ?", [state, reason, at, did])

    def add_decision(self, project: str | None, desk_item: str, question: str, words: str,
                     choice: str, standing: bool, scope: str, matched: bool, session: str | None,
                     granted: bool, tickets: list[str], at) -> dict:
        did = "dc_" + uuid.uuid4().hex[:10]
        with self._lock:
            self.con.execute(
                "INSERT INTO decisions (id, project, desk_item, question, words, choice, standing, "
                "scope, matched, session, granted, tickets, decided_at) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [did, project, desk_item, question, words, choice or None, standing, scope,
                 matched, session, granted, json.dumps(tickets or []), at])
        return next(d for d in self.list_decisions(project, all_=True) if d["id"] == did)

    def list_decisions(self, project: str | None, all_: bool = False) -> list[dict]:
        """A project's decisions, newest first (with `all_`, the cell's decisions not tied to a
        project too)."""
        q = ("SELECT d.id, d.project, d.desk_item, d.question, d.words, d.choice, d.standing, "
             "d.scope, d.matched, d.session, d.granted, d.tickets, d.decided_at, k.number FROM "
             "decisions d LEFT JOIN desk_items k ON k.id = d.desk_item")
        vals: list = []
        if project and not all_:
            q += " WHERE d.project = ?"; vals.append(project)
        elif project:
            q += " WHERE d.project = ? OR d.project IS NULL"; vals.append(project)
        with self._lock:
            rows = self.con.execute(q + " ORDER BY d.decided_at DESC", vals).fetchall()
        return [{"id": r[0], "project": r[1], "desk_item": r[2], "question": r[3], "words": r[4],
                 "choice": r[5], "standing": bool(r[6]), "scope": r[7], "matched": bool(r[8]),
                 "session": r[9], "granted": bool(r[10]),
                 "tickets": json.loads(r[11]) if r[11] else [], "at": r[12],
                 "label": f"D{r[13]}" if r[13] else None} for r in rows]

    def person_turns(self, source: str, sessions: list[str] | None, since) -> list[dict]:
        """What a person typed (user turns that are not tool results, not a subagent's) in
        `sessions` (every session when None) since `since`, newest first (TR-450)."""
        q = ("SELECT key_value, event_time, text FROM events WHERE source = ? AND "
             "event_type = 'user' AND event_time >= ? AND "
             "coalesce(json_extract_string(labels, '$.sidechain'), 'false') <> 'true'")
        vals: list = [source, since]
        if sessions is not None:
            if not sessions:
                return []
            q += f" AND key_value IN ({', '.join('?' for _ in sessions)})"
            vals += sessions
        with self._lock:
            rows = self.con.execute(q + " ORDER BY event_time DESC LIMIT 2000", vals).fetchall()
        return [{"session": r[0], "at": r[1], "text": r[2] or ""} for r in rows]

    def role_sessions(self, role: str) -> list[str]:
        with self._lock:
            return [r[0] for r in self.con.execute(
                "SELECT session FROM session_stations WHERE role = ?", [role]).fetchall()]

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
