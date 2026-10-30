"""Storage for the AI SRE edition, mixed into Store.

TR-742: the services a cell knows. Which services exist comes from the data (a source's service
label, see tares/sre.py); the `services` table holds only what a person changed about one: a
display name, hidden, merged into another, and the repos whose deploys belong to it.
"""
from __future__ import annotations

import json

from .envelope import now_utc

SRE_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS services (
      name        TEXT PRIMARY KEY,
      display     TEXT,
      hidden      BOOLEAN,
      merged_into TEXT,
      repos       JSON,
      updated_at  TIMESTAMPTZ,
      updated_by  TEXT
    )""",
]

UNSET = object()   # a field left out of set_service keeps its value; None clears it


class SreStore:
    """Methods Store gains for the AI SRE edition. Expects self.con, self._lock, self.sc,
    self._sread and self._swrite."""

    # ── services: what a person changed about one ─────────────────────────────
    def service_settings(self) -> dict[str, dict]:
        """name -> {display, hidden, merged_into, repos, updated_at, updated_by}, every row."""
        with self._sread:
            rows = self.sc.execute(
                "SELECT name, display, hidden, merged_into, repos, updated_at, updated_by "
                "FROM services").fetchall()
        out = {}
        for name, display, hidden, merged_into, repos, at, by in rows:
            try:
                repo_list = json.loads(repos) if isinstance(repos, str) else (repos or [])
            except ValueError:
                repo_list = []
            out[name] = {"display": display or None, "hidden": bool(hidden),
                         "merged_into": merged_into or None,
                         "repos": [str(r) for r in repo_list] if isinstance(repo_list, list) else [],
                         "updated_at": at, "updated_by": by}
        return out

    def set_service(self, name: str, by: str, *, display=UNSET, hidden=UNSET, merged_into=UNSET,
                    repos=UNSET) -> dict:
        """Create or change one service's row. Only the fields given change; None clears one
        (display, merged_into, repos) or turns hidden off. Returns the row as stored."""
        name = str(name or "").strip()
        if not name:
            raise ValueError("a service needs a name")
        with self._swrite:
            row = self.sc.execute(
                "SELECT display, hidden, merged_into, repos FROM services WHERE name = ?",
                [name]).fetchone()
            cur = {"display": row[0], "hidden": bool(row[1]), "merged_into": row[2],
                   "repos": row[3]} if row else {"display": None, "hidden": False,
                                                 "merged_into": None, "repos": None}
            if display is not UNSET:
                cur["display"] = (str(display).strip() or None) if display is not None else None
            if hidden is not UNSET:
                cur["hidden"] = bool(hidden)
            if merged_into is not UNSET:
                cur["merged_into"] = (str(merged_into).strip() or None) if merged_into else None
            if repos is not UNSET:
                cur["repos"] = json.dumps([str(r) for r in repos]) if repos else None
            vals = [cur["display"], cur["hidden"], cur["merged_into"], cur["repos"], now_utc(), by]
            if row:
                self.sc.execute(
                    "UPDATE services SET display = ?, hidden = ?, merged_into = ?, repos = ?, "
                    "updated_at = ?, updated_by = ? WHERE name = ?", vals + [name])
            else:
                self.sc.execute(
                    "INSERT INTO services (display, hidden, merged_into, repos, updated_at, "
                    "updated_by, name) VALUES (?, ?, ?, ?, ?, ?, ?)", vals + [name])
        self.clear_services_cache()   # every writer, not only the routes: a change shows at once
        return self.service_settings()[name]

    def clear_services_cache(self) -> None:
        """Forget the computed services (tares/sre.py keeps them for 30 seconds). The counter
        lets a compute that started before the write see that it must not be kept."""
        self.__dict__["services_gen"] = self.__dict__.get("services_gen", 0) + 1
        self.__dict__.setdefault("services_cache", {}).clear()

    # ── services: what arrived, from the events (DuckDB only) ─────────────────
    def service_window_counts(self, spec: dict[str, str], since_10m, since_24h) -> list[dict]:
        """One scan of the last 24 hours. `spec` maps a source to the label that names a service
        in it. One row per (source, label value, 'span' or None, is a metric): the count since
        `since_10m` (n10), the count since `since_24h` (n24) and the newest ingest_time. Events
        live in DuckDB only, so this reads self.con under self._lock and joins no state table."""
        from .store import _FIELD_RE, _label_expr
        if not spec:
            return []
        sources = sorted(spec)
        for label in spec.values():
            if not _FIELD_RE.match(label):
                raise ValueError(f"bad label name {label!r}")
        case = "CASE source " + " ".join(f"WHEN ? THEN {_label_expr(spec[s])}" for s in sources) \
               + " END"
        ph = ", ".join(["?"] * len(sources))
        sql = (f"SELECT source, {case} AS svc, "
               "CASE WHEN event_type = 'span' THEN 'span' END AS etype, "
               "json_extract_string(payload, '$.metricType') IS NOT NULL AS is_metric, "
               "COUNT(*) FILTER (WHERE ingest_time >= ?) AS n10, COUNT(*) AS n24, "
               "MAX(ingest_time) AS last "
               f"FROM events WHERE ingest_time >= ? AND source IN ({ph}) "
               "GROUP BY source, svc, etype, is_metric")
        params = [*sources, since_10m, since_24h, *sources]
        with self._lock:
            rows = self.con.execute(sql, params).fetchall()
        return [{"source": r[0], "value": r[1], "event_type": r[2], "is_metric": bool(r[3]),
                 "n10": int(r[4]), "n24": int(r[5]), "last": r[6]}
                for r in rows if r[1] not in (None, "")]

    def service_label_names(self, scope: dict[str, tuple], since_24h, limit: int = 20) -> list[str]:
        """The label names on the events of one service in the last 24 hours. `scope` maps a
        source to (the label naming a service in it, the raw service names to match)."""
        from .store import _label_expr
        names: set[str] = set()
        with self._lock:
            for source, (label, values) in scope.items():
                if not values:
                    continue
                ph = ", ".join(["?"] * len(values))
                rows = self.con.execute(
                    f"SELECT DISTINCT unnest(json_keys(labels)) FROM events WHERE source = ? "
                    f"AND ingest_time >= ? AND {_label_expr(label)} IN ({ph})",
                    [source, since_24h, *values]).fetchall()
                names.update(r[0] for r in rows if r[0])
        return sorted(names)[:limit]
