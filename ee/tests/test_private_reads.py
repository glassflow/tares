# Tares enterprise licence, see ee/LICENSE
"""A private project's events and cell-wide lists stay out of reach of anyone not on it (TR-629).

Two parts:

1. On the store alone: with no private project the event filter adds no SQL at all.
2. A daemon with auth on: private project p1 has a desk item, a grant, a hand-over, a doc, a skill,
   a station, tickets in review and a rule proposal. Bob (not on p1) sees none of it in any list,
   and the desk routes that take an item by reference answer 404 for its item. Ann (leads p1) sees
   all of it.
(Ask, Slack's /tares ask, entity counts, the read log, the practice run and agent runs went in
TR-1810, and the event reads of the source catalog (/read, /catalog, /api/sources, the label
preview, /api/resources) in TR-1811, with their checks.)

Run: .venv/bin/python ee/tests/test_private_reads.py
"""
import asyncio, os, shutil, signal, socket, subprocess, sys, tempfile

import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import licensed   # puts the repo and tests/ on sys.path
from _env import clean_env
from tares.envelope import now_utc
from tares.store import Store, _exclude_sql

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))

AUTH = "root-token"
TMP = tempfile.mkdtemp(prefix="private-reads-")
DB = os.path.join(TMP, "cell.duckdb")


def free_port() -> str:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return str(s.getsockname()[1])


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


# ── part 1: what the filter costs ───────────────────────────────────────────────

def timing():
    print("== the event filter ==")
    ck("with no private project the filter adds no SQL", _exclude_sql(None) == ("", [])
       and _exclude_sql({"ids": [], "names": []}) == ("", []), str(_exclude_sql(None)))


# ── part 2 and 3: a daemon ──────────────────────────────────────────────────────


async def _wait(url, tries=80):
    for _ in range(tries):
        try:
            async with httpx.AsyncClient() as cx:
                if (await cx.get(url, timeout=1)).status_code == 200:
                    return True
        except Exception:
            pass
        await asyncio.sleep(0.25)
    return False


async def _until(fn, tries=60):
    for _ in range(tries):
        if fn():
            return True
        await asyncio.sleep(0.5)
    return False


