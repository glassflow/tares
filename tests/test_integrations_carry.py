"""Existing links carry over into the integrations (TR-841).

One in-process cell (DuckDB, auth on) with the integrations a project's links point at installed
as fakes: tickets linear and github, ci github, monitoring rius and otlp. Four Tares projects: one
with no link, one linked to a Linear project, one linked to GitHub issues, one linked to both.
Tracing is set to rius. Covered:
 - tickets: no link gives the default; a Linear link gives linear; a GitHub issues link gives
   github; both gives linear (the order `project_ticket_home` already has);
 - ci: github for a project with a repo on GitHub, from its issues link or from the repos it was
   handed to the crew with; a handed-over folder with no owner/name does not count; local otherwise;
 - monitoring: the tracing provider while tracing is on and has an endpoint (rius, then otlp);
   off when tracing is off, or on with no endpoint to send to;
 - nothing is copied: the stores hold no integration choice afterwards, a link removed or tracing
   turned off changes the answer at once, and no secret of the links or of tracing is carried;
 - the order holds: a project's own choice beats carried, the factory's choice beats carried;
 - a carried name that is not installed resolves to the slot's default with reason "not installed";
 - GET /api/factory/integrations on the fixture matches tests/fixtures/integrations_carry.json
   (regenerate with TARES_WRITE_FIXTURE=1 after a change on purpose).

Run: python tests/test_integrations_carry.py
With CI's Postgres run: TARES_TEST_PG=<dsn> PYTHONPATH=tests/pg_harness python tests/test_integrations_carry.py
"""
import asyncio
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
sys.path.insert(0, ROOT)
from _env import scrub_os_environ
scrub_os_environ()

