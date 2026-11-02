"""The build, recorded (M5: TR-433, TR-434, TR-435, TR-436).

Assumptions numbered per project and tied to tickets, decided only by the orchestrator (or the
person's own session); checks kept and written as one line into the Checks part of the working doc
without touching the rest; the project's own memory; subagent transcripts shipped by the plugin
from <session>/subagents/, tagged and capped, and landing under the parent session on Tares; the
pick-up carrying the ticket's assumptions and checks and the project's memory.

Run: .venv/bin/python tests/test_factory_build.py
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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "claude-plugin", "scripts"))

_TMP = tempfile.mkdtemp(prefix="tares-factory-build-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-build"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

import httpx

import ship
import tares.mcp_server as m
import tares.mcp_factory as mf
from tares import factory

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def unit():
    print("== a check goes into the Checks part of Progress, the rest untouched ==")
    doc = "## Goal\nShip it.\n\n## Progress\nStep 1 done.\n\n## Notes\nkeep\n"
    out = factory.add_check_to_doc(doc, "- `make test` -> 12 passed")
    ck("Checks is made inside Progress, before the next section",
       "## Progress\nStep 1 done.\n\n### Checks\n\n- `make test` -> 12 passed\n\n## Notes\nkeep" in out, out)
    out2 = factory.add_check_to_doc(out, "- `make lint` -> clean")
    ck("the next check goes under the last one",
       "- `make test` -> 12 passed\n- `make lint` -> clean\n" in out2, out2)
    out3 = factory.add_check_to_doc("# T1\n", "- x")
    ck("no Progress section: one is made at the end", out3.rstrip().endswith("## Progress\n\n### Checks\n\n- x"), out3)
    ck("a check line names the commit and the broken test",
       factory.check_line("pytest -q", "3 passed", "abcdef12", "test_parse")
       == "- [abcdef1] `pytest -q` -> 3 passed (broke the code under `test_parse`: it failed, then passed again)")


def plugin():
    print("== the plugin ships subagent transcripts ==")
    d = os.path.join(_TMP, "proj")
    os.makedirs(os.path.join(d, "s-1", "subagents"))
    transcript = os.path.join(d, "s-1.jsonl")
    open(transcript, "w").write("")
    sub = os.path.join(d, "s-1", "subagents", "agent-a77.jsonl")
    lines = [{"type": "user", "isSidechain": True, "agentId": "a77", "sessionId": "s-1",
              "message": {"role": "user", "content": "Implement the parser"}},
             {"type": "assistant", "isSidechain": True, "agentId": "a77", "sessionId": "s-1",
              "message": {"role": "assistant", "content": "Done: parser.py"}},
             {"type": "attachment", "isSidechain": True}]
    open(sub, "w").write("\n".join(json.dumps(x) for x in lines) + "\n")
    json.dump({"agentType": "general-purpose", "description": "Write the parser"},
              open(sub[:-6] + ".meta.json", "w"))
    sent = []
    real = ship.ship_lines
    ship.ship_lines = lambda cfg, objs: sent.extend(objs) or True
    cfg = {"base": "http://x", "headers": {}, "data_dir": os.path.join(_TMP, "pd")}
    os.makedirs(cfg["data_dir"], exist_ok=True)
    hook = {"session_id": "s-1", "transcript_path": transcript, "cwd": "/w"}
    try:
        ship.ship_subagents(cfg, hook, "", "Shop")
        ck("two lines shipped, bookkeeping dropped", len(sent) == 2, sent)
        ck("tagged with the subagent, its type and task, under the parent session",
           all(o["subagent"] == "a77" and o["sessionId"] == "s-1" and o["isSidechain"]
               and o["subagent_type"] == "general-purpose"
               and o["subagent_description"] == "Write the parser" and o["tares_project"] == "Shop"
               for o in sent), sent)
        sent.clear()
        ship.ship_subagents(cfg, hook, "", "Shop")
        ck("nothing shipped twice", sent == [], sent)
        open(sub, "a").write(json.dumps({"type": "assistant", "agentId": "a77", "sessionId": "s-1",
                                         "message": {"role": "assistant", "content": "more"}}) + "\n")
        ship.ship_subagents(cfg, hook, "", "Shop")
        ck("new lines ship from where it stopped", len(sent) == 1 and sent[0]["message"]["content"] == "more", sent)
        sent.clear()
        ship.MAX_SUBAGENT_LINES = 4
        open(sub, "a").write("\n".join(json.dumps({"type": "assistant", "sessionId": "s-1",
                                                   "message": {"role": "assistant", "content": f"m{i}"}})
                                       for i in range(5)) + "\n")
        ship.ship_subagents(cfg, hook, "", "Shop")
        ck("past the cap: one line kept, then a note that the rest is not recorded",
           len(sent) == 2 and sent[-1]["type"] == "subagent_truncated", [o.get("type") for o in sent])
        sent.clear()
        open(sub, "a").write(json.dumps({"type": "assistant", "sessionId": "s-1",
                                         "message": {"role": "assistant", "content": "late"}}) + "\n")
        ship.ship_subagents(cfg, hook, "", "Shop")
        ck("after the cap nothing more ships", sent == [], sent)
    finally:
        ship.ship_lines = real
        ship.MAX_SUBAGENT_LINES = 3000


def main_backlog():
    print("== a long session's backlog ships in chunks (TR-809) ==")
    d = os.path.join(_TMP, "backlog")
    os.makedirs(d)
    transcript = os.path.join(d, "s-9.jsonl")
    cfg = {"base": "http://x", "headers": {}, "data_dir": os.path.join(_TMP, "pd-backlog")}
    os.makedirs(cfg["data_dir"], exist_ok=True)
    hook = {"session_id": "s-9", "transcript_path": transcript, "cwd": "/w"}

    def msg(i, size=0):
        return json.dumps({"type": "user", "sessionId": "s-9",
                           "message": {"role": "user", "content": f"m{i}" + "x" * size}}) + "\n"

    with open(transcript, "w") as f:
        for i in range(40):
            f.write(msg(i, 200))
        f.write(msg(40, 5000))     # one line larger than a whole read
        f.write(msg(41, 20000))    # one line too large to send
        f.write(msg(42))
    size = os.path.getsize(transcript)
    off_file = os.path.join(cfg["data_dir"], "offsets",
                            ship.hashlib.sha1(transcript.encode()).hexdigest()[:16] + ".off")

    def offset():
        try:
            return int(open(off_file).read())
        except OSError:
            return 0

    sent, posts, fail = [], [], [False]
    real = (ship.ship_lines, ship.MAX_MAIN_READ, ship.MAX_MAIN_LINE)
    ship.ship_lines = lambda c, objs: (not fail[0]) and (posts.append(len(objs)) or sent.extend(objs) or True)
    ship.MAX_MAIN_READ, ship.MAX_MAIN_LINE = 2048, 8192
    try:
        fail[0] = True
        ok, _ = ship.ship_main(cfg, hook, transcript, "")
        ck("a failed post leaves the offset where it was", not ok and offset() == 0)
        fail[0] = False
        seen = [offset()]
        for _ in range(40):
            ok, _ = ship.ship_main(cfg, hook, transcript, "")
            if offset() == seen[-1]:
                break
            seen.append(offset())
        ck("every hook moves the offset until the end of the file", seen[-1] == size, (seen[-1], size))
        ck("no read sends more than the cap",
           all(b - a <= 2048 for a, b in zip(seen, seen[1:])
               if b - a not in (len(msg(40, 5000)), len(msg(41, 20000)))), seen)
        texts = [o["message"]["content"] for o in sent if o.get("type") == "user"]
        ck("every line ships once, in order, the large one alone",
           [t[:3].rstrip("x") for t in texts] == [f"m{i}" for i in range(41)] + ["m42"]
           and texts[40] == "m40" + "x" * 5000, [t[:4] for t in texts])
        notes = [o for o in sent if o.get("type") == "session_warning"]
        ck("the line too large to send is stepped over with a warning Tares renders",
           len(notes) == 1 and "too large to record" in notes[0]["warning"], notes)
        sent.clear()
        ok, _ = ship.ship_main(cfg, hook, transcript, "")
        ck("nothing ships twice", ok and sent == [], sent)
        with open(transcript, "a") as f:
            f.write(msg(43, 3000)[:-1])    # a large line still being written
        ok, _ = ship.ship_main(cfg, hook, transcript, "")
        ck("a large unfinished line waits", ok and sent == [] and offset() == size)
        with open(transcript, "a") as f:
            f.write("\n")
        ship.ship_main(cfg, hook, transcript, "")
        ck("and ships once it ends", len(sent) == 1 and sent[0]["message"]["content"].startswith("m43"))

        # SessionEnd: no hook follows, so the whole backlog goes out in that one hook
        with open(transcript, "a") as f:
            for i in range(44, 84):
                f.write(msg(i, 200))
        sent.clear()
        before = len(posts)
        ship.drain_main(cfg, hook, transcript, "")
        ck("SessionEnd drains the whole backlog in chunks",
           offset() == os.path.getsize(transcript) and len(posts) - before > 1
           and [o["message"]["content"][:3] for o in sent] == [f"m{i}" for i in range(44, 84)],
           (offset(), os.path.getsize(transcript), len(posts) - before))
        with open(transcript, "a") as f:
            f.write(msg(84, 200))
        fail[0] = True
        ship.drain_main(cfg, hook, transcript, "")
        ck("a failed post stops the drain without moving the offset",
           offset() == os.path.getsize(transcript) - len(msg(84, 200)))
    finally:
        ship.ship_lines, ship.MAX_MAIN_READ, ship.MAX_MAIN_LINE = real


def ago(minutes):
    t = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    return t.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


async def main():
    from tares.daemon import make_app
    app = make_app()
    root = {"Authorization": f"Bearer {TOKEN}"}
    m.TARESD = mf.TARESD = "http://t"
    headers = dict(root)
    mk = lambda timeout=10: httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                              base_url="http://t", headers=dict(headers))
    m._cx = mf._cx = mk

    def as_station(station="", role="", parent=""):
        headers.clear()
        headers.update(root)
        if station:
            headers.update({"X-Tares-Station": station, "X-Tares-Role": role})
        if parent:
            headers["X-Tares-Parent"] = parent

    async with app.router.lifespan_context(app):
        shop = json.loads(await m.create_project("Shop", "Sell things"))["id"]
        async with mk() as cx:
            await cx.post("/api/sources", json={"name": "claude_code", "connector": "claude_code",
                                                "poll": "10s", "config": {"push": True}, "project": shop})
        await m.write_ticket("Catalog", project="Shop")
        await m.write_ticket("Cart", project="Shop")
        wd = json.loads(await m.write_doc("working", "Working: Catalog",
                                          "## Goal\nList products.\n\n## Progress\nStarted.\n", project="Shop"))
        await m.attach_working_doc("T1", wd["id"], project="Shop")

        print("== assumptions (TR-433) ==")
        as_station("shop-build-m1", "builder", "crew-orchestrator")
        a1 = json.loads(await mf.assume("Which currency?", "EUR", "the spec mentions Berlin", ["T1"],
                                        "pricing", project="Shop"))
        as_station("shop-build-m2", "builder", "crew-orchestrator")
        a2 = json.loads(await mf.assume("Page size?", "20", "a common default", ["T2"], project="Shop"))
        ck("two builders get A1 and A2", (a1["label"], a2["label"]) == ("A1", "A2"), (a1, a2))
        ck("each tied to its ticket, open", a1["tickets"] == ["T1"] and a1["state"] == "open", a1)
        bad = await mf.assume("", "x", "y", project="Shop")
        ck("an assumption needs its question", "the question" in bad, bad)
        async with mk() as cx:
            r = await cx.post(f"/api/projects/{shop}/assumptions/A1/state", json={"state": "kept"})
            ck("a builder cannot keep one", r.status_code == 403 and "only the orchestrator" in r.text, r.text)
        as_station("crew-orchestrator", "orchestrator")
        async with mk() as cx:
            r = await cx.post(f"/api/projects/{shop}/assumptions/A2/state", json={"state": "overturned"})
            ck("overturning needs the person's words", r.status_code == 400, r.text)
            r = await cx.post(f"/api/projects/{shop}/assumptions/A2/state",
                              json={"state": "overturned", "words": "use 50"})
            ck("the orchestrator records the person overturning it", r.json()["state"] == "overturned"
               and r.json()["words"] == "use 50", r.text)
        listed = json.loads(await mf.list_assumptions(state="open", project="Shop"))
        ck("list_assumptions narrows by state", [a["label"] for a in listed] == ["A1"], listed)

        print("== checks (TR-435) ==")
        as_station("shop-build-m1", "builder", "crew-orchestrator")
        out = json.loads(await mf.add_check("T1", "pytest -q", "14 passed", "1a2b3c4d", "test_list",
                                            project="Shop"))
        ck("add_check returns the line it wrote", out["line"].startswith("- [1a2b3c4] `pytest -q` -> 14 passed"), out)
        doc = json.loads(await m.get_doc(wd["id"], project="Shop"))["body"]
        ck("the working doc has it under Progress, the rest kept",
           doc.startswith("## Goal\nList products.") and "### Checks" in doc and "`pytest -q`" in doc, doc)
        await mf.add_check("T1", "ruff check .", "clean", project="Shop")
        bad = await mf.add_check("T1", "make", "ok", commit="not a sha!", project="Shop")
        ck("a commit that is not a hash is refused", "hash" in bad, bad)
        out = json.loads(await mf.add_check("T2", "npm test", "3 passed", project="Shop"))
        g = json.loads(await m.get_ticket("T2", project="Shop"))
        ck("a ticket without a working doc gets one with its check",
           g["working_doc"] == out["working_doc"] and "npm test" in g["working_doc_body"], g)
        g = json.loads(await m.get_ticket("T1", project="Shop"))
        ck("get_ticket carries checks in order, and its assumptions",
           [c["command"] for c in g["checks"]] == ["pytest -q", "ruff check ."]
           and g["assumptions"][0]["label"] == "A1", g)

        print("== project memory (TR-436) ==")
        out = json.loads(await mf.remember_for_project("tests need the dev database running", project="Shop"))
        ck("a builder's line lands in the project memory", out["added"], out)
        docs = json.loads(await m.list_docs(project="Shop"))
        mem = [d for d in docs if d["kind"] == "memory" and not d["global"]]
        ck("one memory doc of the project's own, next to the shared one",
           len(mem) == 1 and any(d["kind"] == "memory" and d["global"] for d in docs), docs)
        body = json.loads(await m.get_doc(mem[0]["id"], project="Shop"))["body"]
        ck("dated and signed", "tests need the dev database running (20" in body and "shop-build-m1" in body, body)
        bad = await m.add_to_global("memory", "everything uses EUR")
        ck("a builder cannot write the shared memory", "only the orchestrator" in bad, bad)

        print("== pick-up carries it ==")
        as_station()
        await m.write_ticket("", status="in_progress", id="T1", project="Shop")
        async with mk() as cx:
            pk = (await cx.get(f"/api/projects/{shop}/pickup")).json()
        ck("the pick-up has the ticket's assumptions and checks and the project memory",
           pk["ticket"]["assumptions"] and pk["ticket"]["checks"]
           and "dev database" in pk.get("memory", ""), {k: pk.get(k) for k in ("memory",)})

        print("== subagent lines land under their session (TR-434) ==")
        lines = [
            {"type": "session_project", "sessionId": "s9", "cwd": "/w", "timestamp": ago(5),
             "tares_project": "Shop"},
            {"type": "assistant", "sessionId": "s9", "cwd": "/w", "timestamp": ago(4),
             "message": {"role": "assistant", "content": "I will delegate"}},
            {"type": "user", "sessionId": "s9", "cwd": "/w", "timestamp": ago(3), "isSidechain": True,
             "subagent": "a9", "message": {"role": "user", "content": "Write cart.py"}},
            {"type": "assistant", "sessionId": "s9", "cwd": "/w", "timestamp": ago(2), "isSidechain": True,
             "subagent": "a9", "message": {"role": "assistant", "content": "cart.py written"}},
        ]
        async with mk() as cx:
            await cx.post("/ingest/claude_code", content="\n".join(json.dumps(x) for x in lines) + "\n",
                          headers={"Content-Type": "application/x-ndjson"})
            ses = (await cx.get(f"/api/projects/{shop}/sessions/s9")).json()["lines"]
        subs = [l for l in ses if (l.get("labels") or {}).get("subagent") == "a9"]
        ck("the subagent's lines are the session's, labelled with the subagent",
           len(subs) == 2 and subs[0]["text"] == "Write cart.py", ses)


unit()
plugin()
main_backlog()
asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
