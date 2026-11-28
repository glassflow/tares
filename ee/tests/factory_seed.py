# Tares enterprise licence, see ee/LICENSE
"""A licensed factory store at the size of the real factory, filled with invented content (TR-1604).

`seed(store)` fills a store that is already running (the daemon's, with a licence) until every
table holds as many rows as SHAPE says. SHAPE is the row count of each table of a real factory
cell on 2026-11-24 and nothing else: no name, id or text of that cell is in this file, and every
value written here is made up (a project is "Lantern billing", a person "Ada Quill", a command
"pytest -q tests/test_cart.py"). Ids, times and picks come from `random.Random(1124)` and a frozen
NOW, so two runs write the same rows.

What it makes, in the order it runs (each group prints its seconds):
  people and crews       people, keys, crews, grants, members, connections
  projects               projects (one through the Store API), their objects and log, hand-overs
  tickets                milestones, tickets with their history, checks and milestone checks
  the desk               desk items, decisions, assumptions, answers, spec questions
  reviews                reviews, findings, rule proposals, the merge queue
  docs                   docs and the links that put them in projects
  brain and usage        brain repos and calls, repo maps, model usage
  session lines          300 session lines through `Store.append`; the plan of the other sessions
  sessions               the sessions, their stored lines, facts, stations, states and usage, made
                         here a line at a time from the plan (`_lines`) and written in chunks

Rows go in with SQLAlchemy Core `insert()` into the session store, never one Store call per row:
only the first project, its three tickets, one review, one desk item and 300 session lines go through
the Store API, so a change to what the API writes shows here too. A session that is not complete
holds fewer stored lines than it counts, as a cell whose lines were not all kept does.

The counts hold on both backends (SQLite or Postgres). `counts(store)` reads them back; `seed`
returns the ids a test can ask a page about.
"""
import hashlib
import json
import math
import random
import sys
import time
from datetime import datetime, timedelta, timezone

NOW = datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc)
SEED = 1124

# Row counts of a real factory cell, 2026-11-24. Tables that held no rows are in "zero" and are not
# seeded (the store makes the 2 metadata tables itself). The tables of the source catalog, the query
# log, Ask, triggers and agents are left out: Tares 2.0 makes none of them (TR-1810, TR-1811), and the
# rows of the project parts table that named a source, a trigger or an agent with them. The tables
# go by their 2.0 names (TR-1816): `projects`, `project_log`, and `project_docs` for the doc rows of
# the project parts table.
SHAPE = {
    "date": "2026-11-24",
    "tables": {
        "answers": 20, "api_keys": 37, "assumptions": 323, "brain_calls": 19,
        "brain_repo_updates": 39, "brain_repos": 3,
        "challenges": 2, "crews": 9, "decisions": 360, "desk_items": 415,
        "docs": 899, "github_credentials": 2,
        "grants": 22, "handovers": 21, "integration_choices": 1, "integration_connections": 2,
        "merge_queue": 271, "milestone_check_results": 191, "milestones": 121, "model_usage": 20,
        "people": 5, "project_members": 4, "project_required_checks": 3, "project_sessions": 2270,
        "repo_map_history": 50, "repo_maps": 7,
        "review_findings": 2762, "reviews": 1602, "rule_proposals": 25, "session_commands": 94023,
        "session_files": 2603, "session_lines": 262977,
        "session_messages": 5150, "session_meters": 2050, "session_said": 6225,
        "session_states": 2123, "session_stations": 2091, "session_turns": 27918,
        "session_usage": 219361, "sessions": 2145, "settings": 30,
        "spec_questions": 3, "ticket_checks": 4052, "ticket_history": 3387, "ticket_numbers": 29,
        "tickets": 858, "project_log": 65, "project_docs": 969, "projects": 33, "waiting_told": 23,
    },
    "zero": [
        "brain_repo_folders", "finding_feedback", "guardrail_pairs", "guardrail_rules",
        "handover_overrides", "incident_log", "incident_notes", "incidents",
        "milestone_acceptances", "pending_memory", "pending_writeback", "project_integrations",
        "project_merge_method", "project_secrets", "services",
        "session_archive_parts", "shared_skills", "signoffs",
        "watch_state",
    ],
    # how the rows are cut, so the pages find the same mix of work as the real cell's
    "mix": {
        "project_status": {"done": 19, "active": 12, "later": 2},     # 31 factory, 1 challenger, 1 default
        "projects_with_tickets": 29, "projects_with_desk": 21, "projects_with_reviews": 19,
        "tickets": {"owner_linear": 794, "with_pr": 555, "with_depends_on": 554,
                    "with_assignee": 470, "with_working_doc": 717, "repos": 13},
        "ticket_history_fields": {"stage": 2656, "holder": 610, "send_back": 89, "ladder": 14,
                                  "merge_queue": 18},
        "ticket_history_person": {"stage": 1388, "holder": 435},
        "review_verdicts": {"pass": 1294, "changes": 306, "block": 2},
        "reviews_with_person": 970, "reviewed_tickets": 548, "reviews_with_findings": 832,
        "finding_states": {"P3/open": 1376, "P3/fixed": 861, "P2/fixed": 445, "P2/open": 52,
                           "P1/fixed": 28},
        "findings_blocking": 441,
        "merge_queue_states": {"merged": 268, "sent_back": 3}, "merge_queue_with_desk_item": 25,
        "desk_states": {"answered": 360, "withdrawn": 53, "open": 2}, "desk_with_person": 232,
        "decisions_scope_all": 2,
        "assumption_states": {"kept": 310, "overturned": 8, "open": 5},
        "doc_kinds": {"working": 719, "note": 48, "agents": 26, "start": 25, "plan": 25,
                      "spec": 25, "memory": 16, "grants": 12, "personal_agents": 2,
                      "personal_memory": 1},
        "doc_links_beyond_first": 73,
        "api_keys_with_person": 25,
        "stations": 120,
        "station_roles": {"builder": 547, "reviewer": 711, "releaser": 561, "orchestrator": 272},
        "station_roles_with_project": {"builder": 538, "reviewer": 637, "releaser": 507,
                                       "orchestrator": 224},
        "session_states": {"ended": 1956, "working": 87, "waiting": 7, "idle": 73},
        "projects_with_sessions": 31, "sessions_with_project": 2046,
        "session_message_types": {"VERDICT": 1254, "REVIEW": 1147, "FYI": 601, "DONE": 546,
                                  "ANSWER": 417, "TASK": 373, "READY": 297, "ASK": 239},
        "usage_models": {"claude-opus-5-5": 110684, "claude-sonnet-5-5": 108451,
                         "claude-fable-5-2": 202, "claude-haiku-5-5": 24},
        "usage_with_subagent": 24078, "commands_without_result": 10,
        "meters_with_person": 1948,
    },
    # the sessions: how many, how many seen in the last 7 days, how many have every line in the
    # sessions database (`lines_complete`) and how long they are
    "sessions": {"total": 2145, "last_7_days": 1792, "complete": 868, "complete_lines": 260024,
                 "incomplete_lines": 1153263, "partial": 14, "partial_lines": 2953,
                 "tool_calls_per_line": 0.2, "coordination_per_tool_call": 0.34},
}

ANCHOR_SESSIONS = 3          # the sessions whose lines go through Store.append
ANCHOR_LINES = 100           # lines each: 300 in all

# ── invented words ──────────────────────────────────────────────────────────────────────────────
ADJ = ["Amber", "Birch", "Cedar", "Dune", "Ember", "Fjord", "Gable", "Harbor", "Iris", "Juniper",
       "Kelp", "Lantern", "Maple", "Nimbus", "Orchard", "Pebble", "Quartz", "Reed", "Sorrel",
       "Tidal", "Umber", "Velvet", "Willow", "Yarrow", "Zephyr"]
NOUN = ["billing", "search", "checkout", "catalog", "inbox", "scheduler", "gateway", "reports",
        "onboarding", "alerts", "importer", "exports", "profiles", "payments", "labels", "sync",
        "dashboard", "uploads", "audit log", "notifications"]
VERB = ["Add", "Fix", "Speed up", "Replace", "Document", "Remove", "Split", "Cache", "Validate",
        "Retry", "Log", "Rename", "Paginate", "Migrate", "Secure"]
THING = ["the cart total", "the sign-in form", "the nightly export", "the search index",
         "the invoice list", "the retry queue", "the webhook handler", "the settings page",
         "the upload limit", "the audit trail", "the label picker", "the report filter",
         "the session timeout", "the import preview", "the status badge", "the email digest"]
QUESTIONS = ["Which date format should exports use?", "Keep the old route for one release?",
             "Should a failed import stop the whole run?", "Is a soft delete enough here?",
             "Do we show prices with tax?", "Who owns the retry limit?",
             "Can the digest go out at night?", "Which table holds the audit rows?"]
FINDINGS = ["The empty list is not handled.", "No test covers the failed call.",
            "The lock is held while the request waits.", "The error is swallowed.",
            "The docs still show the old flag.", "The query runs once per row.",
            "The token is written to the log.", "The new column has no default."]
KINDS = ["missing test", "security", "null or empty", "concurrency", "error handling",
         "docs out of date", "performance", "migration", "naming", "dead code"]
