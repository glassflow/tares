"""Seed a fresh cell with eight weeks of invented factory history (TR-1674), for showing the
factory Home and the Report to someone.

    .venv/bin/python scripts/seed_report_demo.py --data-dir <dir>
    TARES_PR_SYNC_SECONDS=off .venv/bin/tares up --data-dir <dir>

Five sample projects: milestones, tickets with their stage history, reviews with findings, desk
questions and answers, decisions, merges at L3 and L4, and session usage with its cost. History in
the past cannot come through the API, so the rows are written through the store's own functions
with times relative to now. Where a store function stamps its own time, the row's time is set
afterwards with an UPDATE, as the ui/tests seeds do. The weeks are ISO weeks in UTC and the last
one is the current, partial week. Every repository, path and machine name is made up.

Everything is owned by the cell's one built-in person: a second person would cut Home to what is
each person's own, and adding one needs a licence.

Run it with the daemon stopped. A second run changes nothing; a run that stopped half way is
written again from the start. A data dir with other projects is refused without --into-existing.
On a cell with no other projects it also picks the factory edition and the crew's choices where
none are stored, and hides the first-run card.
Home says the sample crew is running for an hour after the seed. To show it as running again,
stop the daemon and run the seed with --refresh-crew, which adds no rows.
The pull requests do not exist on GitHub. Each is marked as a sample, so a page shows what the
seed wrote and does not read GitHub. Start the daemon with TARES_PR_SYNC_SECONDS=off, which
keeps the merge queue off them too."""
from __future__ import annotations

import argparse
import json
import os
import random
import shlex
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import delete, select, update  # noqa: E402

from tares import factory, factory_waiting, merge_queue, start_steps  # noqa: E402
from tares.envelope import now_utc  # noqa: E402
from tares.session_store.tables import (answers, metadata, project_sessions,  # noqa: E402
                                        session_states, session_stations, session_usage)
from tares.store import Store, StoreUnavailable  # noqa: E402

SEED = 1674
WEEKS = 8
MARKER = "report_demo_seed"          # the settings key that says how far a run got
SESSION_PREFIX = "demo-report-"

ME = "local"                         # the person every cell starts with
ORCHESTRATOR, REVIEWER, RELEASER = "crew-orchestrator", "crew-reviewer", "crew-releaser"
MODELS = {"orchestrator": "claude-opus-5-5", "builder": "claude-opus-5-5",
          "reviewer": "claude-sonnet-5-5", "releaser": "claude-haiku-4-5",
          "person": "claude-sonnet-5-5"}
REPLY_SIZE = {"orchestrator": 0.5, "builder": 1.0, "reviewer": 0.6, "releaser": 0.3,
              "person": 0.4}

# `accepted`: the week (0 is the oldest, 7 the current one) each milestone but the last was
# accepted in. Together they cover every week.
PROJECTS = [
    {"uid": "uc_demo_notes", "slug": "pocket-notes", "name": "Pocket Notes",
     "goal": "People write, find and share short notes on the web", "autonomy": "L4-ship",
     "per_week": 4, "accepted": (0, 2, 4, 6),
     "milestones": ("Write and save", "Find things", "Share a note", "Take it offline",
                    "Shared notebooks"),
     "things": ("note search", "tag filtering", "the editor toolbar", "share links",
                "notebook export", "sign-in", "autosave", "keyboard shortcuts",
                "the trash folder", "note history", "image uploads", "the reading view")},
    {"uid": "uc_demo_billing", "slug": "tally-billing", "name": "Tally Billing",
     "goal": "Invoices go out on the first of the month and match what was used",
     "autonomy": "L3-review", "per_week": 3, "accepted": (1, 4),
     "milestones": ("Meter usage", "Send invoices", "Handle refunds"),
     "things": ("usage metering", "invoice totals", "tax lines", "the invoice email",
                "credit notes", "proration", "the payment retry", "plan changes",
                "currency rounding", "the monthly run", "receipt downloads", "dunning notices")},
    {"uid": "uc_demo_mobile", "slug": "field-mobile", "name": "Field Mobile",
     "goal": "Technicians see today's visits and close them without a signal",
     "autonomy": "L4-ship", "per_week": 4, "accepted": (1, 3, 5),
     "milestones": ("See today's visits", "Close a visit", "Work offline", "Photos and notes"),
     "things": ("the visit list", "offline sync", "the signature pad", "photo capture",
                "the route map", "push reminders", "visit notes", "the parts picker",
                "login with a code", "time tracking", "the sync queue", "dark mode")},
    {"uid": "uc_demo_office", "slug": "back-office", "name": "Back Office",
     "goal": "Staff look up an account and fix it without asking an engineer",
     "autonomy": "L3-review", "per_week": 3, "accepted": (0, 3, 6),
     "milestones": ("Find an account", "Fix an account", "Audit trail", "Bulk changes"),
     "things": ("account search", "the account page", "role editing", "the audit log",
                "password resets", "bulk email changes", "the support notes", "export to CSV",
                "the activity feed", "access requests", "account merging", "saved filters")},
    {"uid": "uc_demo_import", "slug": "catalog-import", "name": "Catalog Import",
     "goal": "A supplier's price list loads overnight and bad rows are listed for a person",
     "autonomy": "L4-ship", "per_week": 3, "accepted": (2, 5, 7),
     "milestones": ("Read a price list", "Report bad rows", "Run overnight",
                    "More supplier formats"),
     "things": ("the file reader", "column mapping", "duplicate detection", "the error report",
                "the nightly schedule", "price checks", "unit conversion", "the upload page",
                "supplier settings", "run history", "partial reruns", "the summary email")},
]
BILLING, MOBILE, OFFICE = "uc_demo_billing", "uc_demo_mobile", "uc_demo_office"

