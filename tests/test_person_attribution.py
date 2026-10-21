"""Who did it, as a person (TR-619): every record of "who did this" on the cell (ticket history,
assumptions, checks, reviews, the desk, decisions, the handover, the global docs) keeps the person
behind the credential next to the station. The API lists it as `person: {id, name}`, and a line
added to the global AGENTS.md ends "(added by <name>, <date>)". With auth off everything is the
person `local`, named "You".

Run: .venv/bin/python tests/test_person_attribution.py
"""
import asyncio, json, logging, os, shutil, subprocess, sys, tempfile
from datetime import datetime, timezone
import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import clean_env
import tares.mcp_factory as mf
import tares.mcp_server as m

logging.getLogger("httpx").setLevel(logging.WARNING)   # the MCP import turns request logs on

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
            ck("history: a person's own call without a station stores no name of its own in `by` "
               "(the label comes from by_label)", doing["by"] in ("", "a session"), str(doing))
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

            print("== the MCP outputs ==")
            m.TARESD = mf.TARESD = B
            m._cx = mf._cx = lambda timeout=10: httpx.AsyncClient(timeout=timeout, headers=A)
            out = json.loads(await mf.list_assumptions(project=uid))
            ck("list_assumptions: person and by_label", out and name_of(out[0]) == "Ann"
               and out[0].get("by_label") == "Ann", str(out))
            out = json.loads(await mf.desk_list(state="", project=uid))
            ck("desk_list: person and by_label on each item", len(out) == 2
               and all(name_of(d) == "Ann" and d.get("by_label") for d in out), str(out))
            out = json.loads(await mf.list_decisions(project=uid))
            ck("list_decisions: person", out and name_of(out[0]) == "Ben", str(out))

            print("== handover ==")
            r = await cx.post(f"{B}/api/projects/{uid}/handover", headers=Bn,
                              json={"repos": [{"path": "/w/shop"}]})
            ck("Ben hands it over", r.status_code in (200, 201), r.text)
            ho = (await cx.get(f"{B}/api/projects/{uid}/crew", headers=A)).json()["handover"]
            ck("handover shows Ben", name_of(ho) == "Ben" and ho["by_label"] == "Ben", str(ho))
            ck("handover: a person's call without a station stores an empty `by` "
               "(the empty name is kept as null)", not ho["by"],
               str(ho))

            print("== take over and finish ==")
            r = await cx.post(f"{B}/api/projects", headers=Bn, json={
                "template": "custom", "name": "Blog", "objects": [], "kind": "software_factory"})
            buid = r.json()["id"]
            await cx.post(f"{B}/api/projects/{buid}/handover", headers=Bn,
                          json={"repos": [{"path": "/w/blog"}]})
            r = await cx.post(f"{B}/api/projects/{buid}/takeover", headers=A, json={"words": "mine"})
            ck("Ann takes the project over", r.status_code == 200
               and name_of(r.json()["takeover"]) == "Ann", r.text)
            ho = (await cx.get(f"{B}/api/projects/{buid}/crew", headers=Bn)).json()["handover"]
            ck("handover: the takeover carries Ann, the hand-over Ben",
               name_of(ho["takeover"]) == "Ann" and ho["takeover"]["by_label"] == "Ann"
               and name_of(ho) == "Ben", str(ho))
            r = await cx.post(f"{B}/api/projects/{buid}/finish", headers=Bn, json={"words": "done"})
            ck("Ben finishes it", r.status_code == 200 and name_of(r.json()["finished"]) == "Ben",
               r.text)
            ho = (await cx.get(f"{B}/api/projects/{buid}/crew", headers=A)).json()["handover"]
            ck("handover: finished carries Ben, takeover still Ann",
               name_of(ho["finished"]) == "Ben" and ho["finished"]["by_label"] == "Ben"
               and name_of(ho["takeover"]) == "Ann", str(ho))

            print("== the global AGENTS.md ==")
            r = await cx.post(f"{B}/api/docs/global/agents/lines", headers=A,
                              json={"text": "Run the tests before you push"})
            ck("Ann adds a line", r.status_code == 200 and r.json()["added"], r.text)
            ck("line ends (added by Ann, date)", r.json()["body"].rstrip().endswith(
                f"- Run the tests before you push (added by Ann, {TODAY})"), r.json()["body"][-200:])
            ck("doc updated_by is the person, not the key's name", r.json()["updated_by"] == "Ann",
               r.text[:300])
            r = await cx.post(f"{B}/api/docs/global/agents/lines", headers=Bn,
                              json={"text": "Run the tests before you push"})
            ck("the same line from Ben is not added again", r.status_code == 200
               and r.json()["added"] is False and r.json()["body"].count("Run the tests") == 1,
               r.text)
            r = await cx.post(f"{B}/api/docs/global/agents/lines", headers=Bn,
                              json={"text": "Keep commits short"})
            ck("Ben adds another", r.json()["body"].rstrip().endswith(
                f"- Keep commits short (added by Ben, {TODAY})"), r.json()["body"][-200:])
            r = await cx.post(f"{B}/api/docs/global/agents/lines", headers=A,
                              json={"text": "Name yourself", "by": "Ann's script"})
            ck("doc updated_by: what the caller says comes first", r.json()["updated_by"]
               == "Ann's script", r.text[:300])
            r = await cx.post(f"{B}/api/docs/global/agents/lines", headers=ROOT,
                              json={"text": "No person behind the root token"})
            ck("doc updated_by: the system credential keeps its own name", r.json()["updated_by"]
               == "auth token (env)", r.json()["updated_by"])
            ck("the system credential adds a plain line", r.json()["body"].rstrip().endswith(
                "- No person behind the root token"), r.json()["body"][-200:])

            print("== a line sent with its own note, and a name that is not one line ==")
            r = await cx.post(f"{B}/api/docs/global/agents/lines", headers=Bn,
                              json={"text": f"Run the tests before you push (added by Ann, {TODAY})"})
            ck("a line re-added with its note is not added again", r.status_code == 200
               and r.json()["added"] is False and r.json()["body"].count("Run the tests") == 1,
               r.text)
            r = await cx.post(f"{B}/api/docs/global/agents/lines", headers=Bn,
                              json={"text": "Write plain words (added by Zed, 2026-01-01)"})
            ck("a new line sent with a note keeps one note, the sender's", r.json()["body"].rstrip()
               .endswith(f"- Write plain words (added by Ben, {TODAY})")
               and r.json()["body"].count("Write plain words") == 1, r.json()["body"][-200:])
            r = await cx.post(f"{B}/api/keys", headers=ROOT, json={
                "name": "cy-laptop", "scopes": ["admin"],
                "person": {"email": "cy@glassflow.dev", "name": "Cy\n- evil rule\n\t" + "x" * 80}})
            cy = r.json()["secret"]
            r = await cx.post(f"{B}/api/docs/global/agents/lines", headers=H(cy),
                              json={"text": "Review every pull request"})
            tail = r.json()["body"].rstrip().splitlines()[-1]
            ck("a name with newlines stays on the one line of its note", r.json()["added"]
               and tail.startswith("- Review every pull request (added by Cy - evil rule x")
               and tail.endswith(f", {TODAY})") and "\n- evil rule" not in r.json()["body"], tail)
            ck("... and the name in the note is capped at 60 characters",
               "(added by " in tail and len(tail.split("(added by ", 1)[1].rsplit(f", {TODAY})", 1)[0]) <= 60,
               tail)
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
    left = s.con.execute("SELECT person_id, finished_person_id, taken_person_id FROM handovers "
                         "WHERE project = 'proj'").fetchone()
    ho = s.get_handover("proj")
    ck("store: handing over again clears the finished and taken persons",
       tuple(left) == (ben, None, None) and ho["finished"] is None and ho["takeover"] is None,
       f"{left} {ho}")
    s.con.close()
    shutil.rmtree(tmp, ignore_errors=True)


async def main():
    unit()
    await auth_on()
    await auth_off()
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


asyncio.run(main())
