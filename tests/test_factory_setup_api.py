"""GET /api/factory/setup: everything the "Set up your factory" screen shows, in one read (TR-871).

One in-process cell with auth on and fake enterprise integrations installed under the names the
screen's copy knows (Jira with a status mapping, CodeRabbit, Qodo, Cognee, a policy webhook).
Covered:
 - admin only: a read key, a person's key, a project key and a station get 403; the route reads
   and writes nothing (the connections, defaults, picks and pairs are the same after every call);
 - a fresh cell without a licence: seven steps in order, the built-in choice connected on each
   with its Use button, progress "2 of 10 done" (CI and runtime; the crew, GitHub and the terminal
   are counted too), what is installed but needs a licence says so in plain words;
 - the steps setup leaves out (HIDDEN_STEPS, HIDE_WITHOUT_CHOICE): the rest of this file shows all
   seven, so every step stays covered, and one section checks the leaving out;
 - keeping the default: PUT /api/factory/setup/{slot}/keep sets the step, only for the default;
 - a filled cell: defaults, who picks what, the actions and the question asked before a removal,
   forms with saved values and a secret that is only ever "saved", a Jira status mapping,
   a guardrail that checks use cannot be removed, one connection for monitoring, memory and
   runtime;
 - the licence ended: the "Licence ended" tag, and the banner on Tickets when the crew really falls
   back to the built-in choice;
 - Tares Cloud: the edition, the app block on Linear and GitHub only where the connect address is
   set, the state after the app is connected, a row connected through the app; none of it on a
   self-hosted cell;
 - many projects and long names;
 - no secret value in any response, no em dash, no internal name in a shown string;
 - last_test is null: nothing stores a connection test.

Run: python tests/test_factory_setup_api.py
"""
import asyncio
import json
import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
sys.path.insert(0, ROOT)
from _env import scrub_os_environ
scrub_os_environ()

