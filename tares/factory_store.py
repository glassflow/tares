"""Storage for the software factory on Tares (P-TR-176, M2 onward), mixed into Store.

Validation lives in tares/factory.py; these methods trust what they are handed. Every method takes
the store's lock, like the rest of Store. Tables are created after Store's own schema and
migrations (FACTORY_SCHEMA), so they can refer to columns those add.
"""
from __future__ import annotations

import json
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

    def _factory_forget_project(self, uid: str) -> None:
        """A project is deleted: its factory rows go. Lock held."""
        self.con.execute("DELETE FROM milestones WHERE project = ?", [uid])

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

    def delete_milestone(self, mid: str) -> None:
        """The milestone goes; its tickets stay, without a milestone."""
        with self._lock:
            self.con.execute("UPDATE tickets SET milestone = NULL WHERE milestone = ?", [mid])
            self.con.execute("DELETE FROM milestones WHERE id = ?", [mid])
