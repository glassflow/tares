# Tares enterprise licence, see ee/LICENSE
"""Every route the console calls when it opens a page is held to a 300 ms time target (TR-1607, TR-1729).

The cell is the full-size factory seed (`factory_seed.seed`, TR-1604): the row counts of the real
factory cell. One person who signs in with a key reads each route in ROUTES in process
(`httpx.ASGITransport`, a frozen clock, a licence). A route gets one warm-up call, then 5 timed calls,
and the in-process caches in CACHES are cleared before each of them, so a call is the first read of
a page after a quiet minute, which is what a person opening it sees. The median of the 5 is the
route's time.

Which routes. ROUTES holds every GET the factory console makes (the pages of ui/src/editions/routes.tsx
for the factory edition, the shell around them, and what those pages open). NOT_TIMED holds the GETs
that only an older page or another edition makes, or that another server answers, each with the reason;
they are printed.
Nothing checks these two lists against ui/src: a PR that adds a GET to the console adds it here.
A route must answer 200 with the content the page shows, so an empty or error answer is never timed
as fast. What some routes need before they have content is made in `fixtures`. GitHub, Slack and the
brain are faked (`fake_services`), and any attempt to reach another machine fails the test
(`guard_network`), so the times are the cell's own and hold none of theirs.

The rule. A route fails when its median is over TARGET (0.3 s) and OVER does not list it. No slack and
no allowance for a slow machine apply to that. A route that is over TARGET today sits in OVER with the
slowest median that was measured and the ticket that brings it down. Its limit is
`its OVER median * SLACK[backend] * min(SPEED, LIMIT_SPEED_CAP[backend])`. SPEED is how much slower this
machine is than the one the OVER medians were taken on (never under 1): a fixed workload (`calibrate`)
is timed three times at the start and the fastest counts. An OVER entry without a ticket id fails, on
either backend. An entry is kept close to what the route takes now, because the limit is built on it. A
PR that moves a time updates OVER and says why; the PR that merges second rebases and keeps both.

An entry whose route now reads well under TARGET is printed as a note, not a failure: the runners
differ too much to tell a fixed route from a fast machine (on CI the same Postgres route has read from
156 to 398 ms). The ticket that brings a route down removes its entry.

Load does not decide the answer (TR-1640):
- A route that is over its limit is timed again, up to ROUNDS rounds, and keeps its best median: a busy
  moment passes on the next round, a route that is really slower fails every round.
- Before each route, and again before each extra round, the test samples a CPU probe (`calibrate`) and a
  database probe (`db_probe`). If the CPU samples of the run differ by more than BUSY times, or the database
  samples by more than DB_BUSY times, the machine was busy: a route that is over its limit is then not
  called slow, and the test fails once, saying the machine was too busy to measure. Run it again when the
  machine is quiet. A database round trip varies by about 2 times on a quiet machine (a container here, a
  service on CI), so its limit is wider than the CPU's.

Run: .venv/bin/python ee/tests/test_factory_page_times.py
Postgres: TARES_TEST_PG=postgresql://... PYTHONPATH=tests/pg_harness .venv/bin/python ee/tests/test_factory_page_times.py
"""
import asyncio
import json
import os
import re
import socket
import statistics
import sys
import tempfile
import time
from datetime import timedelta, timezone
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import licensed   # scrubs the machine's TARES_* settings and puts the repo and tests/ on sys.path
licensed.install_test_root()
sys.path.insert(0, licensed.REPO)

import factory_seed as fs
import fake_lore
from _timing import SLACK   # tests/_timing.py (TR-1731)

TARGET = 0.300                                       # seconds, the median of a route
# SLACK (imported above) is how much the median of an OVER route may exceed its entry, per backend.
# Postgres gets more: a round trip to the database (a container here, a service on CI) varies from run
# to run in a way `calibrate()` cannot see. A route with no OVER entry gets none of this: its limit is TARGET.
LIMIT_SPEED_CAP = {"duckdb": 1.33, "postgres": 1.11}  # the most SPEED widens an OVER route's limit
RUNS = 5                                             # timed calls per route, after one warm-up
CALIBRATION_REF = 0.021                               # seconds `calibrate()` takes on the machine the OVER medians come from
SPEED_TRIES = 3                                       # `calibrate()` runs at the start; the fastest sets SPEED
ROUNDS = 3                                            # a route over its limit is timed again, up to this many rounds
STALE_SPEED_CAP = 1.25                                # the note on an old entry never divides by more than this
BUSY = 1.5                                            # CPU probe samples that differ by more than this mean a busy machine
DB_BUSY = 4.0                                         # database probe samples that differ by more than this do (a quiet one varies about 2 times)
DB_PROBE_FLOOR = 0.010                                # seconds; below this a database probe says nothing (in-process DuckDB)

