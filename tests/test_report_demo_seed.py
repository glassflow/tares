"""scripts/seed_report_demo.py (TR-1674): the sample history behind the Report and Home demo. A
fresh data dir seeded twice holds the same rows, a run that stopped half way is written again
without doubles, a dir with other projects is refused, and each of the last eight ISO weeks (UTC)
holds what the Report reads. The week checks run on today's seed and again as seeded just after a
week began and just before one ended. The part that reads the Report is skipped while there is no
GET /api/factory/report (TR-1667).

Run: .venv/bin/python tests/test_report_demo_seed.py
"""
import contextlib
import glob
import hashlib
import importlib.util
import io
import json
import os
import re
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
sys.path.insert(0, ROOT)
from _env import clean_env, scrub_os_environ  # noqa: E402

scrub_os_environ()          # the stores and the seed below also run in this process

from tares.session_store import tables as session_tables  # noqa: E402
from tares.store import Store  # noqa: E402

SCRIPT = os.path.join(ROOT, "scripts", "seed_report_demo.py")
NAMES = ["Pocket Notes", "Tally Billing", "Field Mobile", "Back Office", "Catalog Import"]
BILLING, MOBILE = "uc_demo_billing", "uc_demo_mobile"
KINDS = {"architecture", "migration", "missing test", "error handling", "config key",
         "null or empty", "concurrency", "security", "docs out of date"}
WEEKS = 8
P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def run_seed(data_dir, *args):
    return subprocess.run([sys.executable, SCRIPT, "--data-dir", data_dir, *args],
                          capture_output=True, text=True, timeout=120, env=clean_env())