def spawn(port, extra=None):
    env = {**clean_env(), "TARES_DB": DB, "TARES_PORT": port, "TARES_OTLP_GRPC_PORT": "off",
           "TARES_AUTH_TOKEN": AUTH, "TARES_LICENSE": licensed.test_licence(), **(extra or {})}
    return subprocess.Popen(licensed.daemon_argv(),
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def stop(proc):
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()
        proc.wait()


async def setup(B, ids):
    """Phase 1: people, projects, a desk item, a grant, a hand-over."""
    ROOT = H(AUTH)
    async with httpx.AsyncClient(timeout=20) as cx:
        async def person(name, email):
            return (await cx.post(f"{B}/api/people", headers=ROOT,
                                  json={"name": name, "email": email})).json()["id"]

        async def key(pid):
            r = await cx.post(f"{B}/api/keys", headers=ROOT,
                              json={"name": f"k-{pid}", "scopes": ["admin"], "person_id": pid})
            return r.json()["secret"]

        pa, pb = await person("Ann", "ann@x.dev"), await person("Bob", "bob@x.dev")
        ids["ann"], ids["bob"] = await key(pa), await key(pb)
        p1 = (await cx.post(f"{B}/api/projects", headers=H(ids["ann"]), json={
            "template": "custom", "name": "Secret", "objects": []})).json()["id"]
        team = (await cx.post(f"{B}/api/projects", headers=H(ids["ann"]), json={
            "template": "custom", "name": "Open", "objects": []})).json()["id"]
        r = await cx.patch(f"{B}/api/projects/{p1}", headers=H(ids["ann"]), json={"scope": "private"})
        ck("set up: Ann leads a private project, Bob is not on it", r.status_code == 200
           and r.json()["scope"] == "private", r.text[:200])
        team2 = (await cx.post(f"{B}/api/projects", headers=H(ids["ann"]), json={
            "template": "custom", "name": "Open two", "objects": []})).json()["id"]
        ids["p1"], ids["team"], ids["team2"] = p1, team, team2
        r = await cx.post(f"{B}/api/desk", headers=ROOT, json={
            "question": "Ship the secret thing?", "recommendation": "yes", "why": "it is ready",
            "project": p1})
        ck("set up: a desk item on p1", r.status_code == 201, f"{r.status_code} {r.text[:160]}")
        ids["desk"] = r.json()["id"]
        r = await cx.post(f"{B}/api/desk", headers=ROOT, json={
            "question": "Ship the open thing?", "recommendation": "yes", "why": "it is ready",
            "project": team})
        ids["desk_open"] = r.json()["id"]
        r = await cx.post(f"{B}/api/grants", headers=ROOT, json={
            "words": "ship the secret thing whenever it is green", "scope": "project", "project": p1})
        ck("set up: a grant on p1", r.status_code == 201, f"{r.status_code} {r.text[:160]}")
        r = await cx.post(f"{B}/api/projects/{p1}/handover", headers=ROOT, json={
            "repos": [{"path": TMP, "name": "acme/secret-repo"}]})
        ck("set up: p1 handed to the crew", r.status_code == 200, f"{r.status_code} {r.text[:160]}")
        r = await cx.post(f"{B}/api/projects/{team}/handover", headers=ROOT, json={
            "repos": [{"path": TMP, "name": "acme/open-repo"}]})
        ck("set up: the team project handed to the crew", r.status_code == 200, f"{r.status_code} {r.text[:160]}")


def seed(ids):
    """Phase 2: p1's objects, written to the store with the daemon stopped."""
    store = Store(DB)
    now = now_utc()
    p1 = ids["p1"]

    store.link_session(p1, "sess-secret")
    seed_objects(store, ids, now)
    store.con.close()


def seed_objects(store, ids, now):
    """Things p1 owns besides events, and their open counterparts in the team project."""
    p1, team = ids["p1"], ids["team"]
    store.upsert_skill(p1, "secret-skill", "SECRET skill", "body")
    store.upsert_skill(team, "open-skill", "OPEN skill", "body")
    store.create_doc(p1, "note", "SECRET doc", "body")
    store.create_doc(team, "note", "OPEN doc", "body")
    store.set_session_station("s-secret", "secret-station", "builder", "crew-orchestrator", p1, now)
    store.set_session_station("s-open", "open-station", "builder", "crew-orchestrator", team, now)
    # tickets in review, and one finding kind that came back in three of them (a recurring one)
    for uid, repo, label in ((p1, "acme/secret-repo", "SECRET"), (team, "acme/open-repo", "OPEN")):
        for n in range(3):
            tid = store.create_ticket(uid, "ann", f"{label} ticket {n}", repo=repo)
            store.update_ticket(tid, stage="review", pr={"url": f"https://github.com/{repo}/pull/{n}",
                                                         "repo": repo, "head": "abc"})
            store.add_review(uid, tid, f"https://github.com/{repo}/pull/{n}", "abc", "changes", "ran it",
                             "", "crew-reviewer", [{"severity": "P2", "file": "a.py", "line": 1,
                                                     "blocking": True, "text": f"{label} missing check"}],
                             {}, lambda t: f"{label.lower()}-kind", now)
    store.upsert_rule_proposal("acme/secret-repo", "secret-kind", p1, "project",
                               "SECRET rule text", ["Secret T1"], now)
    store.upsert_rule_proposal("acme/open-repo", "open-kind", team, "project",
                               "OPEN rule text", ["Open T1"], now)


async def daemon_checks(B, ids):
    A_, B_ = H(ids["ann"]), H(ids["bob"])
    p1 = ids["p1"]
    async with httpx.AsyncClient(timeout=30) as cx:
        print("== the desk ==")
        lst = (await cx.get(f"{B}/api/desk", headers=B_)).text
        ck("Bob: the list has the open item, not p1's", ids["desk_open"] in lst and ids["desk"] not in lst
           and "secret thing" not in lst, lst[:300])
        lst = (await cx.get(f"{B}/api/desk", headers=A_)).text
        ck("Ann: the list has both", ids["desk_open"] in lst and ids["desk"] in lst, lst[:300])
        for route, body in (("withdraw", {}), ("answer", {"words": "yes, ship it"}), ("allow", {"words": "yes"})):
            r = await cx.post(f"{B}/api/desk/{ids['desk']}/{route}", headers=B_, json=body)
            gone = await cx.post(f"{B}/api/desk/D9999/{route}", headers=B_, json=body)
            ck(f"Bob: {route} on p1's item is as unknown as an item that does not exist",
               r.status_code == gone.status_code == 404, f"{r.status_code} {r.text[:120]} / {gone.status_code}")
        still = next((d for d in (await cx.get(f"{B}/api/desk", headers=A_)).json()["desk"]
                      if d["id"] == ids["desk"]), None)
        ck("... and the item is still open", still is not None and still["state"] == "open", str(still))

        print("== grants and the hand-over ==")
        r = await cx.get(f"{B}/api/grants", headers=B_, params={"project": p1})
        ck("Bob: p1's grants are unknown", r.status_code == 404, f"{r.status_code} {r.text[:120]}")
        ck("Bob: the cell-wide grants do not carry p1's", "secret thing" not in (await cx.get(f"{B}/api/grants", headers=B_)).text)
        ck("Ann: p1's grants", "secret thing" in (await cx.get(f"{B}/api/grants", headers=A_, params={"project": p1})).text)
        crew = (await cx.get(f"{B}/api/crew/projects", headers=B_)).text
        ck("Bob: the crew's project list does not carry p1 or its repo", p1 not in crew and "secret-repo" not in crew, crew[:300])
        crew = (await cx.get(f"{B}/api/crew/projects", headers=A_)).text
        ck("Ann: it does", p1 in crew and "secret-repo" in crew, crew[:300])
        r = await cx.get(f"{B}/api/crew", headers=B_)
        ck("Bob: the crew view does not carry p1", p1 not in r.text and "Secret" not in r.text, r.text[:300])


async def list_checks(B, ids):
    """Every cell-wide list and by-name read that leaves out what only p1 owns (TR-629)."""
    A_, B_ = H(ids["ann"]), H(ids["bob"])
    p1 = ids["p1"]
    async with httpx.AsyncClient(timeout=30) as cx:
        async def both(path, **kw):
            return ((await cx.get(f"{B}{path}", headers=B_, **kw)),
                    (await cx.get(f"{B}{path}", headers=A_, **kw)))

        async def listed(label, path, secret, openw=None, **kw):
            b, a = await both(path, **kw)
            ck(f"{label}: Bob's list has no {secret}", b.status_code == 200 and secret not in b.text,
               f"{b.status_code} {b.text[:200]}")
            ck(f"{label}: Ann's has it", secret in a.text, f"{a.status_code} {a.text[:200]}")
            if openw:
                ck(f"{label}: Bob still sees {openw}", openw in b.text, b.text[:200])

        print("== lists ==")
        await listed("docs", "/api/docs", "SECRET doc", "OPEN doc")
        await listed("standup", "/api/crew/standup", "SECRET ticket", "OPEN ticket")
        b, _ = await both("/api/crew/standup")
        ck("standup: Bob's report names no p1", p1 not in b.text and "Secret" not in b.text, b.text[:300])
        await listed("crew stations", "/api/crew", "secret-station", "open-station")
        await listed("review queue", "/api/review-queue", "SECRET ticket", "OPEN ticket")
        await listed("recurring findings", "/api/recurring-findings", "secret-kind", params={"repo": "acme/secret-repo"})
        await listed("rule proposals", "/api/rule-proposals", "SECRET rule text", "OPEN rule text")

async def main():
    timing()
    ids = {}
    port = free_port()
    B = f"http://127.0.0.1:{port}"
    proc = spawn(port)
    try:
        if not await _wait(f"{B}/health"):
            ck("daemon up", False); return
        await setup(B, ids)
    finally:
        stop(proc)
    seed(ids)

    proc = spawn(port)
    try:
        if not await _wait(f"{B}/health"):
            ck("daemon up (second start)", False); return
        await daemon_checks(B, ids)
        await list_checks(B, ids)
    finally:
        stop(proc)
    shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


if __name__ == "__main__":
    asyncio.run(main())
