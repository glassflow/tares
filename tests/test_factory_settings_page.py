"""The factory Settings page (TR-738): GET /api/factory/settings.

One read: the tools (connected or not, worded), the crew's settings as the page shows them, the
grants with their words, dates and, for a desk answer like "D1 A", the decision's question, the
model and budget, the keys, the edition, the links under Advanced; a read key gets the page with
`can_change` false; changes go through the routes that exist and read back; no forbidden word in
anything but a person's own grant words.

Run: .venv/bin/python tests/test_factory_settings_page.py
"""
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="tares-factory-settings-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-settings"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_EDITION"] = "factory"
os.environ.pop("TARES_WORKSPACE_URL", None)
for _k in ("TARES_SLACK_BOT_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
    os.environ.pop(_k, None)

import httpx

from tares import words
from tares.screens import factory_settings as fs

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def units():
    print("== the helpers ==")
    off = {"on": False}
    ck("pacing off says what the crew does", fs.pace_text(off).startswith("Pacing is off."), fs.pace_text(off))
    ck("pacing on with no reading says so",
       "No reading" in fs.pace_text({"on": True, "known": False}), fs.pace_text({"on": True, "known": False}))
    ck("pacing on with room", fs.pace_text({"on": True, "known": True, "state": "ok", "level": 0})
       == "Pacing is on. The plan has room.")
    slow = {"on": True, "known": True, "state": "slow", "level": 1, "session_pct": 88, "weekly_pct": 40}
    hold = {"on": True, "known": True, "state": "hold", "level": 2, "session_pct": 96, "weekly_pct": 91}
    ck("pacing slow names the limit and the effect",
       fs.pace_text(slow) == "Pacing is on, and the 5-hour window is at 88%, so no new builder starts.",
       fs.pace_text(slow))
    ck("pacing hold names both limits",
       fs.pace_text(hold) == "Pacing is on, and the 5-hour window is at 96% and the weekly limit is at 91%, "
                             "so builders finish their step and wait.", fs.pace_text(hold))
    ck("no pace text carries an internal word", all(words.offenders(fs.pace_text(p)) == []
                                                    for p in (off, slow, hold)))
    m = fs.GRANT_LINE.match('- "may merge, (not) this" (2026-10-30)')
    ck("a grant line is its words and its day", m and m.groups() == ("may merge, (not) this", "2026-10-30"),
       m and m.groups())
    ck("a desk answer is one D-number and a short reply",
       [bool(fs.DESK_ANSWER.match(x)) for x in ("D79 A", "D79", "D7 yes", "D79 A and more of it please",
                                                 "may do D79 A")] == [True, True, True, False, False])


async def main():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store
    mk = lambda token=TOKEN: httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t",
        headers={"Authorization": f"Bearer {token}"} if token else {})

    def clean(label, body):
        found = words.offenders_in(body, ("text", "question", "detail", "name", "label", "project", "href"))
        ck(label + ": no forbidden word or dash outside a person's words", found == [], found)

    async with app.router.lifespan_context(app):
        async with mk() as cx:
            print("== a new cell ==")
            r = await cx.get("/api/factory/settings")
            a = r.json()
            ck("the page answers in one read", r.status_code == 200, r.text[:300])
            clean("a new cell", a)
            ck("an admin can change", a["can_change"] is True, a["can_change"])
            ck("four tools, none connected, each with a place to connect it",
               [t["key"] for t in a["tools"]] == ["github", "linear", "slack", "claude_code"]
               and not any(t["connected"] for t in a["tools"])
               and all(t["href"] and t["detail"] for t in a["tools"]), a["tools"])
            ck("Claude Code says how long nothing reported",
               a["tools"][3]["detail"] == "Not connected. Nothing has reported in 7 days.", a["tools"][3])
            c = a["crew"]
            ck("the crew's defaults: up to merge, releases on, three builders, no second check, no pacing",
               c["how_far"]["value"] == "L3-review" and c["how_far"]["text"] == "Up to merge"
               and c["how_far"]["releases"] is True and c["release_profile"] is False
               and c["builders_max"] == 3 and c["builders_text"] == "Up to 3 builders at once"
               and c["challenger"] is False and c["pace"] is False
               and c["pace_text"].startswith("Pacing is off."), c)
            ck("no grants", c["grants"] == [] and c["grants_total"] == 0, c["grants"])
            ck("a model not chosen yet and no budget",
               a["models"]["default_provider"] == "Not chosen yet" and a["models"]["chosen"] is False
               and a["models"]["budget_text"].startswith("No budget set."), a["models"])
            ck("no access keys yet, no cloud team link",
               a["people"]["keys"]["text"] == "No access keys yet" and a["people"]["keys"]["count"] == 0
               and a["people"]["cloud_team_href"] is None, a["people"])
            e = a["edition"]
            ck("the edition: factory, can be switched, the other ones offered, with the note",
               e["value"] == "factory" and e["label"] == "Software Factory" and e["can_switch"] is True
               and [o["value"] for o in e["options"]] == ["sre", "platform"]
               and e["other"] == {"value": "sre", "label": "AI SRE"}
               and e["note"].startswith("Switching changes the screens, not your data."), e)
            ck("six Advanced links, in order",
               [x["href"] for x in a["advanced"]]
               == ["/settings/advanced", "/sources", "/explore", "/resources", "/connect", "/reads"]
               and a["advanced"][1]["label"] == "Connections", a["advanced"])

            print("== changes go through the routes that exist ==")
            r = await cx.put("/api/crew/settings", json={"builders_max": 4})
            ck("builders to 4", r.status_code == 200, r.text)
            r = await cx.put("/api/crew/settings", json={"autonomy": "L4-ship", "challenger": True,
                                                         "pace": True, "release_profile": "ship it with make release"})
            ck("how far, second check, pacing and a release profile", r.status_code == 200, r.text)
            await cx.put("/api/crew/settings", json={"releaser": False})
            c = (await cx.get("/api/factory/settings")).json()["crew"]
            ck("the page reads them back",
               c["builders_max"] == 4 and c["builders_text"] == "Up to 4 builders at once"
               and c["how_far"]["value"] == "L4-ship"
               and c["how_far"]["text"] == "Merges on its own after review and green CI, no releases"
               and c["how_far"]["releases"] is False and c["release_profile"] is True
               and c["challenger"] is True and c["pace"] is True,
               {k: v for k, v in c.items() if k != "grants"})
            ck("pacing on with no plan reading says so", "No reading" in c["pace_text"], c["pace_text"])
            clean("the crew", c)
            r = await cx.put("/api/crew/settings", json={"builders_max": 0})
            ck("a wrong value is refused", r.status_code in (400, 422), r.status_code)

            print("== grants ==")
            px = (await cx.post("/api/projects", json={"template": "custom", "name": "project-x", "objects": [],
                                                       "goal": "g", "kind": "software_factory"})).json()["id"]
            item = store.add_desk_item(None, "Should the crew squash every pull request it merges?", "", ["A", "B"],
                                       "A", "Keeps history short.", "", [], False, "crew-orchestrator", [],
                                       datetime.now(timezone.utc), None, None)
            store.add_decision(None, item["id"], "Should the crew squash every pull request it merges?",
                               "A", "A", True, "all", True, None, True, [], datetime.now(timezone.utc))
            label = item["label"]
            for body in ({"words": "Alex may merge any pull request once CI is green", "scope": "all"},
                         {"words": f"{label} A", "scope": "all"},
                         {"words": "Kim may push to the release branch <b>now</b>", "scope": "project",
                          "project": px}):
                r = await cx.post("/api/grants", json=body)
                assert r.status_code == 201, r.text
            old = (datetime.now(timezone.utc) - timedelta(days=4)).date().isoformat()
            gid = store.get_setting("global_grants")
            doc = store.get_doc(None, gid)
            today = datetime.now(timezone.utc).date().isoformat()
            lines = doc["body"].splitlines()
            lines[next(i for i, x in enumerate(lines) if x.startswith("- \"Alex"))] = \
                f'- "Alex may merge any pull request once CI is green" ({old})'
            store.update_doc(gid, body="\n".join(lines) + "\n", by="tares")
            c = (await cx.get("/api/factory/settings")).json()["crew"]
            g = c["grants"]
            ck("three grants, counted", len(g) == 3 and c["grants_total"] == 3, g)
            ck("a grant is its words, where it holds and when it was given",
               {x["text"]: (x["scope_text"], x["state_text"]) for x in g}
               == {"Alex may merge any pull request once CI is green": ("For every project", "given 4 days ago"),
                   f"{label} A": ("For every project", "given today"),
                   "Kim may push to the release branch <b>now</b>": ("In project-x", "given today")},
               g)
            ck("the newest are first", g[-1]["text"].startswith("Alex"), [x["text"] for x in g])
            q = next(x for x in g if x["text"] == f"{label} A")
            ck("a desk answer shows the decision's question",
               q.get("question") == "Should the crew squash every pull request it merges?", q)
            ck("a grant that is not a desk answer has no question",
               all("question" not in x for x in g if x is not q), g)
            ck("each grant has an id", len({x["id"] for x in g}) == 3 and all(x["id"] for x in g), g)
            clean("the grants", c)

            print("== tools ==")
            store.upsert_github_credential("main", "ghp_x", "token", "", "example-org")
            t = (await cx.get("/api/factory/settings")).json()["tools"]
            ck("GitHub connected with a token says as whom",
               t[0]["connected"] is True and t[0]["detail"] == "Connected as example-org", t[0])
            r = await cx.post("/api/sources", json={"name": "claude-code", "type": "ingest",
                                                    "connector": "claude_code", "config": {}})
            ts = datetime.now(timezone.utc) - timedelta(minutes=2)
            await cx.post("/ingest/claude_code", headers={"Content-Type": "application/x-ndjson"},
                          content=json.dumps({"type": "user", "sessionId": "s1", "cwd": "/w",
                                              "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                                              "message": {"role": "user", "content": "hi"}}) + "\n")
            t = (await cx.get("/api/factory/settings")).json()["tools"]
            cc = t[3]
            ck("Claude Code that reported lately is connected, with when",
               cc["connected"] is True and cc["detail"].startswith("Reporting: last session "), cc)
            clean("the tools", t)

            print("== keys, models, edition ==")
            k = (await cx.post("/api/keys", json={"name": "reader", "scopes": ["read"]})).json()
            await cx.post("/api/keys", json={"name": "writer", "scopes": ["ingest"]})
            a = (await cx.get("/api/factory/settings")).json()
            ck("keys are counted", a["people"]["keys"] == {"count": 2, "text": "2 access keys"}, a["people"])
            r = await cx.put("/api/settings/edition", json={"edition": "sre"})
            a = (await cx.get("/api/factory/settings")).json()
            ck("the edition follows a switch", r.status_code == 200 and a["edition"]["value"] == "sre"
               and a["edition"]["label"] == "AI SRE" and a["edition"]["other"]["value"] == "factory", a["edition"])
            await cx.put("/api/settings/edition", json={"edition": "factory"})
            clean("the whole page", a)

            print("== a read key ==")
            async with mk(k["secret"]) as rx:
                r = await rx.get("/api/factory/settings")
                b = r.json()
                ck("a read key gets the page", r.status_code == 200, r.text[:200])
                ck("and cannot change anything", b["can_change"] is False and b["edition"]["can_switch"] is False, b)
                ck("it sees the same grants", len(b["crew"]["grants"]) == 3)
                ck("its writes are refused",
                   (await rx.put("/api/crew/settings", json={"builders_max": 5})).status_code == 403)
                ck("including the edition",
                   (await rx.put("/api/settings/edition", json={"edition": "sre"})).status_code == 403)
            ck("no token, no page", (await mk(None).get("/api/factory/settings")).status_code == 401)

            print("== a hostile cell ==")
            store.set_setting("crew_settings", "not json")
            store.set_setting("slack_team", "[1, 2]")
            r = await cx.get("/api/factory/settings")
            ck("broken stored settings still give the page, with defaults",
               r.status_code == 200 and r.json()["crew"]["builders_max"] == 3, r.text[:200])


units()
asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