COMMANDS = ["pytest -q tests/test_cart.py", "npm run lint", "python tests/test_export.py",
            "git status --short", "npx tsc --noEmit", "ruff check src", "make build",
            "git diff --stat", "python -m pytest -x -q", "npm run build"]
PROMPTS = ["please look at the failing export test", "continue with the next ticket",
           "can you check the retry limit", "run the checks and tell me what broke",
           "what changed since the last review", "split this into two commits",
           "update the docs for the new flag", "why is the list slow"]
REPLIES = ["I read the test and the failure comes from the date format.",
           "The change is in place and the checks pass.",
           "I found the cause: the retry limit is read twice.",
           "Two files changed; I ran the suite and it is green.",
           "The review asked for a test, so I added one.",
           "That needs a decision, so I put it on the desk.",
           "The branch is rebased and the pull request is open.",
           "I looked at the log and the slow query is the list."]
OUTPUTS = ["12 passed in 3.1s", "ok", "3 files changed, 41 insertions(+), 7 deletions(-)",
           "no issues found", "build finished", "1 failed, 11 passed", "up to date"]
PEOPLE = [("Ada Quill", "ada.quill@example.com"), ("Bram Oakes", "bram.oakes@example.com"),
          ("Cleo Marsh", "cleo.marsh@example.com"), ("Dov Larkin", "dov.larkin@example.com")]
CREWS = ["harbor", "lantern", "quarry", "meadow", "kestrel", "tundra", "bramble", "saffron", "cobalt"]
PARA = ["The page loads the list once and keeps it for the whole visit.",
        "A change here must keep the old routes working for one more release.",
        "Every call has a timeout, and a timeout is logged with the request id.",
        "The checks run on the branch before a review is asked for.",
        "Nothing in this part reads the network; it works on what it is given.",
        "Names follow the module they live in, and a helper stays next to its caller.",
        "When two readers disagree the newer one wins, and the older one is logged.",
        "The import stops at the first bad row and says which row it was.",
        "Each step has an owner, a check and a day it should be done by.",
        "The list is sorted by the last change, the newest first."]
# real sizes in characters of the doc kinds, rounded
DOC_SIZE = {"working": 4100, "note": 11000, "agents": 7900, "start": 1700, "plan": 3600,
            "spec": 8200, "memory": 1500, "grants": 700, "personal_agents": 3200,
            "personal_memory": 700}


def freeze_clock() -> None:
    """Every `now_utc` the package imported answers NOW, so the words that say how long ago
    something was are the same on every run."""
    for name, mod in list(sys.modules.items()):
        if name.split(".")[0] in ("tares", "tares_ee") and getattr(mod, "now_utc", None) is not None:
            mod.now_utc = lambda: NOW


def _apportion(total: int, weights: list[float], minimum: int = 0) -> list[int]:
    """`total` split by `weights` into whole numbers (each at least `minimum`) that add up to it."""
    n = len(weights)
    if total < minimum * n:
        raise ValueError(f"{total} cannot give each of {n} at least {minimum}")
    rest, s = total - minimum * n, sum(weights)
    raw = [rest * w / s for w in weights]
    out = [minimum + int(r) for r in raw]
    order = sorted(range(n), key=lambda i: raw[i] - int(raw[i]), reverse=True)
    for i in order[:total - sum(out)]:
        out[i] += 1
    return out


class _Ids:
    """Ids that read like the store's own (`tk_` and ten hex digits), the same on every run."""

    def __init__(self):
        self.n: dict[str, int] = {}

    def __call__(self, prefix: str) -> str:
        k = self.n[prefix] = self.n.get(prefix, 0) + 1
        return f"{prefix}_{hashlib.sha1(f'{SEED}:{prefix}:{k}'.encode()).hexdigest()[:10]}"


def _uuid(rng: random.Random) -> str:
    h = f"{rng.getrandbits(128):032x}"
    return f"{h[:8]}-{h[8:12]}-4{h[13:16]}-a{h[17:20]}-{h[20:]}"


def _sha(rng: random.Random) -> str:
    return f"{rng.getrandbits(160):040x}"


def _between(rng: random.Random, lo: datetime, hi: datetime) -> datetime:
    if hi <= lo:
        return lo
    return lo + timedelta(seconds=rng.random() * (hi - lo).total_seconds())


def _words(rng: random.Random, size: int) -> str:
    out, n = [], 0
    while n < size:
        p = rng.choice(PARA)
        out.append(p)
        n += len(p) + 2
    return "\n\n".join(out)[:size]


def session_table(name: str):
    """The session store's table of that name (every table of the cell is there)."""
    from tares.session_store import tables as T
    return T.metadata.tables[name]


def _have(store, table: str) -> int:
    """Rows in `table` now."""
    from sqlalchemy import func, select
    with store.sessions_db.begin() as c:
        return c.execute(select(func.count()).select_from(session_table(table))).scalar_one()


def counts(store, shape=SHAPE) -> dict:
    """Rows now by table, for every table SHAPE names."""
    return {t: _have(store, t) for t in shape["tables"]}


def expected(shape=SHAPE) -> dict:
    return dict(shape["tables"])