TITLES = ("Add {}", "Fix errors in {}", "Make {} faster", "Cover {} with tests", "Simplify {}")

# Architecture findings in the mobile client, week by week: the project that drifts.
MOBILE_ARCHITECTURE = (1, 1, 2, 2, 3, 4, 5, 6)

ARCHITECTURE = (
    ("app/screens.py", "the screen reads the database directly and skips the service layer"),
    ("app/handlers.py", "this puts domain rules inside the request handler, they belong in the domain module"),
    ("app/sync.py", "two modules now import each other, this code should sit behind one interface"),
    ("app/cache.py", "the cache duplicates the repository layer instead of using it"),
    ("app/views.py", "a view helper now owns a business rule that the API also needs"),
    ("app/store.py", "this reaches across a module boundary into private state"),
)
# worded so that factory.finding_kind puts each of these, and of ARCHITECTURE, in the bucket it is
# listed under
FINDINGS = {
    "migration": (("db/migrations/004_change.sql", "the migration adds a column without a default, so older rows read back wrong"),
                  ("db/migrations/007_change.sql", "this schema change locks the table while it rewrites every row")),
    "missing test": (("tests/test_flows.py", "no test covers the retry path added here"),
                     ("tests/test_accounts.py", "the new branch for a closed account is untested")),
    "error handling": (("app/save.py", "the failed write is swallowed and the page still says saved"),
                       ("app/jobs.py", "a timeout from the mail service is unhandled and ends the job")),
    "config key": (("deploy/settings.yaml", "the new environment variable is read here but never set at deploy"),
                   ("config.sample.yaml", "the config key has a different name in the sample file")),
    "null or empty": (("app/render.py", "an empty list reaches the template and the screen renders blank"),
                      ("app/sort.py", "a null due date crashes the sort")),
    "concurrency": (("app/numbers.py", "a race condition lets two requests take the same number"),
                    ("app/jobs.py", "the lock order here can deadlock with the nightly job")),
    "security": (("app/search.py", "the search term goes straight into the query, which allows SQL injection"),
                 ("app/export.py", "the export link skips authorization, anyone with the address can read it")),
    "docs out of date": (("README.md", "the README still describes the old command"),
                         ("app/money.py", "the comment says the value is in cents, the code stores whole units")),
}
KIND_WEIGHTS = (("missing test", 24), ("error handling", 18), ("null or empty", 16),
                ("docs out of date", 12), ("config key", 9), ("migration", 8),
                ("concurrency", 6), ("security", 4), ("architecture", 3))

# (question, options, recommendation, why): a station stops until a person answers
BLOCKING = (
    ("The tests need a second sandbox account. May the crew create one?", ("Yes, create it", "No, share the first"), "Yes, create it", "Sharing one account makes the tests fail at random."),
    ("This change drops a column that may still hold data. Go ahead?", ("Keep the column", "Drop it"), "Keep the column", "Dropping it cannot be undone."),
    ("Two screens round totals differently. Which one is right?", ("Round per line", "Round the total"), "Round per line", "It matches what people already see."),
    ("A library this needs has another licence than the rest. Use it?", ("Use it", "Write our own"), "Write our own", "Nobody has agreed to that licence yet."),
    ("Should deleted items be recoverable, and for how long?", ("30 days", "Not at all"), "30 days", "People delete things by mistake."),
    ("The nightly job overlaps with the backup. Move the job to 03:00?", ("Move it", "Leave it"), "Move it", "Both slow down when they run together."),
    ("An old script still reads the former export format. Keep it one more release?", ("Keep it", "Remove it"), "Keep it", "The script has no replacement yet."),
    ("The limit for uploads is not written down. What should it be?", ("10 MB", "50 MB"), "10 MB", "It covers every file seen so far."),
    ("The staging data is a week old. Refresh it before the release check?", ("Refresh it", "Use it as it is"), "Refresh it", "Old data would hide the bug being fixed."),
    ("A required check fails for a reason outside this change. Wait or fix it here?", ("Fix it here", "Wait"), "Fix it here", "Waiting holds every other ticket too."),
    ("Should a failed run be retried on its own?", ("Retry twice", "Ask a person"), "Retry twice", "Most failures pass on the second try."),
    ("The spec does not say who may see this page. Who should?", ("Staff only", "Every signed-in user"), "Staff only", "It is easier to open up later than to close."),
)
SMALL = (
    ("Which date format should the new screen show?", ("12 Mar 2026", "2026-03-12"), "12 Mar 2026"),
    ("How many items should one page list?", ("25", "100"), "25"),
    ("Should the empty screen offer an example?", ("Yes", "No"), "Yes"),
    ("Keep the old button label for now?", ("Keep it", "Rename it"), "Keep it"),
    ("Log this event, or only the failures?", ("Only failures", "Every event"), "Only failures"),
    ("Sort newest first or by name?", ("Newest first", "By name"), "Newest first"),
)
ASSUMPTIONS = (
    ("How long should a sign-in last?", "14 days", "the spec asks for it to be convenient"),
    ("Which time zone do the daily totals use?", "UTC", "every stored time is already UTC"),
    ("What happens to a draft nobody saved?", "keep it for a week", "losing work is worse than storing it"),
    ("May two records share a name?", "yes, the id tells them apart", "nothing in the spec forbids it"),
    ("Which language are the emails in?", "English", "no other language is mentioned"),
    ("How large can one request be?", "5 MB", "the largest sample is under 2 MB"),
)
VERIFIED = (
    "Ran make test: every test passed. Broke the new check, watched its test fail, then restored it.",
    "Ran make test and make lint on this commit. Both passed.",
    "Ran the new tests alone and then the whole suite. Walked through the change by hand.",
)