def load_script():
    spec = importlib.util.spec_from_file_location("seed_report_demo", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def seed_here(mod, data_dir):
    """The seed run in this process: (exit code, what it printed)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = mod.seed(data_dir)
    return code, out.getvalue()


@contextlib.contextmanager
def opened(data_dir):
    store = Store(os.path.join(data_dir, "tares.duckdb"))
    try:
        yield store
    finally:
        store.close()
        store.con.close()


def every_row(s):
    """{table: rows} for every state and session table."""
    out = {t: [tuple(r) for r in s.sc.execute(f"SELECT * FROM {t}").fetchall()]
           for t in s._state_tables() if t not in session_tables.metadata.tables}
    with s.sessions_db.begin() as conn:
        for t in session_tables.metadata.sorted_tables:
            out[t.name] = [tuple(r) for r in conn.execute(select(t)).fetchall()]
    return out


def snapshot(data_dir):
    """{table: (rows, digest)} for every state and session table."""
    def digest(rows):
        text = "\n".join(sorted(map(repr, rows)))
        return len(rows), hashlib.sha256(text.encode()).hexdigest()[:12]

    with opened(data_dir) as s:
        return {t: digest(rows) for t, rows in every_row(s).items()}


def differing(a, b):
    return {t: (a.get(t), b.get(t)) for t in set(a) | set(b) if a.get(t) != b.get(t)}


def counts(snap):
    return {t: n for t, (n, _) in snap.items()}


def utc(t):
    if isinstance(t, str):
        t = datetime.fromisoformat(t.replace("Z", "+00:00"))
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def week(t):
    return tuple(utc(t).isocalendar()[:2])


def monday(t):
    return (utc(t) - timedelta(days=utc(t).weekday())).replace(hour=0, minute=0, second=0,
                                                              microsecond=0)


TABLES = {
    "usecases": "id name created_at",
    "tickets": "id project number stage status repo pr created_at updated_at",
    "ticket_history": "ticket project changed_at field value",
    "ticket_checks": "project checked_at",
    "reviews": "id project ticket round head verdict reviewed_at",
    "review_findings": "project ticket severity blocking kind state fixed_in found_at",
    "desk_items": "id number project blocking asked_by state created_at closed_at to_person_id",
    "answers": "item kind project person_id answered_at",
    "decisions": "project decided_at",
    "assumptions": "project state made_at decided_at",
    "milestones": "id project created_at updated_at",
    "milestone_check_results": "project result checked_at",
    "milestone_acceptances": "milestone project accepted_at",
    "merge_queue": "repo pr head ticket project_uid state desk_item approved_head entered_at updated_at",
    "people": "id name email",
    "crews": "first_seen last_seen",
}


def load(data_dir):
    with opened(data_dir) as s:
        db = {name: [dict(zip(cols.split(), row)) for row in s.sc.execute(
            f"SELECT {', '.join(cols.split())} FROM {name}").fetchall()]
            for name, cols in TABLES.items()}
        with s.sessions_db.begin() as conn:
            for t in (session_tables.session_usage, session_tables.session_stations,
                      session_tables.session_states, session_tables.project_sessions):
                db[t.name] = [dict(r._mapping) for r in conn.execute(select(t)).fetchall()]
        at = utc(json.loads(s.get_setting("report_demo_seed"))["at"])
        starts = [monday(at) - timedelta(weeks=WEEKS - 1 - w) for w in range(WEEKS)]
        # the console's own cost rule, read as "since", so a week is the difference of two reads
        since = [s.usage_by_project(since=start) for start in starts] + [[]]
        db["cost"] = s.usage_by_project()
        db["text"] = json.dumps(every_row(s), default=str)      # for the made-up checks
    db["week_cost"] = []
    for w in range(WEEKS):
        later = {(r["project"], r["kind"]): r["cost_usd"] for r in since[w + 1]}
        db["week_cost"].append({r["project"]: r["cost_usd"] - later.get((r["project"], r["kind"]), 0)
                                for r in since[w] if r["kind"] == "project"})
    db["at"], db["starts"] = at, starts
    return db


def stage_times(db):
    """{ticket: [(when, stage), ...]} in time order."""
    out = defaultdict(list)
    for h in db["ticket_history"]:
        if h["field"] == "stage":
            out[h["ticket"]].append((utc(h["changed_at"]), h["value"]))
    return {k: sorted(v) for k, v in out.items()}


def week_checks(db, label):
    """What the Report reads, in each of the last eight ISO weeks."""
    weeks = [week(s) for s in db["starts"]]
    projects = [u["id"] for u in db["usecases"] if u["id"].startswith("uc_demo_")]
    levels = {uid: ("L3" if uid in (BILLING, "uc_demo_office") else "L4") for uid in projects}
    stages = stage_times(db)
    ticket = {t["id"]: t for t in db["tickets"]}
    reviewed = {r["id"]: r for r in db["reviews"]}

    def missing(pairs):
        have = set(pairs)
        return [(uid, i) for uid in projects for i, w in enumerate(weeks) if (uid, w) not in have]

    def gaps(times):
        have = {week(t) for t in times}
        return [i for i, w in enumerate(weeks) if w not in have]

    def first(tid, stage):
        return next((t for t, s in stages.get(tid, []) if s == stage), None)

    per_project = {
        "a ticket merged (started, then merged)": [
            (t["project"], week(first(tid, "merged"))) for tid, t in ticket.items()
            if first(tid, "doing") and first(tid, "merged")
            and first(tid, "doing") < first(tid, "merged")],
        "time in doing": [(ticket[k]["project"], week(t)) for k, v in stages.items()
                          for t, s in v if s == "doing"],
        "time in review": [(ticket[k]["project"], week(t)) for k, v in stages.items()
                           for t, s in v if s == "review"],
        "a review": [(r["project"], week(r["reviewed_at"])) for r in db["reviews"]],
        "a finding opened": [(f["project"], week(f["found_at"])) for f in db["review_findings"]],
        "a required check run": [(c["project"], week(c["checked_at"]))
                                 for c in db["ticket_checks"]],
        "a merge in the ledger": [(m["project_uid"], week(m["updated_at"]))
                                  for m in db["merge_queue"] if m["state"] == "merged"],
    }
    for what, pairs in per_project.items():
        ck(f"{label}: every project, every week has {what}", len(projects) == 5
           and not missing(pairs), str(missing(pairs)[:6]))
    short = [(uid, i) for uid in projects for i in range(WEEKS)
             if db["week_cost"][i].get(uid, 0) <= 0]
    ck(f"{label}: every project, every week has a cost", not short, str(short[:6]))
    approved = [(a["project"], week(a["answered_at"])) for a in db["answers"]
                if a["kind"] == "prs_to_review" and a["person_id"]]
    short = [x for x in missing(approved) if levels[x[0]] == "L3"]
    ck(f"{label}: every L3 project, every week has a person's merge approval", not short,
       str(short[:6]))

    # across the projects: not each project every week
    merge_desks = {d["id"] for d in db["desk_items"] if d["asked_by"] == "merge-queue"}
    asked = [d for d in db["desk_items"] if d["id"] not in merge_desks]
    roles = {s["session"]: s["role"] for s in db["session_stations"]}
    portfolio = {
        "a blocked ticket": [t for v in stages.values() for t, s in v if s == "blocked"],
        "changes asked": [t for v in stages.values() for t, s in v if s == "changes"],
        "a blocking question answered": [d["created_at"] for d in asked if d["blocking"]
                                         and d["state"] == "answered" and d["closed_at"]],
        "a question that does not block": [d["created_at"] for d in asked if not d["blocking"]],
        "an assumption": [a["made_at"] for a in db["assumptions"]],
        "a finding fixed": [reviewed[f["fixed_in"]]["reviewed_at"] for f in db["review_findings"]
                            if f["state"] == "fixed" and f["fixed_in"] in reviewed],
        "a first-round pass": [r["reviewed_at"] for r in db["reviews"]
                               if r["round"] == 1 and r["verdict"] == "pass"],
        "a review asking for changes": [r["reviewed_at"] for r in db["reviews"]
                                        if r["verdict"] == "changes"],
        "a decision": [d["decided_at"] for d in db["decisions"]],
        "a milestone accepted": [a["accepted_at"] for a in db["milestone_acceptances"]],
    }
    for level in ("L3", "L4"):
        portfolio[f"a merge at {level}"] = [
            m["updated_at"] for m in db["merge_queue"]
            if m["state"] == "merged" and levels[m["project_uid"]] == level]
    for role in ("orchestrator", "builder", "reviewer", "releaser", None):
        portfolio[f"usage by {role or 'a person of their own (no station)'}"] = [
            u["replied_at"] for u in db["session_usage"]
            if roles.get(u["session"]) == role and (u["cost_usd"] or 0) > 0]
    for what, times in portfolio.items():
        ck(f"{label}: every week has {what}", not gaps(times), f"weeks without: {gaps(times)}")

    drift = [sum(1 for f in db["review_findings"] if f["project"] == MOBILE
                 and f["kind"] == "architecture" and week(f["found_at"]) == w) for w in weeks]
    ck(f"{label}: architecture findings rise in the mobile client",
       drift == sorted(drift) and drift[0] <= 1 and drift[-1] >= 4, str(drift))


def history_checks(db, label, after):
    stages = stage_times(db)
    late, early = [], []
    for name, rows in db.items():
        if name in ("starts", "at", "cost", "week_cost", "text"):
            continue
        for row in rows:
            for column, value in row.items():
                if column.endswith(("_at", "_seen")) and value is not None:
                    if utc(value) > after:
                        late.append((name, column, str(value)))
                    # the built-in person exists from the cell's first start
                    if name != "people" and utc(value) < db["starts"][0]:
                        early.append((name, column, str(value)))
    for t in db["tickets"]:
        merged_at = json.loads(t["pr"]).get("merged_at") if t["pr"] else None
        if merged_at and utc(merged_at) > after:
            late.append(("tickets", "pr.merged_at", merged_at))
    ck(f"{label}: no time is in the future", not late, str(late[:5]))
    ck(f"{label}: nothing is older than the first of the eight weeks", not early, str(early[:5]))
    bad = []
    for t in db["tickets"]:
        rows = stages.get(t["id"], [])
        names = [s for _, s in rows]
        if not rows or names[0] != "todo" or (t["stage"] or "todo") != names[-1]:
            bad.append((t["project"], t["number"], t["stage"], names))
        elif "merged" in names and (names.index("merged") != len(names) - 1
                                    or "doing" not in names or "review" not in names):
            bad.append((t["project"], t["number"], names))
        elif utc(t["created_at"]) > rows[0][0] or rows[0][0] > utc(t["updated_at"]):
            bad.append((t["project"], t["number"], "created or updated out of order"))
    ck(f"{label}: each ticket's history runs in order and ends at its stage", not bad,
       str(bad[:3]))
    out_of_order = [d["number"] for d in db["desk_items"]
                    if d["closed_at"] and utc(d["closed_at"]) < utc(d["created_at"])]
    out_of_order += [a["project"] for a in db["assumptions"]
                     if a["decided_at"] and utc(a["decided_at"]) < utc(a["made_at"])]
    ck(f"{label}: nothing is answered before it was asked", not out_of_order,
       str(out_of_order[:5]))


def shape_checks(db):
    stages = stage_times(db)
    hours = []
    for rows in stages.values():
        doing = next((t for t, s in rows if s == "doing"), None)
        merged = next((t for t, s in rows if s == "merged"), None)
        if doing and merged:
            hours.append((merged - doing).total_seconds() / 3600)
    ck("over a hundred tickets merged", len(hours) > 100, str(len(hours)))
    under = sum(1 for h in hours if h < 24) / max(1, len(hours))
    ck("most tickets merge within a day", under > 0.6 and statistics.median(hours) < 12,
       f"{under:.2f} under a day, median {statistics.median(hours):.1f}h")
    long = sum(1 for h in hours if h > 48)
    ck("a long tail takes days", long >= 0.05 * len(hours) and max(hours) > 96,
       f"{long} over two days, longest {max(hours):.0f}h")
    blocked = sum(1 for rows in stages.values() if any(s == "blocked" for _, s in rows))
    ck("a few tickets were blocked, not many", 8 <= blocked <= 0.25 * len(stages),
       f"{blocked} of {len(stages)}")

    spend = {r["project"]: r["cost_usd"] for r in db["cost"] if r["kind"] == "project"}
    rest = max((v for k, v in spend.items() if k != BILLING), default=0)
    ck("one project spends far more than any other", len(spend) == 5 and rest > 0
       and spend.get(BILLING, 0) > 3 * rest, str(spend))
    ck("the crew's own cost is there too",
       any(r["kind"] == "crew" and r["cost_usd"] > 0 for r in db["cost"]), str(db["cost"]))
    ck("every reply is priced", all(r["unpriced_replies"] == 0 for r in db["cost"]),
       str(db["cost"]))

    kinds = {f["kind"] for f in db["review_findings"]}
    ck("findings of every kind, architecture included", kinds == KINDS, str(KINDS ^ kinds))
    sevs = {f["severity"] for f in db["review_findings"]}
    ck("findings of three severities, blocking and not, open and fixed",
       sevs == {"P1", "P2", "P3"}
       and {bool(f["blocking"]) for f in db["review_findings"]} == {True, False}
       and {f["state"] for f in db["review_findings"]} == {"open", "fixed"}, str(sevs))
    states = {a["state"] for a in db["assumptions"]}
    ck("assumptions kept, overturned and open", states == {"kept", "overturned", "open"},
       str(states))
    ck("a milestone check failed once", any(c["result"] == "fail"
       for c in db["milestone_check_results"]))
    ck("a ticket took three review rounds", max(r["round"] for r in db["reviews"]) >= 3)

    # the merge ledger
    number = {t["id"]: t["number"] for t in db["tickets"]}
    prs = {t["id"]: json.loads(t["pr"]) for t in db["tickets"] if t["pr"]}
    passed = {}
    for r in sorted(db["reviews"], key=lambda r: utc(r["reviewed_at"])):
        passed[r["ticket"]] = r["head"] if r["verdict"] == "pass" else None
    rows = [m for m in db["merge_queue"] if m["state"] == "merged"]
    merged = {t["id"] for t in db["tickets"] if t["stage"] == "merged"}
    ck("one ledger row for each merged ticket",
       sorted(m["ticket"] for m in rows) == sorted(merged), f"{len(rows)} for {len(merged)}")
    bad = [number.get(m["ticket"]) for m in rows
           if not (m["head"] == passed.get(m["ticket"]) == prs[m["ticket"]]["head"]
                   and prs[m["ticket"]]["merged"] and prs[m["ticket"]].get("merge_commit"))]
    ck("what merged is the sha the review passed", not bad, str(bad[:5]))
    yes = {a["item"] for a in db["answers"] if a["kind"] == "prs_to_review" and a["person_id"]}
    desks = {d["id"]: d for d in db["desk_items"]}
    bad = []
    for m in rows:
        t = m["ticket"]
        if m["project_uid"] in (BILLING, "uc_demo_office"):
            ok = (m["approved_head"] == m["head"] and m["desk_item"] in desks
                  and desks[m["desk_item"]]["state"] == "answered"
                  and f"prs_to_review:{m['project_uid']}:T{number[t]}" in yes)
        else:
            ok = m["desk_item"] is None and m["approved_head"] is None
        if not ok:
            bad.append((m["project_uid"], number[t]))
    ck("an L3 merge names who approved which sha, an L4 merge has no approval", not bad,
       str(bad[:5]))

    # Home has something to show
    open_stages = {t["stage"] or "todo" for t in db["tickets"]} - {"merged"}
    ck("work is open now: todo, doing, review, changes and blocked",
       open_stages == {"todo", "doing", "review", "changes", "blocked"}, str(open_stages))
    waiting = [d for d in db["desk_items"] if d["state"] == "open"]
    ck("open questions wait, one blocking and one not, and a merge asks for a yes",
       {bool(d["blocking"]) for d in waiting} == {True, False}
       and any(m["state"] == "waiting_approval" for m in db["merge_queue"]), str(waiting))


def made_up_checks(db):
    ck("only the cell's own person", [p["id"] for p in db["people"]] == ["local"],
       str(db["people"]))
    text = db["text"]
    emails = [e for e in re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}", text)
              if not e.endswith("@example.com")]
    ck("no real email address in any row", not emails, str(emails[:5]))
    repos = set(re.findall(r"github\.com/([\w-]+)/", text))
    repos |= {json.loads(t["pr"])["repo"].split("/")[0] for t in db["tickets"] if t["pr"]}
    repos |= {x["repo"].split("/")[0]
              for t in ("tickets", "merge_queue", "project_sessions") for x in db[t] if x["repo"]}
    ck("every repository belongs to example-org", repos == {"example-org"}, str(repos))
    with open(SCRIPT, encoding="utf-8") as f:
        ck("no em dash in the seed", "\u2014" not in f.read())


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return str(s.getsockname()[1])


def daemon_checks(data_dir):
    port = free_port()
    env = clean_env(TARES_DB=os.path.join(data_dir, "tares.duckdb"), TARES_PORT=port,
                    TARES_PR_SYNC_SECONDS="off", TARES_OTLP_GRPC_PORT="off",
                    ANTHROPIC_API_KEY=None, ANTHROPIC_AUTH_TOKEN=None, OPENAI_API_KEY=None,
                    TARES_STATE_DSN=None, TARES_EDITION=None)
    log = os.path.join(data_dir, "daemon.log")
    base = f"http://127.0.0.1:{port}"
    with open(log, "w") as out:
        proc = subprocess.Popen(
            [sys.executable, "-c", "from tares.cli import run_daemon; run_daemon()"],
            env=env, stdout=out, stderr=subprocess.STDOUT)
        try:
            up = False
            for _ in range(160):
                if proc.poll() is not None:
                    break
                try:
                    if httpx.get(f"{base}/health", timeout=1).json().get("status") == "ok":
                        up = True
                        break
                except (httpx.HTTPError, ValueError):
                    pass
                time.sleep(0.25)
            ck("a daemon starts on the seeded dir", up, open(log).read()[-800:])
            if not up:
                return
            home = httpx.get(f"{base}/api/home", timeout=30).json()
            shown = {p["name"] for p in home.get("projects") or []}
            ck("Home is the factory's and lists the five projects",
               home.get("edition") == "factory" and shown == set(NAMES),
               f"{home.get('edition')} {shown}")
            waiting = {w["kind"] for w in home.get("waiting_for_you") or []}
            ck("Home has things waiting for the person", "question" in waiting, str(waiting))
            crew = home.get("crew") or {}
            roles = [m.get("role") for m in crew.get("members") or []]
            ck("Home shows the crew running, with its five builders",
               crew.get("running") is True and str(crew.get("summary")).startswith("Running")
               and roles.count("builder") == 5, f"{crew.get('summary')} {roles}")
            ck("and how far it goes on its own matches the sample crew",
               (crew.get("how_far") or {}).get("autonomy") == "L4-ship", str(crew.get("how_far")))
            split = {}
            for p in home.get("projects") or []:
                rows = httpx.get(f"{base}/api/projects/{p['id']}/repos", timeout=30).json()["repos"]
                if len(rows) != 1 or not rows[0]["tickets"] or not rows[0]["merged_prs"]:
                    split[p["name"]] = [(r["name"], len(r["tickets"])) for r in rows]
            ck("each project page lists one repository, with its tickets", not split, str(split))
            ck("the first-run card is out of the way",
               (home.get("start") or {}).get("show") is False, str(home.get("start")))
            answered = httpx.get(f"{base}/api/desk/answered", timeout=30).json()
            ck("the console counts the answered questions", answered.get("count", 0) > 20,
               str(answered))

            again = run_seed(data_dir)
            ck("seeding while the daemon runs fails with a plain message",
               again.returncode == 2 and "stop it and run the seed again" in again.stderr
               and "Traceback" not in again.stdout + again.stderr,
               f"{again.returncode} {again.stderr[:600]}")

            sources = "".join(open(f, encoding="utf-8").read() for f in glob.glob(
                os.path.join(ROOT, "tares", "**", "*.py"), recursive=True))
            if '"/api/factory/report"' not in sources:
                print("  skip the Report read: there is no GET /api/factory/report yet (TR-1667)")
            else:
                report = httpx.get(f"{base}/api/factory/report", timeout=60)
                ck("GET /api/factory/report answers on the seeded cell",
                   report.status_code == 200 and any(n in report.text for n in NAMES),
                   f"{report.status_code} {report.text[:300]}")
        finally:
            proc.terminate()            # only the daemon started here
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)


def twice_checks(data_dir):
    """The first run and the second. Returns what the first run wrote: (loaded rows, row counts,
    when it had finished), or None when it failed."""
    print("a fresh data dir, seeded twice")
    began = time.monotonic()
    one = run_seed(data_dir)
    took = time.monotonic() - began
    ck("the seed runs", one.returncode == 0, one.stdout[-800:] + one.stderr[-1500:])
    if one.returncode != 0:
        return None
    ck("it takes under a minute", took < 60, f"{took:.1f}s")
    ck("it prints the five projects", all(n in one.stdout for n in NAMES), one.stdout)
    after = datetime.now(timezone.utc)
    db = load(data_dir)
    first = snapshot(data_dir)
    two = run_seed(data_dir)
    ck("a second run says there is nothing to add",
       two.returncode == 0 and "Nothing to add" in two.stdout, two.stdout + two.stderr[-800:])
    second = snapshot(data_dir)
    ck("a second run leaves every row count as it was", counts(first) == counts(second),
       str(differing(first, second)))
    ck("and changes no row", first == second, str(differing(first, second)))
    return db, counts(first), after


def content_checks(db, after):
    print("what the Report reads")
    week_checks(db, "today")
    history_checks(db, "today", after)
    shape_checks(db)
    made_up_checks(db)


def edge_checks(root):
    mod = load_script()
    this_monday = monday(datetime.now(timezone.utc))
    for label, at in (("just after a week began", this_monday - timedelta(weeks=1, minutes=-5)),
                      ("just before a week ended", this_monday - timedelta(minutes=5))):
        print(f"seeded {label}")
        edge = os.path.join(root, label.replace(" ", "-"))
        mod.now_utc = lambda at=at: at
        code, said = seed_here(mod, edge)
        ck(f"{label}: the seed runs", code == 0, said[-800:])
        if code == 0:
            then = load(edge)
            week_checks(then, label)
            # a store function may stamp its own bookkeeping with the real time
            history_checks(then, label, datetime.now(timezone.utc))


def half_checks(root, wanted):
    print("a run that stopped half way")
    half = os.path.join(root, "half")
    mod = load_script()
    whole = mod.Seeder.accept

    def stop_at_the_third(self, p, milestones):
        if p["uid"] == MOBILE:
            raise KeyboardInterrupt
        whole(self, p, milestones)

    mod.Seeder.accept = stop_at_the_third
    try:
        seed_here(mod, half)
        stopped = False
    except KeyboardInterrupt:
        stopped = True
    partial = counts(snapshot(half))
    ck("the run stopped with part of the rows written",
       stopped and 0 < partial.get("tickets", 0) < wanted["tickets"], str(partial.get("tickets")))
    mod.Seeder.accept = whole
    code, said = seed_here(mod, half)
    again = counts(snapshot(half))
    ck("the next run writes it again from the start", code == 0 and "Wrote 8 weeks" in said,
       said[-800:])
    exact = ("usecases", "milestones", "milestone_acceptances", "milestone_check_results",
             "people", "crews", "session_stations", "project_sessions", "handovers",
             "ticket_numbers")
    ck("with no row twice", all(again.get(t) == wanted.get(t) for t in exact),
       str({t: (wanted.get(t), again.get(t)) for t in exact if again.get(t) != wanted.get(t)}))
    # the current week holds its share of the time passed, so a later run may hold a ticket more
    grown = ("tickets", "ticket_history", "ticket_checks", "reviews", "review_findings",
             "desk_items", "answers", "decisions", "assumptions", "merge_queue", "session_usage",
             "session_states")
    far = {t: (wanted[t], again[t]) for t in grown
           if not wanted[t] * 0.98 - 2 <= again[t] <= wanted[t] * 1.02 + 12}
    ck("and the same history", not far, str(far))
    numbers = defaultdict(list)
    for t in load(half)["tickets"]:
        numbers[t["project"]].append(t["number"])
    ck("ticket numbers start at one again",
       all(sorted(v) == list(range(1, len(v) + 1)) for v in numbers.values()),
       str({k: (min(v), max(v), len(v)) for k, v in numbers.items()}))


def other_projects_checks(root):
    print("a data dir with other projects")
    theirs = os.path.join(root, "theirs")
    os.makedirs(theirs)
    with opened(theirs) as s:
        s.create_project("uc_other", "custom", "Somebody Else", {})
    before = snapshot(theirs)
    refused = run_seed(theirs)
    ck("it is refused, with the project named",
       refused.returncode == 1 and "Somebody Else" in refused.stderr
       and "--into-existing" in refused.stderr and "Traceback" not in refused.stderr,
       f"{refused.returncode} {refused.stderr[-600:]}")
    after = snapshot(theirs)
    ck("and nothing is written", after == before, str(differing(before, after)))
    beside = run_seed(theirs, "--into-existing")
    ck("--into-existing writes beside it", beside.returncode == 0, beside.stderr[-1200:])
    with opened(theirs) as s:
        names = {p["name"] for p in s.list_projects()}
        settings = [s.get_setting(k) for k in ("edition", "crew_settings", "start_hidden:console")]
    ck("the other project is still there", names >= set(NAMES) | {"Somebody Else"}, str(names))
    ck("and the cell's edition, crew choices and first-run card are left alone",
       settings == [None, None, None], str(settings))


def main():
    root = tempfile.mkdtemp(prefix="tares-report-seed-")
    fresh = os.path.join(root, "fresh")
    wrote = twice_checks(fresh)
    if not wrote:
        return
    db, wanted, after = wrote
    content_checks(db, after)
    edge_checks(root)
    half_checks(root, wanted)
    other_projects_checks(root)
    print("a daemon on the seeded dir")
    daemon_checks(fresh)


if __name__ == "__main__":
    main()
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)