# Routes over TARGET today, by backend: (the slowest median in seconds that was measured, the ticket
# that brings it down). A route absent from here must meet TARGET.
OVER = {
    "duckdb": {},
    "postgres": {
        "project": (0.418, "TR-1727"),
        "project_tickets": (0.448, "TR-1727"),
        "tickets": (0.527, "TR-1727"),
        "tickets_list": (0.527, "TR-1727"),
        "project_big": (0.424, "TR-1727"),
        "projects_list": (0.318, "TR-1727"),
    },
}

# What each route is, where the console asks for it, and what its answer must hold for the time to
# count (the answer must be 200, and the function must be true of its JSON). The `{...}` names are
# filled from `fixtures`. Paths under ui/src.
ROUTES = [
    # the shell, on every page (editions/edition.tsx, licence.tsx, cloud.ts, components/AuthGate.tsx,
    # editions/Shell.tsx)
    ("whoami", "/api/whoami", lambda d: d["person"]["name"] == "Sam" and d["edition"] == "factory"),
    ("licence", "/api/licence", lambda d: d["valid"] and d["ee_allowed"]),
    ("health", "/health", lambda d: d["status"] == "ok"),
    ("capabilities", "/api/capabilities", lambda d: d["version"] and d["slack_configured"]),
    ("brain", "/api/brain", lambda d: d["configured"] and d["repos"]),
    ("setup_progress", "/api/factory/setup/progress", lambda d: d["total"] > 0 and d["projects"] > 0),
    # Home (editions/factory/FactoryHome.api.ts)
    ("home", "/api/home", lambda d: d["projects"] and d["waiting_for_you"] and d["crew"]["members"]),
    ("answers", "/api/answers", lambda d: d["answers"]),
    ("answer_item", "/api/answers?item={item}", lambda d: d["answers"]),
    # Projects and one project (FactoryProjects.api.ts, FactoryProject.api.ts, ProjectRoute.tsx,
    # ProjectShell.tsx, OtherProject.tsx, FactoryMergeQueue.api.ts, FactoryDocs.api.ts)
    ("projects", "/api/factory/projects", lambda d: d["projects"]),
    ("project", "/api/factory/projects/{p}", lambda d: d["counts"]["total"] and d["moving"] and d["waiting_others"]),
    ("project_big", "/api/factory/projects/{bp}", lambda d: d["counts"]["total"] and d["moving"] and d["waiting_others"]),
    ("milestone_summary", "/api/projects/{mp}/milestones/{m}/summary", lambda d: d["summary"]["text"]),
    ("other_project_summary", "/api/projects/{other}/summary", lambda d: d["name"]),
    ("project_github", "/api/factory/projects/{p}/github", lambda d: d["repos"] and d["counts"]["open"]),
    ("project_merge_queue", "/api/projects/{p}/merge-queue", lambda d: d["groups"]),
    ("project_docs", "/api/factory/projects/{p}/docs", lambda d: d["total"] and d["doc"]["lines"]),
    ("project_doc_open", "/api/factory/projects/{p}/docs?doc={pdoc}", lambda d: d["doc"]["kind"] == "plan" and d["doc"]["lines"]),
    ("project_tickets", "/api/factory/tickets?project={p}&sort=open&limit=200", lambda d: d["tickets"] and d["total"]),
    # A ticket and the ticket lists (FactoryTicket.api.ts, FactoryTickets.api.ts)
    ("ticket", "/api/factory/projects/{p}/tickets/{t}", lambda d: d["title"] and d["reviews"]),
    ("ticket_big", "/api/factory/projects/{bp}/tickets/{bt}", lambda d: d["reviews"] and d["history"]),
    ("tickets", "/api/factory/tickets?sort=open&limit=200", lambda d: len(d["tickets"]) == 200),
    ("tickets_list", "/api/factory/tickets?limit=25", lambda d: len(d["tickets"]) == 25),
    # Reviews, Report, Crew, Docs, People (FactoryReviews.api.ts, FactoryReport.api.ts,
    # FactoryCrew.api.ts, FactoryDocs.api.ts, FactoryPeople.api.ts)
    ("reviews", "/api/factory/reviews", lambda d: d["with_the_crew"]),
    ("report", "/api/factory/report?scope=factory&from={from}&to={to}", lambda d: d["range"]["merged"] and d["sections"]["delivered"]),
    ("crew", "/api/factory/crew", lambda d: d["talk_to"] and d["reviewer"]),
    ("crew_member", "/api/crew/{station}?recent=0&conversation=200&crew={crew}", lambda d: d["lines"] and d["conversation"]),
    ("crew_tool", "/api/crew/{station}/tool/{tool}?crew={crew}", lambda d: d["output"]),
    ("docs", "/api/factory/docs", lambda d: d["shared"] and d["projects"]),
    ("doc", "/api/factory/docs/{doc}", lambda d: d["doc"]["lines"]),
    ("people", "/api/factory/people", lambda d: d["people"]),
    # the Brain pages (BrainConnect.api.ts, BrainFolders.api.ts, BrainDecisions.api.ts)
    ("brain_available", "/api/brain/repos/available", lambda d: d["github"] and d["repos"]),
    ("brain_available_folders", "/api/brain/repos/available/{repo}/folders", lambda d: d["suggested"]),
    ("brain_folders", "/api/brain/repos/{repo}/folders", lambda d: d["folders"]),
    ("brain_updates", "/api/brain/repos/{repo}/updates", lambda d: d["updates"]),
    ("brain_decisions", "/api/brain/decisions?limit=25", lambda d: len(d["decisions"]) == 25),
    # Settings and its tabs (FactorySettings.api.ts, editions/settings/*Panel*.tsx,
    # components/UsagePanels.tsx, ClaudeUsage.tsx, SlackPick.tsx, ee/settings/AreasPanel.tsx)
    ("settings", "/api/factory/settings", lambda d: d["tools"] and d["crew"]),
    ("people_list", "/api/people", lambda d: d["people"]),
    ("settings_providers", "/api/settings/providers", lambda d: d["providers"]),
    ("settings_usage", "/api/usage", lambda d: d["events"]),
    ("settings_usage_model", "/api/usage/model?days=30", lambda d: d["total"]["calls"] and d["days"]),
    ("settings_claude_usage", "/api/claude-code/usage?days=7", lambda d: d["projects"]),
    ("settings_keys", "/api/keys", lambda d: d["keys"]),
    ("projects_list", "/api/projects", lambda d: d["projects"]),               # the keys tab (ApiKeysPanel.tsx)
    ("areas", "/api/areas", lambda d: d[0]["repo"]),
    ("slack_channels", "/api/slack/channels", lambda d: d["channels"]),        # MorningStatus.tsx
    # Setup and Integrations (FactorySetup.api.ts, FactorySetup.tsx, editions/settings/GithubPanel.tsx,
    # SlackPanels.tsx)
    ("setup", "/api/factory/setup", lambda d: d["steps"]),
    ("setup_github_repos", "/api/factory/setup/github/repos?via=app", lambda d: d["repos"] and not d["error"]),
    ("github_integrations", "/api/integrations/github", lambda d: d["credentials"]),
    ("github_install_link", "/api/integrations/github/{app}/install", lambda d: d["url"].startswith("https://github.com/apps/")),
    ("me_slack", "/api/me/slack", lambda d: d["connected"]),
    ("slack_bot_token", "/api/settings/slack-bot-token", lambda d: d["configured"]),
    ("slack_signing_secret", "/api/settings/slack-signing-secret", lambda d: d["configured"]),
]