def rng_for(*parts) -> random.Random:
    """One generator per thing written, so dropping one (a short current week) moves no other."""
    return random.Random(":".join(str(p) for p in (SEED, *parts)))


class Seeder:
    def __init__(self, store: Store, now):
        self.store, self.now = store, now
        monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        self.starts = [monday - timedelta(weeks=WEEKS - 1 - w) for w in range(WEEKS)]
        self.sessions: dict[str, dict] = {}
        self.replies = 0
        self.name = ME             # how the console names the person, read in run()
        self.crew_id = None

    # ── time ──────────────────────────────────────────────────────────────────
    def window(self, w: int):
        """The part of week `w` that has happened."""
        start = self.starts[w]
        end = min(start + timedelta(weeks=1), self.now - timedelta(minutes=1))
        if end <= start:          # the week began less than a minute ago
            end = start + (self.now - start) / 2
        return start, end

    def week_of(self, at) -> int:
        return max(0, min(WEEKS - 1, (at - self.starts[0]).days // 7))

    def stamp(self, table: str, rid: str, **times) -> None:
        """Set the times of a row whose store function stamps its own."""
        t = metadata.tables[table]
        with self.store.sessions_db.begin() as conn:
            conn.execute(update(t).where(t.c.id == rid).values(**times))

    # ── sessions and usage ────────────────────────────────────────────────────
    def session(self, role: str, p: dict | None, at, w: int | None = None) -> str:
        """The session of a station that was at work at `at`: one per station and week."""
        w = self.week_of(at) if w is None else w
        station = {"orchestrator": ORCHESTRATOR, "reviewer": REVIEWER, "releaser": RELEASER}.get(
            role) or (f"{p['slug']}-build" if role == "builder" else f"{p['slug']}-person")
        sid = f"{SESSION_PREFIX}{station}-w{w}"
        s = self.sessions.setdefault(sid, {"station": station, "role": role, "week": w,
                                           "project": p, "last": at})
        s["last"] = max(s["last"], at)
        return sid

    def reply(self, r: random.Random, role: str, p: dict, at, scale: float = 1.0,
              stamped: bool = True) -> None:
        """One model reply. The cost follows from its tokens, priced as the console prices it."""
        w = self.week_of(at)
        k = REPLY_SIZE[role] * scale
        if p["uid"] == BILLING:
            k *= 4 + 0.6 * w          # the project that spends far more than the others
        written = int(r.randint(10_000, 60_000) * k)
        usage = {"input_tokens": int(r.randint(2_000, 12_000) * k),
                 "output_tokens": int(r.randint(3_000, 14_000) * k),
                 "cache_creation_input_tokens": written,
                 "cache_read_input_tokens": int(r.randint(200_000, 900_000) * k),
                 "cache_creation": {"ephemeral_1h_input_tokens": 0}}
        self.replies += 1
        self.store.record_reply_usage(self.session(role, p, at), f"msg_demo_{self.replies:06d}", at,
                                      MODELS[role], p["uid"] if stamped else None, None, usage)

    def write_stations(self) -> None:
        # this week's stations are idle and heard from now: Home then says the crew is running for
        # an hour. A working station would read "not heard from" after ten silent minutes.
        recent = self.now
        last_week = WEEKS - 1
        for role in ("orchestrator", "reviewer", "releaser"):
            self.session(role, None, recent, last_week)
        for p in PROJECTS:
            self.session("builder", p, recent, last_week)
        self.crew_id = crew_seen(self.store, recent)   # so the crew was last seen now
        for sid, s in sorted(self.sessions.items()):
            p, role = s["project"], s["role"]
            current = s["week"] == last_week and role != "person"
            at = recent if current else s["last"]
            if role == "person":
                self.store.link_session(p["uid"], sid, f"example-org/{p['slug']}")
                with self.store.sessions_db.begin() as conn:
                    conn.execute(update(project_sessions).where(project_sessions.c.session == sid)
                                 .values(linked_at=at))
                continue
            self.store.set_session_station(
                sid, s["station"], role, ORCHESTRATOR if role == "builder" else "",
                p["uid"] if role == "builder" else None, at, self.crew_id)
            self.store.set_session_state(sid, "idle" if current else "ended", None, at)

    # ── the desk ──────────────────────────────────────────────────────────────
    def ask(self, p: dict, labels: list[str], at, question: str, options, recommendation: str,
            why: str, blocking: bool, by: str) -> dict:
        return self.store.add_desk_item(
            p["uid"], question, f"{by} asked while working on {', '.join(labels)}.", list(options),
            recommendation, why, f"{by} on {', '.join(labels)}", labels, blocking, by, [], at,
            station=by, to_person_id=ME)

    def answer(self, p: dict, item: dict, choice: str, at, standing: bool = False) -> None:
        self.store.add_decision(p["uid"], item["id"], item["question"], choice, choice, standing,
                                "project", False, None, False, item["tickets"], at,
                                via="console", person_id=ME)
        self.store.close_desk_item(item["id"], "answered", None, at)
        self.store.add_answer(f"question:{item['label']}", "question", p["uid"],
                              factory_waiting.ask_question(item["question"]), None, choice,
                              "console", ME, at)

    # ── projects ──────────────────────────────────────────────────────────────
    def project(self, p: dict) -> list[str]:
        s, uid, begin = self.store, p["uid"], self.starts[0]
        s.create_project(uid, p["name"], goal=p["goal"], lead_person_id=ME)
        s.set_project_kind(uid, "software_factory")
        self.stamp("projects", uid, created_at=begin, updated_at=self.now)
        # named as the tickets name it, or the project page lists a second, empty repository
        s.set_handover(uid, [{"path": f"/home/demo/{p['slug']}",
                              "name": f"example-org/{p['slug']}"}],
                       p["autonomy"], "spec session", begin, person_id=ME)
        ids = []
        for n, name in enumerate(p["milestones"]):
            checks = [{"check": f"run the end to end test for {name.lower()}",
                       "expect": "every step passes"},
                      {"check": f"try {name.lower()} on the staging site",
                       "expect": "it works without help", "for": "person"}]
            mid = s.create_milestone(uid, name, f"{name}, usable by the people it is for", checks,
                                     position=float(n + 1))
            self.stamp("milestones", mid, created_at=begin, updated_at=begin)
            ids.append(mid)
        return ids

    def accept(self, p: dict, milestones: list[str]) -> None:
        s, uid = self.store, p["uid"]
        for n, (mid, w) in enumerate(zip(milestones, p["accepted"])):
            start, end = self.window(w)
            at = start + (end - start) * 0.97
            step = (end - start) * 0.005
            s.record_milestone_check(uid, mid, 0, "pass", "every step passed", "crew", RELEASER,
                                     None, at - 3 * step)
            if p["uid"] == BILLING:     # a person found something first, then it was fixed
                s.record_milestone_check(uid, mid, 1, "fail", "the totals did not add up", "person",
                                         self.name, ME, at - 2 * step)
            s.record_milestone_check(uid, mid, 1, "pass", None, "person", self.name, ME, at - step)
            s.accept_milestone(uid, mid, self.name, ME, at)
            s.add_answer(f"milestone_to_check:{uid}:{mid}", "milestone_to_check", uid,
                         factory_waiting.milestone_ask(p["name"], n + 1), "yes", "Accepted",
                         "console", ME, at)
            self.stamp("milestones", mid, updated_at=at)

    # ── tickets ───────────────────────────────────────────────────────────────
    def plan_merged(self, p: dict, index: int, w: int, n: int, architecture: int) -> dict:
        """A ticket that merged in week `w`. The first of each week (n == 0) starts in that week
        too, so every week has a whole ticket of its own. Those first tickets take turns: half go
        through changes and half pass the first review, so every week shows both."""
        r = rng_for(p["slug"], w, n)
        start_w, end_w = self.window(w)
        first = n == 0
        roll = r.random()          # most merge in hours, a long tail in days
        hours = (r.uniform(1, 10) if roll < 0.75 else
                 r.uniform(24, 72) if roll < 0.93 else r.uniform(96, 216))
        merged = start_w + (end_w - start_w) * (r.uniform(0.6, 0.9) if first else r.uniform(0.15, 0.9))
        floor = start_w if first else self.starts[0]
        length = min(timedelta(hours=hours), (merged - floor) * 0.95)
        start = merged - length
        created = max(floor, start - timedelta(hours=r.uniform(0.5, 30)))
        slow = p["uid"] == BILLING
        rounds = r.choices((0, 1, 2), (40, 38, 22) if slow else (62, 27, 11))[0]
        if first:
            rounds = max(rounds, 1) if index % 2 == w % 2 else 0
        if architecture:
            rounds = max(rounds, 1, (architecture + 1) // 2)
        blocked = first and (w % len(PROJECTS) == index or (slow and w % 2 == 1))
        parts = ([("doing", 0.2), ("blocked", r.uniform(0.15, 0.3)), ("doing", 0.25)] if blocked
                 else [("doing", r.uniform(0.4, 0.6))])
        for _ in range(rounds):
            parts += [("review", r.uniform(0.05, 0.15)), ("changes", r.uniform(0.08, 0.2))]
        parts.append(("review", r.uniform(0.05, 0.15)))
        if p["autonomy"] == "L3-review":     # ready to merge, waiting for a person's yes
            parts.append(("approval", r.uniform(0.05, 0.25)))
        total = sum(x for _, x in parts)
        segments, t = [], start
        for stage, weight in parts:
            end = t + length * (weight / total)
            segments.append((stage, t, end))
            t = end
        segments[-1] = (segments[-1][0], segments[-1][1], merged)
        return {"r": r, "week": w, "first": first, "created": created, "start": start,
                "merged": merged, "segments": segments, "rounds": rounds,
                "architecture": architecture, "open": None,
                "milestone": sum(1 for a in p["accepted"] if a < w)}

    def plan_open(self, p: dict, n: int, stage: str) -> dict:
        r = rng_for(p["slug"], "open", n)
        start = max(self.starts[0], self.now - timedelta(hours=r.uniform(3, 30)))
        created = max(self.starts[0], start - timedelta(hours=r.uniform(1, 48)))
        return {"r": r, "week": WEEKS - 1, "first": False, "created": created, "start": start,
                "open": stage, "milestone": len(p["accepted"])}

    def plans(self, p: dict, index: int) -> list[dict]:
        out = []
        for w in range(WEEKS):
            count = p["per_week"] + rng_for(p["slug"], w, "count").randint(-1, 1)
            if w == WEEKS - 1:       # the current week holds only its share so far
                count = max(1, round(count * (self.now - self.starts[w]) / timedelta(weeks=1)))
            drift = MOBILE_ARCHITECTURE[w] if p["uid"] == MOBILE else 0
            for n in range(count):
                share = drift // count + (1 if n < drift % count else 0)
                out.append(self.plan_merged(p, index, w, n, share))
        stages = {"uc_demo_notes": ("doing", "review", "changes", "todo"),
                  BILLING: ("doing", "blocked", "review", "todo"),
                  MOBILE: ("doing", "changes", "review", "todo"),
                  OFFICE: ("doing", "approval", "todo"),
                  "uc_demo_import": ("doing", "review", "todo")}[p["uid"]]
        out += [self.plan_open(p, n, stage) for n, stage in enumerate(stages)]
        return sorted(out, key=lambda t: t["created"])

    def new_ticket(self, p: dict, plan: dict, title: str, milestone: str) -> dict:
        s, uid, at = self.store, p["uid"], plan["created"]
        tid = s.create_ticket(uid, "tares", title, milestone=milestone,
                              repo=f"example-org/{p['slug']}")
        s.add_ticket_history(tid, uid, "stage", "todo", "the ticket was opened", ORCHESTRATOR,
                             "orchestrator", at)
        return s.get_ticket(uid, tid)

    def stage(self, p: dict, tid: str, stage: str, at, reason: str | None = None,
              by: str | None = None, role: str = "builder") -> None:
        self.store.add_ticket_history(tid, p["uid"], "stage", stage, reason,
                                      by or f"{p['slug']}-build", role, at)

    def pick_up(self, p: dict, tid: str, plan: dict) -> None:
        at = plan["start"] - (plan["start"] - plan["created"]) * 0.02
        self.store.add_ticket_history(tid, p["uid"], "holder", f"{p['slug']}-build", None,
                                      ORCHESTRATOR, "orchestrator", at)
        self.reply(plan["r"], "orchestrator", p, at, stamped=plan["r"].random() < 0.25)

    def pull_request(self, p: dict, ticket: dict, number: int, head: str, at) -> dict:
        repo = f"example-org/{p['slug']}"
        return {"url": f"https://github.com/{repo}/pull/{number}", "number": number, "repo": repo,
                "head": head, "base": "main", "branch": f"build/{factory.label(ticket).lower()}",
                "title": ticket["title"], "state": "open", "merged": False, "ci": "success",
                "ci_checks": [], "checked_at": at.isoformat(),
                "sample": True}          # not on GitHub, so never read there (factory_github.stale)

    def review(self, p: dict, ticket: dict, pr: dict, r: random.Random, verdict: str, at,
               findings: list[dict], resolved: dict) -> None:
        s, uid, tid = self.store, p["uid"], ticket["id"]
        s.add_check(uid, tid, "make test", f"{r.randint(40, 260)} passed", pr["head"][:8], None,
                    f"{p['slug']}-build", self.session("builder", p, at), at - timedelta(seconds=20))
        s.add_review(uid, tid, pr["url"], pr["head"], verdict, r.choice(VERIFIED),
                     "No load test." if r.random() < 0.3 else "", REVIEWER, findings, resolved,
                     factory.finding_kind, at)
        for k in range(r.randint(1, 2)):
            self.reply(r, "reviewer", p, at - timedelta(seconds=30 * (k + 1)))

    def finding(self, p: dict, r: random.Random, kind: str | None, blocking: bool) -> dict:
        """A finding of `kind`, or of a kind drawn by weight. The drifting project draws no
        architecture finding by chance: its weekly number is set by MOBILE_ARCHITECTURE."""
        if kind is None:
            kinds = [(k, v) for k, v in KIND_WEIGHTS
                     if not (k == "architecture" and p["uid"] == MOBILE)]
            kind = r.choices([k for k, _ in kinds], [v for _, v in kinds])[0]
        file, text = r.choice(ARCHITECTURE if kind == "architecture" else FINDINGS[kind])
        severity = ("P1" if kind in ("security", "migration") else "P2") if blocking else "P3"
        return {"severity": severity, "file": file, "line": r.randint(3, 220),
                "blocking": blocking, "text": text}

    def rounds(self, p: dict, plan: dict) -> list[tuple[str, list[dict], dict]]:
        """(verdict, findings, resolved) per review round: what blocked a round is fixed in the
        next. The drifting project's architecture findings are spread over its changes rounds."""
        r, out, blockers, minor, number = plan["r"], [], [], [], 0
        drift = plan["architecture"]
        for i in range(plan["rounds"] + 1):
            last = i == plan["rounds"]
            resolved = {f"F{n}": "fixed" for n in blockers}
            resolved.update({f"F{n}": "fixed" for n in minor if r.random() < 0.5})
            minor = [n for n in minor if f"F{n}" not in resolved]
            findings = []
            if not last:
                left = plan["rounds"] - i
                take = (drift + left - 1) // left
                drift -= take
                findings = [self.finding(p, r, "architecture", True) for _ in range(take)]
                if not findings:
                    findings = [self.finding(p, r, None, True)]
            clean = plan["first"] and plan["rounds"] == 0     # still leaves a note
            if clean or r.random() < (0.2 if last else 0.4):
                findings.append(self.finding(p, r, "docs out of date" if r.random() < 0.5
                                             else "missing test", False))
            blockers = []
            for f in findings:
                number += 1
                (blockers if f["blocking"] else minor).append(number)
            out.append(("pass" if last else "changes", findings, resolved))
        return out

    def merged_ticket(self, p: dict, index: int, plan: dict, title: str, milestone: str,
                      number: int) -> None:
        s, uid, r, w = self.store, p["uid"], plan["r"], plan["week"]
        ticket = self.new_ticket(p, plan, title, milestone)
        tid, label = ticket["id"], factory.label(ticket)
        builder = f"{p['slug']}-build"
        self.pick_up(p, tid, plan)
        rounds = self.rounds(p, plan)
        pr, done = None, 0
        for stage, a, b in plan["segments"]:
            span = b - a
            if stage != "approval":
                self.stage(p, tid, stage, a, "waiting for a person's answer"
                           if stage == "blocked" else None)
            if stage in ("doing", "changes"):
                for _ in range(r.randint(2, 5)):
                    self.reply(r, "builder", p, a + span * r.random())
            elif stage == "blocked":
                q, options, recommendation, why = BLOCKING[(w + 3 * index) % len(BLOCKING)]
                item = self.ask(p, [label], a, q, options, recommendation, why, True, builder)
                self.answer(p, item, options[0], a + span * 0.95)
            elif stage == "review":
                head = "%040x" % r.getrandbits(160)
                pr = self.pull_request(p, ticket, number, head, a)
                verdict, findings, resolved = rounds[done]
                done += 1
                self.review(p, ticket, pr, r, verdict, a + span * 0.8, findings, resolved)
        ready = plan["segments"][-1]
        desk = None
        if ready[0] == "approval":
            a, b = ready[1], ready[2]
            asked, yes = a + (b - a) * 0.02, a + (b - a) * 0.97
            desk = self.ask_merge(p, label, title, pr, asked)
            s.add_decision(uid, desk["id"], desk["question"], merge_queue.APPROVE_WORDS,
                           merge_queue.APPROVE_CHOICE, False, "project", False, None, False,
                           [label], yes, via="console", person_id=ME)
            s.close_desk_item(desk["id"], "answered", None, yes)
            s.add_answer(f"prs_to_review:{uid}:{label}", "prs_to_review", uid,
                         f"Merge PR {number}", "yes", "Merge", "console", ME, yes)
        at = plan["merged"]
        entered = ready[1] if desk else at - timedelta(seconds=40)
        row = s.merge_queue_add(pr["repo"], "main", number, pr["head"], tid, uid, state="merged")
        if desk:
            s.merge_queue_update(row["id"], desk_item=desk["id"], approved_head=pr["head"])
        self.stamp("merge_queue", row["id"], entered_at=entered, updated_at=at)
        pr.update(state="closed", merged=True, merged_at=at.isoformat(), checked_at=at.isoformat(),
                  merge_commit="%040x" % r.getrandbits(160), changed_files=r.randint(1, 14))
        s.update_ticket(tid, stage="merged", pr=pr, holder=builder)
        s.update_ticket(tid, status="done")
        self.stage(p, tid, "merged", at, f"Tares merged PR #{number}", merge_queue.BY, "tares")
        self.reply(r, "releaser", p, at)
        self.stamp("tickets", tid, created_at=plan["created"], updated_at=at)
        if plan["first"]:
            self.extras(p, index, plan, label)

    def ask_merge(self, p: dict, label: str, title: str, pr: dict, at) -> dict:
        """The question Tares files at L3-review before it merges (merge_queue._ask)."""
        return self.store.add_desk_item(
            p["uid"], f"Merge PR #{pr['number']} ({title}) at {pr['head'][:7]}?",
            f"{pr['repo']} into {pr['base']}. The review gate passed and CI is green. Tares "
            "merges it once you say yes.", [merge_queue.APPROVE_CHOICE, "No, hold"],
            merge_queue.APPROVE_CHOICE,
            "Tares merges ready pull requests itself and asks you first at L3-review.",
            f"The merge of PR #{pr['number']}", [label], True, merge_queue.BY, [], at,
            to_person_id=ME)

    def extras(self, p: dict, index: int, plan: dict, label: str) -> None:
        """Questions that do not block, assumptions and a person's own session, spread so that
        every week has some of each across the projects."""
        w, r, count = plan["week"], plan["r"], len(PROJECTS)
        builder = f"{p['slug']}-build"
        _, a, b = plan["segments"][0]
        end = plan["merged"]
        if index in ((w + 1) % count, (w + 3) % count):
            q, options, recommendation = SMALL[(w + index) % len(SMALL)]
            asked = a + (b - a) * 0.3
            item = self.ask(p, [label], asked, q, options, recommendation,
                            "Either works, the crew carries on meanwhile.", False, builder)
            answered = min(asked + timedelta(hours=r.uniform(1, 20)), end - (end - asked) * 0.1)
            self.answer(p, item, recommendation, answered, standing=w % 3 == 0)
        if index in ((w + 2) % count, (w + 4) % count):
            q, choice, why = ASSUMPTIONS[(w + index) % len(ASSUMPTIONS)]
            made = a + (b - a) * 0.5
            row = self.store.add_assumption(p["uid"], q, choice, why, "", [label], builder,
                                            "builder", made)
            if w < WEEKS - 1:
                kept = index != (w + 4) % count or w % 3 != 0
                self.store.decide_assumption(row["id"], "kept" if kept else "overturned",
                                             "Keep it" if kept else "Do it the other way", None,
                                             end - (end - made) * 0.05)
        for k in range(2):
            self.reply(r, "person", p, a + (b - a) * (0.2 + 0.3 * k), stamped=False)

    def open_ticket(self, p: dict, plan: dict, title: str, milestone: str, number: int) -> None:
        s, uid, r = self.store, p["uid"], plan["r"]
        ticket = self.new_ticket(p, plan, title, milestone)
        tid, label, want = ticket["id"], factory.label(ticket), plan["open"]
        if want == "todo":
            self.stamp("tickets", tid, created_at=plan["created"], updated_at=plan["created"])
            return
        builder = f"{p['slug']}-build"
        start = plan["start"]
        span = self.now - timedelta(minutes=5) - start
        self.pick_up(p, tid, plan)
        self.stage(p, tid, "doing", start)
        for _ in range(r.randint(2, 4)):
            self.reply(r, "builder", p, start + span * r.uniform(0, 0.45))
        stage, status, pr, reason, last = want, "in_progress", None, None, start
        if want == "blocked":
            last = start + span * 0.6
            reason = "waiting for a person's answer"
            self.stage(p, tid, "blocked", last, reason)
            q, options, recommendation, why = BLOCKING[-1]
            self.ask(p, [label], last, q, options, recommendation, why, True, builder)
        elif want in ("review", "changes", "approval"):
            last = start + span * 0.5
            if want == "review":      # still unreviewed, so it has not been waiting long
                last = max(last, self.now - timedelta(hours=r.uniform(0.3, 2)))
            self.stage(p, tid, "review", last)
            pr = self.pull_request(p, ticket, number, "%040x" % r.getrandbits(160), self.now)
            stage, status = "review", "in_review"
            if want == "changes":
                verdict_at = start + span * 0.7
                self.review(p, ticket, pr, r, "changes", verdict_at,
                            [self.finding(p, r, None, True)], {})
                last = start + span * 0.75
                self.stage(p, tid, "changes", last)
                stage, status = "changes", "in_progress"
            elif want == "approval":
                last = start + span * 0.7
                self.review(p, ticket, pr, r, "pass", last, [], {})
                asked = start + span * 0.72
                desk = self.ask_merge(p, label, title, pr, asked)
                row = s.merge_queue_add(pr["repo"], "main", number, pr["head"], tid, uid,
                                        state="waiting_approval")
                s.merge_queue_update(row["id"], desk_item=desk["id"])
                self.stamp("merge_queue", row["id"], entered_at=last, updated_at=asked)
        elif p["uid"] == MOBILE:
            q, options, recommendation = SMALL[-1]
            self.ask(p, [label], start + span * 0.4, q, options, recommendation,
                     "Either works, the crew carries on meanwhile.", False, builder)
        s.update_ticket(tid, stage=stage, status=status, pr=pr, holder=builder, stage_reason=reason)
        self.stamp("tickets", tid, created_at=plan["created"], updated_at=last)

    def run(self) -> None:
        s = self.store
        self.name = (s.get_person(ME) or {}).get("name") or ME
        crew_seen(self.store, self.starts[0])
        for index, p in enumerate(PROJECTS):
            milestones = self.project(p)
            titles = [t.format(thing) for t in TITLES for thing in p["things"]]
            number = 100
            for n, plan in enumerate(self.plans(p, index)):
                number += 1
                args = (plan, titles[n % len(titles)], milestones[plan["milestone"]], number)
                if plan["open"]:
                    self.open_ticket(p, *args)
                else:
                    self.merged_ticket(p, index, *args)
            self.accept(p, milestones)
        self.write_stations()


def wipe(store: Store) -> None:
    """Remove whatever an earlier run of this seed wrote, so a half-written one is not doubled."""
    uids = [p["uid"] for p in PROJECTS]
    for uid in uids:
        if store.get_project(uid):
            store.delete_project(uid)
    with store.sessions_db.begin() as conn:
        # answers stay when a project is deleted
        conn.execute(delete(answers).where(answers.c.project.in_(uids)))
        for t in (session_usage, session_stations, session_states, project_sessions):
            conn.execute(delete(t).where(t.c.session.like(f"{SESSION_PREFIX}%")))


def dress(store: Store) -> None:
    """On a cell the seed has to itself: the crew's choices as the sample crew works, and no
    first-run card, so Home opens on the work."""
    if not store.get_setting("crew_settings"):    # choices already made stay
        choices = factory.crew_settings_ok(
            {"autonomy": "L4-ship", "releaser": True, "builders_max": len(PROJECTS)},
            dict(factory.CREW_DEFAULTS))
        store.set_setting("crew_settings", json.dumps(choices))
    start_steps.hide(store, "console")


def crew_seen(store: Store, at) -> str:
    """The sample crew, last seen at `at`. Answers its id."""
    return store.resolve_crew("demo-crew", ME, "demo-laptop", at)


def summary(store: Store) -> None:
    for p in PROJECTS:
        tickets = store.list_tickets(p["uid"])
        merged = sum(1 for t in tickets if t.get("stage") == "merged")
        print(f"  {p['name']:<15} {p['autonomy']:<10} {merged:>3} merged, "
              f"{len(tickets) - merged} open")


def open_store(data_dir: str) -> Store | None:
    """The data dir's store, or None, with the reason printed, when it cannot be opened."""
    try:
        os.makedirs(data_dir, exist_ok=True)
        return Store(data_dir)
    except (OSError, StoreUnavailable) as e:
        lines = str(e).splitlines()
        print(f"Could not open the data dir {data_dir}. If a Tares daemon is running on it, stop "
              f"it and run the seed again.\n{lines[0] if lines else type(e).__name__}",
              file=sys.stderr)
        return None


def refresh_command(data_dir: str) -> str:
    return " ".join(shlex.quote(x) for x in (
        sys.executable, os.path.abspath(__file__), "--data-dir", os.path.abspath(data_dir),
        "--refresh-crew"))


def refresh_crew(data_dir: str) -> int:
    """Stamp this week's sample stations as idle and heard from now. Adds no rows."""
    nothing = (f"{data_dir} holds no sample history to refresh. Run the seed on it first, "
               "without --refresh-crew.")
    if not os.path.isdir(data_dir):
        print(nothing, file=sys.stderr)
        return 1
    store = open_store(data_dir)
    if store is None:
        return 2
    try:
        if json.loads(store.get_setting(MARKER) or "{}").get("state") != "done":
            print(nothing, file=sys.stderr)
            return 1
        now = now_utc()
        this_week = session_stations.c.session.like(f"{SESSION_PREFIX}%-w{WEEKS - 1}")
        with store.sessions_db.begin() as conn:
            sids = conn.execute(select(session_stations.c.session).where(this_week)).scalars().all()
            # set_session_station keeps the time a session was first seen
            conn.execute(update(session_stations).where(this_week).values(seen_at=now))
        for sid in sids:
            store.set_session_state(sid, "idle", None, now)
        crew_seen(store, now)
        print(f"The sample crew in {data_dir} counts as heard from now. Start Tares again: Home "
              "says the crew is running for an hour from now.")
        return 0
    finally:
        store.close()


def seed(data_dir: str, into_existing: bool = False) -> int:
    store = open_store(data_dir)
    if store is None:
        return 2
    try:
        done = json.loads(store.get_setting(MARKER) or "{}")
        if done.get("state") == "done":
            print(f"{data_dir} already holds the sample history, written {done.get('at')}. "
                  "Nothing to add. For newer dates, seed an empty data dir.")
            return 0
        ours = {p["uid"] for p in PROJECTS} | {store.default_project_id_if_any()}
        others = [p["name"] for p in store.list_projects() if p["id"] not in ours]
        if others and not into_existing:
            print(f"{data_dir} already holds projects this seed did not write: "
                  f"{', '.join(others[:5])}. Use an empty data dir, or pass --into-existing to "
                  "write the sample projects beside them.", file=sys.stderr)
            return 1
        now = now_utc()
        store.set_setting(MARKER, json.dumps({"state": "writing", "at": now.isoformat()}))
        wipe(store)
        # set before the rows: a start with data and no edition would settle as `platform`
        if not others and not store.get_setting("edition"):
            store.set_setting("edition", "factory")
        seeder = Seeder(store, now)
        seeder.run()
        if not others:
            dress(store)
        store.set_setting(MARKER, json.dumps({"state": "done", "at": now.isoformat()}))
        print(f"Wrote {WEEKS} weeks of sample history from {seeder.starts[0]:%Y-%m-%d} into "
              f"{data_dir}:")
        summary(store)
        print(f"  {seeder.replies} model replies with their cost\n"
              f"Start it with: TARES_PR_SYNC_SECONDS=off tares up --data-dir {data_dir}\n"
              "Home says the sample crew is running for an hour from now. After that it says the "
              "crew was not heard from: stop Tares, run this, and start Tares again.\n"
              f"  {refresh_command(data_dir)}")
        return 0
    finally:
        store.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data-dir", required=True, help="the data dir of a stopped Tares")
    ap.add_argument("--into-existing", action="store_true",
                    help="write the sample projects beside projects that are already there")
    ap.add_argument("--refresh-crew", action="store_true",
                    help="on a seeded data dir, show the sample crew as heard from now; adds no rows")
    args = ap.parse_args(argv)
    if args.refresh_crew:
        return refresh_crew(args.data_dir)
    return seed(args.data_dir, args.into_existing)


if __name__ == "__main__":
    sys.exit(main())