_TMP = tempfile.mkdtemp(prefix="tares-factory-setup-api-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-factory-setup"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
for _k in ("TARES_WORKSPACE_API_URL", "TARES_LINEAR_CONNECT_URL", "TARES_GITHUB_CONNECT_URL"):
    os.environ.pop(_k, None)

import httpx

from tares import licence, linear
from tares.factory_store import PROJECT_SLOTS
from tares.screens import factory_setup as fs
from tares.integrations import registry
from tares.integrations.guardrails import Guardrail
from tares.integrations.memory import MemoryStore
from tares.integrations.review import ReviewGate
from tares.integrations.tickets import TicketBackend
from tares.integrations.types import Field

# Setup leaves some steps out (monitoring until it is tested, memory and guardrails while they offer
# no choice). This file checks every step, so it shows all of them; "== steps left out ==" puts the
# real lists back and checks them.
HIDDEN, HIDE_WITHOUT = fs.HIDDEN_STEPS, fs.HIDE_WITHOUT_CHOICE
fs.HIDDEN_STEPS, fs.HIDE_WITHOUT_CHOICE = (), ()

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


STAGES = ["todo", "in_progress", "in_review", "done"]


class Jira(TicketBackend):
    name = "jira"
    title = "Jira"
    ee = True
    settings_schema = [Field("site", "Site", "url", required=True),
                       Field("email", "Email", "text", required=True),
                       Field("token", "Token", "secret", required=True),
                       Field("status", "Statuses", "mapping", options=STAGES)]


class CodeRabbit(ReviewGate):
    name = "coderabbit"
    title = "CodeRabbit"
    ee = True
    settings_schema = [Field("key", "Key", "secret", required=True)]


class Qodo(ReviewGate):
    name = "qodo"
    title = "Qodo"
    ee = True
    settings_schema = [Field("key", "Key", "secret", required=True)]


class Cognee(MemoryStore):
    name = "cognee"
    title = "Cognee"
    ee = True
    settings_schema = [Field("url", "Address", "url", required=True), Field("key", "Key", "secret")]


class Webhook(Guardrail):
    name = "webhook"
    title = "Policy webhook"
    ee = True
    settings_schema = [Field("url", "Address", "url", required=True),
                       Field("secret", "Secret", "secret")]


registry.load()
for cls in (Jira, CodeRabbit, Qodo, Cognee, Webhook):
    registry._loaded[(cls.slot, cls.name)] = cls

licensed = [False]
licence.ee_allowed = lambda feature="enterprise": licensed[0]
REAL_CURRENT = licence.current

ALL = []     # every response text of the run, searched for secret values at the end
SECRETS = {"jira": "placeholder-not-real-jira", "linear": "placeholder-not-real-linear",
           "rabbit": "placeholder-not-real-rabbit", "cognee": "placeholder-not-real-cognee",
           "otlp": "placeholder-not-real-otlp", "hook": "placeholder-not-real-hook",
           "app": "placeholder-not-real-app", "gh": "placeholder-not-real-gh"}

# what a person is shown is searched for these; the status label is the one place the approved copy
# says "mapping"
INTERNAL = re.compile(r"schema|\bslot\b|TARES_|api_key|settings_schema|integration_connections", re.I)
ID_KEYS = {"slot", "id", "key", "kind", "uid", "state", "mode", "stage", "url", "edition"}


def shown(node, out=None):
    """The strings a person reads: every string value except the ids the console passes back."""
    out = [] if out is None else out
    if isinstance(node, dict):
        for k, v in node.items():
            if k in ID_KEYS or (k == "name" and "kind" in node):
                continue
            shown(v, out)
    elif isinstance(node, list):
        for v in node:
            shown(v, out)
    elif isinstance(node, str):
        out.append(node)
    return out


def snapshot(store):
    """What the route must leave alone."""
    rows = {}
    for slot in registry.SLOTS:
        rows[slot] = (store.get_choice(slot),
                      [(c["name"], store.stored_settings(slot, c["name"])) for c in store.list_connections(slot)])
    return json.dumps([rows, store.list_guardrail_pairs(),
                       [(p["id"], [store.get_project_choice(p["id"], s) for s in ("tickets", "review", "ci")])
                        for p in store.list_projects()],
                       [c["name"] for c in store.list_github_credentials()]], sort_keys=True, default=str)


def step(body, slot):
    return next(s for s in body["steps"] if s["slot"] == slot)


def conn(st, name):
    return next((c for c in st["connected"] if c["id"] == name), None)


def avail(st, name):
    return next((c for c in st["available"] if c["id"] == name), None)


def field(form, name):
    return next(f for f in form["fields"] if f["name"] == name)


async def main():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store

    def mk(token=TOKEN, station=None, role="builder"):
        hdr = {"Authorization": f"Bearer {token}"}
        if station:
            hdr.update({"X-Tares-Station": station, "X-Tares-Role": role})
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=hdr)

    async with app.router.lifespan_context(app):
        dana = store.create_person("Dana Whitfield", email="dana@northwind.example")
        async with mk() as root:
            async def get():
                r = await root.get("/api/factory/setup")
                ALL.append(r.text)
                return r

            async def key(name, scopes, person=None, project=None):
                body = {"name": name, "scopes": scopes}
                if person:
                    body["person_id"] = person["id"]
                if project:
                    body["project"] = project
                return (await root.post("/api/keys", json=body)).json()["secret"]

            reader = await key("reader", ["read"])
            dana_k = await key("dana", ["read", "ingest"], dana)

            print("== who may read it ==")
            for who, k in (("a read key", reader), ("a person's ingest key", dana_k)):
                async with mk(k) as cx:
                    r = await cx.get("/api/factory/setup")
                    ck(f"{who} gets 403", r.status_code == 403, r.status_code)
            async with mk(station="northwind-build-1", role="builder") as cx:
                r = await cx.get("/api/factory/setup")
                ck("a station gets 403", r.status_code == 403, r.status_code)
            async with mk("not-a-token") as cx:
                ck("a wrong token gets 401", (await cx.get("/api/factory/setup")).status_code == 401)

            print("== a fresh cell, no licence ==")
            before = snapshot(store)
            r = await get()
            ck("an admin reads it", r.status_code == 200, r.text[:300])
            body = r.json()
            ck("nothing was written by the read", snapshot(store) == before)
            ck("the top-level keys", sorted(body) == ["edition", "exit_label", "licence", "onboarding", "progress", "steps",
                                                 "terminal", "title", "tools"],
               sorted(body))
            ck("title and exit label", body["title"] == "Set up your factory" and body["exit_label"] == "Exit setup")
            ck("a self-hosted cell", body["edition"] == {"key": "self_hosted", "name": "Self-hosted", "cloud": False},
               body["edition"])
            ck("no licence says so", body["licence"]["state"] == "none" and body["licence"]["ended_on"] is None
               and body["licence"]["text"] == "This cell has no licence.", body["licence"])
            ck("seven steps in order", [s["slot"] for s in body["steps"]] == [
                "tickets", "review", "ci", "memory", "monitoring", "guardrails", "runtime"]
               and [s["number"] for s in body["steps"]] == list(range(1, 8)))
            ck("steps are named and say why", [s["name"] for s in body["steps"]] == [
                "Tickets", "Review", "CI", "Memory", "Monitoring", "Guardrails", "Runtime"]
               and step(body, "review")["why"] == "Who reviews each pull request before it can merge.")
            ck("only CI and the runtime count as set: nothing to choose there", [s["state"] for s in body["steps"]] == [
                "not_set", "not_set", "set", "not_set", "not_set", "not_set", "set"]
               and body["progress"] == {"set": 2, "total": 10, "text": "2 of 10 done"}, body["progress"])
            ck("the crew, GitHub and the terminal come from the home",
               body["onboarding"]["choices"]["chosen"] is False and body["onboarding"]["github"]["connected"] is False
               and [t["key"] for t in body["terminal"]] == ["install", "login", "crew"]
               and body["tools"] == {"github": {"connected": False, "detail": "Not connected"},
                                     "slack": {"connected": False, "detail": "Not connected"}}, body["tools"])
            tix = step(body, "tickets")
            ck("the step line", tix["line"] == "Tares (not set yet)" and step(body, "runtime")["line"] == "Claude Code",
               (tix["line"], step(body, "runtime")["line"]))
            ck("tickets: Tares connected as the built-in default",
               [c["id"] for c in tix["connected"]] == ["tares"]
               and tix["connected"][0]["default"] and tix["connected"][0]["built_in"]
               and [t["text"] for t in tix["connected"][0]["tags"]] == ["Default", "Built in"]
               and tix["connected"][0]["description"] == "Tickets live in Tares."
               and tix["connected"][0]["actions"] == [{"key": "keep", "label": "Use Tares"}]
               and tix["connected"][0]["form"] is None
               and tix["connected"][0]["confirm"] is None and tix["connected"][0]["last_test"] is None,
               tix["connected"])
            ck("headings", tix["connected_heading"] == "Connected (1)" and tix["available_heading"] == "Connect another")
            ck("the use line with no project", tix["connected"][0]["use"] == "Projects use it unless they pick another.",
               tix["connected"][0]["use"])
            ck("tickets: what could be connected, in install order, Jira last",
               [a["id"] for a in tix["available"]] == ["linear", "github", "jira"], [a["id"] for a in tix["available"]])
            jira = avail(tix, "jira")
            ck("Jira needs a licence and says why in plain words",
               jira["enterprise"] is True and jira["allowed"] is False and jira["form"] is None and jira["app"] is None
               and jira["reason"] == "This cell has no licence. Without it, Jira cannot be connected. Your crew "
                                     "keeps using the built-in choice for this step."
               and jira["description"].startswith("Tickets live in a Jira project."), jira)
            lin = avail(tix, "linear")
            ck("Linear can be connected, with a form", lin["allowed"] and lin["reason"] is None and lin["button"] == "Connect"
               and lin["app"] is None and lin["form"]["mode"] == "connect" and lin["form"]["title"] == "Connect Linear"
               and lin["form"]["save_label"] == "Save and connect" and lin["form"]["save_as_default"] is True
               and lin["form"]["lead"].endswith("It becomes the default."), lin["form"])
            lf = field(lin["form"], "api_key")
            ck("the Linear key field has its words and no value",
               lf["label"] == "API key" and lf["kind"] == "secret" and lf["requirement"] == "Optional"
               and lf["error"] == "Enter the API key." and lf["saved"] is False and "value" not in lf
               and lf["help"].startswith("A Linear personal API key."), lf)
            gh = field(avail(tix, "github")["form"], "repo")
            ck("the GitHub repository field", gh["placeholder"] == "owner/name" and gh["value"] == ""
               and gh["error"] == "Enter the repository as owner/name, for example northwind/orders-api."
               and gh["picker"] == "github_repo"
               and gh["picker_help"] == "The repository that holds the issues. Choose it from the list, or type it as owner/name."
               and gh["picker_placeholder"] == "Choose or type a repository"
               and gh["picker_loading"] == "Loading your repositories from GitHub."
               and gh["picker_more"] == "{n} more. Type to narrow the list.", gh)
            ck("only the GitHub repository field is picked from a list",
               field(lin["form"], "api_key")["picker"] == "" and field(avail(tix, "github")["form"], "issue_label")["picker"] == "")
            ck("the form's test hint", lin["form"]["test_hint"] == "Test connection checks the settings above with "
                                                                "Linear. Nothing is saved by it.")
            ck("tickets: no banner and no note", tix["banner"] is None and tix["note"] is None)
            ck("tickets: footer", tix["footer"] == {"hint": "", "next": "Next: Review"}, tix["footer"])
            ck("the last step finishes the setup", step(body, "runtime")["footer"]["next"] == "Finish setup")
            gd = step(body, "guardrails")
            ck("guardrails: the note points at the Guardrails page", gd["note"] == (
                "Your own rules and the checks asked before a build, a merge or a release are on the factory "
                "Guardrails page, not here."))
            ck("guardrails: the webhook needs a licence", avail(gd, "webhook")["allowed"] is False
               and avail(gd, "webhook")["description"] == "Ask your own policy service before an action.")
            mon = step(body, "monitoring")
            ck("monitoring: Nothing is sent is connected, Rius and OTLP can be", [c["id"] for c in mon["connected"]] == ["off"]
               and mon["connected"][0]["description"] == "Nothing is sent."
               and mon["connected"][0]["actions"] == [{"key": "keep", "label": "Keep it off"}]
               and [a["id"] for a in mon["available"]] == ["rius", "otlp"], (mon["connected"], mon["available"]))
            ck("monitoring: the Rius form leads with how it is replaced",
               avail(mon, "rius")["form"]["lead"].endswith("It replaces Off."), avail(mon, "rius")["form"]["lead"])
            ck("runtime: nothing left to connect, and no line saying so", step(body, "runtime")["available"] == []
               and "available_empty" not in step(body, "runtime"))
            ck("a fresh cell has no app block", all(a["app"] is None for a in tix["available"]))

            print("== a filled cell ==")
            licensed[0] = True
            for uid, nm in (("pr1", "orders-api"), ("pr2", "billing-service"), ("pr3", "storefront"),
                            ("pr4", "warehouse-sync"), ("pr5", "mobile-app")):
                store.create_project(uid, "custom", nm, {})
                store.set_project_kind(uid, "software_factory")
            store.set_choice("tickets", "linear", {"api_key": SECRETS["linear"]}, "Dana Whitfield")
            store.set_choice("tickets", "github", {"repo": "northwind/orders-api"}, "Dana Whitfield", default=False)
            store.set_choice("tickets", "jira", {"site": "https://northwind.atlassian.net", "email": "dana@northwind.example",
                                                 "token": SECRETS["jira"],
                                                 "status": {"todo": "To Do", "in_progress": "In Progress",
                                                            "in_review": "In Review", "done": "Done"}},
                             "Dana Whitfield", default=False)
            store.set_project_choice("pr1", "tickets", "linear", {}, "Dana Whitfield")
            store.set_project_choice("pr2", "tickets", "github", {}, "Dana Whitfield")
            store.set_project_choice("pr4", "tickets", "github", {}, "Dana Whitfield")
            store.set_choice("review", "coderabbit", {"key": SECRETS["rabbit"]}, "Dana Whitfield")
            store.set_choice("memory", "cognee", {"url": "https://cognee.northwind.example", "key": SECRETS["cognee"]},
                             "Dana Whitfield")
            store.set_choice("monitoring", "otlp", {"endpoint": "https://collector.northwind.example:4318",
                                                    "api_key": SECRETS["otlp"]}, "Dana Whitfield")
            store.set_choice("guardrails", "webhook", {"url": "https://policy.northwind.example/check",
                                                       "secret": SECRETS["hook"]}, "Dana Whitfield", default=False)
            store.save_guardrail_pair("factory", None, "pre_merge", "webhook", "block")
            licence.current(store, force=True)
            before = snapshot(store)
            r = await get()
            body = r.json()
            ck("the read changed nothing", snapshot(store) == before)
            tix = step(body, "tickets")
            ck("the Jira connected rows are in install order, Linear the default",
               [c["id"] for c in tix["connected"]] == ["tares", "linear", "github", "jira"]
               and [c["id"] for c in tix["connected"] if c["default"]] == ["linear"], [c["id"] for c in tix["connected"]])
            ck("tickets is set and the line counts the rest", tix["state"] == "set" and tix["line"] == "Linear and 3 more"
               and tix["connected_heading"] == "Connected (4)", (tix["line"], tix["state"]))
            lin, gh, jira = conn(tix, "linear"), conn(tix, "github"), conn(tix, "jira")
            tl = registry.get("tickets", "github").title
            ck("who picks Linear and who follows it", lin["picked_by"] == [{"uid": "pr1", "name": "orders-api"}]
               and lin["follows"] == 2 and lin["use"] == "Used by 3 projects; 2 picked another.",
               (lin["picked_by"], lin["follows"], lin["use"]))
            ck("who picks GitHub", [p["name"] for p in gh["picked_by"]] == ["billing-service", "warehouse-sync"]
               and gh["use"] == "Used by billing-service and warehouse-sync." and gh["follows"] == 0, gh["use"])
            ck("a connection no project picks", jira["use"] == "No project uses it yet." and jira["picked_by"] == [],
               jira["use"])
            ck("the default has no make default and no remove", [a["key"] for a in lin["actions"]] == ["edit"]
               and lin["confirm"] is None, lin["actions"])
            ck("another has edit, make default and remove", [a["label"] for a in gh["actions"]] == [
                "Edit settings", "Make default", "Remove"], gh["actions"])
            ck("the question before a removal names the projects who move",
               gh["confirm"] == {"title": f"Remove {tl}", "yes": "Remove it", "no": "Keep it", "moves_projects": True,
                                 "text": f"2 projects pick {tl}: billing-service, warehouse-sync. They move to the "
                                         f"default, Linear. The saved settings of {tl} are deleted."}, gh["confirm"])
            ck("one that nobody picks says only what is deleted", jira["confirm"]["text"] == "The saved settings of Jira are "
                                                                                    "deleted." and not jira["confirm"]["moves_projects"],
               jira["confirm"])
            ck("tags on Jira", [t["key"] for t in jira["tags"]] == [], jira["tags"])
            ck("the licence block of a cell with no key", body["licence"] == {
                "state": "none", "ended_on": None, "text": "This cell has no licence."}, body["licence"])
            jf = jira["form"]
            ck("the Jira form is an edit", jf["mode"] == "edit" and jf["title"] == "Edit Jira" and jf["save_label"] == "Save"
               and jf["save_as_default"] is False and jf["lead"] == (
                   "Tickets live in a Jira project. Works with Jira Cloud and Jira Data Center. Projects that pick it "
                   "use the new settings from the next time they read them."), jf["lead"])
            ck("Jira site, email and token words", field(jf, "site")["label"] == "Site address"
               and field(jf, "site")["placeholder"] == "https://northwind.atlassian.net"
               and field(jf, "site")["value"] == "https://northwind.atlassian.net"
               and field(jf, "site")["requirement"] == "Required" and field(jf, "email")["label"] == "Email"
               and field(jf, "email")["value"] == "dana@northwind.example"
               and field(jf, "token")["label"] == "API token", [f["label"] for f in jf["fields"]])
            tf = field(jf, "token")
            ck("a saved secret is only saved", tf["saved"] is True and "value" not in tf
               and tf["placeholder"] == "Saved. Leave blank to keep it." and tf["saved_note"] == (
                   "A key is saved. Leave this blank to keep it."), tf)
            sf = field(jf, "status")
            ck("the status mapping has a row per stage with its saved value", sf["label"] == "Status mapping"
               and sf["help"] == "Match each Tares stage to the Jira status that means it."
               and [(x["key"], x["label"], x["value"]) for x in sf["rows"]] == [
                   ("todo", "To do", "To Do"), ("in_progress", "In progress", "In Progress"),
                   ("in_review", "In review", "In Review"), ("done", "Done", "Done")]
               and sf["rows"][1]["aria_label"] == "Jira status for In progress", sf["rows"])
            ck("a form's fields all carry the words", all(f["label"] and f["requirement"] in ("Required", "Optional")
                                                         for fm in (jf, gh["form"], lin["form"]) for f in fm["fields"]))
            rv = step(body, "review")
            cr = conn(rv, "coderabbit")
            ck("review: CodeRabbit is the default, Tares can be made the default",
               cr["default"] and cr["description"] == "CodeRabbit reviews each pull request."
               and conn(rv, "tares")["actions"] == [{"key": "make_default", "label": "Make default"}]
               and [a["id"] for a in rv["available"]] == ["qodo"] and rv["line"] == "CodeRabbit and 1 more",
               (rv["line"], [a["id"] for a in rv["available"]]))
            ck("review: connecting Qodo says where it goes",
               avail(rv, "qodo")["form"]["lead"] == "Qodo reviews each pull request. It is added next to Tares reviewer and "
                                                    "CodeRabbit. The default stays CodeRabbit.", avail(rv, "qodo")["form"]["lead"])
            ck("a project that follows the review default is counted", cr["follows"] == 5 and
               cr["use"] == "Used by 5 projects.", cr["use"])
            mem = step(body, "memory")
            ck("memory: one connection, no make default or remove", [c["id"] for c in mem["connected"]] == ["cognee"]
               and [a["key"] for a in mem["connected"][0]["actions"]] == ["edit"]
               and [a["id"] for a in mem["available"]] == ["tares"] and mem["state"] == "set", mem["connected"])
            ck("memory: the address field", field(mem["connected"][0]["form"], "url")["value"] ==
               "https://cognee.northwind.example" and field(mem["connected"][0]["form"], "key")["saved"] is True)
            mon = step(body, "monitoring")
            ck("monitoring: OTLP is the one connection", [c["id"] for c in mon["connected"]] == ["otlp"]
               and mon["connected"][0]["use"] == "Every project uses it." and mon["state"] == "set"
               and [a["id"] for a in mon["available"]] == ["off", "rius"], (mon["connected"], mon["available"]))
            ck("monitoring: the OTLP address", field(mon["connected"][0]["form"], "endpoint")["value"] ==
               "https://collector.northwind.example:4318"
               and field(mon["connected"][0]["form"], "endpoint")["placeholder"] ==
               "https://collector.northwind.example:4318")
            gd = step(body, "guardrails")
            wh = conn(gd, "webhook")
            ck("guardrails: a webhook a check uses cannot be removed", wh is not None and wh["use"] == "1 check uses it."
               and "remove" not in [a["key"] for a in wh["actions"]] and wh["blocked"] == {
                   "key": "remove", "text": "Remove its checks on the Guardrails page first."} and wh["confirm"] is None, wh)
            ck("guardrails: the built-in rules are the default", conn(gd, "tares")["default"] is True)
            ck("progress counts the steps with a choice, out of all ten", body["progress"]["set"] == 7 and
               body["progress"]["text"] == "7 of 10 done", body["progress"])
            ck("every last_test is null", all(c["last_test"] is None for s in body["steps"] for c in s["connected"]))
            ci = step(body, "ci")
            ck("ci: chosen per project while nothing is chosen at the factory, GitHub Actions can be connected",
               [c["id"] for c in ci["connected"]] == ["local"] and ci["connected"][0]["title"] == "Automatic"
               and ci["connected"][0]["use"] == ""
               and ci["line"] == "GitHub Actions for GitHub repos, local checks otherwise"
               and [a["id"] for a in ci["available"]] == ["github"] and avail(ci, "github")["form"]["fields"] == []
               and avail(ci, "github")["form"]["lead"] == "Reads check results from GitHub. It becomes the default "
                                                         "for every project, also one without a repo on GitHub.",
               (ci["line"], ci["connected"][0]["title"], avail(ci, "github")["form"]))

            print("== the licence ended ==")
            expired = lambda store_, now=None, force=False: {
                "valid": True, "expired": True, "expires": "2026-10-31", "seats": 10, "used": 1, "features": ["enterprise"]}
            licence.current = expired
            r = await get()
            body = r.json()
            tix = step(body, "tickets")
            ck("the state and the day", body["licence"] == {"state": "ended", "ended_on": "31 October",
                                                           "text": "Your licence ended on 31 October."}, body["licence"])
            ck("an ee row carries the tag while the backend still lets it work",
               "Licence ended" in [t["text"] for t in conn(tix, "jira")["tags"]]
               and "Licence ended" in [t["text"] for t in conn(step(body, "review"), "coderabbit")["tags"]]
               and "Licence ended" not in [t["text"] for t in conn(tix, "linear")["tags"]])
            ck("no banner while the crew still uses Jira", tix["banner"] is None)
            store.set_choice("tickets", "jira", {}, "Dana Whitfield")        # Jira becomes the default
            licensed[0] = False                                               # and the backend falls back
            r = await get()
            body = r.json()
            tix = step(body, "tickets")
            ck("the banner says what the crew does now", tix["banner"] == {
                "lead": "Your licence ended on 31 October.",
                "text": "Your licence ended on 31 October. Jira is still connected, but the crew now keeps its tickets "
                        "in Tares until you renew. Your Jira settings are kept."}, tix["banner"])
            ck("a licence that ended names the day where something cannot be connected",
               "Your licence ended on 31 October. Without it, Qodo cannot be connected. Your crew keeps using the "
               "built-in choice for this step." == avail(step(body, "review"), "qodo")["reason"],
               avail(step(body, "review"), "qodo")["reason"])
            ck("the row is not allowed", avail(step(body, "review"), "qodo")["allowed"] is False
               and avail(step(body, "review"), "qodo")["form"] is None)
            ck("the Jira row still has its settings", conn(tix, "jira")["form"] is not None)
            licence.current = REAL_CURRENT
            store.set_choice("tickets", "linear", {}, "Dana Whitfield")       # Linear is the default again
            licensed[0] = True

            print("== Tares Cloud ==")
            r = await get()
            tix = step(r.json(), "tickets")
            ck("self-hosted: no app on Linear or GitHub even when connected", all(
                x["via"] is None for x in tix["connected"]) and avail(tix, "linear") is None)
            os.environ["TARES_WORKSPACE_API_URL"] = "http://workspace.example/api"
            r = await get()
            body = r.json()
            tix = step(body, "tickets")
            ck("cloud without a connect address: still no app block", body["edition"] == {
                "key": "cloud", "name": "Tares Cloud", "cloud": True} and body["licence"]["text"] == ""
               and all(x["via"] is None for x in tix["connected"]), body["edition"])
            # fresh set of available rows: drop Linear and GitHub so they show as available
            store.clear_project_choice("pr2", "tickets")
            store.clear_project_choice("pr4", "tickets")
            for n in ("github", "jira"):
                store.delete_connection("tickets", n)
            r = await get()
            ck("the read still works after the removals", r.status_code == 200, r.text[:200])
            os.environ["TARES_LINEAR_CONNECT_URL"] = "https://connect.northwind.example/linear"
            os.environ["TARES_GITHUB_CONNECT_URL"] = "https://connect.northwind.example/github"
            r = await get()
            tix = step(r.json(), "tickets")
            gha = avail(tix, "github")
            ck("cloud: GitHub offers the app", gha is not None and gha["button"] == "Connect GitHub"
               and gha["app"]["stage"] == "start" and gha["app"]["app_name"] == "GlassFlow GitHub App"
               and gha["app"]["repo_step"] is True and len(gha["app"]["steps"]) == 3
               and gha["app"]["use_key_label"] == "Use a GitHub credential instead"
               and gha["app"]["use_app_label"] == "Connect with the GitHub App instead"
               and gha["app"]["url"] == "https://connect.northwind.example/github"
               and gha["app"]["done"] is None, gha)
            ck("cloud: the connect button needs no key", conn(tix, "linear")["via"] == "key"
               and "API key" in [t["text"] for t in conn(tix, "linear")["tags"]], conn(tix, "linear")["tags"])
            ck("cloud: the Licence words are not shown", all("licence" not in s.lower() for s in shown(r.json())),
               [s for s in shown(r.json()) if "licence" in s.lower()][:3])
            # Linear connected through the app
            linear.set_cloud_connection(store, {"token": SECRETS["app"], "expires_at": 4102444800,
                                                "account": {"name": "Dana Whitfield", "org": "Northwind",
                                                            "email": "dana@northwind.example"}})
            store.set_choice("tickets", "linear", {"api_key": None}, "Dana Whitfield")   # null clears the key
            r = await get()
            tix = step(r.json(), "tickets")
            lin = conn(tix, "linear")
            ck("cloud: Linear through the app", lin["via"] == "app" and lin["who"] == "as Dana Whitfield, workspace Northwind"
               and lin["description"] == "Linear is connected through the GlassFlow Linear app as Dana Whitfield, "
                                         "workspace Northwind."
               and "Connected with the app" in [t["text"] for t in lin["tags"]]
               and "edit" not in [a["key"] for a in lin["actions"]] and lin["form"] is None
               and lin["use"].endswith("To disconnect it, make another integration the default first."), lin)
            store.set_choice("tickets", "tares", {}, "Dana Whitfield")        # another default
            r = await get()
            lin = conn(step(r.json(), "tickets"), "linear")
            ck("cloud: a non-default app row can be disconnected", [a["label"] for a in lin["actions"]] == [
                "Make default", "Disconnect"] and lin["confirm"]["yes"] == "Disconnect it"
               and lin["confirm"]["text"] == (
                   "1 project picks Linear: orders-api. They move to the default, Tares. Tares takes the access "
                   "away in Linear and deletes the settings of Linear.")
               and lin["confirm"]["title"] == "Disconnect Linear", lin["confirm"])
            # GitHub through the app
            store.upsert_github_credential("glassflow-github-app", SECRETS["gh"], kind="app_broker", account="northwind",
                                           config={"installation_id": 4242, "account_type": "Organization"})
            r = await get()
            gha = avail(step(r.json(), "tickets"), "github")
            ck("cloud: the GitHub app block after approval", gha["app"]["stage"] == "done" and gha["app"]["done"] == (
                "GitHub is connected. The GlassFlow GitHub App is installed on the northwind organisation."), gha["app"])

            print("== the repositories the GitHub Issues form offers ==")
            from tares import github_credentials as ghc
            from tares.screens import factory_setup as fsmod
            listed = {"glassflow-github-app": ["northwind/orders-api", "northwind/Billing"],
                      "dana-token": ["dana/notes", "northwind/orders-api"], "ghe": ["corp/secret"]}
            asked = []
            slow = asyncio.Event()

            async def fake_list(cred, query=""):
                asked.append(cred["name"])
                if cred["name"] == "broken":
                    raise ValueError("GitHub refused the credential broken: Bad credentials")
                if cred["name"] == "slow":
                    await slow.wait()
                    return [{"full_name": "slow/repo"}]
                return [{"full_name": n} for n in listed[cred["name"]]]
            real_list, ghc.list_repos_for = ghc.list_repos_for, fake_list
            real_budget, fsmod.REPOS_BUDGET = fsmod.REPOS_BUDGET, 0.3

            async def repos(q=""):
                return (await root.get("/api/factory/setup/github/repos" + q)).json()
            try:
                store.upsert_github_credential("dana-token", "ghp_" + "d" * 36)
                store.upsert_github_credential("ghe", "ghp_" + "e" * 36, api_url="https://ghe.northwind.example/api/v3")
                r = await repos()
                ck("with the app: the app's repos, sorted, and no other credential is asked",
                   r == {"repos": ["northwind/Billing", "northwind/orders-api"], "error": "", "note": ""}
                   and asked == ["glassflow-github-app"], (r, asked))
                asked.clear()
                r = await repos("?via=key")
                ck("no credential named: every github.com credential's repos, once each, and not Enterprise's",
                   r["repos"] == ["dana/notes", "northwind/Billing", "northwind/orders-api"]
                   and sorted(asked) == ["dana-token", "glassflow-github-app"], (r, asked))
                r = await repos("?via=key&credential=dana-token")
                ck("a named credential: only its repos", r["repos"] == ["dana/notes", "northwind/orders-api"], r)
                r = await repos("?via=key&credential=credential:github/dana-token")
                ck("a credential named the way the connection writes it works too",
                   r["repos"] == ["dana/notes", "northwind/orders-api"], r)
                r = await repos("?via=key&credential=ghe")
                ck("an Enterprise credential named: its repos", r["repos"] == ["corp/secret"], r)
                r = await repos("?via=app&credential=dana-token")
                ck("a credential saved with the app connection narrows the list too", r["repos"] == ["dana/notes", "northwind/orders-api"], r)
                r = await repos("?via=key&credential=nobody")
                ck("an unknown credential: no repos and no sentence", r == {"repos": [], "error": "", "note": ""}, r)
                store.upsert_github_credential("broken", "ghp_" + "b" * 36)
                r = await repos("?via=key&credential=broken")
                ck("a refused credential: no repos, and GitHub's reason in the sentence", r == {
                    "repos": [], "error": "GitHub refused the credential broken: Bad credentials",
                    "note": "GitHub did not list the repositories: GitHub refused the credential broken: Bad credentials. "
                            "Type the repository as owner/name."}, r)
                r = await repos("?via=key")
                ck("a refused credential next to others does not hide their repos", len(r["repos"]) == 3
                   and r["error"] == "" and r["note"] == "", r)
                store.upsert_github_credential("slow", "ghp_" + "s" * 36)
                t0 = asyncio.get_running_loop().time()
                r = await repos("?via=key")
                ck("a slow credential is not waited on past the budget, and the others answer",
                   len(r["repos"]) == 3 and asyncio.get_running_loop().time() - t0 < 2, r)
                r = await repos("?via=key&credential=slow")
                ck("a listing still going says so", r == {"repos": [], "error": "", "note": fsmod.REPOS_GOING}, r)
                n = asked.count("slow")
                slow.set()
                await asyncio.sleep(0)
                r = await repos("?via=key&credential=slow")
                ck("the listing kept going, so the next open has it, without asking GitHub again",
                   r["repos"] == ["slow/repo"] and asked.count("slow") == n, (r, asked.count("slow"), n))
                for c in [c["name"] for c in store.list_github_credentials() if ghc.is_app(c)]:
                    store.delete_github_credential(c)
                asked.clear()
                r = await repos()
                ck("with the app chosen but no app connection: the github.com credentials",
                   "glassflow-github-app" not in asked and "ghe" not in asked and "dana/notes" in r["repos"], (r, asked))
                async with mk(reader) as cx:
                    ck("a read key may not list them", (await cx.get("/api/factory/setup/github/repos")).status_code == 403)
                async with mk(station="northwind-build-1", role="builder") as cx:
                    ck("a crew station may not list them", (await cx.get("/api/factory/setup/github/repos")).status_code == 403)
            finally:
                ghc.list_repos_for = real_list
                fsmod.REPOS_BUDGET = real_budget
                for n in ("dana-token", "broken", "ghe", "slow"):
                    store.delete_github_credential(n)
                store.upsert_github_credential("glassflow-github-app", SECRETS["gh"], kind="app_broker", account="northwind",
                                               config={"installation_id": 4242, "account_type": "Organization"})
            # self-hosted with the connect addresses still set: the app is a Tares Cloud thing only
            os.environ.pop("TARES_WORKSPACE_API_URL", None)
            r = await get()
            tix = step(r.json(), "tickets")
            ck("self-hosted with connect addresses set: still no app block", avail(tix, "github") is not None
               and all(a["app"] is None for a in tix["available"]) and r.json()["edition"]["cloud"] is False,
               [a["app"] for a in tix["available"]])
            for k in ("TARES_LINEAR_CONNECT_URL", "TARES_GITHUB_CONNECT_URL"):
                os.environ.pop(k, None)
            r = await get()
            ck("self-hosted again: no app block and no cloud words", all(
                a["app"] is None for a in step(r.json(), "tickets")["available"])
               and r.json()["edition"]["cloud"] is False)

            print("== many projects, long text ==")
            long_name = "warehouse-sync-with-a-very-long-project-name-" + "x" * 120
            store.set_choice("tickets", "linear", {}, "Dana Whitfield")
            for i in range(40):
                uid = f"many{i}"
                store.create_project(uid, "custom", long_name + str(i) if i % 2 else f"storefront-{i}", {})
                store.set_project_kind(uid, "software_factory")
                store.set_project_choice(uid, "tickets", "linear", {}, "Dana Whitfield")
            r = await get()
            lin = conn(step(r.json(), "tickets"), "linear")
            ck("forty projects are counted, not listed", r.status_code == 200 and len(lin["picked_by"]) >= 40
               and lin["use"] == f"Used by {len(lin['picked_by']) + lin['follows']} projects"
               + (f"; {sum(len(c['picked_by']) for c in step(r.json(), 'tickets')['connected'] if c['id'] != 'linear')} picked another."
                  if any(c["picked_by"] for c in step(r.json(), "tickets")["connected"] if c["id"] != "linear") else "."),
               lin["use"])

            # TR-1628: who picks what is read once per slot for every project, not once per project
            reads = {"one": 0, "batch": 0, "asked": 0}
            real_one, real_batch = store.get_project_choice, store.project_choice_names

            def counted_one(*a, **kw):
                reads["one"] += 1
                return real_one(*a, **kw)

            def counted_batch(slot, uids):
                reads["batch"] += 1
                reads["asked"] = max(reads["asked"], len(uids))
                return real_batch(slot, uids)
            store.get_project_choice, store.project_choice_names = counted_one, counted_batch
            try:
                r = await get()
            finally:
                store.get_project_choice, store.project_choice_names = real_one, real_batch
            ck("setup reads no project's choice on its own", r.status_code == 200 and reads["one"] == 0, reads)
            ck("setup reads the choices once per project slot, for all the projects",
               0 < reads["batch"] <= len(PROJECT_SLOTS) and reads["asked"] >= 40, reads)
            for slot in PROJECT_SLOTS:
                got = {c['id']: {x['uid'] for x in c['picked_by']}
                       for c in step(r.json(), slot)['connected'] if c['picked_by']}
                want = {}
                for p in store.list_projects():
                    row = real_one(p["id"], slot)
                    if row:
                        want.setdefault(row["name"], set()).add(p["id"])
                ck(f"the {slot} step lists the same projects under each choice as one read per project",
                   got == want, (got, want))

            print("== keeping the default ==")
            b = (await get()).json()
            rev = step(b, "review")
            dflt = next(c["id"] for c in rev["connected"] if c["default"])
            other = next((c["id"] for c in rev["connected"] if not c["default"]), "nope")
            r = await root.put("/api/factory/setup/review/keep", json={"name": other})
            ck("only the default can be kept", r.status_code == 422, (r.status_code, r.text))
            async with mk(reader) as rx:
                r = await rx.put("/api/factory/setup/review/keep", json={"name": dflt})
                ck("a read key may not keep it", r.status_code == 403, r.status_code)
            r = await root.put("/api/factory/setup/nope/keep", json={"name": "x"})
            ck("a step that does not exist is a 404", r.status_code == 404, r.status_code)
            r = await root.put("/api/factory/setup/review/keep", json={"name": dflt})
            ck("the admin keeps the default", r.status_code == 200 and r.json() == {"slot": "review", "kept": dflt}, r.text)
            ck("and it is stored", store.get_setting(fs.KEEP_PREFIX + "review") == dflt)
            ck("a kept step is set and has no Use button", step((await get()).json(), "review")["set"] is True
               and not any(a["key"] == "keep" for c in step((await get()).json(), "review")["connected"]
                           for a in c["actions"]))

            print("== steps left out ==")
            fs.HIDDEN_STEPS, fs.HIDE_WITHOUT_CHOICE = HIDDEN, HIDE_WITHOUT
            b = (await get()).json()
            slots = [x["slot"] for x in b["steps"]]
            ck("monitoring is left out until it is tested", "monitoring" not in slots and "monitoring" in HIDDEN, slots)
            ck("memory and guardrails stay while they offer a choice",
               all(x in slots for x in ("memory", "guardrails")) and set(HIDE_WITHOUT) == {"memory", "guardrails"}, slots)
            ck("the count is out of the steps shown, and the crew, GitHub and the terminal",
               b["progress"]["total"] == len(slots) + 3, b["progress"])
            r = await root.get("/api/factory/setup/progress")
            ck("the progress call says the same, with the number of factory projects",
               r.status_code == 200 and {k: v for k, v in r.json().items() if k != "projects"} == b["progress"]
               and r.json()["projects"] == sum(1 for p in store.list_projects() if p.get("kind") == "software_factory"),
               r.text)
            fs.HIDE_WITHOUT_CHOICE = ("runtime",)
            ck("a step with one connection and nothing to connect is left out",
               "runtime" not in [x["slot"] for x in (await get()).json()["steps"]])
            fs.HIDDEN_STEPS, fs.HIDE_WITHOUT_CHOICE = (), ()
            async with mk(reader) as rx:
                ck("a read key gets no progress", (await rx.get("/api/factory/setup/progress")).status_code == 403)

            print("== what the whole run shows ==")
            texts = "\n".join(ALL)
            ck("no secret value in any response", not any(v in texts for v in SECRETS.values()),
               [k for k, v in SECRETS.items() if v in texts])
            ck("secrets are only ever saved flags", '"saved":true' in texts)
            ck("no em dash anywhere", chr(0x2014) not in texts and chr(0x2013) not in texts)
            strings = []
            for t in ALL:
                if t.startswith("{"):
                    shown(json.loads(t), strings)
            bad = sorted({s for s in strings if INTERNAL.search(s) or ("mapping" in s.lower() and s != "Status mapping")})
            ck("no internal name in a shown string", not bad, bad[:5])
            ck("every shown string was looked at", len(strings) > 500, len(strings))

    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


asyncio.run(main())