_OLDER = "only an older page asks for it: "
_GONE = "gone with Tares agents, Ask and triggers (TR-1810); the console stops asking in its own ticket"
_NO_CATALOG = "gone with the source catalog (TR-1811); the console stops asking in its own ticket"
# The GETs the build of the console can make that are not timed, and why.
NOT_TIMED = [
    ("/api/ask/sessions", _GONE),
    ("/api/ask/sessions/{id}", _GONE),
    ("/api/triggers", _GONE),
    ("/api/settings/agents", _GONE),
    ("/api/settings/tracing", _GONE),
    ("/api/connectors", _NO_CATALOG),
    ("/api/sources/{name}", _NO_CATALOG),
    ("/api/settings/mcp", _NO_CATALOG),
    ("/api/sources", _OLDER + "Sources, Connect Claude Code, Export, Resources, project setup"),
    ("/api/sources/{name}/events", _OLDER + "one source's page"),
    ("/api/sources/{name}/fields", _OLDER + "the source form, project setup"),
    ("/api/catalog/dependents", _OLDER + "one source's page"),
    ("/api/catalog/export", _OLDER + "Export"),
    ("/api/discover/environment", _OLDER + "Discover and the source form"),
    ("/api/entities", _OLDER + "Explore"),
    ("/api/resources", _OLDER + "Resources, project setup"),
    ("/api/agents/builtin", _OLDER + "Resources, project setup, old agent links"),
    ("/api/agents/builtin/{name}/runs", _OLDER + "the try step of project setup"),
    ("/api/mcp-servers", _OLDER + "Resources, project setup"),
    ("/api/mcp/tools", _OLDER + "Connect"),
    ("/api/activity/queries", _OLDER + "Reads"),
    ("/api/activity/dispatches/{id}", _OLDER + "an old /dispatches link"),
    ("/api/integrations/github/{name}/repos", _OLDER + "the source form"),
    ("/api/setup/github-suggestion", _OLDER + "project setup"),
    ("/api/skills", _OLDER + "project setup"),
    ("/api/projects/{id}/setup", _OLDER + "project setup, where a draft project goes"),
    ("/api/projects/{id}/health", _OLDER + "project setup"),
    ("/api/projects/{id}/outline", _OLDER + "project setup"),
    ("/api/projects/{id}/results", _OLDER + "project setup"),
    ("/api/projects/{id}/results/{run}", _OLDER + "project setup"),
    ("/api/projects/{id}/docs", _OLDER + "project setup"),
    ("/api/projects/{id}/skills", _OLDER + "project setup"),
    ("/api/projects/{id}/skills/{name}", _OLDER + "project setup"),
    ("/api/projects/{id}/tickets", _OLDER + "project setup"),
    ("/api/projects/{id}", _OLDER + "the older project setup forms"),
    ("/api/projects/templates", "gone with project templates (TR-1812); the console stops asking in its own ticket"),
    ("/api/projects/{id}/assumptions", _OLDER + "the older project page"),
    ("/api/projects/{id}/crew", _OLDER + "the older project page"),
    ("/api/projects/{id}/decisions", _OLDER + "the older project page"),
    ("/api/projects/{id}/docs/{doc}", _OLDER + "the older project page"),
    ("/api/projects/{id}/external-agents", _OLDER + "the older project page and its keys"),
    ("/api/projects/{id}/keys", _OLDER + "the older project page's keys"),
    ("/api/projects/{id}/milestones", _OLDER + "the older project page"),
    ("/api/projects/{id}/repos", _OLDER + "the older project page"),
    ("/api/projects/{id}/sessions", _OLDER + "the older project page"),
    ("/api/projects/{id}/sessions/{session}", _OLDER + "the older project page"),
    ("/api/projects/{id}/tickets/{ref}", _OLDER + "the older project page"),
    ("/api/projects/{id}/tickets/{ref}/messages", _OLDER + "the older project page"),
    ("/api/projects/{id}/timeline", _OLDER + "the older project page"),
    ("/api/grants", _OLDER + "the older project page"),
    ("/api/docs/personal", _OLDER + "the older project page"),
    ("/api/agents", _OLDER + "the older project page"),
    ("/api/activity/dispatches", _OLDER + "the older project page"),
    ("/api/crew", _OLDER + "the older Crew page"),
    ("/api/desk", _OLDER + "the older Crew page"),
    ("/api/review-queue", _OLDER + "the older Crew page"),
    ("/api/rule-proposals", _OLDER + "the older Crew page"),
    ("/api/integrations/github/{name}/tree", _OLDER + "the older shared context setup"),
    ("/api/linear", _OLDER + "the older Settings page"),
    ("/api/linear/projects", _OLDER + "the older project page"),
    ("/api/settings/edition", _OLDER + "the older Settings page"),
    ("/api/sre/incidents", "only a page of the AI SRE edition asks for it"),
    ("/api/sre/incidents/{id}", "only a page of the AI SRE edition asks for it"),
    ("/api/sre/runbooks", "only a page of the AI SRE edition asks for it"),
    ("/api/sre/services", "only a page of the AI SRE edition asks for it"),
    ("/api/sre/services/{name}", "only a page of the AI SRE edition asks for it"),
    ("/api/sre/settings", "only a page of the AI SRE edition asks for it"),
    ("/api/sre/timeline", "only a page of the AI SRE edition asks for it"),
    ("/api/sre/timeline/raw", "only a page of the AI SRE edition asks for it"),
    ("/api/projects/{id}/questions", "only the project page of the platform edition asks for it"),
    ("the control plane's workspaces, overview and members addresses", "Tares Cloud answers them, not the cell"),
    ("the MCP address (/mcp)", _OLDER + "Connect probes it; the MCP server answers it, not this API"),
]

