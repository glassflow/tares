"""The AI SRE Settings page (TR-764): GET /api/sre/settings and PUT /api/sre/settings/{group}.

The seven groups in order, every editable part saved and shown again, the sentence that says
which part was saved and which was not when one fails, who may write (a read key may load the
page and gets 403 on every write, and sees no revoke data), the words (no `trial`, no forbidden
word, no em dash, "1 minute ago" and "10 minutes"), a new cell that says plainly that nothing has
been spent and that Slack is not connected, the budget reset wording, the edition switch and the
keys, and a platform cell that still answers 404 and changes nothing.

The model loop is a stand-in. The cell is the shared fixture (tests/fixtures/sre_cell.py).

Run: .venv/bin/python tests/test_sre_settings.py
     (the Postgres variant: PYTHONPATH=$PWD/tests/pg_harness:$PWD TARES_TEST_PG=<dsn> the same way)
"""
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import scrub_os_environ

scrub_os_environ()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp(prefix="tares-sre-settings-")
ROOT_TOKEN = "root-token-sre-settings"
os.environ["TARES_DB"] = os.path.join(TMP, "t.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = ROOT_TOKEN
os.environ["TARES_EDITION"] = "sre"                     # before make_app(): the first start picks it
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_TRIGGER_DEBOUNCE_SECONDS"] = "0"
os.environ["TARES_SCHEDULE_CLOCK_SECONDS"] = "3600"     # the clock must not fire the watcher under us
os.environ["TARES_WATCH_SECONDS"] = "3600"              # nor the watch loop
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-not-a-real-key"

import httpx

from fixtures.sre_cell import PROJECT, build
from tares import edition as edition_mod
from tares import slack as slack_mod
from tares import sre_home
from tares import watch as W
from tares import words
from tares.builtin_agents import AgentRunner
from tares.screens import sre_settings as S
from tares.store import day_month_words

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def eq(label, got, want):
    ck(label, got == want, f"got {got!r}, want {want!r}")


async def fake_loop(self, agent, trigger_name, key, payload, provider, model, usage, tracer=None,
                    obs=None, concluded=None, produced=None, skills_loaded=None, handoff=False):
    concluded.update({"outcome": "finding", "summary": "looked", "verdict": None, "key": None,
                      "label": None, "headline": None, "next_step": None, "produced": []})
    return "looked", 1, 1, [], False, ""


AgentRunner._loop = fake_loop

UTC = timezone.utc
NOW = datetime.now(UTC).replace(second=0, microsecond=0)
UID = "prj-" + PROJECT
SEEN: list = []                       # every body built, checked for words at the end
SKIP = "value,options,name,text"      # the keys the words check skips: what a person wrote
GROUPS = ["watching", "investigating", "notes", "tools", "people", "edition", "advanced"]
BANNED_WORD = "tri" + "al"
SLACK_TOKEN = "xoxb-test-not-a-real-token"
CHANNELS = [{"id": "C0AAAAAAA1", "name": "oncall", "is_private": False, "is_member": True},
            {"id": "C0BBBBBBB2", "name": "ops-notes", "is_private": False, "is_member": True}]
LISTED = []


async def fake_channels(token, timeout=5.0):
    LISTED.append(token)
    return CHANNELS, None, "", None


slack_mod.list_channels = fake_channels


def field(body, group, key):
    return next((f for f in body[group]["fields"] if f["key"] == key), None)


def fv(body, group, key):
    f = field(body, group, key)
    return f["value"] if f else "(no such field)"


async def main():
    from tares.daemon import make_app
    app = make_app()
    async with app.router.lifespan_context(app):
        auth = {"Authorization": f"Bearer {ROOT_TOKEN}"}
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=auth)
        store, runtime = app.state.store, app.state.runtime

        async def get(client=cx):
            r = await client.get("/api/sre/settings")
            if r.status_code == 200:
                SEEN.append(r.json())
            return r

        async def put(group, body, client=cx):
            r = await client.put(f"/api/sre/settings/{group}", json=body)
            if r.status_code == 200:
                SEEN.append(r.json())
            return r

        # ── a new cell: nothing connected, nothing spent ────────────────────
        print("== a new cell ==")
        r = await get()
        eq("GET answers on an AI SRE cell", r.status_code, 200)
        b = r.json()
        eq("the groups are in the order a person changes things",
           [k for k in b if k != "can_edit"], GROUPS)
        eq("and can_edit comes with them", b["can_edit"], True)
        ck("with no watcher it says nothing has been spent",
           "nothing has been spent" in b["watching"]["empty_words"], b["watching"]["empty_words"])
        ck("and the same for the investigator",
           "nothing has been spent" in b["investigating"]["empty_words"], b["investigating"]["empty_words"])
        eq("the name of the system can still be set", fv(b, "watching", "system_name"), "")
        eq("Slack is not connected", b["notes"]["slack"]["connected"], False)
        ck("and the page says so in words, not an empty line",
           "not connected" in field(b, "notes", "slack_channel")["value_words"], field(b, "notes", "slack_channel"))
        ck("the tools say nothing is connected yet", "Nothing is connected" in b["tools"]["empty_words"],
           b["tools"]["empty_words"])
        eq("no keys yet", (b["people"]["keys"], b["people"]["empty_words"]), ([], "No keys yet."))
        r = await put("watching", {"every": "5m"})
        eq("a change to a watcher that does not exist is refused in a sentence", r.status_code, 400)
        ck("which says there is no watcher",
           "no watcher is set up yet" in r.json()["detail"], r.json()["detail"])
        eq("and says nothing was saved", r.json()["saved"], [])

        # ── the cell: a watcher, an investigator, three tools ───────────────
        build(store, NOW, watcher=True, investigator=True)
        runtime.reload_catalog()
        sre_home.clear_cache()
        store.set_setting(W.SYSTEM_NAME_SETTING, None)
        key_read = (await cx.post("/api/keys", json={"name": "viewer", "scopes": ["read"]})).json()
        read = {"Authorization": f"Bearer {key_read['secret']}"}
        rc = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=read)

        print("== the page ==")
        b = (await get()).json()
        eq("every group, in order", [k for k in b if k != "can_edit"], GROUPS)
        eq("the watching fields in order",
           [f["key"] for f in b["watching"]["fields"]],
           ["system_name", "every", "model", "budget_usd", "budget_period", "rule", "rule_text", "hit_rate"])
        eq("how often it checks", (fv(b, "watching", "every"), field(b, "watching", "every")["value_words"]),
           ("10m", "Every 10 minutes"))
        eq("the watcher's budget", (fv(b, "watching", "budget_usd"), field(b, "watching", "budget_usd")["value_words"]),
           (25.0, "$25 a month"))
        eq("nothing spent yet says so, with the budget",
           field(b, "watching", "budget_usd")["spent_words"], "Nothing spent yet, a budget of $25 a month")
        eq("the investigator's budget never resets", fv(b, "investigating", "budget_period"), "lifetime")
        eq("the wait between investigations", (fv(b, "investigating", "cooldown"),
                                               field(b, "investigating", "cooldown")["value_words"]),
           ("1h", "1 hour"))
        ck("the rule is read-only and shows its words",
           field(b, "watching", "rule_text")["read_only"] and "prod-otlp" in field(b, "watching", "rule_text")["text"],
           field(b, "watching", "rule_text"))
        eq("the hit rate with no answers yet", field(b, "watching", "hit_rate")["value_words"], "0 answers so far")
        eq("the tools are the connected ones, then Slack",
           [t["name"] for t in b["tools"]["rows"]], ["docs-site", "github", "prod-otlp", "Slack"])
        ck("Slack as a tool says it is not connected",
           b["tools"]["rows"][-1]["ok"] is False and "Not connected" in b["tools"]["rows"][-1]["problem_words"],
           b["tools"]["rows"][-1])
        ck("a working tool has no problem to word", b["tools"]["rows"][2]["problem_words"] is None, b["tools"]["rows"][2])
        eq("the edition", (b["edition"]["value"], b["edition"]["label"], b["edition"]["can_switch"]),
           ("sre", "AI SRE", True))
        ck("Advanced is links and a sentence",
           [x["value"] for x in b["advanced"]["links"]]
           == ["/settings/advanced", "/sources", "/explore", "/resources", "/connect", "/reads"]
           and "Advanced setup" in b["advanced"]["note"], b["advanced"])
        ck("the page does not claim a check every 30 seconds",
           "30 seconds" not in json.dumps(b), "30 seconds is in the page")

        # ── who may write ───────────────────────────────────────────────────
        print("== who may write ==")
        r = await get(rc)
        eq("a read key may load the page", r.status_code, 200)
        rb = r.json()
        eq("and it cannot edit", (rb["can_edit"], rb["edition"]["can_switch"], rb["people"]["editable"]),
           (False, False, False))
        ck("it sees no key ids and no revoke", all("id" not in k and "can_revoke" not in k
                                                   for k in rb["people"]["keys"]) and rb["people"]["keys"],
           rb["people"]["keys"])
        ck("while the admin does", all(k["id"] and k["can_revoke"] for k in b["people"]["keys"]) and b["people"]["keys"],
           b["people"]["keys"])
        before = store.get_catalog_agent("watcher", project=UID)
        for group, body in (("watching", {"every": "5m"}), ("investigating", {"cooldown": "2h"}),
                            ("notes", {"watch_channel": "oncall"}), ("people", {"create": {"name": "x", "scopes": ["read"]}}),
                            ("edition", {"value": "factory", "confirm": True})):
            r = await rc.put(f"/api/sre/settings/{group}", json=body)
            eq(f"a read key gets 403 on PUT {group}", r.status_code, 403)
        eq("and nothing changed", store.get_catalog_agent("watcher", project=UID), before)
        eq("not the edition either", edition_mod.edition(store)[0], "sre")
        eq("no key at all is 401", (await httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t").get("/api/sre/settings")).status_code, 401)

        # ── each editable part is stored and shown ──────────────────────────
        print("== watching ==")
        r = await put("watching", {"system_name": "  Checkout   services ", "every": "30m", "budget_usd": 40,
                                   "budget_period": "lifetime", "rule": ["service", "namespace"]})
        eq("a save answers 200", r.status_code, 200)
        b = r.json()
        eq("the name is stored without the extra spaces", store.get_setting(W.SYSTEM_NAME_SETTING), "Checkout services")
        eq("and shown", (fv(b, "watching", "system_name"), field(b, "watching", "system_name")["value_words"]),
           ("Checkout services", "Checkout services"))
        trig = next(t for t in store.list_catalog_triggers() if t["name"] == "watch-prod")
        eq("how often goes to the trigger", trig["condition"]["every"], "30m")
        eq("and the labels it compares", trig["condition"]["summary_by"], ["service", "namespace"])
        eq("the engine reads it back", next(t for t in runtime.catalog.triggers if t.name == "watch-prod").condition.every, 1800.0)
        eq("the page shows how often", field(b, "watching", "every")["value_words"], "Every 30 minutes")
        eq("and what it compares", field(b, "watching", "rule")["value_words"], "Compares by service and namespace")
        agent = store.get_catalog_agent("watcher", project=UID)
        ck("the budget is stored", agent["budget_usd"] == 40.0 and agent["budget_period"] in (None, "lifetime"), agent)
        eq("and shown", (fv(b, "watching", "budget_usd"), field(b, "watching", "budget_usd")["value_words"]),
           (40.0, "$40 in all"))
        ck("and the watcher's other fields did not move",
           agent["prompt"] == before["prompt"] and agent["trigger"] == before["trigger"]
           and agent["concludes"] == before["concludes"], agent)
        eq("and its handoff is still there", [(h["agent"], h["cooldown"]) for h in store.list_handoffs(project=UID, from_agent="watcher")],
           [("investigator", "1h")])
        r = await put("watching", {"every": "1m", "rule": "", "budget_usd": ""})
        b = r.json()
        eq("every minute is worded in the singular", field(b, "watching", "every")["value_words"], "Every minute")
        eq("no labels compares everything", field(b, "watching", "rule")["value_words"], "Compares everything together")
        eq("no budget is worded", (fv(b, "watching", "budget_usd"), field(b, "watching", "budget_usd")["value_words"]),
           (None, "No budget"))
        eq("and spent says nothing spent", field(b, "watching", "budget_usd")["spent_words"], "Nothing spent yet")
        r = await put("watching", {"every": "1h", "budget_usd": 25, "budget_period": "month", "system_name": ""})
        b = r.json()
        eq("an empty name clears it", (store.get_setting(W.SYSTEM_NAME_SETTING), fv(b, "watching", "system_name")), (None, ""))
        eq("every hour is worded in the singular", field(b, "watching", "every")["value_words"], "Every hour")
        model = "claude-opus-5"
        r = await put("watching", {"model": model})
        eq("a listed model is stored", (r.status_code, store.get_catalog_agent("watcher", project=UID)["model"]),
           (200, model))
        eq("and shown", field(r.json(), "watching", "model")["value_words"], "Opus")
        r = await put("watching", {"model": ""})
        eq("the usual model is the empty one", (r.status_code, store.get_catalog_agent("watcher", project=UID)["model"] or ""),
           (200, ""))
        await put("watching", {"every": "10m"})

        print("== investigating ==")
        r = await put("investigating", {"model": "claude-haiku-4-5-20251001", "budget_usd": 12.5,
                                        "budget_period": "month", "cooldown": "2h"})
        eq("a save answers 200", r.status_code, 200)
        b = r.json()
        inv = store.get_catalog_agent("investigator", project=UID)
        eq("the model and budget are stored", (inv["model"], inv["budget_usd"], inv["budget_period"]),
           ("claude-haiku-4-5-20251001", 12.5, "month"))
        eq("and shown", (field(b, "investigating", "model")["value_words"], field(b, "investigating", "budget_usd")["value_words"]),
           ("Haiku", "$12.50 a month"))
        eq("the wait goes to the handoff", [(h["verdict"], h["agent"], h["cooldown"])
                                            for h in store.list_handoffs(project=UID, from_agent="watcher")],
           [("investigate", "investigator", "2h")])
        eq("and shown in words", (fv(b, "investigating", "cooldown"), field(b, "investigating", "cooldown")["value_words"]),
           ("2h", "2 hours"))
        eq("the watcher still hands off to the investigator", store.get_catalog_agent("watcher", project=UID)["handoffs"],
           [{"verdict": "investigate", "agent": "investigator", "cooldown": "2h"}])
        r = await put("investigating", {"cooldown": "1m"})
        eq("a wait of a minute is worded in the singular", field(r.json(), "investigating", "cooldown")["value_words"], "1 minute")
        await put("investigating", {"cooldown": "1h", "budget_period": "lifetime", "budget_usd": 25, "model": ""})

        print("== when the budget resets ==")
        r = await put("watching", {"budget_period": "month"})
        b = r.json()
        f = field(b, "watching", "budget_period")
        resets = store.budget_resets_on({"budget_period": "month"}, NOW)
        eq("a monthly budget is stored as a month", store.get_catalog_agent("watcher", project=UID)["budget_period"], "month")
        eq("and the page says which day it starts again", (f["value"], f["resets_words"]),
           ("month", "Starts again on " + day_month_words(resets)))
        r = await put("watching", {"budget_period": "lifetime"})
        f = field(r.json(), "watching", "budget_period")
        eq("a lifetime budget has no reset day", (f["value"], f["resets_words"], f["resets_on"]), ("lifetime", None, None))
        ck("and is stored as lifetime", store.get_catalog_agent("watcher", project=UID)["budget_period"] in (None, "lifetime"),
           store.get_catalog_agent("watcher", project=UID)["budget_period"])
        r = await put("watching", {"budget_period": "weekly"})
        eq("a period that does not exist is refused", r.status_code, 400)
        await put("watching", {"budget_period": "month"})

        print("== the spent line ==")
        rid = "run_spent_1"
        store.start_agent_run(rid, "watcher", "watch-prod", "d", "old", "h")
        store.finish_agent_run(rid, "ok")
        store.sc.execute("UPDATE agent_runs SET cost_usd = ?, started_at = ? WHERE id = ?",
                         [4.2, datetime.now(UTC), rid])
        b = (await get()).json()
        eq("a monthly spend says so", field(b, "watching", "budget_usd")["spent_words"], "Spent $4.20 of $25 this month")
        store.sc.execute("UPDATE agent_runs SET cost_usd = ? WHERE id = ?", [25.0, rid])
        b = (await get()).json()
        eq("a used up monthly budget says when it starts again", field(b, "watching", "budget_usd")["spent_words"],
           "Spent $25.00 of $25 this month, starts again on " + day_month_words(resets))
        store.sc.execute("DELETE FROM agent_runs WHERE id = ?", [rid])

        print("== notes ==")
        r = await put("notes", {"watch_channel": "#oncall", "slack_channel": "C0AAAAAAA1"})
        eq("a channel save answers 200 even with Slack not connected", r.status_code, 200)
        eq("the channel for services nobody watches is stored without the #", store.get_setting(W.CHANNEL_SETTING), "oncall")
        eq("the Slack channel goes to the watcher and the investigator",
           (store.get_catalog_agent("watcher", project=UID)["slack_channel"],
            store.get_catalog_agent("investigator", project=UID)["slack_channel"]), ("C0AAAAAAA1", "C0AAAAAAA1"))
        b = r.json()
        ck("without Slack the page says a channel needs Slack", "not connected" in field(b, "notes", "slack_channel")["value_words"],
           field(b, "notes", "slack_channel"))
        store.set_setting(slack_mod.SETTING_KEY, SLACK_TOKEN)
        S._CHANNELS.clear()
        b = (await get()).json()
        eq("with Slack connected the channels are offered by name",
           [o["label"] for o in field(b, "notes", "slack_channel")["options"]], ["No channel", "#oncall", "#ops-notes"])
        eq("and the chosen one is worded by its name", field(b, "notes", "slack_channel")["value_words"], "#oncall")
        eq("Slack is connected", (b["notes"]["slack"]["connected"], b["tools"]["rows"][-1]["ok"]), (True, True))
        n = len(LISTED)
        await get()
        await get()
        eq("Slack is asked once a minute, not on every read", len(LISTED), n)
        r = await put("notes", {"slack_channel": "C0BBBBBBB2"})
        eq("another channel", (r.status_code, field(r.json(), "notes", "slack_channel")["value_words"]), (200, "#ops-notes"))
        r = await put("notes", {"slack_channel": ""})
        eq("no channel says so in words", field(r.json(), "notes", "slack_channel")["value_words"],
           "No channel chosen, so notes are not sent to Slack.")
        r = await put("notes", {"slack_channel": "general"})
        eq("a channel name instead of its Slack ID is refused", r.status_code, 400)
        ck("in a sentence that says what to do", "pick a channel from the list" in r.json()["detail"], r.json()["detail"])
        await put("notes", {"watch_channel": "", "slack_channel": "C0AAAAAAA1"})
        eq("an empty channel clears the setting", store.get_setting(W.CHANNEL_SETTING), None)

        # ── the first part that fails stops the rest ────────────────────────
        print("== stop at the first failure ==")
        store.set_setting(W.SYSTEM_NAME_SETTING, None)
        await put("watching", {"every": "10m", "budget_usd": 25, "budget_period": "month"})
        r = await put("watching", {"system_name": "Prod", "every": "10m", "budget_usd": -3, "budget_period": "lifetime"})
        eq("it answers 400", r.status_code, 400)
        d = r.json()
        eq("and says what was saved, what was not, and what was not tried", d["detail"],
           "Saved the name and how often it checks. Did not save the budget: it must be more than 0 dollars, "
           "or empty for no limit. Not tried: when the budget resets.")
        eq("the parts are named in the body too", (d["saved"], d["failed"], d["not_tried"]),
           (["system_name", "every"], "budget_usd", ["budget_period"]))
        eq("the part before it was saved", store.get_setting(W.SYSTEM_NAME_SETTING), "Prod")
        eq("the part after it was not", store.get_catalog_agent("watcher", project=UID)["budget_period"], "month")
        eq("and the one that failed is as it was", store.get_catalog_agent("watcher", project=UID)["budget_usd"], 25.0)
        r = await put("watching", {"every": "10s", "system_name": "Other"})
        eq("a first part that fails after one that saved", r.json()["detail"],
           "Saved the name. Did not save how often it checks: it must be at least 1 minute.")
        eq("the name was saved", store.get_setting(W.SYSTEM_NAME_SETTING), "Other")
        r = await put("watching", {"every": "10s"})
        eq("a first part that fails says nothing was saved", r.json()["detail"],
           "Nothing was saved. Did not save how often it checks: it must be at least 1 minute.")
        r = await put("investigating", {"cooldown": "soon"})
        eq("a wait that is not a length of time", (r.status_code, r.json()["detail"]),
           (400, "Nothing was saved. Did not save the wait between investigations: "
                 "it must be a length of time such as 10m or 1h."))
        r = await put("watching", {"colour": "red", "every": "5m"})
        eq("a part that does not exist is refused before anything is saved", (r.status_code, r.json()["saved"]), (400, []))
        eq("and the part that did exist was not saved", next(t for t in store.list_catalog_triggers()
                                                              if t["name"] == "watch-prod")["condition"]["every"], "10m")
        r = await put("watching", {"model": "gpt-nothing"})
        eq("a model that is not set up is refused", r.status_code, 400)
        ck("with a sentence", "not one of the models set up here" in r.json()["detail"], r.json()["detail"])
        r = await put("watching", {"rule_text": "x"})
        eq("a part that is only shown cannot be written", r.status_code, 400)
        r = await put("watching", {"rule": ["a", "b", "c", "d", "e", "f"]})
        eq("more than five labels are refused", r.status_code, 400)
        r = await put("watching", {})
        eq("an empty save is refused", r.status_code, 400)
        r = await cx.put("/api/sre/settings/tools", json={"x": 1})
        eq("a group with nothing to change is 404", r.status_code, 404)
        r = await cx.put("/api/sre/settings/watching", content=b"[1]", headers={"content-type": "application/json"})
        eq("a body that is not an object is refused", r.status_code, 400)

        # ── people and keys ─────────────────────────────────────────────────
        print("== people and keys ==")
        r = await put("people", {"create": {"name": "grafana", "scopes": ["ingest"]}})
        eq("a key is made", r.status_code, 200)
        b = r.json()
        made = b["created_key"]
        ck("its secret is in this answer", made["secret"].startswith("nvf_") and made["name"] == "grafana", made)
        ck("and the list never shows a secret", "secret" not in json.dumps(b["people"]) and "nvf_" not in json.dumps(b["people"]),
           b["people"])
        row = next(k for k in b["people"]["keys"] if k["name"] == "grafana")
        eq("it is worded for people", (row["scopes_words"], row["last_used_words"]), ("Can send data", "Never used"))
        r2 = await httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                     headers={"Authorization": "Bearer " + made["secret"]}).get("/api/sre/settings")
        eq("the new key is real: a key that only sends data cannot read the page", r2.status_code, 403)
        store.sc.execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?", [datetime.now(UTC) - timedelta(minutes=1, seconds=5), made["id"]])
        b = (await get()).json()
        eq("a key used a minute ago is worded in the singular",
           next(k for k in b["people"]["keys"] if k["name"] == "grafana")["last_used_words"], "Used 1 minute ago")
        store.sc.execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?", [datetime.now(UTC) - timedelta(minutes=12), made["id"]])
        b = (await get()).json()
        eq("and one used twelve minutes ago in the plural",
           next(k for k in b["people"]["keys"] if k["name"] == "grafana")["last_used_words"], "Used 12 minutes ago")
        r = await put("people", {"create": {"name": "", "scopes": ["read"]}})
        eq("a key needs a name", r.status_code, 400)
        r = await put("people", {"create": {"name": "n" * 81, "scopes": ["read"]}})
        eq("a name over 80 characters is a 400, not a crash", r.status_code, 400)
        eq("and says what is wrong in plain words", r.json()["detail"], "The key's name: it can be at most 80 characters.")
        r = await put("people", {"create": {"name": 7, "scopes": ["read"]}})
        eq("a name that is not text is a 400", r.status_code, 400)
        eq("with a plain message", r.json()["detail"], "The key's name: it must be text.")
        r = await put("people", {"create": {"name": "x", "scopes": ["findings"]}})
        eq("and a scope it can have", r.status_code, 400)
        r = await put("people", {"revoke": made["id"]})
        eq("a key is revoked", r.status_code, 200)
        ck("and gone from the list", "grafana" not in [k["name"] for k in r.json()["people"]["keys"]], r.json()["people"])
        r = await put("people", {"revoke": made["id"]})
        eq("revoking it again is a plain 404", (r.status_code, "not active" in r.json()["detail"]), (404, True))
        os.environ["TARES_WORKSPACE_API_URL"] = "https://workspace.invalid"
        try:
            b = (await get()).json()
        finally:
            del os.environ["TARES_WORKSPACE_API_URL"]
        eq("on the cloud there is a link to manage people", b["people"]["workspace"]["link"]["value"],
           "/settings/advanced?tab=workspace")
        eq("and elsewhere there is none", (await get()).json()["people"]["workspace"], None)

        # ── the edition ─────────────────────────────────────────────────────
        print("== the edition ==")
        r = await put("edition", {"value": "factory"})
        eq("switching needs the confirm flag", (r.status_code, r.json().get("needs_confirm")), (409, True))
        eq("and nothing changed", edition_mod.edition(store)[0], "sre")
        r = await put("edition", {"value": "nonsense", "confirm": True})
        eq("an edition that does not exist is refused", r.status_code, 400)
        r = await put("edition", {"value": "sre"})
        eq("the same edition is just the page", (r.status_code, r.json()["edition"]["value"]), (200, "sre"))

        # ── the words ───────────────────────────────────────────────────────
        print("== the words ==")
        bad, trial = [], 0
        for body in SEEN:
            bad += words.offenders_in(body, SKIP.split(","))
            trial += json.dumps(body).lower().count(BANNED_WORD)
        eq(f"{len(SEEN)} bodies, no forbidden word and no em dash outside what a person wrote", bad, [])
        eq("and the word trial is nowhere", trial, 0)
        out = json.dumps((await get()).json())
        ck("the check is real: it catches a word", words.offenders_in({"x": "the source"}, SKIP.split(",")) != [], "")
        ck("and an em dash", words.offenders_in({"x": "a " + chr(0x2014) + " b"}, SKIP.split(",")) != [], "")

        # ── a platform cell is unchanged ────────────────────────────────────
        print("== a platform cell ==")
        before = (store.get_catalog_agent("watcher", project=UID), store.get_setting(W.SYSTEM_NAME_SETTING),
                  next(t for t in store.list_catalog_triggers() if t["name"] == "watch-prod"))
        store.set_setting(edition_mod.EDITION_SETTING, "platform")
        eq("GET is 404", (await cx.get("/api/sre/settings")).status_code, 404)
        for group, body in (("watching", {"every": "5m", "system_name": "Changed"}), ("investigating", {"cooldown": "2h"}),
                            ("notes", {"watch_channel": "x"}), ("people", {"create": {"name": "x", "scopes": ["read"]}}),
                            ("edition", {"value": "factory", "confirm": True})):
            eq(f"PUT {group} is 404", (await cx.put(f"/api/sre/settings/{group}", json=body)).status_code, 404)
        eq("and nothing changed", (store.get_catalog_agent("watcher", project=UID), store.get_setting(W.SYSTEM_NAME_SETTING),
                                   next(t for t in store.list_catalog_triggers() if t["name"] == "watch-prod")), before)
        eq("no key was made", [k["name"] for k in store.list_api_keys() if k["name"] == "x"], [])
        eq("the edition is still platform", edition_mod.edition(store)[0], "platform")

        # ── the edition switch, last: it leaves the page ────────────────────
        print("== switching away ==")
        store.set_setting(edition_mod.EDITION_SETTING, "sre")
        r = await put("edition", {"value": "factory", "confirm": True})
        eq("a confirmed switch answers 200", (r.status_code, r.json()["edition"]), (200, "factory"))
        eq("and is stored", edition_mod.edition(store)[0], "factory")
        eq("the settings page is then 404", (await cx.get("/api/sre/settings")).status_code, 404)


asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