_TMP = tempfile.mkdtemp(prefix="tares-integrations-carry-")
os.environ["TARES_DB"] = os.path.join(_TMP, "carry.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-integrations-carry"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

import httpx

from tares import licence, tracing
from tares.integrations import registry, resolve
from tares.integrations.ci import CISource
from tares.integrations.monitoring import Monitoring
from tares.integrations.tickets import TicketBackend
from tares.integrations.types import Field
from tares.store import now_utc

FIXTURE = os.path.join(ROOT, "tests", "fixtures", "integrations_carry.json")

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


class Linear(TicketBackend):
    name = "linear"
    title = "Linear"
    settings_schema = [Field("token", "API key", "secret")]


class GithubIssues(TicketBackend):
    name = "github"
    title = "GitHub Issues"
    settings_schema = []


class GithubCI(CISource):
    name = "github"
    title = "GitHub Actions"
    settings_schema = [Field("no_ci", "This repository has no CI", "bool")]


class Rius(Monitoring):
    name = "rius"
    title = "Rius"
    settings_schema = [Field("key", "API key", "secret")]


class Otlp(Monitoring):
    name = "otlp"
    title = "OTLP"
    settings_schema = [Field("endpoint", "Endpoint", "url")]


class Faketix(TicketBackend):
    name = "faketix"
    title = "Fake tickets"
    settings_schema = [Field("url", "Address", "url"), Field("token", "Token", "secret")]


registry.load()
for cls in (Linear, GithubIssues, GithubCI, Rius, Otlp, Faketix):
    registry._loaded[(cls.slot, cls.name)] = cls

licence.ee_allowed = lambda feature="enterprise": False

# values that must never appear in a response
PLANTED = ["plant-linear-1", "plant-github-1", "plant-tracekey-1", "plant-tracehdr-1"]


async def main():
    from tares.daemon import make_app
    app = make_app()
    s = app.state.store

    def eff(slot, uid=None):
        return resolve.effective(slot, uid, store=s)

    def seen(slot, uid=None):
        e = eff(slot, uid)
        return (e.integration, e.source, e.reason)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                     headers={"Authorization": f"Bearer {TOKEN}"}) as root:
            for uid, name in (("p_tares", "tares only"), ("p_linear", "linear"),
                              ("p_github", "github"), ("p_both", "both")):
                s.create_project(uid, "custom", name, {})
            s.set_project_linear("p_linear", {"project_id": "lin-1", "api_key": PLANTED[0]})
            s.set_project_github("p_github", {"repo": "acme/app", "issue_label": "tares",
                                              "token": PLANTED[1]})
            s.set_project_linear("p_both", {"project_id": "lin-2"})
            s.set_project_github("p_both", {"repo": "acme/both", "issue_label": "tares"})
            # a project handed to the crew with a repo on GitHub, and one with a folder only
            s.set_handover("p_linear", [{"path": "/work/api", "name": "acme/api"}], "L4-ship",
                           "ann", now_utc())
            s.set_handover("p_tares", [{"path": "/work/scratch", "name": None}], "L4-ship",
                           "ann", now_utc())
            s.set_setting("tracing_enabled", "true")
            s.set_setting("tracing_provider", "rius")
            s.set_setting("tracing_api_key", PLANTED[2])
            s.set_setting("tracing_headers", f"x-token={PLANTED[3]}")

            print("== tickets ==")
            ck("no link: the default", seen("tickets", "p_tares") == ("tares", "default", ""),
               seen("tickets", "p_tares"))
            ck("a Linear link carries as linear",
               seen("tickets", "p_linear") == ("linear", "carried", ""), seen("tickets", "p_linear"))
            ck("a GitHub issues link carries as github",
               seen("tickets", "p_github") == ("github", "carried", ""), seen("tickets", "p_github"))
            ck("both links: linear, as project_ticket_home orders them",
               seen("tickets", "p_both") == ("linear", "carried", "")
               and s.project_ticket_home("p_both") == "linear", seen("tickets", "p_both"))
            ck("the factory view has no project, so no ticket link to carry",
               seen("tickets") == ("tares", "default", ""), seen("tickets"))
            ck("a project that does not exist carries nothing",
               seen("tickets", "p_nobody") == ("tares", "default", ""), seen("tickets", "p_nobody"))

            print("== ci ==")
            ck("no repo on GitHub: local", seen("ci", "p_tares") == ("local", "default", ""),
               seen("ci", "p_tares"))
            ck("a folder handed over with no owner/name does not count",
               s.get_handover("p_tares")["repos"] == [{"path": "/work/scratch", "name": None}]
               and seen("ci", "p_tares")[0] == "local")
            ck("a repo handed to the crew carries github (project linked to Linear)",
               seen("ci", "p_linear") == ("github", "carried", ""), seen("ci", "p_linear"))
            ck("a GitHub issues link names a repo: github",
               seen("ci", "p_github") == ("github", "carried", ""), seen("ci", "p_github"))
            ck("both links: github", seen("ci", "p_both") == ("github", "carried", ""),
               seen("ci", "p_both"))
            ck("the factory view: local", seen("ci") == ("local", "default", ""), seen("ci"))

            print("== monitoring ==")
            ck("tracing set to rius: rius", seen("monitoring") == ("rius", "carried", ""), seen("monitoring"))
            ck("it is the same for every project", seen("monitoring", "p_tares") == seen("monitoring"))
            s.set_setting("tracing_provider", "otlp")
            ck("otlp with no endpoint has nowhere to send: off",
               seen("monitoring") == ("off", "default", ""), seen("monitoring"))
            s.set_setting("tracing_endpoint", "https://otel.example.com")
            ck("otlp with an endpoint: otlp", seen("monitoring") == ("otlp", "carried", ""), seen("monitoring"))
            s.set_setting("tracing_enabled", "false")
            ck("tracing off: off", seen("monitoring") == ("off", "default", ""), seen("monitoring"))
            s.set_setting("tracing_enabled", "true")
            s.set_setting("tracing_provider", "rius")
            s.set_setting("tracing_endpoint", None)
            ck("back to rius", seen("monitoring") == ("rius", "carried", ""), seen("monitoring"))

            print("== read at resolve time, nothing copied ==")
            ck("no integration choice was written",
               all(s.get_choice(sl) is None for sl in resolve.SLOTS)
               and all(s.get_project_choice(u, sl) is None for u in ("p_tares", "p_linear", "p_github", "p_both")
                       for sl in resolve.PROJECT_SLOTS))
            s.set_project_linear("p_linear", None)
            ck("a link removed changes the answer at once",
               seen("tickets", "p_linear") == ("tares", "default", ""), seen("tickets", "p_linear"))
            s.set_project_linear("p_linear", {"project_id": "lin-1"})
            ck("and back when it is linked again", seen("tickets", "p_linear")[0] == "linear")
            e = eff("tickets", "p_linear")
            ck("a carried choice has no integration settings, so no secret",
               e.settings == {} and eff("monitoring").settings == {} and eff("ci", "p_github").settings == {},
               (e, eff("monitoring")))

            print("== the order ==")
            s.set_project_choice("p_linear", "tickets", "faketix", {"url": "mine"}, "a")
            ck("a project's own choice beats carried",
               seen("tickets", "p_linear") == ("faketix", "project", ""), seen("tickets", "p_linear"))
            s.clear_project_choice("p_linear", "tickets")
            ck("cleared: carried again", seen("tickets", "p_linear")[:2] == ("linear", "carried"))

            print("== not installed ==")
            gone = registry._loaded.pop(("tickets", "linear"))
            ck("a carried name that is not installed: the default, not installed",
               seen("tickets", "p_linear") == ("tares", "default", resolve.NOT_INSTALLED),
               seen("tickets", "p_linear"))
            ck("the other projects are not affected",
               seen("tickets", "p_github") == ("github", "carried", ""))
            registry._loaded[("tickets", "linear")] = gone

            print("== the API ==")
            r = await root.get("/api/factory/integrations")
            ck("GET /api/factory/integrations is 200", r.status_code == 200, r.text[:200])
            slots = {x["slot"]: x for x in r.json()["slots"]}
            ck("the factory view: monitoring is rius, carried; the rest default",
               slots["monitoring"]["effective"]["integration"] == "rius"
               and slots["monitoring"]["effective"]["source"] == "carried"
               and slots["monitoring"]["choice"] is None
               and all(slots[k]["effective"]["source"] == "default"
                       for k in ("tickets", "review", "ci", "memory", "guardrails", "runtime")),
               {k: v["effective"] for k, v in slots.items()})
            got = {"integrations": r.json()["slots"], "projects": {}}
            for uid in ("p_tares", "p_linear", "p_github", "p_both"):
                rp = await root.get(f"/api/factory/integrations/effective?project={uid}")
                ck(f"effective for {uid} is 200", rp.status_code == 200, rp.text[:200])
                got["projects"][uid] = rp.json()
            text = json.dumps(got, sort_keys=True)
            ck("no secret of a link or of tracing appears in a response",
               not any(x in text for x in PLANTED), [x for x in PLANTED if x in text])
            if os.environ.get("TARES_WRITE_FIXTURE"):
                with open(FIXTURE, "w") as fh:
                    json.dump(got, fh, indent=2, sort_keys=True)
                    fh.write("\n")
                print(f"  wrote {FIXTURE}")
            with open(FIXTURE) as fh:
                want = json.load(fh)
            ck("the API on the fixture matches the stored expected JSON",
               json.loads(text) == want,
               "differs: " + ", ".join(k for k in set(got) | set(want) if got.get(k) != want.get(k)))
            proj = got["projects"]
            def names(uid):
                return {x["slot"]: x["integration"] for x in proj[uid]["slots"]} if "slots" in proj[uid] \
                    else {x["slot"]: x["integration"] for x in proj[uid]}
            ck("the four projects resolve as stated, and monitoring is rius for all",
               [(names(u)["tickets"], names(u)["ci"], names(u)["monitoring"]) for u in
                ("p_tares", "p_linear", "p_github", "p_both")] == [
                   ("tares", "local", "rius"), ("linear", "github", "rius"),
                   ("github", "github", "rius"), ("linear", "github", "rius")],
               {u: names(u) for u in proj})

            print("== the factory's choice beats carried ==")
            s.set_choice("tickets", "faketix", {"url": "factory"}, "a")
            ck("every project follows the factory's tickets choice, links or not",
               all(seen("tickets", u) == ("faketix", "factory", "")
                   for u in ("p_tares", "p_linear", "p_github", "p_both")),
               {u: seen("tickets", u) for u in ("p_tares", "p_linear", "p_github", "p_both")})
            ck("the link data is still stored", s.project_ticket_home("p_linear") == "linear")

    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


asyncio.run(main())
