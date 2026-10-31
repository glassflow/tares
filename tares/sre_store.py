"""Storage for the AI SRE edition, mixed into Store.

TR-742: the services a cell knows. Which services exist comes from the data (a source's service
label, see tares/sre.py); the `services` table holds only what a person changed about one: a
display name, hidden, merged into another, and the repos whose deploys belong to it.
"""
from __future__ import annotations

import json
import math
import uuid
from datetime import timedelta, timezone

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
    # TR-743: whether anybody is watching each service, and whether Slack was told. One row per
    # service; a service that is no longer known has its row removed (tares/watch.py).
    """CREATE TABLE IF NOT EXISTS watch_state (
      service    TEXT PRIMARY KEY,
      watched    BOOLEAN,
      reason     TEXT,
      since      TIMESTAMPTZ,
      agent      TEXT,
      project    TEXT,
      told_at    TIMESTAMPTZ,
      told_where TEXT
    )""",
    # TR-744: incidents. An incident is the run of notes an investigator wrote about one service
    # while it was still the same trouble: it is open, someone is on it, or it is resolved.
    """CREATE TABLE IF NOT EXISTS incidents (
      id           TEXT PRIMARY KEY,
      service      TEXT,
      project      TEXT,
      state        TEXT,
      opened_at    TIMESTAMPTZ,
      last_note_at TIMESTAMPTZ,
      owner        TEXT,
      owner_name   TEXT,
      owner_at     TIMESTAMPTZ,
      resolved_at  TIMESTAMPTZ,
      resolved_by  TEXT,
      resolved_by_name TEXT,
      resolution   TEXT,
      slack        JSON,
      updated_at   TIMESTAMPTZ
    )""",
    """CREATE TABLE IF NOT EXISTS incident_notes (
      run_id        TEXT PRIMARY KEY,
      incident_id   TEXT,
      joined_at     TIMESTAMPTZ,
      slack_channel TEXT,
      slack_ts      TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS incident_log (
      incident_id TEXT, at_time TIMESTAMPTZ, action TEXT, by_id TEXT, by_name TEXT, via TEXT
    )""",
]

INCIDENT_COLS = ("id", "service", "project", "state", "opened_at", "last_note_at", "owner",
                 "owner_name", "owner_at", "resolved_at", "resolved_by", "resolved_by_name",
                 "resolution", "slack", "updated_at")
