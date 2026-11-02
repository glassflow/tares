"""The AI SRE Runbooks and memory screen (TR-753): GET /api/sre/runbooks, and the writes
POST /api/sre/runbooks/{service}/lines and DELETE /api/sre/runbooks/{service}/lines/{index}.

Covers a line added (author and day stored, the skill runbook-<service> listed to the investigator
and to the watcher, also when they sit in two projects, updated_by set), a line removed (and the
runbook gone with its last line), a duplicate, an empty and an over-long line refused with a plain
sentence, the agents' memory shown under its own service only (exact key, newest first, no runbook
lines for a service that has only memory), the words for counts ("1 line", "once"), a read key (403
on both writes, 200 on the read), unknown services and lines (404), a service name that is not a
valid skill name, no forbidden word and no em dash outside what people and agents wrote, and a
platform cell, where the routes answer 404 and the skills work as before.

Run: .venv/bin/python tests/test_sre_runbooks.py
"""
import asyncio
import json
import os
import sys
import tempfile
import types
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import scrub_os_environ

scrub_os_environ()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp(prefix="tares-sre-runbooks-")
ROOT_TOKEN = "root-token-sre-runbooks"
os.environ["TARES_DB"] = os.path.join(TMP, "t.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = ROOT_TOKEN
os.environ["TARES_EDITION"] = "sre"                     # before make_app(): the first start picks it
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_TRIGGER_DEBOUNCE_SECONDS"] = "0"
os.environ["TARES_SCHEDULE_CLOCK_SECONDS"] = "3600"
os.environ["TARES_WATCH_SECONDS"] = "3600"
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-not-a-real-key"

import httpx

from fixtures.sre_cell import PROJECT, _otlp, build
from tares import edition as edition_mod
from tares import skills, words
from tares.builtin_agents import AgentRunner
from tares.envelope import now_utc
from tares.screens import sre_runbooks

P = F = 0
SKIP = ("name", "text", "by")      # what a person or an agent wrote, or a name a person gave
DASH = chr(0x2014)                  # spelled out so this file has none
UID = "prj-" + PROJECT
SVC = "service-a"


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def eq(label, got, want):
    ck(label, got == want, f"got {got!r}, want {want!r}")


def entry(screen, name):
    return next((s for s in screen["services"] if s["name"] == name), None)


def listed(store, agent_project):
    """The skill names the agent of that project is offered, the way a run builds them."""
    stub = types.SimpleNamespace(store=store)
    return [s["name"] for s in AgentRunner._project_skills(stub, {"projects": [agent_project]})]


def finished_run(store, rid, agent, skills_loaded, days_back=0):
    store.start_agent_run(rid, agent, "watch-prod", "", SVC, "h")
    store.finish_agent_run(rid, "ok")
    store.set_run_skills(rid, skills_loaded)
    with store._swrite:
        store.sc.execute("UPDATE agent_runs SET started_at = ? WHERE id = ?",
                         [now_utc() - timedelta(days=days_back, minutes=5), rid])


async def main():
    from tares.daemon import make_app
    app = make_app()
    async with app.router.lifespan_context(app):
        root = {"Authorization": f"Bearer {ROOT_TOKEN}"}
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=root)
        store, runtime = app.state.store, app.state.runtime
        now = now_utc()

        # ── the cell: the shared fixture, a service with a name that is not a skill name, a
        #    second project with a watcher of its own ──
        build(store, now, watcher=True, investigator=True)
        store.append([_otlp(now, 2, "Pay.Gateway", "INFO", "charged 3"),
                      _otlp(now, 3, "pay-gateway", "INFO", "charged 4"),
                      _otlp(now, 4, "org/repo", "INFO", "built 1")])
        uid2 = "prj-second"
        store.create_project(uid2, "custom", "second-watch", {}, goal="Keep pay-gateway healthy")
        store.upsert_catalog_trigger("watch-2", ["prod-otlp"], {"every": "10m"}, {}, "10m")
        store.upsert_catalog_agent("watcher-2", "watch-2", "Look at prod-otlp.", budget_usd=5.0,
                                   budget_period="month", concludes=True)
        store.put_in_project("trigger", "watch-2", uid2)
        store.put_in_project("agent", "watcher-2", uid2)
        store.set_wake(uid2, "watch-2", "watcher-2", enabled=True)
        runtime.reload_catalog()

        key = (await cx.post("/api/keys", json={"name": "viewer", "scopes": ["read"]})).json()
        read = {"Authorization": f"Bearer {key['secret']}"}
        key = (await cx.post("/api/keys", json={"name": "oncall", "scopes": ["admin"]})).json()
        oncall = {"Authorization": f"Bearer {key['secret']}"}
        today = now.date().isoformat()

        print("== the screen on a cell with nothing written ==")
        r = await cx.get("/api/sre/runbooks")
        eq("the read answers", r.status_code, 200)
        s = r.json()
        eq("the answer has the two keys", sorted(s), ["empty_words", "services"])
        names = [x["name"] for x in s["services"]]
        for n in ("edge-proxy", SVC, "docs-site", "service-a-receiver", "Pay.Gateway", "pay-gateway"):
            ck(f"{n} is on the page", n in names, str(names))
        ck("no empty words while services exist", s["empty_words"] is None)
        a = entry(s, SVC)
        eq("nothing written: no runbook lines", a["runbook"], [])
        eq("nothing written: no memory", a["memory"], [])
        eq("nothing read yet", a["used_words"], None)
        eq("the count is worded", a["lines_words"], "No lines yet")
        eq("?service= narrows the page to one", [x["name"] for x in
                                                 (await cx.get("/api/sre/runbooks", params={"service": SVC})).json()["services"]],
           [SVC])
        eq("an unknown service is a 404 on the read", (await cx.get("/api/sre/runbooks", params={"service": "nope"})).status_code, 404)

        print("== adding a line ==")
        r = await cx.post(f"/api/sre/runbooks/{SVC}/lines", headers=oncall,
                          json={"text": "  Restart  the job runner before the failover  "})
        eq("an admin key adds a line", r.status_code, 200)
        a = entry(r.json(), SVC)
        eq("the answer is the new screen: one line", len(a["runbook"]), 1)
        line = a["runbook"][0]
        eq("the text is stored as one tidy line", line["text"], "Restart the job runner before the failover")
        eq("the author is the key's name", line["by"], "oncall")
        eq("and it says today", line["when_words"], "today")
        eq("singular count", a["lines_words"], "1 line")
        eq("the screen read agrees", entry((await cx.get("/api/sre/runbooks")).json(), SVC)["runbook"], a["runbook"])
        sk = store.get_skill(None, f"runbook-{SVC}")
        ck("the shared skill exists", sk is not None)
        ck("its body ends the line with who and the day",
           f"- Restart the job runner before the failover (by oncall, {today})" in sk["body"].splitlines(), sk["body"])
        row = store.sc.execute("SELECT updated_by, made_by FROM shared_skills WHERE name = ?",
                               [f"runbook-{SVC}"]).fetchone()
        eq("updated_by is the person", row[0], "oncall")
        ck("the investigator is offered the runbook", f"runbook-{SVC}" in listed(store, UID))
        ck("the second project's watcher is offered it too", f"runbook-{SVC}" in listed(store, uid2),
           str(listed(store, uid2)))
        users = store.skill_users(f"runbook-{SVC}")
        eq("wired into every project the roles list", sorted(users), sorted([UID, uid2]))
        r = await cx.post(f"/api/sre/runbooks/{SVC}/lines", headers=oncall, json={"text": "Page the platform team after 10 minutes"})
        eq("a second line", [l["text"] for l in entry(r.json(), SVC)["runbook"]],
           ["Restart the job runner before the failover", "Page the platform team after 10 minutes"])
        eq("plural count", entry(r.json(), SVC)["lines_words"], "2 lines")
        eq("the other services are untouched", entry(r.json(), "edge-proxy")["runbook"], [])
        ck("only service-a has a skill", [n for n in (x["name"] for x in store.list_all_skills())
                                          if n.startswith("runbook-")] == [f"runbook-{SVC}"])
        r = await cx.post(f"/api/sre/runbooks/{SVC}/lines", json={"text": "Check the dashboards first"})
        who = entry(r.json(), SVC)["runbook"][-1]["by"]
        ck("the root token's lines carry a name too", bool(who), who)
        # an older line, written by hand into the skill, is read back with its day
        old = (now - timedelta(days=3)).date().isoformat()
        with store._swrite:
            store.sc.execute("UPDATE shared_skills SET body = body || ? WHERE name = ?",
                             [f"- Old advice (by Dana, {old})\n- A line nobody stamped\n", f"runbook-{SVC}"])
        rb = entry((await cx.get("/api/sre/runbooks")).json(), SVC)["runbook"]
        eq("an older line says how many days ago", (rb[-2]["by"], rb[-2]["when_words"]), ("Dana", "3 days ago"))
        eq("a line without a stamp has no author or day", (rb[-1]["text"], rb[-1]["by"], rb[-1]["when_words"]),
           ("A line nobody stamped", "", ""))
        eq("when_words: yesterday", sre_runbooks.when_words((now - timedelta(days=1)).date().isoformat(), now.date()), "yesterday")
        ck("when_words: long ago is a date", sre_runbooks.when_words("2025-01-05", now.date()).startswith("5 Jan"))

        print("== refused lines ==")
        before = entry((await cx.get("/api/sre/runbooks")).json(), SVC)["runbook"]
        refusals = []
        for label, text, code in (("a duplicate", "Restart the job runner before the failover", 409),
                                  ("a duplicate with other spacing", "  Restart the   job runner before the failover ", 409),
                                  ("an empty line", "   ", 400),
                                  ("a missing text", None, 400),
                                  ("a line over 500 characters", "x" * 501, 400)):
            r = await cx.post(f"/api/sre/runbooks/{SVC}/lines", headers=oncall, json={"text": text})
            eq(f"{label} is refused", r.status_code, code)
            msg = r.json().get("detail", "")
            ck(f"{label}: a plain sentence", msg.endswith(".") and not words.offenders(msg), msg)
            refusals.append(msg)
        r = await cx.post(f"/api/sre/runbooks/{SVC}/lines", headers=oncall, json={"text": "y" * 500})
        eq("a line of exactly 500 characters is fine", r.status_code, 200)
        r = await cx.post(f"/api/sre/runbooks/{SVC}/lines", headers=oncall, content=b"not json",
                          )
        eq("a body that is not JSON is refused", r.status_code, 400)
        r = await cx.post(f"/api/sre/runbooks/{SVC}/lines", headers=oncall, json=["a"])
        eq("a body that is not an object is refused", r.status_code, 400)
        after = entry((await cx.get("/api/sre/runbooks")).json(), SVC)["runbook"]
        eq("the refused lines were not stored", [l["text"] for l in after], [l["text"] for l in before] + ["y" * 500])
        eq("a service the cell does not know is a 404", (await cx.post("/api/sre/runbooks/nope/lines", headers=oncall, json={"text": "a"})).status_code, 404)

        print("== removing a line ==")
        rb = entry((await cx.get("/api/sre/runbooks")).json(), SVC)["runbook"]
        gone = rb[1]["text"]
        r = await cx.delete(f"/api/sre/runbooks/{SVC}/lines/1", headers=oncall)
        eq("an admin key removes a line", r.status_code, 200)
        left = [l["text"] for l in entry(r.json(), SVC)["runbook"]]
        ck("the line is gone and the rest keep their order", gone not in left and left == [l["text"] for i, l in enumerate(rb) if i != 1], str(left))
        ck("it is gone from the skill body too", gone not in store.get_skill(None, f"runbook-{SVC}")["body"])
        eq("a line number that is not there is a 404", (await cx.delete(f"/api/sre/runbooks/{SVC}/lines/99", headers=oncall)).status_code, 404)
        r = await cx.delete(f"/api/sre/runbooks/{SVC}/lines/-1", headers=oncall)
        eq("a negative line number is a 404", r.status_code, 404)
        ck("and says so plainly", r.json()["detail"].endswith(".") and not words.offenders(r.json()["detail"]), r.json()["detail"])
        eq("a service with no runbook is a 404", (await cx.delete("/api/sre/runbooks/edge-proxy/lines/0", headers=oncall)).status_code, 404)
        eq("a service the cell does not know is a 404 on delete", (await cx.delete("/api/sre/runbooks/nope/lines/0", headers=oncall)).status_code, 404)
        # remove until none is left: the runbook goes with its last line
        n = len(left)
        for _ in range(n):
            r = await cx.delete(f"/api/sre/runbooks/{SVC}/lines/0", headers=oncall)
        eq("the last line removed leaves an empty runbook", entry(r.json(), SVC)["runbook"], [])
        eq("worded as none", entry(r.json(), SVC)["lines_words"], "No lines yet")
        ck("and the skill is gone from the cell", store.get_skill(None, f"runbook-{SVC}") is None)
        ck("nobody is offered it any more", f"runbook-{SVC}" not in listed(store, UID) + listed(store, uid2))
        r = await cx.post(f"/api/sre/runbooks/{SVC}/lines", headers=oncall, json={"text": "Start again from the dashboards"})
        eq("a line can be added again", [l["text"] for l in entry(r.json(), SVC)["runbook"]], ["Start again from the dashboards"])

        print("== memory ==")
        for key_, content in ((SVC, "Retries pile up after a deploy"), ("edge-proxy", "502s follow a certificate renewal"),
                              ("service-a-receiver", "Quiet on weekends"), ("Service-A", "Wrong case on purpose")):
            r = await cx.post("/remember", json={"key": key_, "content": content})
            eq(f"remember {key_}", r.status_code, 202)
        await asyncio.sleep(0.1)
        r = await cx.post("/remember", json={"key": SVC, "content": "Second note about retries", "memory_type": "decision",
                                              "fields": {"agent": "watcher"}})
        s = (await cx.get("/api/sre/runbooks")).json()
        a, e = entry(s, SVC), entry(s, "edge-proxy")
        eq("service-a shows its own notes, newest first", [m["text"] for m in a["memory"]],
           ["Second note about retries", "Retries pile up after a deploy"])
        eq("a note that names an agent shows it", a["memory"][0]["by"], "Watcher")
        eq("a note that names nobody shows the Investigator", a["memory"][1]["by"], "Investigator")
        eq("and the day", a["memory"][0]["when_words"], "today")
        eq("edge-proxy shows only its own", [m["text"] for m in e["memory"]], ["502s follow a certificate renewal"])
        eq("a service with memory and no runbook has no runbook lines", e["runbook"], [])
        eq("and its count words only the runbook", e["lines_words"], "No lines yet")
        ck("another service's key is not mixed in (exact key)",
           all("Quiet on weekends" not in m["text"] and "Wrong case" not in m["text"] for m in a["memory"]))
        eq("service-a-receiver has its own", [m["text"] for m in entry(s, "service-a-receiver")["memory"]], ["Quiet on weekends"])
        eq("the page for one service holds only that service's notes",
           [m["text"] for m in entry((await cx.get("/api/sre/runbooks", params={"service": "edge-proxy"})).json(), "edge-proxy")["memory"]],
           ["502s follow a certificate renewal"])
        eq("memory is not a runbook line: the skill holds none of it", "Retries pile up" in store.get_skill(None, f"runbook-{SVC}")["body"], False)
        names = [x["name"] for x in s["services"]]
        ck("services with something written come first", set(names[:2]) >= {SVC, "edge-proxy"} or
           all(entry(s, n)["runbook"] or entry(s, n)["memory"] for n in names[:3]), str(names[:4]))

        print("== how often the agents read it ==")
        finished_run(store, "run_r1", "investigator", [f"runbook-{SVC}"])
        eq("one read is worded once", entry((await cx.get("/api/sre/runbooks")).json(), SVC)["used_words"], "Read by the investigator once this week")
        for i in range(2, 5):
            finished_run(store, f"run_r{i}", "investigator", [f"runbook-{SVC}", "other"])
        finished_run(store, "run_old", "investigator", [f"runbook-{SVC}"], days_back=9)
        finished_run(store, "run_other", "investigator", ["runbook-edge-proxy"])
        s = (await cx.get("/api/sre/runbooks")).json()
        eq("four reads this week, the old one not counted", entry(s, SVC)["used_words"], "Read by the investigator 4 times this week")
        eq("a runbook nobody read says nothing", entry(s, "docs-site")["used_words"], None)
        eq("each service counts its own skill", entry(s, "edge-proxy")["used_words"], "Read by the investigator once this week")
        finished_run(store, "run_w", "watcher", [f"runbook-{SVC}"])
        eq("the watcher reading it is worded too",
           entry((await cx.get("/api/sre/runbooks")).json(), SVC)["used_words"], "Read by the watcher and the investigator 5 times this week")

        finished_run(store, "run_w2", "watcher", ["runbook-docs-site"])
        eq("only the watcher reading it is worded too",
           entry((await cx.get("/api/sre/runbooks")).json(), "docs-site")["used_words"], "Read by the watcher once this week")

        print("== a service name that is not a skill name ==")
        for n in ("service-a", "Pay.Gateway", "pay-gateway", "x" * 100, "...", "Ünï"):
            ck(f"{n[:20]!r} gives a valid skill name", skills.NAME_RE.match(sre_runbooks.skill_name(n)) is not None,
               sre_runbooks.skill_name(n))
        eq("a valid name is used as it is", sre_runbooks.skill_name("pay-gateway"), "runbook-pay-gateway")
        ck("Pay.Gateway and pay-gateway never share a runbook", sre_runbooks.skill_name("Pay.Gateway") != sre_runbooks.skill_name("pay-gateway"))
        r = await cx.post("/api/sre/runbooks/Pay.Gateway/lines", headers=oncall, json={"text": "Check the card network status"})
        eq("a line for Pay.Gateway", r.status_code, 200)
        s = r.json()
        eq("it is Pay.Gateway's", [l["text"] for l in entry(s, "Pay.Gateway")["runbook"]], ["Check the card network status"])
        eq("and not pay-gateway's", entry(s, "pay-gateway")["runbook"], [])

        print("== lines people write ==")
        for text in ("-Xmx512m is required", "--force only after the drain", "- one bullet marker is dropped"):
            r = await cx.post("/api/sre/runbooks/pay-gateway/lines", headers=oncall, json={"text": text})
            eq(f"{text[:20]!r} is accepted", r.status_code, 200)
        eq("the dashes of a flag stay, one bullet marker goes",
           [l["text"] for l in entry(r.json(), "pay-gateway")["runbook"]],
           ["-Xmx512m is required", "--force only after the drain", "one bullet marker is dropped"])
        eq("the same flag again is a duplicate",
           (await cx.post("/api/sre/runbooks/pay-gateway/lines", headers=oncall, json={"text": "-Xmx512m is required"})).status_code, 409)

        print("== a service name with a slash ==")
        for how, base in (("a plain slash", "org/repo"), ("an encoded slash", "org%2Frepo")):
            r = await cx.post(f"/api/sre/runbooks/{base}/lines", headers=oncall, json={"text": f"Check the build via {how}"})
            eq(f"a line for org/repo through {how}", r.status_code, 200)
        eq("both are org/repo's", [l["text"] for l in entry(r.json(), "org/repo")["runbook"]],
           ["Check the build via a plain slash", "Check the build via an encoded slash"])
        r = await cx.delete("/api/sre/runbooks/org%2Frepo/lines/0", headers=oncall)
        eq("a line of org/repo is removed", (r.status_code, [l["text"] for l in entry(r.json(), "org/repo")["runbook"]]),
           (200, ["Check the build via an encoded slash"]))
        eq("an unknown service with a slash is a 404", (await cx.post("/api/sre/runbooks/no/such/lines", headers=oncall, json={"text": "a"})).status_code, 404)

        print("== no watcher or investigator left ==")
        real_roles = sre_runbooks.sre.roles
        sre_runbooks.sre.roles = lambda *a, **k: {"watchers": [], "investigators": []}
        try:
            r = await cx.post("/api/sre/runbooks/pay-gateway/lines", headers=oncall, json={"text": "A line with nobody to read it"})
            eq("adding a line is refused", r.status_code, 409)
            msg = r.json().get("detail", "")
            ck("with a plain sentence", msg.endswith(".") and not words.offenders(msg) and DASH not in msg, msg)
            r = await cx.delete("/api/sre/runbooks/pay-gateway/lines/0", headers=oncall)
            eq("removing a line still works", r.status_code, 200)
            eq("and keeps the rest", [l["text"] for l in entry(r.json(), "pay-gateway")["runbook"]],
               ["--force only after the drain", "one bullet marker is dropped"])
            eq("the projects that held the runbook still hold it", len(store.skill_users(sre_runbooks.skill_name("pay-gateway"))) > 0, True)
        finally:
            sre_runbooks.sre.roles = real_roles

        print("== a read key ==")
        eq("a read key reads", (await cx.get("/api/sre/runbooks", headers=read)).status_code, 200)
        r = await cx.post(f"/api/sre/runbooks/{SVC}/lines", headers=read, json={"text": "Not allowed"})
        eq("a read key may not add a line", r.status_code, 403)
        r = await cx.delete(f"/api/sre/runbooks/{SVC}/lines/0", headers=read)
        eq("a read key may not remove a line", r.status_code, 403)
        eq("nothing changed", [l["text"] for l in entry((await cx.get("/api/sre/runbooks")).json(), SVC)["runbook"]],
           ["Start again from the dashboards"])
        eq("no key is a 401", (await httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t").post(
            f"/api/sre/runbooks/{SVC}/lines", json={"text": "x"})).status_code, 401)

        print("== words ==")
        shots = [(await cx.get("/api/sre/runbooks")).json(),
                 (await cx.get("/api/sre/runbooks", params={"service": SVC})).json(),
                 (await cx.post(f"/api/sre/runbooks/{SVC}/lines", headers=oncall, json={"text": "One more line"})).json(),
                 (await cx.delete(f"/api/sre/runbooks/{SVC}/lines/0", headers=oncall)).json()]
        refusals += [(await cx.post(f"/api/sre/runbooks/{SVC}/lines", headers=oncall, json={"text": "One more line"})).json()["detail"],
                     (await cx.delete(f"/api/sre/runbooks/{SVC}/lines/99", headers=oncall)).json()["detail"],
                     (await cx.get("/api/sre/runbooks", params={"service": "nope"})).json()["detail"]]
        for i, shot in enumerate(shots):
            bad = words.offenders_in(shot, SKIP)
            ck(f"no word a person should not read in answer {i + 1}", not bad, str(bad[:5]))
        ck("no word a person should not read in a refusal", not any(words.offenders(m) for m in refusals), str(refusals))
        stripped = json.loads(json.dumps(shots[0]))
        for x in stripped["services"]:
            for part in ("runbook", "memory"):
                x[part] = [{k: v for k, v in m.items() if k not in ("text", "by")} for m in x[part]]
        ck("no em dash in the page outside the lines", DASH not in json.dumps(stripped, ensure_ascii=False))

        real = sre_runbooks.sre.list_services
        sre_runbooks.sre.list_services = lambda *a, **k: []
        try:
            none = sre_runbooks.build(store, runtime.catalog)
        finally:
            sre_runbooks.sre.list_services = real
        eq("no services: an empty list", none["services"], [])
        ck("no services: words that say what happens next", bool(none["empty_words"]) and not words.offenders(none["empty_words"])
           and DASH not in none["empty_words"], str(none["empty_words"]))

        print("== a platform cell is unchanged ==")
        store.set_setting(edition_mod.EDITION_SETTING, "platform")
        eq("the read is a 404", (await cx.get("/api/sre/runbooks")).status_code, 404)
        eq("the add is a 404", (await cx.post(f"/api/sre/runbooks/{SVC}/lines", headers=oncall, json={"text": "Not here"})).status_code, 404)
        eq("the remove is a 404", (await cx.delete(f"/api/sre/runbooks/{SVC}/lines/0", headers=oncall)).status_code, 404)
        eq("the runbook was not touched", len(entry_lines(store)), 1)
        store.upsert_skill(UID, "plain-skill", "A skill.", "Do the thing.")
        eq("a skill saved the old way works", store.get_skill(UID, "plain-skill")["body"], "Do the thing.")
        eq("and has no updated_by", store.sc.execute("SELECT updated_by FROM shared_skills WHERE name = 'plain-skill'").fetchone()[0], None)
        ck("it is listed to its project", "plain-skill" in listed(store, UID))
        store.set_setting(edition_mod.EDITION_SETTING, "sre")
        eq("back on an AI SRE cell the page is there", (await cx.get("/api/sre/runbooks")).status_code, 200)


def entry_lines(store):
    sk = store.get_skill(None, f"runbook-{SVC}")
    return sre_runbooks.lines_of(sk["body"]) if sk else []


asyncio.run(main())
print(f"{P} passed, {F} failed")
sys.exit(1 if F else 0)
