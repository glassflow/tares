"""Who did it, as a person (TR-619): every record of "who did this" on the cell (ticket history,
assumptions, checks, reviews, the desk, decisions, the handover, the global docs) keeps the person
behind the credential next to the station. The API lists it as `person: {id, name}`, and a line
added to the global AGENTS.md ends "(added by <name>, <date>)". With auth off everything is the
person `local`, named "You". The checks with two people are in
ee/tests/test_person_attribution_team.py.

Run: .venv/bin/python tests/test_person_attribution.py
"""
import asyncio, os, shutil, subprocess, sys, tempfile
from datetime import datetime, timezone
import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import clean_env

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))

AUTH = "root-token"


async def _wait(url):
    for _ in range(80):
        try:
            async with httpx.AsyncClient() as cx:
                if (await cx.get(url, timeout=1)).status_code == 200:
                    return True
        except Exception:
            pass
        await asyncio.sleep(0.25)
    return False


def start(db, port, auth):
    for p in (db, db + ".wal"):
        if os.path.exists(p):
            os.remove(p)
    env = {**clean_env(), "TARES_DB": db, "TARES_PORT": port, "TARES_OTLP_GRPC_PORT": "off"}
    if auth:
        env["TARES_AUTH_TOKEN"] = AUTH
    return subprocess.Popen([sys.executable, "-c", "from tares.cli import run_daemon; run_daemon()"],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


TODAY = datetime.now(timezone.utc).date().isoformat()


def name_of(row):
    return ((row or {}).get("person") or {}).get("name")


async def make_project(cx, B, hd):
    r = await cx.post(f"{B}/api/projects", headers=hd, json={
        "template": "custom", "name": "Shop", "objects": [], "kind": "software_factory"})
    ck("project made", r.status_code == 201, r.text)
    uid = r.json()["id"]
    ids = []
    for t in ("Cart", "Checkout"):
        r = await cx.post(f"{B}/api/projects/{uid}/tickets", headers=hd, json={"title": t})
        ck(f"ticket {t}", r.status_code == 201, r.text)
        ids.append(r.json()["id"])
    return uid, ids


async def auth_off():
    port = "8868"
    proc = start("/tmp/person_attr_off.duckdb", port, False)
    B = f"http://127.0.0.1:{port}"
    try:
        if not await _wait(f"{B}/health"):
            ck("daemon up (auth off)", False); return
        async with httpx.AsyncClient(timeout=30) as cx:
            uid, (t1, _) = await make_project(cx, B, {})
            await cx.post(f"{B}/api/projects/{uid}/tickets/{t1}/stage", json={"stage": "doing"})
            tk = (await cx.get(f"{B}/api/projects/{uid}/tickets/{t1}")).json()
            h0 = tk["history"][0]
            ck("auth off: history shows You", name_of(h0) == "You" and h0["person"]["id"] == "local"
               and h0["by_label"] == "You", str(h0))
            r = await cx.post(f"{B}/api/projects/{uid}/assumptions", json={
                "question": "Which db?", "choice": "DuckDB", "why": "it is there"})
            ck("auth off: assumption by You", name_of(r.json()) == "You", r.text)
            r = await cx.post(f"{B}/api/desk", json={
                "question": "Ship it?", "recommendation": "yes", "why": "ready", "project": uid})
            ck("auth off: desk item by You", name_of(r.json()) == "You", r.text)
            await cx.post(f"{B}/api/projects/{uid}/handover", json={"repos": [{"path": "/w/shop"}]})
            ho = (await cx.get(f"{B}/api/projects/{uid}/crew")).json()["handover"]
            ck("auth off: handover by You", name_of(ho) == "You", str(ho))
            r = await cx.post(f"{B}/api/docs/global/memory/lines", json={"text": "Tabs are fine"})
            ck("auth off: global line added by You", r.json()["body"].rstrip().endswith(
                f"- Tabs are fine (added by You, {TODAY})"), r.json()["body"][-200:])
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def unit():
    """The pieces the daemon test cannot see: the note on a line, and the hand-over columns."""
    print("== append_line ==")
    from tares import docs as docs_mod
    note = " (added by Ann, 2026-10-21)"
    body, added = docs_mod.append_line("# Rules\n", "Run tests", note)
    ck("a line gets the note", added and body == f"# Rules\n- Run tests{note}\n", body)
    body2, added = docs_mod.append_line(body, "Run tests (added by Ben, 2026-11-01)", " (added by Ben, 2026-11-01)")
    ck("the same line with another note is not added", not added and body2 == body, body2)
    body3, added = docs_mod.append_line("", "New rule (added by Zed, 2026-01-01)", note)
    ck("a new line's own note is replaced by the sender's", added and body3 == f"- New rule{note}\n", body3)
    body4, added = docs_mod.append_line("", "Bare rule (added by Zed, 2026-01-01)")
    ck("without a sender the note is dropped", added and body4 == "- Bare rule\n", body4)

    print("== hand-over columns ==")
    from tares.store import Store
    tmp = tempfile.mkdtemp(prefix="person-attr-store-")
    s = Store(os.path.join(tmp, "store.duckdb"))
    ann = s.create_person("Ann", "ann@glassflow.dev")["id"]
    ben = s.create_person("Ben", "ben@glassflow.dev")["id"]
    at = datetime.now(timezone.utc)
    s.set_handover("proj", [{"path": "/w/x"}], None, "", at, ann)
    s.take_over("proj", "mine", "", at, ben)
    s.finish_handover("proj", "done", None, "", at, ann)
    ho = s.get_handover("proj")
    ck("store: take_over keeps its person", (ho["takeover"]["person"] or {}).get("id") == ben, str(ho))
    ck("store: finish_handover keeps its person", (ho["finished"]["person"] or {}).get("id") == ann, str(ho))
    s.set_handover("proj", [{"path": "/w/x"}], None, "", at, ben)
    left = s.sc.execute("SELECT person_id, finished_person_id, taken_person_id FROM handovers "
                         "WHERE project = 'proj'").fetchone()
    ho = s.get_handover("proj")
    ck("store: handing over again clears the finished and taken persons",
       tuple(left) == (ben, None, None) and ho["finished"] is None and ho["takeover"] is None,
       f"{left} {ho}")
    s.con.close()
    shutil.rmtree(tmp, ignore_errors=True)


async def main():
    unit()
    await auth_off()
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


asyncio.run(main())
