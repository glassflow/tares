"""Who did it, as a person (TR-619): every record of "who did this" on the cell (ticket history,
assumptions, checks, reviews, the desk, decisions, the handover, the global docs) keeps the person
behind the credential next to the station. The API lists it as `person: {id, name}`, and a line
added to the global AGENTS.md ends "(added by <name>, <date>)". With auth off everything is the
person `local`, named "You".

Run: .venv/bin/python tests/test_person_attribution.py
"""
import asyncio, os, subprocess, sys
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


def H(tok, station=None, role=None, parent=None):
    h = {"Authorization": f"Bearer {tok}"}
    if station:
        h["X-Tares-Station"] = station
    if role:
        h["X-Tares-Role"] = role
    if parent:
        h["X-Tares-Parent"] = parent
    return h


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


async def auth_on():
    port = "8867"
    proc = start("/tmp/person_attr_on.duckdb", port, True)
    B = f"http://127.0.0.1:{port}"
    try:
        if not await _wait(f"{B}/health"):
            ck("daemon up (auth on)", False); return
        async with httpx.AsyncClient(timeout=30) as cx:
            keys, pid = {}, {}
            for who_, email in (("Ann", "ann@glassflow.dev"), ("Ben", "ben@glassflow.dev")):
                r = await cx.post(f"{B}/api/keys", headers=H(AUTH), json={
                    "name": f"{who_.lower()}-laptop", "scopes": ["admin"],
                    "person": {"email": email, "name": who_}})
                ck(f"key for {who_}", r.status_code == 201, r.text)
                keys[who_], pid[who_] = r.json()["secret"], r.json()["person"]["id"]
            A, Bn = H(keys["Ann"]), H(keys["Ben"])
            ROOT = H(AUTH)
            uid, (t1, t2) = await make_project(cx, B, A)

            print("== ticket history ==")
            r = await cx.post(f"{B}/api/projects/{uid}/tickets/{t1}/assign", headers=A,
                              json={"holder": "crew-builder-1"})
            ck("Ann assigns", r.status_code == 200, r.text)
            r = await cx.post(f"{B}/api/projects/{uid}/tickets/{t1}/stage", headers=A,
                              json={"stage": "doing"})
            ck("Ann moves the ticket (no station)", r.status_code == 200, r.text)
            r = await cx.post(f"{B}/api/projects/{uid}/tickets/{t1}/stage",
                              headers=H(keys["Ben"], "crew-builder-1", "builder"),
                              json={"stage": "review"})
            ck("Ben moves it from the builder station", r.status_code == 200, r.text)
            r = await cx.post(f"{B}/api/projects/{uid}/tickets/{t2}/assign", headers=ROOT,
                              json={"holder": "crew-builder-2"})
            ck("the root token assigns another ticket", r.status_code == 200, r.text)
            tk = (await cx.get(f"{B}/api/projects/{uid}/tickets/{t1}", headers=A)).json()
            hist = {(x["field"], x["value"]): x for x in tk["history"]}
            doing, review = hist[("stage", "doing")], hist[("stage", "review")]
            ck("history: Ann's stage change lists Ann", name_of(doing) == "Ann"
               and doing["person"]["id"] == pid["Ann"], str(doing))
            ck("history: no station, the person alone", doing["by_label"] == "Ann", str(doing))
            ck("history: Ben through a station reads Ben (station)",
               name_of(review) == "Ben" and review["by_label"] == "Ben (crew-builder-1)"
               and review["by"] == "crew-builder-1", str(review))
            t2h = (await cx.get(f"{B}/api/projects/{uid}/tickets/{t2}", headers=A)).json()["history"]
            ck("history: the system credential has no person, its name stays",
               t2h and t2h[0]["person"] is None and t2h[0]["by_label"], str(t2h))

            print("== assumption ==")
            r = await cx.post(f"{B}/api/projects/{uid}/assumptions", headers=A, json={
                "question": "Which payment provider?", "choice": "Stripe", "why": "test mode",
                "tickets": [t1]})
            ck("Ann assumes", r.status_code == 201 and name_of(r.json()) == "Ann", r.text)
            lst = (await cx.get(f"{B}/api/projects/{uid}/assumptions", headers=Bn)).json()
            lst = lst.get("assumptions", lst)
            ck("assumption listed with Ann", lst and name_of(lst[0]) == "Ann", str(lst))
            tk = (await cx.get(f"{B}/api/projects/{uid}/tickets/{t1}", headers=A)).json()
            ck("ticket view: assumption carries Ann", tk["assumptions"]
               and name_of(tk["assumptions"][0]) == "Ann", str(tk["assumptions"]))

            print("== check and review ==")
            r = await cx.post(f"{B}/api/projects/{uid}/tickets/{t1}/checks", headers=Bn, json={
                "command": "python tests/test_cart.py", "result": "5 passed", "commit": "abc1234"})
            ck("Ben records a check", r.status_code == 201 and name_of(r.json()["checks"][-1]) == "Ben",
               r.text)
            r = await cx.post(f"{B}/api/projects/{uid}/tickets/{t1}/reviews", headers=A, json={
                "head": "abc1234", "verdict": "pass", "verified": "ran the tests: 5 passed"})
            ck("Ann records a review", r.status_code == 201 and name_of(r.json()) == "Ann", r.text)
            tk = (await cx.get(f"{B}/api/projects/{uid}/tickets/{t1}", headers=A)).json()
            ck("ticket view: check by Ben", tk["checks"] and name_of(tk["checks"][-1]) == "Ben",
               str(tk["checks"]))
            ck("ticket view: review by Ann", tk["reviews"] and name_of(tk["reviews"][-1]) == "Ann",
               str(tk["reviews"]))

            print("== desk and decision ==")
            r = await cx.post(f"{B}/api/desk", headers=A, json={
                "question": "Keep the cart in the session?", "recommendation": "yes",
                "why": "it is simple", "project": uid})
            ck("Ann asks on the desk", r.status_code == 201 and name_of(r.json()) == "Ann", r.text)
            r = await cx.post(f"{B}/api/desk", headers=A, json={
                "question": "shop-build-m1 was refused a command. Allow it?",
                "recommendation": "allow", "why": "needed", "project": uid,
                "station": "shop-build-m1", "command": "gh pr merge 12 --squash"})
            pq = r.json()
            desk = (await cx.get(f"{B}/api/desk", headers=Bn)).json()["desk"]
            ck("desk listing: both asked by Ann", len(desk) == 2
               and all(name_of(d) == "Ann" for d in desk), str(desk))
            r = await cx.post(f"{B}/api/desk/{pq['label']}/allow", headers=Bn, json={
                "words": "shop-build-m1 may run `gh pr merge 12 --squash` in Shop", "scope": "project"})
            ck("Ben allows it in the console", r.status_code == 200
               and name_of(r.json()["decision"]) == "Ben", r.text)
            decs = (await cx.get(f"{B}/api/projects/{uid}/decisions", headers=A)).json()
            decs = decs.get("decisions", decs)
            ck("decisions listing: Ben", decs and name_of(decs[0]) == "Ben", str(decs))

            print("== handover ==")
            r = await cx.post(f"{B}/api/projects/{uid}/handover", headers=Bn,
                              json={"repos": [{"path": "/w/shop"}]})
            ck("Ben hands it over", r.status_code in (200, 201), r.text)
            ho = (await cx.get(f"{B}/api/projects/{uid}/crew", headers=A)).json()["handover"]
            ck("handover shows Ben", name_of(ho) == "Ben" and ho["by_label"] == "Ben", str(ho))

            print("== the global AGENTS.md ==")
            r = await cx.post(f"{B}/api/docs/global/agents/lines", headers=A,
                              json={"text": "Run the tests before you push"})
            ck("Ann adds a line", r.status_code == 200 and r.json()["added"], r.text)
            ck("line ends (added by Ann, date)", r.json()["body"].rstrip().endswith(
                f"- Run the tests before you push (added by Ann, {TODAY})"), r.json()["body"][-200:])
            r = await cx.post(f"{B}/api/docs/global/agents/lines", headers=Bn,
                              json={"text": "Run the tests before you push"})
            ck("the same line from Ben is not added again", r.status_code == 200
               and r.json()["added"] is False and r.json()["body"].count("Run the tests") == 1,
               r.text)
            r = await cx.post(f"{B}/api/docs/global/agents/lines", headers=Bn,
                              json={"text": "Keep commits short"})
            ck("Ben adds another", r.json()["body"].rstrip().endswith(
                f"- Keep commits short (added by Ben, {TODAY})"), r.json()["body"][-200:])
            r = await cx.post(f"{B}/api/docs/global/agents/lines", headers=ROOT,
                              json={"text": "No person behind the root token"})
            ck("the system credential adds a plain line", r.json()["body"].rstrip().endswith(
                "- No person behind the root token"), r.json()["body"][-200:])
    finally:
        proc.terminate()
        proc.wait(timeout=10)


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


async def main():
    await auth_on()
    await auth_off()
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


asyncio.run(main())