class _Gen:
    def __init__(self, store, shape):
        self.store, self.shape = store, shape
        self.rng = random.Random(SEED)
        self.id = _Ids()
        self.mix = shape["mix"]
        self.out: dict = {}
        # everything later groups need from earlier ones
        self.people: list[str] = []
        self.crews: list[dict] = []
        self.stations: dict[str, list[dict]] = {}
        self.projects: list[dict] = []
        self.milestones: dict[str, list[str]] = {}
        self.tickets: list[dict] = []
        self.desk: list[dict] = []
        self.session_ids = [_uuid(self.rng) for _ in range(shape["sessions"]["total"])]
        self.sessions: list[dict] = []

    # ── small helpers ──────────────────────────────────────────────────────────────────────────
    def need(self, table: str) -> int:
        n = self.shape["tables"][table] - _have(self.store, table)
        if n < 0:
            raise RuntimeError(f"{table} already holds more rows than SHAPE says")
        return n

    def put_t(self, table: str, cols: str, rows: list, tail: str = "") -> None:
        """Rows given as lists in the order of `cols`, into the session store through Core (a JSON
        column takes the list itself and not its text). `tail` is not used: the rows are new."""
        names = [c.strip() for c in cols.split(",")]
        self.put_s(session_table(table), [dict(zip(names, r)) for r in rows])

    def put_s(self, table, rows: list, chunk: int = 20000) -> None:
        """SQLAlchemy Core insert() into a table of the sessions database."""
        if not rows:
            return
        from sqlalchemy import insert
        with self.store.sessions_db.begin() as c:
            for i in range(0, len(rows), chunk):
                c.execute(insert(table), rows[i:i + chunk])

    def stamp(self, days_ago_max: float, lo: float = 0.0) -> datetime:
        """A time up to `days_ago_max` days back, most of them recent."""
        u = self.rng.random() ** 3.5
        return NOW - timedelta(days=lo + (days_ago_max - lo) * u)

    def person(self):
        return self.rng.choice(self.people) if self.people else None

    def station_of(self, crew: int, role: str) -> str:
        c = self.crews[crew]
        if role == "builder":
            return self.rng.choice(c["builders"])
        return c[role]

    # ── people and crews ───────────────────────────────────────────────────────────────────────
    def people_and_crews(self):
        rng, st = self.rng, self.store
        from sqlalchemy import select
        from tares.session_store.tables import people as people_t
        with st.sessions_db.read() as c:
            have = [r[0] for r in c.execute(select(people_t.c.id).order_by(people_t.c.created_at)).all()]
        self.people = have[:]
        rows = []
        k = 0
        while len(self.people) < self.shape["tables"]["people"]:
            name, email = PEOPLE[k % len(PEOPLE)] if k < len(PEOPLE) else (f"Person {k}", f"p{k}@example.com")
            k += 1
            pid = self.id("per")
            self.people.append(pid)
            rows.append([pid, name, email, [email], None, k == 1,
                         NOW - timedelta(days=50 - k), "invite", None, None])
        self.put_t("people", "id, name, email, emails, github, is_admin, created_at, created_via, "
                             "picture, removed_at", rows)
        # crews, with the stations each runs
        rows = []
        for i in range(self.need("crews")):
            name = CREWS[i % len(CREWS)]
            cid = self.id("crew")
            first = NOW - timedelta(days=55 - i * 3)
            builders = []
            c = {"id": cid, "name": name, "person": self.people[i % len(self.people)],
                 "orchestrator": f"{name}-orchestrator", "reviewer": f"{name}-reviewer",
                 "releaser": f"{name}-releaser", "builders": builders}
            self.crews.append(c)
            rows.append([cid, name, c["person"], f"{name}-desk.local", first, NOW - timedelta(minutes=5 + i)])
        # the 120 stations the sessions run under: 3 per crew and the rest builders
        stations_total = self.mix["stations"]
        nb = stations_total - 3 * len(self.crews)
        for j in range(nb):
            c = self.crews[j % len(self.crews)]
            c["builders"].append(f"{c['name']}-build-m{len(c['builders']) + 1}")
        self.put_t("crews", "id, name, person_id, machine, first_seen, last_seen", rows)
        # keys
        rows = []
        for i in range(self.need("api_keys")):
            with_person = i < self.mix["api_keys_with_person"]
            rows.append([self.id("key"), f"laptop-{i:02d}", f"tk_{rng.getrandbits(24):06x}",
                         hashlib.sha256(f"seed-key-{i}".encode()).hexdigest(),
                         ["admin"] if i % 7 == 0 else ["read", "write"],
                         self.stamp(60), self.stamp(10) if i % 5 else None,
                         self.stamp(20) if i % 9 == 0 else None, None,
                         self.people[i % len(self.people)] if with_person else None])
        self.put_t("api_keys", "id, name, prefix, hash, scopes, created_at, last_used_at, revoked_at, "
                               "project, person_id", rows)
        # settings: harmless keys of their own, and one left for `default_project` (projects_group)
        rows = [[f"seed.note_{i:02d}", str(i), NOW - timedelta(days=i)]
                for i in range(self.need("settings") - 1)]
        self.put_s(session_table("settings"), [dict(zip(("key", "value", "updated_at"), r)) for r in rows])

    # ── projects: one through the Store API, the rest as rows ─────────────────────────────────
    def projects_group(self):
        rng, st = self.rng, self.store
        total = self.shape["tables"]["projects"]
        status = []
        for s, n in self.mix["project_status"].items():
            status += [s] * n
        # 29 factory projects with tickets (19 done, 10 active), 2 later without, then the 2 others
        tp = self.mix["projects_with_tickets"]
        done_n = self.mix["project_status"]["done"]
        order = ["done"] * done_n + ["active"] * (tp - done_n)
        rng.shuffle(order)
        order.remove("active")
        order.insert(0, "active")                                  # project 0 is the one the API makes
        order += ["later"] * self.mix["project_status"]["later"]
        order += ["active"] * (total - len(order) - 1) + ["active"]
        weights = [1 / (i + 1) ** 0.9 for i in range(tp)]
        for i in range(total):
            adj, noun = ADJ[i % len(ADJ)], NOUN[(i * 7 + i // len(ADJ)) % len(NOUN)]
            kind = "software_factory" if i < total - 2 else ("challenger_workflow" if i == total - 2 else None)
            created = NOW - timedelta(days=rng.uniform(40, 59) if order[i] == "done" else rng.uniform(1, 30))
            if i == 0:
                created = NOW - timedelta(days=45)
            updated = (min(created + timedelta(days=rng.uniform(3, 20)), NOW - timedelta(days=1))
                       if order[i] == "done" else NOW - timedelta(minutes=rng.uniform(3, 4000)))
            self.projects.append({
                "i": i, "uid": self.id("uc"), "name": f"{adj} {noun}", "status": order[i], "kind": kind,
                "created": created, "updated": updated, "weight": weights[i] if i < tp else 0,
                "repo": f"example-org/{ADJ[i % 13].lower()}-{NOUN[i % 13].replace(' ', '-')}",
                "lead": self.people[i % len(self.people)] if i < 21 else None,
                "crew": i % len(self.crews)})
        # project 0 through the Store API (its tickets, review and desk item follow below)
        p0 = self.projects[0]
        st.create_project(p0["uid"], p0["name"], goal=f"Make {p0['name']} fast and clear.")
        st.set_project_kind(p0["uid"], "software_factory")
        rows = []
        for p in self.projects[1:]:
            rows.append([p["uid"], p["name"], p["status"], p["created"], p["updated"],
                         f"Make {p['name']} fast and clear.", None, None, p["kind"],
                         p["lead"], "team", ["\U0001f9ed", "\U0001f9e0", "\U0001f3ce️", "\U0001f41e"][p["i"] % 4] if p["i"] in (31, 32, 30, 29) else None])
        self.put_t("projects", "id, name, status, created_at, updated_at, goal, linear, github_issues, kind, "
                               "lead_person_id, scope, icon", rows)
        # the anchor's own row keeps the dates the others have
        from sqlalchemy import update
        from tares.session_store.tables import projects as projects_t
        with st.sessions_db.begin() as c:
            c.execute(update(projects_t).where(projects_t.c.id == p0["uid"]).values(
                created_at=p0["created"], updated_at=p0["updated"], lead_person_id=p0["lead"]))
            # the project with no kind: the default one
            c.execute(update(projects_t).where(projects_t.c.id == self.projects[total - 1]["uid"]).values(
                kind=None))
        st.set_setting("default_project", self.projects[total - 1]["uid"])

    # ── tickets ────────────────────────────────────────────────────────────────────────────────
    def tickets_group(self):
        from tares.session_store.pr_columns import pr_columns
        rng, st, mix = self.rng, self.store, self.mix
        tp = mix["projects_with_tickets"]
        projects = self.projects[:tp]
        total = self.shape["tables"]["tickets"]
        sizes = _apportion(total, [p["weight"] for p in projects], minimum=4)
        ms_sizes = _apportion(self.shape["tables"]["milestones"] - 0,
                              [math.sqrt(n) for n in sizes], minimum=1)
        # the anchor project's tickets go through the API; the rest are rows
        p0 = projects[0]
        api_ms = st.create_milestone(p0["uid"], "M1: first pages", "The pages are live.",
                                     [{"check": "open the home page", "expect": "it loads"}])
        self.milestones[p0["uid"]] = [api_ms]
        api_tickets = []
        for j in range(3):
            tid = st.create_ticket(p0["uid"], "tares", f"{VERB[j]} {THING[j]}", milestone=api_ms, repo=p0["repo"])
            api_tickets.append(tid)
        st.update_ticket(api_tickets[0], stage="merged", status="done")
        st.update_ticket(api_tickets[1], stage="review", status="in_progress", holder=f"{CREWS[0]}-build-m1",
                         pr={"url": f"https://github.com/{p0['repo']}/pull/1", "repo": p0["repo"], "number": 1,
                             "head": "a" * 40, "state": "open", "merged": False, "ci": "success"})
        for k, (field, value) in enumerate((("stage", "doing"), ("stage", "review"))):
            st.add_ticket_history(api_tickets[1], p0["uid"], field, value, None, f"{CREWS[0]}-build-m1",
                                  "builder", NOW - timedelta(days=2, hours=-k))
        self.out["anchor_ticket"] = api_tickets[1]
        self.out["anchor_project"] = p0["uid"]
        self.out["crew_id"], self.out["station"] = self.crews[0]["id"], f"{CREWS[0]}-build-m1"
        self.api_tickets = api_tickets
        # milestones
        mrows, mpos = [], {}
        for p, n in zip(projects, ms_sizes):
            lst = self.milestones.setdefault(p["uid"], [])
            for k in range(len(lst), n):
                mid = self.id("ms")
                lst.append(mid)
                t = p["created"] + (p["updated"] - p["created"]) * (k / max(n, 1))
                checks = [{"check": f"open {THING[(k + c) % len(THING)]}", "expect": "it loads and shows the rows"}
                          for c in range(rng.randint(2, 4))]
                mrows.append([mid, p["uid"], "tares", None, f"M{k + 1}: {VERB[k % len(VERB)]} {THING[(k * 3) % len(THING)]}",
                              f"{VERB[k % len(VERB)]} {THING[(k * 3) % len(THING)]} and ship it.",
                              checks, float(k + 1), t, t])
        self.put_t("milestones", "id, project, owner, external_id, name, goal, checks, position, created_at, "
                               "updated_at", mrows)
        # ticket rows
        pool = {"done": [("done", "merged", 465), ("done", None, 177), ("done", "shipped", 94), ("canceled", None, 32)],
                "open": [("todo", None, 82), ("todo", "todo", 2), ("in_progress", "doing", 6),
                         ("in_progress", "review", 6), ("in_progress", "changes", 4),
                         ("in_progress", "blocked", 2), ("canceled", None, 3)]}
        for kind, items in pool.items():
            pool[kind] = ([(s, g) for s, g, _ in items], [w for *_, w in items])
        tickets, number = [], {p["uid"]: 3 if p is p0 else 0 for p in projects}
        for p, n in zip(projects, sizes):
            first = 3 if p is p0 else 0
            ms = self.milestones[p["uid"]]
            for j in range(first, n):
                f = j / n
                if p["status"] == "done" or f < 0.72:
                    s, g = rng.choices(*pool["done"])[0]
                else:
                    s, g = rng.choices(*pool["open"])[0]
                number[p["uid"]] += 1
                created = p["created"] + (p["updated"] - p["created"]) * f
                updated = min(created + timedelta(hours=rng.uniform(1, 90)), p["updated"] if p["status"] == "done" else NOW)
                tickets.append({"id": self.id("tk"), "project": p["uid"], "p": p, "no": number[p["uid"]],
                                "status": s, "stage": g, "created": created, "updated": max(created, updated),
                                "ms": ms[min(len(ms) - 1, j * len(ms) // n)], "pos": float(j + 1),
                                "repo": p["repo"] if rng.random() < 0.85 else f"example-org/shared-{rng.randint(1, 3)}",
                                "pr": None, "deps": None, "assignee": None, "doc": None, "owner": "tares",
                                "holder": None})
        t_mix = mix["tickets"]
        # the PR goes to the tickets that would have one, then the rest
        rank = {"merged": 0, "review": 0, "changes": 0, "doing": 0, "blocked": 1, "shipped": 1, None: 2, "todo": 3}
        order = sorted(range(len(tickets)), key=lambda i: (rank[tickets[i]["stage"]], rng.random()))
        repo_pr: dict[str, int] = {}
        for i in order[:t_mix["with_pr"] - 1]:                      # one is the API's
            t = tickets[i]
            repo_pr[t["repo"]] = repo_pr.get(t["repo"], 1) + 1
            done = t["stage"] in ("merged", "shipped") or t["status"] == "done"
            t["pr"] = {"url": f"https://github.com/{t['repo']}/pull/{repo_pr[t['repo']]}", "repo": t["repo"],
                       "number": repo_pr[t["repo"]], "head": _sha(rng), "state": "closed" if done else "open",
                       "merged": done, "ci": "failure" if t["stage"] == "changes" else "success"}
        for t in tickets:
            if t["stage"] in ("doing", "review", "changes"):
                t["holder"] = self.station_of(t["p"]["crew"], "builder")
        for t in rng.sample(tickets, t_mix["owner_linear"]):
            t["owner"] = "linear"
        for k, t in enumerate(rng.sample(tickets, t_mix["with_assignee"])):
            t["assignee"] = self.people[k % len(self.people)]
        by_project: dict[str, list] = {}
        for t in tickets:
            by_project.setdefault(t["project"], []).append(t)
        later = [t for t in tickets if t["no"] > 1]
        for t in rng.sample(later, min(t_mix["with_depends_on"], len(later))):
            earlier = [x for x in by_project[t["project"]] if x["no"] < t["no"]]
            t["deps"] = [x["id"] for x in rng.sample(earlier, min(len(earlier), rng.choice((1, 1, 2))))]
        for t in rng.sample(tickets, t_mix["with_working_doc"]):
            t["doc"] = self.id("doc")
        rows, lin = [], 1000
        for t in tickets:
            ext = ident = url = None
            if t["owner"] == "linear":
                lin += 1
                ext, ident, url = f"lin-{lin:05d}", f"TR-{lin}", f"https://linear.example.com/issue/TR-{lin}"
            rows.append([t["id"], t["project"], t["owner"], ext, ident, url,
                         f"{rng.choice(VERB)} {rng.choice(THING)}", t["status"], t["pos"], t["doc"],
                         t["created"], t["updated"], t["ms"], t["deps"] or None,
                         t["no"], t["holder"], t["stage"], "waiting on a decision" if t["stage"] == "blocked" else None,
                         t["pr"] or None, t["repo"], t["assignee"], None, *pr_columns(t["pr"])])
        if len(rows) + 3 != total:
            raise RuntimeError(f"{len(rows) + 3} tickets planned, {total} in SHAPE")
        self.put_t("tickets", "id, project, owner, external_id, identifier, url, title, status, position, "
                              "working_doc, created_at, updated_at, milestone, depends_on, number, holder, stage, "
                              "stage_reason, pr, repo, assignee_person_id, assignee_raw, pr_repo, "
                              "pr_number", rows)
        self.tickets = tickets
        self.ticket_projects = projects
        # the project's own times move with its tickets, as they do on the real cell
        # numbers: the next number of each project
        self.put_t("ticket_numbers", "project, last", [[uid, n] for uid, n in number.items() if uid != p0["uid"]])
        # history
        fields = [f for f, n in mix["ticket_history_fields"].items() for _ in range(n)]
        have_h = _have(st, "ticket_history")
        fields = fields[have_h:] if False else fields
        rng.shuffle(fields)
        want = self.shape["tables"]["ticket_history"] - have_h
        fields = fields[:want]
        w = [3 if t["status"] == "done" else 1 for t in tickets]
        counts_ = _apportion(len(fields), w)
        persons = dict(mix["ticket_history_person"])
        stage_vals = ["doing", "review", "changes", "merged", "shipped", "blocked"]
        rows, fi = [], 0
        for t, n in zip(tickets, counts_):
            times = sorted(_between(rng, t["created"], t["updated"]) for _ in range(n))
            holder = t["holder"] or self.station_of(t["p"]["crew"], "builder")
            for ts in times:
                f = fields[fi]
                fi += 1
                if f == "stage":
                    v = rng.choice(stage_vals)
                elif f == "holder":
                    v = holder
                elif f == "send_back":
                    v = "changes"
                elif f == "ladder":
                    v = "L3"
                else:
                    v = rng.choice(("queued", "merged"))
                pid = None
                if persons.get(f, 0) > 0:
                    persons[f] -= 1
                    pid = self.person()
                rows.append([t["id"], t["project"], ts, f, v,
                             "the review asked for changes" if f == "send_back" else None, holder, "builder", pid])
        self.put_t("ticket_history", "ticket, project, changed_at, field, value, reason, by_station, by_role, "
                                     "person_id", rows)
        # checks
        n = self.need("ticket_checks")
        with_pr = [t for t in tickets if t["pr"]]
        counts_ = _apportion(n, [1 + (t["stage"] == "merged") for t in with_pr])
        rows = []
        for t, c in zip(with_pr, counts_):
            for _ in range(c):
                bad = rng.random() < 0.08
                rows.append([self.id("ck"), t["project"], t["id"], rng.choice(COMMANDS),
                             "1 failed, 11 passed" if bad else rng.choice(OUTPUTS), _sha(rng),
                             "tests/test_cart.py::test_total" if bad else None,
                             self.station_of(t["p"]["crew"], "builder"), self.rng.choice(self.session_ids),
                             _between(rng, t["created"], t["updated"]), self.person()])
        self.put_t("ticket_checks", "id, project, ticket, command, result, commit_sha, broke_test, by_station, "
                                  "by_session, checked_at, person_id", rows)
        # milestone checks
        ms_all = [(p, m) for p in projects for m in self.milestones[p["uid"]]]
        n = self.need("milestone_check_results")
        rows = []
        for k in range(n):
            p, m = ms_all[k % len(ms_all)]
            crew = rng.random() < 0.7
            rows.append([self.id("mc"), p["uid"], m, k // len(ms_all), "pass" if rng.random() < 0.92 else "fail",
                         None, "crew" if crew else "person",
                         self.crews[p["crew"]]["reviewer"] if crew else "Ada Quill", None if crew else self.person(),
                         _between(rng, p["created"], p["updated"])])
        self.put_t("milestone_check_results", "id, project, milestone, idx, result, note, by_kind, by_name, "
                                            "person_id, checked_at", rows)

    # ── the desk ───────────────────────────────────────────────────────────────────────────────
    def desk_group(self):
        rng, st, mix = self.rng, self.store, self.mix
        projects = self.ticket_projects
        dp = projects[:mix["projects_with_desk"]]
        p0 = projects[0]
        # one desk item through the API
        item = st.add_desk_item(p0["uid"], "Which date format should exports use?", "The export writes dates.",
                                ["ISO", "local"], "ISO", "One format everywhere.", "blocks", [self.api_tickets[1]],
                                True, f"{CREWS[0]}-orchestrator", [], NOW - timedelta(days=1),
                                station=f"{CREWS[0]}-orchestrator")
        n = self.need("desk_items")
        states = [s for s, c in mix["desk_states"].items() for _ in range(c)]
        have_states = _have(st, "desk_items")
        states.remove("open")                                       # the API's item is the first open one
        rng.shuffle(states)
        states = states[:n]
        sizes = _apportion(n, [p["weight"] for p in dp])
        by_project: dict[str, list] = {}
        for t in self.tickets:
            by_project.setdefault(t["project"], []).append(t)
        rows, k = [], 0
        number = _have(st, "desk_items")
        person_left = mix["desk_with_person"]
        for p, c in zip(dp, sizes):
            tix = by_project[p["uid"]]
            for j in range(c):
                state = states[k]
                k += 1
                created = _between(rng, p["created"], p["updated"])
                closed = None if state == "open" else min(NOW, created + timedelta(hours=rng.uniform(0.2, 30)))
                did = self.id("dk")
                pid = None
                if person_left > 0 and state != "withdrawn":
                    person_left -= 1
                    pid = self.person()
                row = {"id": did, "project": p["uid"], "state": state, "created": created, "closed": closed,
                       "person": pid, "tickets": [t["id"] for t in rng.sample(tix, min(2, len(tix)))]}
                self.desk.append(row)
                number += 1
                rows.append([did, number, p["uid"], rng.choice(QUESTIONS), "Raised while building " + rng.choice(THING) + ".",
                             ["yes", "no"], "yes", "The simpler choice.", "blocks",
                             row["tickets"], state == "open", self.station_of(p["crew"], "builder"),
                             [], state, "no longer needed" if state == "withdrawn" else None, created, closed,
                             self.station_of(p["crew"], "builder"), None, pid, None])
        self.put_t("desk_items", "id, number, project, question, context, options, recommendation, why, blocks, "
                               "tickets, blocking, asked_by, assumptions, state, reason, created_at, closed_at, "
                               "station, command, person_id, to_person_id", rows)
        # decisions: one per answered item
        answered = [d for d in self.desk if d["state"] == "answered"]
        n = self.need("decisions")
        rows = []
        for k in range(n):
            d = answered[k % len(answered)]
            scope_all = k < mix["decisions_scope_all"]
            rows.append([self.id("dc"), None if scope_all else d["project"], d["id"], rng.choice(QUESTIONS),
                         "Yes, keep it simple.", "yes", scope_all, "all" if scope_all else "project", False,
                         rng.choice(self.session_ids), False, d["tickets"], d["closed"],
                         "session" if rng.random() < 0.55 else "console", d["person"]])
        self.put_t("decisions", "id, project, desk_item, question, words, choice, standing, scope, matched, session, "
                              "granted, tickets, decided_at, via, person_id", rows)
        # assumptions
        n = self.need("assumptions")
        states = [s for s, c in mix["assumption_states"].items() for _ in range(c)]
        rng.shuffle(states)
        sizes = _apportion(n, [p["weight"] for p in projects])
        rows, k = [], 0
        for p, c in zip(projects, sizes):
            tix = by_project[p["uid"]]
            for j in range(c):
                s = states[k % len(states)]
                k += 1
                made = _between(rng, p["created"], p["updated"])
                rows.append([self.id("as"), p["uid"], j + 1, rng.choice(QUESTIONS), "Keep the current way.",
                             "It is the smallest change.", rng.choice(THING), [rng.choice(tix)["id"]],
                             s, None, f"batch-{j // 5}", self.station_of(p["crew"], "builder"), "builder", made,
                             None if s == "open" else made + timedelta(hours=3), self.person()])
        self.put_t("assumptions", "id, project, number, question, choice, why, affects, tickets, state, words, "
                                "batch, made_by, made_role, made_at, decided_at, person_id", rows)
        # answers, spec questions, waiting
        n = self.need("answers")
        kinds = ["question"] * 12 + ["calls_to_confirm"] * 5 + ["signoff"] * 3
        rows = []
        for k in range(n):
            d = answered[k]
            rows.append([self.id("an"), d["id"], kinds[k % len(kinds)], d["project"], rng.choice(QUESTIONS), "yes",
                         None, "session" if k < 11 else "console", d["person"], d["closed"]])
        self.put_t("answers", "id, item, kind, project, ask, choice, said, via, person_id, answered_at", rows)
        rows = [[p["uid"], "Q1", self.person(), p["repo"], "Which repo holds the shared client?",
                 self.crews[p["crew"]]["orchestrator"], NOW - timedelta(days=1 + i), None, None, None, "open", "session"]
                for i, p in enumerate(projects[:self.need("spec_questions")])]
        self.put_t("spec_questions", "project, id, to_person_id, repo, text, asked_by, asked_at, answer, answered_by, "
                                   "answered_at, state, via", rows)
        n = self.need("waiting_told")
        rows = [[self.people[k % len(self.people)], self.desk[k]["id"], NOW - timedelta(hours=k * 3), True, None,
                 None, None, None, None] for k in range(n)]
        self.put_t("waiting_told", "person_id, item, told_at, sent, ask, channel, ts, seq, closed", rows)

    # ── reviews ────────────────────────────────────────────────────────────────────────────────
    def reviews_group(self):
        rng, st, mix = self.rng, self.store, self.mix
        tickets = self.tickets
        p0 = self.projects[0]
        # one review with a finding through the API
        st.add_review(p0["uid"], self.api_tickets[1], "1", "a" * 40, "changes", "Ran the checks.", "Did not run the app.",
                      f"{CREWS[0]}-reviewer",
                      [{"severity": "P2", "file": "src/cart.py", "line": 30, "blocking": True, "text": FINDINGS[1]}],
                      {}, lambda x: "missing test", NOW - timedelta(days=1, hours=2))
        n = self.need("reviews")
        eligible = [t for t in tickets if t["pr"]]
        rp = mix["projects_with_reviews"]
        rev_projects = {p["uid"] for p in self.ticket_projects[:rp]}
        eligible = [t for t in eligible if t["project"] in rev_projects] or eligible
        picked = rng.sample(eligible, min(len(eligible), mix["reviewed_tickets"] - 1))
        sizes = _apportion(n, [1 + (t["stage"] in ("merged", "review", "changes")) * 2 for t in picked], minimum=1)
        verdicts = [v for v, c in mix["review_verdicts"].items() for _ in range(c)]
        verdicts.remove("changes")                                  # the API's review
        rng.shuffle(verdicts)
        verdicts = verdicts[:n]
        rows, reviews, k, person_left = [], [], 0, mix["reviews_with_person"]
        for t, c in zip(picked, sizes):
            times = sorted(_between(rng, t["created"], t["updated"]) for _ in range(c))
            for r, ts in enumerate(times):
                v = verdicts[k]
                k += 1
                rid = self.id("rv")
                pid = None
                if person_left > 0:
                    person_left -= 1
                    pid = self.person()
                reviews.append({"id": rid, "ticket": t["id"], "project": t["project"], "verdict": v, "at": ts, "p": t["p"]})
                rows.append([rid, t["project"], t["id"], r + 1, str(t["pr"]["number"]), _sha(rng), v,
                             "Ran the checks and read the diff.", "Did not run the app.",
                             self.crews[t["p"]["crew"]]["reviewer"], ts, pid])
        self.put_t("reviews", "id, project, ticket, round, pr, head, verdict, verified, not_verified, by_station, "
                            "reviewed_at, person_id", rows)
        # findings: on every review that asked for changes, and on some that passed
        n = self.need("review_findings")
        with_f = [r for r in reviews if r["verdict"] != "pass"]
        rest = [r for r in reviews if r["verdict"] == "pass"]
        with_f += rng.sample(rest, max(0, min(len(rest), mix["reviews_with_findings"] - 1 - len(with_f))))
        sizes = _apportion(n, [1] * len(with_f), minimum=1)
        states = [s for s, c in mix["finding_states"].items() for _ in range(c)]
        have = _have(st, "review_findings")
        states.remove("P2/open")                                    # the API's finding
        rng.shuffle(states)
        blocking = mix["findings_blocking"] - 1
        rows, k = [], 0
        for r, c in zip(with_f, sizes):
            for j in range(c):
                sev, state = states[k].split("/")
                k += 1
                b = blocking > 0 and sev != "P3"
                blocking -= 1 if b else 0
                if blocking > 0 and k % 9 == 0:
                    b, blocking = True, blocking - 1
                rows.append([self.id("rf"), r["project"], r["ticket"], r["id"], j + 1, sev,
                             f"src/{rng.choice(NOUN).replace(' ', '_')}.py", rng.randint(3, 400), b, rng.choice(FINDINGS),
                             rng.choice(KINDS), state, _sha(rng)[:12] if state == "fixed" else None, r["at"]])
        self.put_t("review_findings", "id, project, ticket, review, number, severity, file, line, blocking, text, kind, "
                                    "state, fixed_in, found_at", rows)
        # rule proposals
        n = self.need("rule_proposals")
        rrepos = sorted({t["repo"] for t in tickets})
        rows = [[self.id("rp"), rrepos[k % len(rrepos)].lower(), KINDS[(k // len(rrepos)) % len(KINDS)], None, "repo",
                 f"Every {KINDS[k % len(KINDS)]} finding gets a test first.", [],
                 ["open", "accepted", "rejected"][k % 3], NOW - timedelta(days=k),
                 None if k % 3 == 0 else NOW - timedelta(days=k - 1)] for k in range(n)]
        self.put_t("rule_proposals", "id, repo, kind, project, scope, text, tickets, state, created_at, decided_at", rows)
        # the merge queue: one row per merged ticket
        n = self.need("merge_queue")
        merged = [t for t in tickets if t["pr"] and t["pr"]["merged"]]
        queue = rng.sample(merged, n)
        queue.sort(key=lambda t: t["updated"])
        states = [s for s, c in mix["merge_queue_states"].items() for _ in range(c)]
        rng.shuffle(states)
        desk_ids = [d["id"] for d in self.desk]
        rows = []
        for k, t in enumerate(queue):
            rows.append([self.id("mq"), t["repo"], "main", t["pr"]["number"], t["pr"]["head"], t["id"], t["project"],
                         states[k % len(states)], None,
                         rng.choice(desk_ids) if k < mix["merge_queue_with_desk_item"] else None,
                         t["pr"]["head"], t["updated"] - timedelta(minutes=20), t["updated"], None, 0])
        self.put_t("merge_queue", "id, repo, base, pr, head, ticket, project_uid, state, reason, desk_item, "
                                "approved_head, entered_at, updated_at, before_files, unknown_ticks", rows)
        # a ticket page a test can ask for: the project's biggest, with reviews
        have_r = {r["ticket"] for r in reviews}
        pick = next((t for t in tickets if t["id"] in have_r), tickets[0])
        self.out["big_project"], self.out["big_ticket"] = pick["project"], pick["id"]

    # ── docs ───────────────────────────────────────────────────────────────────────────────────
    def docs_group(self):
        rng, st, mix = self.rng, self.store, self.mix
        projects = self.ticket_projects
        rows, links = [], []
        kinds = dict(mix["doc_kinds"])
        by_project = {}
        for t in self.tickets:
            by_project.setdefault(t["project"], []).append(t)

        def doc(kind, project, title, owner=None, did=None, at=None):
            did = did or self.id("doc")
            at = at or _between(rng, project["created"], project["updated"]) if project else NOW - timedelta(days=3)
            rows.append([did, kind, title, _words(rng, DOC_SIZE[kind]), project["uid"] if project else None,
                         self.station_of(project["crew"], "builder") if project else None, at,
                         min(NOW, at + timedelta(hours=rng.uniform(0, 80))), owner])
            if project and not owner:
                links.append((project["uid"], did, at))
            return did
        for t in self.tickets:
            if t["doc"]:
                doc("working", t["p"], f"Working doc for {t['p']['name']} ticket {t['no']}", did=t["doc"], at=t["created"])
                kinds["working"] -= 1
        for k in range(kinds.pop("working")):                        # working docs no ticket points to
            doc("working", rng.choice(projects), f"Spare working doc {k + 1}")
        for kind in ("spec", "plan", "start", "agents"):
            for i in range(kinds.pop(kind)):
                doc(kind, projects[i % len(projects)], f"{kind.title()} of {projects[i % len(projects)]['name']}")
        for kind in ("note", "memory", "grants"):
            for i in range(kinds.pop(kind)):
                doc(kind, projects[(i * 5) % len(projects)], f"{kind.title()} {i + 1}")
        for kind in ("personal_agents", "personal_memory"):
            for i in range(kinds.pop(kind)):
                doc(kind, None, f"Your {kind.replace('_', ' ')}", owner=self.people[i % len(self.people)])
        if len(rows) != self.shape["tables"]["docs"] - _have(st, "docs"):
            raise RuntimeError(f"{len(rows)} docs planned, SHAPE has {self.shape['tables']['docs']}")
        self.put_t("docs", "id, kind, title, body, made_by, updated_by, created_at, updated_at, owner", rows)
        # links: each doc in its project, then the extras in a second one
        want = self.shape["tables"]["project_docs"]
        orows = [[uid, did, at] for uid, did, at in links]
        extra = mix["doc_links_beyond_first"]
        for k in range(extra):
            uid, did, at = links[(k * 11) % len(links)]
            other = projects[(next(i for i, p in enumerate(projects) if p["uid"] == uid) + 1 + k) % len(projects)]
            orows.append([other["uid"], did, at])
        seen, uniq = set(), []
        for r in orows:
            if (r[0], r[1]) not in seen:
                seen.add((r[0], r[1]))
                uniq.append(r)
        # fill what duplicates removed with links to one more project
        k = 0
        while len(uniq) < want:
            uid, did, at = links[(k * 7 + 3) % len(links)]
            other = projects[(k * 3 + 2) % len(projects)]
            key = (other["uid"], did)
            k += 1
            if key not in seen and other["uid"] != uid:
                seen.add(key)
                uniq.append([other["uid"], did, at])
        self.put_t("project_docs", "project, doc, created_at", uniq[:want])
        # the project log: two rows each, and one automatic
        n = self.need("project_log")
        rows = []
        for k, p in enumerate(self.projects):
            rows.append([p["uid"], p["created"], "create", None])
            rows.append([p["uid"], p["created"] + timedelta(seconds=1), "created", None])
        rows.append([self.projects[0]["uid"], NOW - timedelta(days=2), "auto", None])
        self.put_t("project_log", "project, logged_at, action, detail", rows[:n])
        # hand-overs, grants, members, checks, wiring
        n = self.need("handovers")
        rows = []
        for p in self.ticket_projects[:n]:
            at = p["created"] + timedelta(hours=2)
            done = p["status"] == "done"
            c = self.crews[p["crew"]]
            rows.append([p["uid"], f"/work/{p['repo'].split('/')[1]}", "Ada Quill", at,
                         [{"path": f"/work/{p['repo'].split('/')[1]}", "name": p["repo"]}], "L3",
                         p["updated"] if done else None, "All tickets merged." if done else None,
                         "done" if done else None, "crew" if done else None, None, None, None, None, p["lead"],
                         None, None, c["name"], None, None, None])
        self.put_t("handovers", "project, repo, handed_by, handed_at, repos, autonomy, finished_at, finished_words, "
                              "finished_decision, finished_by, taken_at, taken_words, taken_by, released_at, "
                              "person_id, finished_person_id, taken_person_id, crew, override_reason, override_at, "
                              "override_person_id", rows)
        n = self.need("grants")
        top = _have(st, "grants")
        rows = [[f"G{k + 1 + top}", "project" if k < n - 1 else "personal", self.ticket_projects[k]["uid"] if k < n - 1 else None,
                 self.people[k % len(self.people)], "May push the branches of this project.", "Ada Quill", NOW - timedelta(days=k),
                 [], [], "active", False] for k in range(n)]
        self.put_t("grants", "id, kind, project, holder_person_id, words, given_by, given_at, areas, consents, state, "
                           "needs_lead", rows)
        n = self.need("project_members")
        self.put_t("project_members", "project, person_id, added_by, added_at, reason",
                 [[self.ticket_projects[k]["uid"], self.people[(k + 1) % len(self.people)], "Ada Quill",
                   NOW - timedelta(days=k + 2), "joined"] for k in range(n)])
        n = self.need("project_required_checks")
        self.put_t("project_required_checks", "project, commands, updated_at, updated_by",
                 [[self.ticket_projects[k]["uid"], ["npm run lint", "python tests/test_cart.py"],
                   NOW - timedelta(days=k + 1), "Ada Quill"] for k in range(n)])
    # ── brain, usage and the small tables ──────────────────────────────────────────────────────
    def small_group(self):
        rng, st = self.rng, self.store
        pr = self.ticket_projects
        repos = sorted({t["repo"] for t in self.tickets})
        self.put_t("github_credentials", "name, kind, token, api_url, account, created_at, updated_at, config",
                 [[f"seed-github-{k}", "pat", "not-a-token", "https://api.github.com", "example-org",
                   NOW - timedelta(days=30), NOW - timedelta(days=30), "{}"] for k in range(self.need("github_credentials"))])
        self.put_t("integration_choices", "slot, name, settings, updated_at, updated_by",
                 [[f"seed_slot_{k}", "seed", {}, NOW - timedelta(days=20), "Ada Quill"] for k in range(self.need("integration_choices"))])
        self.put_t("integration_connections", "slot, name, settings, updated_at, updated_by",
                 [["seed_slot_0", f"seed-{k}", {}, NOW - timedelta(days=20), "Ada Quill"] for k in range(self.need("integration_connections"))])
        self.put_t("challenges", "source, key_value, event_time, event_type, branch, tares_project, payload",
                 [["github", f"example-org/x#{k}", NOW - timedelta(days=k + 1), "challenge", "main", pr[0]["uid"], "{}"]
                  for k in range(self.need("challenges"))])
        # the brain
        n = self.need("brain_repos")
        self.put_t("brain_repos", "repo, repo_source_id, state, files_done, files_total, indexed_commit, last_synced, "
                                  "error, added_by, added_at, place, job_started_at, sync_failures, sync_error",
                 [[repos[k % len(repos)], None, "ready", 420 + k, 420 + k, _sha(rng), NOW - timedelta(hours=k + 1), None,
                   "Ada Quill", NOW - timedelta(days=30), k, None, 0, None] for k in range(n)])
        rows = [[self.id("bu"), repos[k % 3], NOW - timedelta(hours=k * 5), "update", 1, _sha(rng), rng.randint(1, 30), 0,
                 400 + k, None] for k in range(self.need("brain_repo_updates"))]
        self.put_t("brain_repo_updates", 'id, repo, "at", kind, ok, indexed_commit, files_changed, files_deleted, '
                                         "files_total, error", rows)
        rows = [[self.id("bc"), NOW - timedelta(hours=k * 7), "ask_brain", k % 2, rng.choice(QUESTIONS), repos[k % 3],
                 rng.randint(800, 9000), rng.randint(1000, 8000), rng.randint(100, 900), 1, None,
                 self.station_of(k % len(self.crews), "builder"), self.person(), "session"]
                for k in range(self.need("brain_calls"))]
        self.put_t("brain_calls", 'id, "at", tool, deep, question, repos, duration_ms, input_tokens, output_tokens, ok, '
                                  "error, station, person_id, via", rows)
        rows = [[repos[k % len(repos)], _words(rng, 1500), NOW - timedelta(days=k), "Ada Quill"]
                for k in range(self.need("repo_maps"))]
        self.put_t("repo_maps", "repo, body, updated_at, updated_by", rows)
        rows = [[repos[k % 7 % len(repos)], NOW - timedelta(hours=k * 11), self.station_of(k % len(self.crews), "builder"),
                 pr[k % len(pr)]["uid"], "Updated after the merge."] for k in range(self.need("repo_map_history"))]
        self.put_t("repo_map_history", "repo, changed_at, by_station, project, note", rows)
        rows = [[self.id("mu"), NOW - timedelta(days=k), "ask", None, None, "claude-sonnet-5-5", 1,
                 rng.randint(1000, 9000), rng.randint(100, 900), 0, 0, round(rng.random() * 0.2, 4), "env", "anthropic"]
                for k in range(self.need("model_usage"))]
        self.put_t("model_usage", "id, ts, surface, agent, run_id, model, calls, input_tokens, output_tokens, "
                                  "cache_creation_input_tokens, cache_read_input_tokens, cost_usd, key_source, provider",
                   rows)

    # ── session lines ──────────────────────────────────────────────────────────────────────────
    def lines_group(self):
        """300 lines through Store.append, then the plan of the other sessions."""
        from tares.envelope import Envelope
        rng, st = self.rng, self.store
        p0, crew0 = self.projects[0], self.crews[0]
        tools, uses = [], 0
        envs = []
        for s in range(ANCHOR_SESSIONS):
            sid = self.session_ids[s]
            t0 = NOW - timedelta(hours=3 + s)
            st.set_session_station(sid, f"{CREWS[0]}-build-m1", "builder", f"{CREWS[0]}-orchestrator", p0["uid"], t0,
                                   crew0["id"])
            for i in range(ANCHOR_LINES):
                at = t0 + timedelta(seconds=i * 20)
                kind = _TYPES[i % 10]
                key = f"{sid}:{i}"
                envs.append(Envelope("claude_code", "claude_code", sid, kind, _text(kind, i), at,
                                     json.loads(_payload(kind, sid, i, key)), {"tares_project": p0["uid"]}, at))
        st.append(envs)
        from sqlalchemy import update
        from tares.session_store import tables as T
        with st.sessions_db.begin() as c:        # these lines are the session's whole story
            c.execute(update(T.sessions).where(T.sessions.c.session.in_(self.session_ids[:ANCHOR_SESSIONS]))
                      .values(lines_complete=True))
        self.plan_sessions()
        by_session_project = self.assign_projects()
        for s in self.sessions:
            s["project"] = by_session_project.get(s["id"])
            s["person"] = self.people[int(s["id"][:4], 16) % len(self.people)]

    def plan_sessions(self):
        """Who the 2142 other sessions are, how long, when, and how many of their lines are stored."""
        rng, mix, sh = self.rng, self.mix, self.shape["sessions"]
        total = sh["total"] - ANCHOR_SESSIONS
        recent = sh["last_7_days"] - ANCHOR_SESSIONS
        complete = sh["complete"] - ANCHOR_SESSIONS
        lines_c = sh["complete_lines"] - ANCHOR_SESSIONS * ANCHOR_LINES
        incomplete = total - complete
        # sizes: heavy tailed, summing to the real totals
        wc = [rng.lognormvariate(0, 1.0) for _ in range(complete)]
        wi = [rng.lognormvariate(0, 1.2) for _ in range(incomplete)]
        size_c = _apportion(lines_c, wc, minimum=3)
        size_i = _apportion(sh["incomplete_lines"], wi, minimum=3)
        # which are recent: all the complete ones, and enough of the others
        kinds = ["c"] * complete + ["i"] * incomplete
        sess = []
        ids = self.session_ids[ANCHOR_SESSIONS:]
        recent_i = recent - complete
        flags = [True] * recent_i + [False] * (incomplete - recent_i)
        rng.shuffle(flags)
        for k in range(total):
            c = k < complete
            n = size_c[k] if c else size_i[k - complete]
            is_recent = True if c else flags[k - complete]
            if is_recent:
                last = NOW - timedelta(days=7 * rng.random() ** 2, seconds=30)
            else:
                last = NOW - timedelta(days=rng.uniform(7.2, 59.5))
            dur = min(max(30.0, n * rng.uniform(4, 25)), 3 * 86400.0)
            first = last - timedelta(seconds=dur)
            sess.append({"id": ids[k], "n": n, "complete": c, "recent": is_recent, "t0": first, "last": last,
                         "step": max(1000, int(dur * 1e6 / n)), "stored": n if c else 0})
        # a few have only the first lines stored
        part = [s for s in sess if not s["complete"] and s["recent"] and s["n"] >= 1000]
        part = rng.sample(part, sh["partial"])
        for s, st_ in zip(part, _apportion(sh["partial_lines"], [1.0] * len(part), minimum=1)):
            s["stored"] = min(st_, s["n"] - 1)
        # the busiest go through the states: the newest ones are not over
        sess.sort(key=lambda s: s["last"])
        self.sessions = sess

    def assign_projects(self) -> dict:
        """The session -> project link the project_sessions rows and the lines' labels share."""
        rng, mix = self.rng, self.mix
        projects = [p for p in self.projects if p["kind"] == "software_factory"][:mix["projects_with_sessions"]]
        w = [p["weight"] or 0.05 for p in projects]
        n = mix["sessions_with_project"] - ANCHOR_SESSIONS
        ids = [s["id"] for s in self.sessions]
        chosen = rng.sample(ids, n)
        picks = rng.choices(range(len(projects)), weights=w, k=n)
        self.session_project = {sid: projects[i] for sid, i in zip(chosen, picks)}
        self.session_project_list = projects
        return {sid: p["uid"] for sid, p in self.session_project.items()}

    # ── sessions ───────────────────────────────────────────────────────────────────────────────
    def sessions_group(self):
        from sqlalchemy import update
        from tares.session_store import tables as T
        rng, st, mix, sh = self.rng, self.store, self.mix, self.shape["sessions"]
        # sessions
        rows = []
        for s in self.sessions:
            n = s["n"]
            tc = 2 * (n // 10) + sum(1 for r in range(n % 10) if r in (2, 5))
            rows.append({"session": s["id"], "source": "claude_code", "started_at": s["t0"], "last_line_at": s["last"],
                         "line_count": n, "tool_calls": tc, "coordination_calls": int(tc * sh["coordination_per_tool_call"]),
                         "lines_complete": s["complete"], "updated_at": s["last"]})
        self.put_s(T.sessions, rows)
        # the stored lines
        flags = json.dumps({"include_thinking": False, "redact": True})
        chunk = []
        for ln in self._lines():
            if ln["i"] >= ln["stored"]:
                continue
            chunk.append({"session": ln["session"], "seq": ln["i"] + 1, "line_key": ln["key"], "at": ln["ts"],
                          "type": ln["type"], "labels": "{}", "render": flags,
                          "payload": _payload(ln["type"], ln["session"], ln["i"], ln["key"])})
            if len(chunk) >= 50000:
                self.put_s(T.session_lines, chunk)
                chunk = []
        self.put_s(T.session_lines, chunk)
        # facts: from the tool_use, user and assistant lines, a different pick for each table
        n = self.need("session_commands")
        r = [(x["key"], x["session"], x["ts"], x["cmd"], x["i"])
             for x in self._pick("tool_use", "c", n)]
        none = mix["commands_without_result"]
        rows = []
        for k, (key, sid, ts, cmd, i) in enumerate(r):
            res = k >= none
            rows.append({"tool_use_id": "tu_" + key, "source": "claude_code", "session": sid, "at": ts, "command": cmd,
                         "exit_code": (0 if i % 13 else 1) if res else None, "output_tail": OUTPUTS[i % len(OUTPUTS)] if res else None,
                         "result_at": ts + timedelta(seconds=2) if res else None})
        self.put_s(T.session_commands, rows)
        n = self.need("session_turns")
        r = [(x["session"], x["key"], x["ts"], x["person"], x["text"]) for x in self._pick("user", "t", n)]
        self.put_s(T.session_turns, [{"session": a, "line_key": b, "source": "claude_code", "at": c_, "person": d, "text": e}
                                     for a, b, c_, d, e in r])
        n = self.need("session_said")
        r = [(x["session"], x["key"], x["ts"], x["text"]) for x in self._pick("assistant", "s", n)]
        self.put_s(T.session_said, [{"session": a, "line_key": b, "source": "claude_code", "at": c_, "text": d}
                                    for a, b, c_, d in r])
        n = self.need("session_messages")
        types = [t for t, c in mix["session_message_types"].items() for _ in range(c)]
        types += ["FYI"] * (n - len(types)) if n > len(types) else []
        rng.shuffle(types)
        r = [(x["key"], x["session"], x["ts"]) for x in self._pick("tool_use", "m", n)]
        stations = [x for c in self.crews for x in (c["orchestrator"], c["reviewer"], c["releaser"])]
        self.put_s(T.session_messages, [
            {"tool_use_id": "tm_" + key, "source": "claude_code", "session": sid, "at": ts,
             "to_station": stations[k % len(stations)], "type": types[k % len(types)],
             "text": f"[TF:{types[k % len(types)]}] {rng.choice(REPLIES)}"} for k, (key, sid, ts) in enumerate(r)])
        n = self.need("session_files")
        touched: dict = {}
        for x in self._lines():
            if x["type"] == "tool_use":
                k = (x["session"], f"src/mod_{x['i'] % 40}.py")
                touched[k] = max(touched.get(k, x["ts"]), x["ts"])
        r = [(sid, path, touched[(sid, path)]) for sid, path in
             sorted(touched, key=lambda k: _order(k[0] + k[1]))[:n]]
        self.put_s(T.session_files, [{"session": a, "path": b, "source": "claude_code", "last_at": c_} for a, b, c_ in r])
        # usage: one row per assistant reply
        n = self.need("session_usage")
        r = [(x["session"], x["key"], x["ts"], x["project"], x["i"]) for x in self._pick("assistant", "u", n)]
        models = [m for m, c in mix["usage_models"].items() for _ in range(1)]
        mw = list(mix["usage_models"].values())
        sub = mix["usage_with_subagent"] / max(n, 1)
        rows = []
        for sid, key, ts, proj, i in r:
            m = rng.choices(models, weights=mw)[0]
            inp, out = rng.randint(2, 900), rng.randint(20, 3000)
            cw, cr = rng.randint(0, 40000), rng.randint(0, 180000)
            rate = 15.0 if "opus" in m else 3.0
            rows.append({"session": sid, "message": "msg_" + hashlib.md5(key.encode()).hexdigest()[:20], "replied_at": ts,
                         "model": m, "project": proj, "subagent": f"agent-{i:04x}" if rng.random() < sub else None,
                         "input_tokens": inp, "output_tokens": out, "cache_write_tokens": cw, "cache_read_tokens": cr,
                         "cost_usd": round((inp * rate + out * rate * 5 + cw * rate * 1.25 + cr * rate * 0.1) / 1e6, 6),
                         "cache_write_1h_tokens": cw // 3})
        self.put_s(T.session_usage, rows)
        # who runs which session
        self.stations_and_states()
        # the hourly usage totals a cell builds in the background after its first start (TR-1632)
        from tares import claude_usage_hourly
        claude_usage_hourly.fill(st)

    def stations_and_states(self):
        from tares.session_store import tables as T
        rng, mix = self.rng, self.mix
        sess = self.sessions
        # stations
        from tares.session_store import tables as T  # noqa: F811
        want = self.need("session_stations")
        roles = {r: n for r, n in mix["station_roles"].items()}
        with_proj = dict(mix["station_roles_with_project"])
        have = self.store.sessions_db
        from sqlalchemy import func, select
        with have.begin() as c:
            for role, cnt, cntp in c.execute(select(T.session_stations.c.role, func.count(), func.count(T.session_stations.c.project))
                                       .group_by(T.session_stations.c.role)).all():
                roles[role] -= cnt
                with_proj[role] -= cntp
        plan = [r for r, n in roles.items() for _ in range(max(n, 0))]
        rng.shuffle(plan)
        plan = plan[:want]
        picks = rng.sample(sess, min(len(sess), len(plan)))
        left = {r: max(with_proj[r], 0) for r in with_proj}
        rows = []
        for s, role in zip(picks, plan):
            p = self.session_project.get(s["id"]) or rng.choice(self.session_project_list)
            c = self.crews[p["crew"]]
            station = rng.choice(c["builders"]) if role == "builder" else c[role]
            if c is self.crews[0] and station == self.out["station"]:
                # the anchor station's sessions are the three whose lines went through Store.append:
                # a planned session whose lines are not stored would be its current one
                station = next(x for x in c["builders"] if x != station)
            proj = None
            if left[role] > 0:
                left[role] -= 1
                proj = p["uid"]
            rows.append({"session": s["id"], "station": station, "role": role,
                         "parent": c["orchestrator"] if role == "builder" and proj else None, "project": proj,
                         "seen_at": s["last"], "crew_id": c["id"]})
        self.put_s(T.session_stations, rows)
        # states: the newest sessions are the ones still going
        n = self.need("session_states")
        pool = sorted(sess, key=lambda s: s["last"], reverse=True)
        states = dict(mix["session_states"])
        live = [s for st_, c in states.items() if st_ != "ended" for s in [st_] * c]
        rows = []
        for k, s in enumerate(pool[:n]):
            st_ = live[k] if k < len(live) else "ended"
            at = NOW - timedelta(minutes=2 + k * 3) if k < len(live) else s["last"]
            rows.append({"session": s["id"], "state": st_,
                         "reason": None if st_ == "working" else ("session ended" if st_ == "ended" else "waiting on a reply"),
                         "state_at": at})
            if k < len(live):
                s["last"] = at
        self.put_s(T.session_states, rows)
        # meters
        n = self.need("session_meters")
        status = json.dumps({"model": {"id": "model-x", "display_name": "Model X"}, "context_window": {"size": 1000000,
                                                                                                         "used_percentage": 9}})
        rows = [{"session": s["id"], "status": status, "status_at": s["last"], "context": None, "context_at": None,
                 "context_asked_at": None, "status_person": s["person"] if k < mix["meters_with_person"] else None}
                for k, s in enumerate(rng.sample(sess, n))]
        self.put_s(T.session_meters, rows)
        # project_sessions: each project's sessions, and the sessions that serve two projects
        n = self.need("project_sessions")
        rows, seen = [], set()
        for sid, p in self.session_project.items():
            rows.append({"project": p["uid"], "session": sid, "repo": p["repo"], "linked_at": next(s["last"] for s in [self._by_id()[sid]])})
            seen.add((p["uid"], sid))
        k = 0
        pl = self.session_project_list
        ids = list(self.session_project)
        while len(rows) < n:
            sid = ids[(k * 13 + 5) % len(ids)]
            p = pl[(k * 7 + 3) % len(pl)]
            k += 1
            if (p["uid"], sid) not in seen:
                seen.add((p["uid"], sid))
                rows.append({"project": p["uid"], "session": sid, "repo": p["repo"], "linked_at": self._by_id()[sid]["last"]})
        self.put_s(T.project_sessions, rows[:n])

    def _lines(self):
        """Every line of the planned sessions, the stored ones and the others, a dict at a time, in
        session and line order: what the rows of the session tables are made from."""
        for s in self.sessions:
            sid, t0, step = s["id"], s["t0"], s["step"]
            for i in range(s["n"]):
                kind = _TYPES[i % 10]
                yield {"session": sid, "i": i, "ts": t0 + timedelta(microseconds=i * step), "type": kind,
                       "stored": s["stored"], "project": s["project"], "person": s["person"],
                       "key": f"{sid}:{i}", "cmd": COMMANDS[(i // 3) % len(COMMANDS)], "text": _text(kind, i)}

    def _pick(self, kind: str, salt: str, n: int) -> list:
        """`n` lines of `kind`, the same ones on every run: those whose key hashes lowest."""
        import heapq
        return [x for _, x in heapq.nsmallest(n, ((_order(x["key"] + salt), x) for x in self._lines()
                                                  if x["type"] == kind), key=lambda p: p[0])]

    def _by_id(self):
        if not hasattr(self, "_idx"):
            self._idx = {s["id"]: s for s in self.sessions}
        return self._idx



# the claude_code lines of a planned session: ten kinds in a fixed order
_TYPES = ["user", "assistant", "tool_use", "tool_result", "system", "tool_use", "assistant", "tool_result", "assistant", "system"]


def _order(text: str) -> int:
    """A number that orders lines the same way on every run (the seed's stand-in for a random pick)."""
    return int.from_bytes(hashlib.md5(text.encode()).digest()[:8], "big")


def _text(kind: str, i: int) -> str:
    if kind == "user":
        return PROMPTS[i % len(PROMPTS)]
    if kind == "assistant":
        return REPLIES[i % len(REPLIES)]
    if kind == "tool_use":
        return f"Bash: {COMMANDS[(i // 3) % len(COMMANDS)]}"
    if kind == "tool_result":
        return OUTPUTS[i % len(OUTPUTS)]
    return "session note"


def _payload(kind: str, sid: str, i: int, key: str) -> str:
    """The JSON text of a line."""
    head = f'{{"uuid": "{key}", "type": "{kind}", "sessionId": "{sid}", '
    if kind == "user":
        return head + f'"message": {{"role": "user", "content": "{_text(kind, i)}"}}}}'
    if kind == "assistant":
        return head + (f'"message": {{"id": "msg_{key}", "role": "assistant", "content": '
                       f'[{{"type": "text", "text": "{_text(kind, i)}"}}]}}}}')
    if kind == "tool_use":
        return head + (f'"message": {{"content": [{{"type": "tool_use", "id": "tu_{key}", "name": "Bash", '
                       f'"input": {{"command": "{COMMANDS[(i // 3) % len(COMMANDS)]}"}}}}]}}}}')
    if kind == "tool_result":
        back = i - 1 if i % 10 == 3 else i - 2
        return head + (f'"message": {{"content": [{{"type": "tool_result", "tool_use_id": "tu_{sid}:{back}", '
                       f'"content": "{_text(kind, i)}"}}]}}}}')
    return head + '"subtype": "note"}'


def seed(store, shape=SHAPE, log=print) -> dict:
    """Fill `store` until every table holds what `shape` says. Returns the ids a test can ask pages
    about (`anchor_project`, `anchor_ticket`, `big_project`, `big_ticket`) and the seconds per group."""
    g = _Gen(store, shape)
    seconds = {}
    t_all = time.time()
    for name, fn in (("people and crews", g.people_and_crews), ("projects", g.projects_group),
                     ("tickets", g.tickets_group), ("the desk", g.desk_group), ("reviews", g.reviews_group),
                     ("docs and links", g.docs_group), ("brain and usage", g.small_group),
                     ("session lines", g.lines_group), ("sessions", g.sessions_group)):
        t = time.time()
        fn()
        seconds[name] = time.time() - t
        log(f"  seeded {name:<18} {seconds[name]:7.1f}s")
    seconds["total"] = time.time() - t_all
    log(f"  seeded in total        {seconds['total']:7.1f}s")
    return {**g.out, "seconds": seconds}