_INCIDENT_SELECT = "SELECT " + ", ".join(INCIDENT_COLS) + " FROM incidents "

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

    # ── services: event counts over time (TR-767) ─────────────────────────────
    def _series_scope(self, services: list[str]):
        """What a chart over `services` reads: ({service: the raw names counted for it}, {source:
        the label naming a service in it}). A service that is merged into another is charted as
        that one, and a service counts every name merged into it. The settings (a state table)
        are read first and only names go into the events query."""
        from . import sre
        from .config import catalog_from_db
        settings = self.service_settings()
        overrides = sre._overrides(self)
        spec = {}
        for s in catalog_from_db(self).sources.values():
            label = sre.service_label(s, overrides)
            if label:
                spec[s.name] = label
        names = {}
        for svc in services:
            top = sre.canonical(svc, settings)
            names[svc] = {top, svc} | {n for n in settings if sre.canonical(n, settings) == top}
        return names, spec

    def _series_query(self, spec, names, since, until, bucket_s, errors_only):
        """(source, raw service name, bucket start, count) rows for the sources in `spec` and the
        raw `names`. Events live in DuckDB only, so this reads self.con under self._lock."""
        from . import sre
        from .store import _label_expr
        sources = sorted(spec)
        case = "CASE source " + " ".join(f"WHEN ? THEN {_label_expr(spec[s])}" for s in sources) \
               + " END"
        ph_s = ", ".join(["?"] * len(sources))
        ph_n = ", ".join(["?"] * len(names))
        sql = (f"SELECT source, svc, b, COUNT(*) FROM (SELECT source, {case} AS svc, "
               "time_bucket(INTERVAL '" + str(int(bucket_s)) + " seconds', ingest_time, ?) AS b "
               f"FROM events WHERE ingest_time >= ? AND ingest_time < ? AND source IN ({ph_s})")
        params = [*sources, since, since, until, *sources]
        if errors_only:
            where, wparams = sre.is_error_where(spec)
            sql += f" AND {where}"
            params += wparams
        sql += f") WHERE svc IN ({ph_n}) GROUP BY source, svc, b"
        params += sorted(names)
        with self._lock:
            return self.con.execute(sql, params).fetchall()

    @staticmethod
    def _series_points(since, until, bucket_s) -> list:
        if bucket_s < 1 or until <= since:
            raise ValueError("a series needs a window that ends after it starts and a bucket "
                             "of at least one second")
        n = math.ceil((until - since).total_seconds() / bucket_s)
        return [(since + timedelta(seconds=i * bucket_s)).astimezone(timezone.utc).isoformat()
                for i in range(n)]

    def event_series_many(self, services: list[str], since, until, bucket_s: int,
                          errors_only: bool = False) -> dict[str, list[tuple[str, int]]]:
        """For each service, [(bucket start as ISO text, events)] over [since, until) in buckets
        of `bucket_s` seconds aligned to `since`, with 0 for a bucket where nothing arrived.
        errors_only counts only what sre.is_error_where says is an error. A service counts the
        services merged into it. One query for all of them."""
        labels = self._series_points(since, until, bucket_s)
        services = list(dict.fromkeys(services))
        out = {svc: [0] * len(labels) for svc in services}
        if services:
            names, spec = self._series_scope(services)
            every = set().union(*names.values())
            if spec:
                owners = {}
                for svc, group in names.items():
                    for n in group:
                        owners.setdefault(n, []).append(svc)
                for _src, raw, b, n in self._series_query(spec, every, since, until, bucket_s,
                                                          errors_only):
                    i = int((b - since).total_seconds() // bucket_s)
                    if 0 <= i < len(labels):
                        for svc in owners.get(raw, ()):
                            out[svc][i] += int(n)
        return {svc: list(zip(labels, counts)) for svc, counts in out.items()}

    def event_series(self, service: str, since, until, bucket_s: int,
                     errors_only: bool = False) -> list[tuple[str, int]]:
        """event_series_many for one service."""
        return self.event_series_many([service], since, until, bucket_s, errors_only)[service]

    # ── watching: whether anybody is watching each service (TR-743) ───────────
    def watch_rows(self) -> dict[str, dict]:
        """service -> {watched, reason, since, agent, project, told_at, told_where}, every row."""
        with self._sread:
            rows = self.sc.execute(
                "SELECT service, watched, reason, since, agent, project, told_at, told_where "
                "FROM watch_state").fetchall()
        return {r[0]: {"watched": bool(r[1]), "reason": r[2], "since": r[3], "agent": r[4],
                       "project": r[5], "told_at": r[6], "told_where": r[7]} for r in rows}

    def set_watch_row(self, service: str, **fields) -> None:
        """Create or change one service's row: only the fields given change (watched, reason,
        since, agent, project, told_at, told_where); None clears one."""
        cols = ("watched", "reason", "since", "agent", "project", "told_at", "told_where")
        bad = sorted(set(fields) - set(cols))
        if bad:
            raise ValueError(f"no such watch field: {', '.join(bad)}")
        with self._swrite:
            row = self.sc.execute(
                "SELECT watched, reason, since, agent, project, told_at, told_where "
                "FROM watch_state WHERE service = ?", [service]).fetchone()
            cur = dict(zip(cols, row)) if row else dict.fromkeys(cols)
            cur.update(fields)
            vals = [bool(cur["watched"]) if cur["watched"] is not None else None,
                    *(cur[c] for c in cols[1:])]
            if row:
                self.sc.execute(
                    "UPDATE watch_state SET watched = ?, reason = ?, since = ?, agent = ?, "
                    "project = ?, told_at = ?, told_where = ? WHERE service = ?", vals + [service])
            else:
                self.sc.execute(
                    "INSERT INTO watch_state (watched, reason, since, agent, project, told_at, "
                    "told_where, service) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", vals + [service])

    def delete_watch_rows(self, services: list[str]) -> None:
        with self._swrite:
            for s in services:
                self.sc.execute("DELETE FROM watch_state WHERE service = ?", [s])

    def mark_told(self, services: list[str], at, where: str) -> list[str]:
        """Say Slack was told about these services at `at`, in `where`. Only a row not told yet
        changes, and the services that did are returned, so two posts for one change cannot both
        count."""
        done = []
        with self._swrite:
            for s in services:
                row = self.sc.execute(
                    "SELECT 1 FROM watch_state WHERE service = ? AND told_at IS NULL",
                    [s]).fetchone()
                if row:
                    self.sc.execute(
                        "UPDATE watch_state SET told_at = ?, told_where = ? WHERE service = ? "
                        "AND told_at IS NULL", [at, where, s])
                    done.append(s)
        return done

    def trigger_created_at(self, name: str):
        """When a trigger was made (None for one the catalog does not hold)."""
        with self._sread:
            row = self.sc.execute("SELECT created_at FROM catalog_triggers WHERE name = ?",
                                  [name]).fetchone()
        return row[0] if row else None

    # ── incidents (TR-744) ────────────────────────────────────────────────────
    @staticmethod
    def _incident(row) -> dict:
        out = dict(zip(INCIDENT_COLS, row))
        slack = out["slack"]
        try:
            slack = json.loads(slack) if isinstance(slack, str) else slack
        except ValueError:
            slack = None
        out["slack"] = slack if isinstance(slack, dict) and slack.get("channel") else None
        return out

    def create_incident(self, service: str, project: str | None, at, *, state: str = "open",
                        resolved_at=None, resolution: str | None = None) -> dict:
        """A new incident whose first note is at `at`. `state` is open, except for one made
        already resolved (the backfill's old ones)."""
        iid = "inc_" + uuid.uuid4().hex[:10]
        with self._swrite:
            self.sc.execute(
                "INSERT INTO incidents (id, service, project, state, opened_at, last_note_at, "
                "resolved_at, resolution, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [iid, service, project, state, at, at, resolved_at, resolution, at])
        return self.get_incident(iid)

    def get_incident(self, incident_id: str) -> dict | None:
        with self._sread:
            row = self.sc.execute(_INCIDENT_SELECT + "WHERE id = ?", [incident_id]).fetchone()
        return self._incident(row) if row else None

    def open_incident_for(self, service: str, since, until=None) -> dict | None:
        """The newest incident of `service` that is not resolved and had a note at or after
        `since`. With `until`, only one opened at or before it: the backfill files an old note
        and an incident that began after that note is not its incident."""
        sql = (_INCIDENT_SELECT + "WHERE service = ? AND state <> 'resolved' AND "
               "last_note_at >= ? ")
        args = [service, since]
        if until is not None:
            sql += "AND opened_at <= ? "
            args.append(until)
        with self._sread:
            row = self.sc.execute(sql + "ORDER BY last_note_at DESC LIMIT 1", args).fetchone()
        return self._incident(row) if row else None

    def add_incident_note(self, incident_id: str, run_id: str, at) -> bool:
        """File a run's note under an incident and move the incident's last note up to `at`.
        False when the run is filed already."""
        with self._swrite:
            if self.sc.execute("SELECT 1 FROM incident_notes WHERE run_id = ?",
                               [run_id]).fetchone():
                return False
            self.sc.execute("INSERT INTO incident_notes (run_id, incident_id, joined_at) "
                            "VALUES (?, ?, ?)", [run_id, incident_id, at])
            self.sc.execute(
                "UPDATE incidents SET last_note_at = ?, updated_at = ? WHERE id = ? "
                "AND (last_note_at IS NULL OR last_note_at < ?)", [at, at, incident_id, at])
        return True

    def set_note_slack(self, run_id: str, channel: str | None, ts: str | None) -> None:
        with self._swrite:
            self.sc.execute("UPDATE incident_notes SET slack_channel = ?, slack_ts = ? "
                            "WHERE run_id = ?", [channel, ts, run_id])

    def set_incident_slack(self, incident_id: str, channel: str, ts: str) -> None:
        with self._swrite:
            self.sc.execute("UPDATE incidents SET slack = ? WHERE id = ?",
                            [json.dumps({"channel": channel, "ts": ts}), incident_id])

    def notes_of(self, incident_ids: list[str]) -> dict[str, list[dict]]:
        """incident id -> its notes, oldest first: {run_id, joined_at, slack_channel, slack_ts}."""
        out: dict[str, list[dict]] = {i: [] for i in incident_ids}
        if not incident_ids:
            return out
        ph = ", ".join(["?"] * len(incident_ids))
        with self._sread:
            rows = self.sc.execute(
                "SELECT incident_id, run_id, joined_at, slack_channel, slack_ts FROM "
                f"incident_notes WHERE incident_id IN ({ph}) ORDER BY joined_at, run_id",
                list(incident_ids)).fetchall()
        for iid, run_id, at, ch, ts in rows:
            out[iid].append({"run_id": run_id, "joined_at": at, "slack_channel": ch,
                             "slack_ts": ts})
        return out

    def incident_notes(self, incident_id: str) -> list[dict]:
        return self.notes_of([incident_id])[incident_id]

    def incident_for_run(self, run_id: str) -> dict | None:
        """The incident a run's note was filed under, if any."""
        with self._sread:
            row = self.sc.execute(
                "SELECT incident_id FROM incident_notes WHERE run_id = ?", [run_id]).fetchone()
        return self.get_incident(row[0]) if row else None

    def set_incident_state(self, incident_id: str, state: str, by_id: str | None,
                           by_name: str | None, via: str, *, at=None,
                           resolution: str | None = None, expect: str | None = None):
        """Move an incident to `state` and log it. on_it names who took it; resolved names who
        closed it (nobody, with resolution "itself"); open clears both. `expect` makes it a
        compare-and-set on the current state. Returns the incident as stored, or None when it
        is unknown or was not in the state `expect` asked for."""
        if state not in ("open", "on_it", "resolved"):
            raise ValueError(f"no such incident state: {state}")
        at = at or now_utc()
        with self._swrite:
            row = self.sc.execute("SELECT state FROM incidents WHERE id = ?",
                                  [incident_id]).fetchone()
            if row is None or (expect is not None and row[0] != expect):
                return None
            if state == "on_it":
                sql = ("UPDATE incidents SET state = 'on_it', owner = ?, owner_name = ?, "
                       "owner_at = ?, resolved_at = NULL, resolved_by = NULL, "
                       "resolved_by_name = NULL, resolution = NULL, updated_at = ? WHERE id = ?")
                args = [by_id, by_name, at, at, incident_id]
                action = "on_it"
            elif state == "resolved":
                sql = ("UPDATE incidents SET state = 'resolved', resolved_at = ?, "
                       "resolved_by = ?, resolved_by_name = ?, resolution = ?, updated_at = ? "
                       "WHERE id = ?")
                args = [at, by_id, by_name, resolution or ("person" if by_id else "itself"),
                        at, incident_id]
                action = "resolved_itself" if resolution == "itself" else "resolved"
            else:
                sql = ("UPDATE incidents SET state = 'open', owner = NULL, owner_name = NULL, "
                       "owner_at = NULL, resolved_at = NULL, resolved_by = NULL, "
                       "resolved_by_name = NULL, resolution = NULL, updated_at = ? WHERE id = ?")
                args = [at, incident_id]
                action = "reopened"
            self.sc.execute(sql, args)
            self._log_incident(incident_id, action, by_id, by_name, via, at)
        return self.get_incident(incident_id)

    def _log_incident(self, incident_id, action, by_id, by_name, via, at) -> None:
        """The log insert, for a caller already holding the state write lock."""
        self.sc.execute(
            "INSERT INTO incident_log (incident_id, at_time, action, by_id, by_name, via) "
            "VALUES (?, ?, ?, ?, ?, ?)", [incident_id, at or now_utc(), action, by_id, by_name,
                                          via])

    def log_incident(self, incident_id: str, action: str, by_id: str | None,
                     by_name: str | None, via: str, at=None) -> None:
        with self._swrite:
            self._log_incident(incident_id, action, by_id, by_name, via, at)

    def incident_log(self, incident_id: str) -> list[dict]:
        with self._sread:
            rows = self.sc.execute(
                "SELECT at_time, action, by_id, by_name, via FROM incident_log WHERE incident_id = ? "
                "ORDER BY at_time", [incident_id]).fetchall()
        return [{"at": r[0], "action": r[1], "by_id": r[2], "by_name": r[3], "via": r[4]}
                for r in rows]

    def last_reopened(self, incident_ids: list[str]) -> dict:
        """incident id -> when a person last reopened it (the newest `reopened` log row), for the
        ones that were."""
        if not incident_ids:
            return {}
        ph = ", ".join(["?"] * len(incident_ids))
        with self._sread:
            rows = self.sc.execute(
                f"SELECT incident_id, at_time FROM incident_log WHERE action = 'reopened' "
                f"AND incident_id IN ({ph})", list(incident_ids)).fetchall()
        out: dict = {}
        for iid, at in rows:
            if at is not None and (iid not in out or at > out[iid]):
                out[iid] = at
        return out

    def list_incidents(self, state: str | None = None, service: str | None = None,
                       limit: int = 50, before=None, offset: int = 0) -> list[dict]:
        """Newest note first. `state` is open, on_it, resolved or active (open and on_it);
        `before` keeps the incidents whose last note is older than it; `offset` pages."""
        where, args = [], []
        if state == "active":
            where.append("state <> 'resolved'")
        elif state:
            where.append("state = ?")
            args.append(state)
        if service:
            where.append("service = ?")
            args.append(service)
        if before is not None:
            where.append("last_note_at < ?")
            args.append(before)
        sql = _INCIDENT_SELECT + ("WHERE " + " AND ".join(where) + " " if where else "")
        with self._sread:
            rows = self.sc.execute(sql + "ORDER BY last_note_at DESC, id LIMIT ? OFFSET ?",
                                   args + [int(limit), max(0, int(offset))]).fetchall()
        return [self._incident(r) for r in rows]

    def incident_counts(self, service: str | None = None) -> dict:
        sql, args = "SELECT state, COUNT(*) FROM incidents", []
        if service:
            sql += " WHERE service = ?"
            args.append(service)
        with self._sread:
            rows = self.sc.execute(sql + " GROUP BY state", args).fetchall()
        got = {r[0]: int(r[1]) for r in rows}
        return {s: got.get(s, 0) for s in ("open", "on_it", "resolved")}

    def delete_backfilled_incidents(self) -> int:
        """Remove the incidents the backfill made with their notes and log rows: the ones it
        closed (resolution before_tracking) and the ones it left open that nobody has touched
        since (every log row is the backfill's). Returns how many incidents went."""
        with self._swrite:
            ids = [r[0] for r in self.sc.execute(
                "SELECT id FROM incidents WHERE resolution = 'before_tracking' "
                "OR (state = 'open' AND id NOT IN "
                "(SELECT incident_id FROM incident_log WHERE via <> 'backfill'))").fetchall()]
            for iid in ids:
                self.sc.execute("DELETE FROM incident_notes WHERE incident_id = ?", [iid])
                self.sc.execute("DELETE FROM incident_log WHERE incident_id = ?", [iid])
                self.sc.execute("DELETE FROM incidents WHERE id = ?", [iid])
        return len(ids)

    def services_carrying(self, spec: dict[str, str], label: str, value: str, since) -> list[tuple]:
        """[(service, count)] by count: which service the events with `label` = `value` since
        `since` belong to. `spec` maps a source to the label that names a service in it. Events
        live in DuckDB only: this reads self.con under self._lock and joins no state table."""
        from .store import _label_expr
        if not spec:
            return []
        sources = sorted(spec)
        case = "CASE source " + " ".join(f"WHEN ? THEN {_label_expr(spec[s])}" for s in sources) \
               + " END"
        ph = ", ".join(["?"] * len(sources))
        sql = (f"SELECT {case} AS svc, COUNT(*) AS n FROM events WHERE ingest_time >= ? "
               f"AND source IN ({ph}) AND {_label_expr(label)} = ? GROUP BY svc ORDER BY n DESC")
        with self._lock:
            rows = self.con.execute(sql, [*sources, since, *sources, value]).fetchall()
        return [(r[0], int(r[1])) for r in rows if r[0] not in (None, "")]