# ── the services outside the cell ──
OUTSIDE: list = []                                    # hosts the run tried to reach on another machine
GITHUB_REPOS: list = []                               # what the fake GitHub lists; filled by `fixtures`
APP_CREDENTIAL = "times-app"
TREE = ([{"path": "docs", "type": "tree"}, {"path": "docs/adr", "type": "tree"}]
        + [{"path": f"docs/adr/{k:04d}-a-decision.md", "type": "blob"} for k in range(1, 41)]
        + [{"path": f"src/module_{k}.py", "type": "blob"} for k in range(400)])


def guard_network() -> None:
    """From here on, a connection or a name lookup for another machine fails and is recorded."""
    allowed_hosts = {"localhost", "::1", "0.0.0.0", urlsplit(os.environ.get("TARES_TEST_PG", "")).hostname}

    def check(host):
        host = host.decode() if isinstance(host, bytes) else host
        if host and host not in allowed_hosts and not str(host).startswith("127."):
            OUTSIDE.append(host)
            raise OSError(f"the page time test does not use the network: {host}")

    real_info, real_connect, real_connect_ex = socket.getaddrinfo, socket.socket.connect, socket.socket.connect_ex

    def getaddrinfo(host, *a, **kw):
        check(host)
        return real_info(host, *a, **kw)

    def connect(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            check(address[0])
        return real_connect(self, address)

    def connect_ex(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            check(address[0])
        return real_connect_ex(self, address)

    socket.getaddrinfo, socket.socket.connect, socket.socket.connect_ex = getaddrinfo, connect, connect_ex


def fake_services(lore) -> None:
    """GitHub, Slack and the brain answer from here, and the loops that would write to them do nothing.
    Call before `make_app`."""
    from tares import factory_github, github_app, github_credentials, slack, waiting_slack
    from tares_ee.brain import decisions, decisions_repo, index, repos, sync

    async def list_repos(*a, **kw):
        return [dict(r) for r in GITHUB_REPOS]

    async def git_tree(*a, **kw):
        return {"entries": TREE, "truncated": False}

    async def get_token(*a, **kw):
        return "not-a-token"

    async def list_channels(token, *a, **kw):
        return [{"id": f"C{k:04d}", "name": f"team-{k}", "is_private": False, "is_member": True}
                for k in range(60)], None, None, None

    async def read_pr(store, repo, number, prev=None):    # a ticket read asks for its own pull request
        return {**(prev or {}), "repo": repo, "number": number,
                "checked_at": factory_github.now_utc().isoformat()}

    async def compare(*a, **kw):
        return {"ok": True, "behind_by": 0}

    async def nothing(self, *a, **kw):
        return None

    async def never(self):
        await asyncio.Event().wait()

    github_credentials.list_repos = list_repos
    github_credentials.git_tree = git_tree
    github_credentials.get_token = get_token
    factory_github.read_pr = read_pr
    factory_github.compare = compare
    github_app.installation_repos = list_repos
    slack.list_channels = list_channels
    waiting_slack.WaitingSlack.read_once = nothing
    repos.lore_client = lore.client
    index.Indexer.run = never
    sync.Syncer.run = never
    decisions.Decisions.run = never
    decisions_repo.RepoDecisions.startup = nothing
    os.environ["TARES_BRAIN_URL"] = fake_lore.BASE_URL
    os.environ["TARES_BRAIN_KEY"] = lore.key


async def fixtures(cx, made: dict) -> dict:
    """What some routes need to have content, added after the seed, and the ids the paths name.
    `cx` reads and writes as Sam."""
    from tares import report, slack_verify
    from tares.slack import resolve_token
    import tares.daemon as daemon

    async def get(path):
        r = await cx.get(path)
        assert r.status_code == 200, f"{path} {r.status_code} {r.text[:300]}"
        return r.json()

    p, station, crew = made["anchor_project"], made["station"], made["crew_id"]
    ids = {"p": p, "t": made["anchor_ticket"], "bp": made["big_project"], "bt": made["big_ticket"],
           "station": station, "crew": crew, "app": APP_CREDENTIAL}

    # Claude Code usage by hour: the app built these totals while the cell was still empty and the seed
    # writes its replies past them, so every "this week" read would be empty. Build them again.
    from sqlalchemy import delete
    from tares import claude_usage_hourly
    from tares.session_store import tables
    with STORE.sessions_db.begin() as c:
        c.execute(delete(tables.session_rollup_state).where(
            tables.session_rollup_state.c.name == claude_usage_hourly.NAME))
        c.execute(delete(tables.session_usage_hourly))
    STORE._usage_hourly_ready = False
    claude_usage_hourly.fill(STORE)

    # GitHub: a connected App (the seed's connections name api.github.com, which the setup form skips)
    STORE.upsert_github_credential(APP_CREDENTIAL, "", "app", "", "example-org", config={
        "slug": "tares-times", "app_id": "1",
        "installations": [{"id": 1, "account": "example-org", "repository_selection": "all"}]})
    connected = [r["repo"] for r in STORE.list_brain_repos()]
    GITHUB_REPOS.extend({"full_name": name, "default_branch": "main", "private": True,
                         "pushed_at": "2026-11-20T10:00:00Z"} for name in [*connected, "example-org/not-connected"])

    # the brain: one connected repo with a decision folder, and decisions added by hand
    ids["repo"] = connected[0]
    STORE.set_brain_folders(ids["repo"], ["docs/adr"], None, fs.NOW)
    for k in range(30):
        r = await cx.post("/api/brain/decisions", json={"title": f"Decision {k}",
                                                        "text": f"We chose option {k}. It is simpler to run."})
        assert r.status_code == 201, r.text[:300]

    # Slack: a bot token and a signing secret
    STORE.set_setting(daemon.SLACK_TOKEN_SETTING, "xoxb-" + "not-a-token")
    STORE.set_setting(slack_verify.SETTING_KEY, "not-a-" + "signing-secret")
    assert resolve_token(STORE)[0], "the fake Slack token does not resolve"

    # the report's weeks, which a background pass works out on a running cell
    report.rebuild_all(STORE)
    to = fs.NOW.astimezone(timezone.utc).date()
    ids["from"], ids["to"] = (to - timedelta(weeks=8)).isoformat(), to.isoformat()

    # an accepted milestone with its summary written (the seed accepts none)
    ms = (await get(f"/api/projects/{p}/milestones"))["milestones"][0]
    me = (await get("/api/whoami"))["person"]
    STORE.accept_milestone(p, ms["id"], me["name"], me["id"], fs.NOW)
    STORE.put_report_text("milestone", ms["id"], "The first pages are live. " * 8, "template",
                          {"delivered": [], "decisions": [], "left": []}, fs.NOW, True)
    ids["mp"], ids["m"] = p, ms["id"]

    # answers given today (the seed's are older): Home lists them, and a link names one
    for k in range(12):
        STORE.add_answer(f"question:times-{k}", "question", p, "Who owns the retry limit?", "yes", "yes",
                         "console", me["id"], fs.NOW - timedelta(minutes=10 * k))
    ids["item"] = "question:times-0"

    # who owns each connected repo (Settings, the areas panel)
    r = await cx.put("/api/areas", json=[{"repo": name, "person_id": me["id"], "note": "owns it"}
                                         for name in connected])
    assert r.status_code == 200, r.text[:300]

    # a project the factory pages do not have, which the project address shows another way
    ids["other"] = None
    for row in (await get("/api/projects"))["projects"]:
        if row["status"] != "draft" and (await cx.get(f"/api/factory/projects/{row['id']}")).status_code == 404:
            ids["other"] = row["id"]
            break
    if ids["other"] is None:
        r = await cx.post("/api/projects", json={"template": "custom", "name": "Times other", "goal": "Watch it."})
        assert r.status_code in (200, 201), r.text[:300]
        ids["other"] = r.json()["id"]

    # the plan of the project, opened from its Docs page and from the page of all documents (not the
    # document either page shows by itself)
    docs = (await get(f"/api/factory/projects/{p}/docs"))["groups"][0]["docs"]
    ids["pdoc"] = ids["doc"] = next(d["id"] for d in docs if d["kind"] == "plan")

    # one tool call of the station's current session, as a card on the Crew page opens it. The seed's
    # lines are not the shape the conversation makes a card of, so the id comes from the stored lines
    session = (await get(f"/api/crew/{station}?recent=0&crew={crew}"))["session"]
    results = [r for r in STORE.key_payload_rows("claude_code", session, limit=1000) if r["event_type"] == "tool_result"]
    payload = results[-1]["payload"]
    payload = json.loads(payload) if isinstance(payload, str) else payload
    ids["tool"] = payload["message"]["content"][0]["tool_use_id"]
    return ids


def caches() -> list:
    """Every in-process cache a page read can hit, as (name, the function that empties it). The home
    cache is `factory_home`'s; the sessions' caught-up flag is a flag on the store that the next read
    sets again from `session_ingest_marks`; the 7-day usage keep exists once TR-1627 has merged."""
    from tares import factory_home as fh
    out = [("factory_home.clear_cache", fh.clear_cache)]
    out.append(("store._sessions_caught_up", lambda: setattr(STORE, "_sessions_caught_up", False)))
    try:
        from tares import claude_usage
        if hasattr(claude_usage, "clear_week_by_project"):
            out.append(("claude_usage.clear_week_by_project", claude_usage.clear_week_by_project))
    except ImportError:
        pass
    from tares import github_credentials as gc   # the repo list GitHub gave, kept 5 minutes per credential
    out.append(("github_credentials._repos_cache", gc._repos_cache.clear))
    return out


CACHES: list = []
STORE = None


def _licence() -> str:
    kit = licensed.kit
    root_priv, _ = kit.keypair(kit.ROOT_PHRASE)
    iss_priv, iss_pub = kit.keypair(kit.ISSUER_PHRASE)
    cert = kit.issuer_cert(root_priv, iss_pub, "2099-01-01")
    return kit.sign(iss_priv, cert, "Test Org", 5, ("enterprise",), "2020-01-01T00:00:00Z",
                    "2099-01-01", "lic_ee_page_times")


os.environ["TZ"] = "UTC"
time.tzset()
_TMP = tempfile.mkdtemp(prefix="tares-page-times-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_LICENSE"] = _licence()
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-page-times"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_EDITION"] = "factory"

import httpx

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def calibrate() -> float:
    """Seconds a fixed workload takes here (the median of 7 runs): sorting, hashing and JSON, the kind
    of work a page read does in Python. It does not touch the database or the cell."""
    import hashlib
    import json
    times = []
    for _ in range(7):
        t = time.perf_counter()
        rows = [{"id": i, "name": f"row-{i * 7919 % 10007}", "n": (i * 31) % 977} for i in range(20000)]
        rows.sort(key=lambda r: (r["name"], r["n"]))
        json.loads(json.dumps(rows))
        h = hashlib.sha256()
        for r in rows[:5000]:
            h.update(r["name"].encode())
        times.append(time.perf_counter() - t)
    return statistics.median(times)


def db_probe() -> float:
    """Seconds 100 trivial statements take on the state database: a round trip to Postgres, or a call
    into DuckDB. It tells a busy database from a busy CPU; it never sets a limit."""
    t = time.perf_counter()
    for _ in range(100):
        STORE.sc.execute("SELECT 1").fetchall()
    return time.perf_counter() - t


def probe() -> tuple:
    return calibrate(), max(db_probe(), DB_PROBE_FLOOR)


def spread(values) -> float:
    return max(values) / min(values)


SPEED = 1.0   # set in main(): how many times slower than the OVER machine this one is, never under 1


def allowed(over: dict, name: str, backend: str, speed: float) -> float:
    """The most a route's median may be. With no OVER entry it is TARGET, on any machine. With one it
    is the entry (never under TARGET) times the backend's slack and the machine's speed, which counts
    up to the backend's cap."""
    if name not in over:
        return TARGET
    return max(TARGET, over[name][0]) * SLACK[backend] * min(speed, LIMIT_SPEED_CAP[backend])


def check_lists() -> None:
    """ROUTES and OVER are well formed, for both backends, whichever one this run uses."""
    names = [r[0] for r in ROUTES]
    ck("ROUTES is not empty and names each route once", bool(names) and len(set(names)) == len(names),
       str(sorted({n for n in names if names.count(n) > 1})))
    ck("OVER has one list per backend", set(OVER) == set(SLACK), str(sorted(OVER)))
    for b, entries in OVER.items():
        for name, entry in entries.items():
            median, ticket = entry if len(entry) == 2 else (None, None)
            ck(f"the OVER entry of {name} ({b}) names a ticket",
               isinstance(ticket, str) and re.fullmatch(r"TR-[0-9]+", ticket) is not None,
               f"the entry is {entry!r}: it needs a ticket id like TR-1")
            ck(f"the OVER entry of {name} ({b}) names a route in ROUTES", name in names, "no such route")
            ck(f"the OVER entry of {name} ({b}) is over the target",
               isinstance(median, (int, float)) and median > TARGET, f"the entry is {entry!r}")
    # a route with no OVER entry is held to the target itself: no slack, no allowance for the machine
    ck("a route with no OVER entry is held to exactly the target at any speed",
       all(allowed({}, "x", b, speed) == TARGET for b in SLACK for speed in (1.0, 1000.0)))


def answers_with_content(resp, has) -> str | None:
    """None when the answer is 200 and holds what the page shows; otherwise what is wrong with it."""
    if resp.status_code != 200:
        return f"status {resp.status_code}: {resp.text[:200]}"
    try:
        if has(resp.json()):
            return None
    except Exception as e:
        return f"not the expected content ({type(e).__name__}: {e}): {resp.text[:200]}"
    return f"not the expected content: {resp.text[:200]}"


async def main():
    global STORE, CACHES, SPEED
    check_lists()
    guard_network()
    lore = fake_lore.FakeLore()
    fake_services(lore)
    from tares.daemon import make_app
    fs.freeze_clock()
    app = make_app()
    STORE = app.state.store
    backend = "postgres" if os.environ.get("TARES_TEST_PG") else "duckdb"
    over = OVER[backend]
    slack = SLACK[backend]
    samples: list = []                                 # (cpu, database) probes taken during the run
    cal = min(calibrate() for _ in range(SPEED_TRIES))
    SPEED = max(1.0, cal / CALIBRATION_REF)
    print(f"calibration {cal * 1000:.0f} ms (reference {CALIBRATION_REF * 1000:.0f} ms): speed {SPEED:.2f}, "
          f"limits times {min(SPEED, LIMIT_SPEED_CAP[backend]):.2f}")
    root = {"Authorization": f"Bearer {TOKEN}"}
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=root,
                                     timeout=120) as cx:
            r = await cx.post("/api/keys", json={"name": "sam-laptop", "scopes": ["admin"],
                                                 "person": {"email": "sam-times@example.com", "name": "Sam"}})
            assert r.status_code in (200, 201), r.text
            sam = {"Authorization": f"Bearer {r.json()['secret']}"}
        print(f"== seeding ({backend}) ==")
        made = fs.seed(STORE, log=lambda *_: None)
        CACHES = caches()
        print("caches cleared before each timed call: " + ", ".join(n for n, _ in CACHES))
        results = {}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=sam,
                                     timeout=300) as cx:
            ids = await fixtures(cx, made)
            for name, template, has in ROUTES:
                path = template.format(**ids)
                for _, clear in CACHES:
                    clear()
                samples.append(probe())
                bad = answers_with_content(await cx.get(path), has)
                best = None
                for rnd in range(ROUNDS):
                    times = []
                    for _ in range(RUNS):
                        for _, clear in CACHES:
                            clear()
                        t = time.perf_counter()
                        resp = await cx.get(path)
                        times.append(time.perf_counter() - t)
                        bad = bad or answers_with_content(resp, has)
                    if not times:
                        break                                # no sample: the route is reported below
                    med = statistics.median(times)
                    best = med if best is None else min(best, med)
                    if bad or best <= allowed(over, name, backend, SPEED):
                        break
                    samples.append(probe())              # over its limit: the machine may have been busy, time it again
                if best is not None:
                    results[name] = (best, bad, path, template)
        samples.append(probe())
        busy = spread([c for c, _ in samples]) > BUSY or spread([d for _, d in samples]) > DB_BUSY
        print(f"probes: cpu {min(c for c, _ in samples) * 1000:.0f} to {max(c for c, _ in samples) * 1000:.0f} ms, "
              f"database {min(d for _, d in samples) * 1000:.0f} to {max(d for _, d in samples) * 1000:.0f} ms "
              f"({len(samples)} samples): {'BUSY' if busy else 'quiet'}")
        print(f"\n== medians ({backend}, target {TARGET * 1000:.0f} ms, slack {slack}, speed {SPEED:.2f}) ==")
        print(f"  {'route':<24} {'median ms':>10} {'allowed ms':>11}  {'status':<22} request")
        for name, (med, bad, path, template) in results.items():
            lim = allowed(over, name, backend, SPEED)
            status = "ok" if med <= lim and not bad else "FAIL"
            if med > lim and busy and not bad:
                status = "not measured: busy"
            if name in over:
                status += f" (over, {over[name][1]})"
            print(f"  {name:<24} {med * 1000:>10.0f} {lim * 1000:>11.0f}  {status:<22} GET {template}")
        for route, why in NOT_TIMED:
            print(f"  not timed: GET {route}, {why}")
        print()
        unmeasured: list = []
        ck("every route in ROUTES was timed", len(results) == len(ROUTES),
           "no sample for " + ", ".join(r[0] for r in ROUTES if r[0] not in results))
        for name, (med, bad, path, template) in results.items():
            lim = allowed(over, name, backend, SPEED)
            ck(f"{name} answers 200 with content", bad is None, f"{path} {bad}")
            if med > lim and busy and not bad:
                unmeasured.append(f"{name} {med * 1000:.0f} ms")
                continue
            why = f"limit of its OVER entry, {over[name][1]}" if name in over else "it has no OVER entry"
            ck(f"{name} median {med * 1000:.0f} ms is within {lim * 1000:.0f} ms", med <= lim,
               f"GET {path} took {med * 1000:.0f} ms: {why}")
        for name, (med, bad, path, template) in results.items():
            # a note, not a failure: the runners differ too much to tell a fixed route from a fast one.
            # The median is divided by SPEED so a slower machine does not hide a fixed route
            scaled = med / min(SPEED, STALE_SPEED_CAP)
            if name in over and scaled < TARGET / slack:
                print(f"  note: the OVER entry of {name} ({over[name][1]}) may be stale: {scaled * 1000:.0f} ms "
                      f"here is under {TARGET / slack * 1000:.0f} ms")
        ck("the machine was quiet enough to time every route", not unmeasured,
           f"the machine was too busy to measure ({', '.join(sorted(set(unmeasured)))}): the CPU probe ran "
           f"{spread([c for c, _ in samples]):.2f} times apart and the database probe "
           f"{spread([d for _, d in samples]):.2f} times apart during the run; run it again when the machine is quiet")
        ck("no call tried to reach another machine", not OUTSIDE, ", ".join(sorted(set(OUTSIDE))))
    print(f"\n{P} passed, {F} failed")
    return F


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(main()) else 0)
