"""Limits on what a person sets for themselves (TR-624 and TR-630 follow-ups). A person's own
autonomy and challenger are ceilings under the team's value: looser is refused, stricter is kept,
clearing falls back to the team's. A personal AGENTS.md or memory has a size limit. Reading grants
settles only grants the reader may see, and as Tares, not as the reader.

Run: .venv/bin/python tests/test_personal_limits.py
"""
import asyncio, os, shutil, socket, subprocess, sys, tempfile
import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _env import clean_env
from tares import docs as docs_mod

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))

AUTH = "root-token"
TMP = tempfile.mkdtemp(prefix="personal-limits-")


def free_port() -> str:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return str(s.getsockname()[1])


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


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


def start(db):
    port = free_port()
    env = {**clean_env(), "TARES_DB": db, "TARES_PORT": port, "TARES_OTLP_GRPC_PORT": "off",
           "TARES_AUTH_TOKEN": AUTH}
    proc = subprocess.Popen([sys.executable, "-c", "from tares.cli import run_daemon; run_daemon()"],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc, f"http://127.0.0.1:{port}"


async def main():
    proc, B = start(os.path.join(TMP, "limits.duckdb"))
    ROOT = H(AUTH)
    try:
        if not await _wait(f"{B}/health"):
            ck("daemon up", False); return
        async with httpx.AsyncClient(timeout=30) as cx:
            async def person(name, email):
                r = await cx.post(f"{B}/api/people", headers=ROOT, json={"name": name, "email": email})
                return r.json()["id"]

            async def key(pid, scope):
                r = await cx.post(f"{B}/api/keys", headers=ROOT,
                                  json={"name": f"k-{pid}-{scope}", "scopes": scope.split("+"), "person_id": pid})
                return H(r.json()["secret"])

            a, b, c = (await person("Person A", "a@example.test"), await person("Person B", "b@example.test"),
                       await person("Person C", "c@example.test"))
            KA, KB = await key(a, "read+ingest+admin"), await key(b, "read+ingest+admin")
            KC, KA_ADMIN = await key(c, "read+ingest"), await key(a, "admin")

            async def mine(k):
                return (await cx.get(f"{B}/api/crew/settings", headers=k)).json()

            print("-- a personal value is a ceiling under the team's --")
            r = await cx.put(f"{B}/api/crew/settings", headers=ROOT,
                             json={"autonomy": "L3-review", "challenger": True})
            ck("set up: the team is at L3-review with the challenger on", r.status_code == 200, r.text)
            r = await cx.put(f"{B}/api/crew/settings", headers=KA, json={"autonomy": "L4-ship"})
            ck("A asks for L4-ship, looser than the team's: 400 naming the team's L3-review",
               r.status_code == 400 and "L3-review" in r.text, r.text)
            r = await cx.put(f"{B}/api/crew/settings", headers=KA, json={"challenger": False})
            ck("A turns the challenger off, looser than the team's: 400 naming the team's value",
               r.status_code == 400 and "team" in r.text, r.text)
            s = await mine(KA)
            ck("... and nothing of that was kept", s["autonomy"] == "L3-review" and s["challenger"] is True, str(s))
            r = await cx.put(f"{B}/api/crew/settings", headers=KA, json={"autonomy": "L3-review", "challenger": True})
            ck("A asks for the team's own values: fine", r.status_code == 200, r.text)
            r = await cx.put(f"{B}/api/crew/settings", headers=KA, json={"builders_max": 9})
            ck("a setting with no ceiling (builders_max) is A's to set", r.status_code == 200
               and (await mine(KA))["builders_max"] == 9, r.text)

            print("-- stricter is kept, and clearing falls back --")
            await cx.put(f"{B}/api/crew/settings", headers=ROOT, json={"autonomy": "L4-ship", "challenger": False})
            r = await cx.put(f"{B}/api/crew/settings", headers=KB, json={"autonomy": "L3-review", "challenger": True})
            s = await mine(KB)
            ck("with the team at L4-ship, B asks for L3-review and challenger on: both kept",
               r.status_code == 200 and s["autonomy"] == "L3-review" and s["challenger"] is True, str(s))
            ck("... and C, who set nothing, gets the team's", (await mine(KC))["autonomy"] == "L4-ship", str(await mine(KC)))
            r = await cx.put(f"{B}/api/crew/settings", headers=KB, json={"clear": ["autonomy", "challenger"]})
            s = await mine(KB)
            ck("B clears both: they fall back to the team's L4-ship and challenger off",
               r.status_code == 200 and s["autonomy"] == "L4-ship" and not s["challenger"], str(s))
            r = await cx.put(f"{B}/api/crew/settings", headers=KB, json={"clear": ["nonsense"]})
            ck("clearing a setting that does not exist -> 400", r.status_code == 400, r.text)

            print("-- the team tightens after the person chose --")
            await cx.put(f"{B}/api/crew/settings", headers=KA, json={"autonomy": "L4-ship", "challenger": False})
            s = await mine(KA)
            ck("set up: A sits at L4-ship with the challenger off, as the team does",
               s["autonomy"] == "L4-ship" and not s["challenger"], str(s))
            await cx.put(f"{B}/api/crew/settings", headers=ROOT, json={"autonomy": "L3-review", "challenger": True})
            s = await mine(KA)
            ck("the team moves to L3-review with the challenger on: A's older, looser values no longer apply",
               s["autonomy"] == "L3-review" and s["challenger"] is True, str(s))

            print("-- pace and the kit --")
            r = await cx.put(f"{B}/api/crew/settings", headers=KA, json={"pace": True})
            ck("A turns pace on for themselves", r.status_code == 200 and r.json()["pace"] is True, r.text)
            pa = (await cx.get(f"{B}/api/crew/pace", headers=KA)).json()
            ck("GET /api/crew/pace counts A's own pace", pa["state"] != "off", str(pa))
            ck("... GET /api/crew shows it too",
               (await cx.get(f"{B}/api/crew", headers=KA)).json()["pace"]["state"] != "off")
            ck("... the cell's own pace stays off",
               (await cx.get(f"{B}/api/crew/pace", headers=ROOT)).json()["state"] == "off")
            r = await cx.put(f"{B}/api/crew/settings", headers=KA, json={"kit": {"frozen": None}})
            ck("A cannot set the kit: 403 rather than a 200 that changes nothing", r.status_code == 403, r.text)

            print("-- a personal doc has a size limit --")
            for name in ("agents", "memory"):
                last, n = None, 0
                for i in range(200):
                    last = await cx.post(f"{B}/api/docs/personal/{name}/lines", headers=KA,
                                         json={"text": f"line {i:03d} " + "x" * 440})
                    if last.status_code != 200:
                        break
                    n = i + 1
                body = (await cx.get(f"{B}/api/docs/personal", headers=KA)).json()[name]["body"]
                ck(f"A's personal {name} stops at the limit with a 400 saying it is full",
                   last.status_code == 400 and "full" in last.text and 20 < n < 200, f"{n} {last.status_code} {last.text}")
                ck(f"... and holds at most {docs_mod.MAX_PERSONAL_BYTES // 1024} KB",
                   len(body.encode()) <= docs_mod.MAX_PERSONAL_BYTES, str(len(body.encode())))
                r = await cx.post(f"{B}/api/docs/personal/{name}/lines", headers=KB, json={"text": "B's first line"})
                ck(f"... while B's {name} is B's own and still takes lines", r.status_code == 200, r.text)

            print("-- reading grants settles only what the reader may see, as Tares --")
            p = (await cx.post(f"{B}/api/projects", headers=KA_ADMIN, json={
                "template": "custom", "name": "Secret", "objects": []})).json()["id"]
            await cx.patch(f"{B}/api/projects/{p}", headers=KA_ADMIN, json={"scope": "private"})
            await cx.put(f"{B}/api/areas/acme/ops", headers=ROOT, json={"person_id": b})
            r = await cx.post(f"{B}/api/grants", headers=KA,
                              json={"kind": "project", "project": p, "words": "restart acme/ops",
                                    "areas": ["acme/ops"]})
            g = r.json()["grant"]
            ck("set up: A's grant on the private project waits for B", g["state"] == "waiting", r.text)
            await cx.put(f"{B}/api/areas/acme/ops", headers=ROOT, json={"person_id": a})

            async def grants_doc():
                rows = (await cx.get(f"{B}/api/docs", headers=KA)).json()
                return next((d for d in rows if d["kind"] == "grants"
                             and p in [x["id"] for x in d["projects"]]), None)

            r = await cx.get(f"{B}/api/grants", headers=KC)
            ck("C, who cannot see the project, reads the grants list", r.status_code == 200, r.text)
            d = await grants_doc()
            body = ((await cx.get(f"{B}/api/projects/{p}/docs/{d['id']}", headers=KA)).json()["body"]
                    if d else "")
            ck("... and that read did not put the grant in the private project's grants doc",
               "restart acme/ops" not in body and (d or {}).get("updated_by") != "Person C", body)
            r = await cx.get(f"{B}/api/grants", headers=KA, params={"project": p})
            now = next(x for x in r.json()["grants"] if x["id"] == g["id"])
            ck("A reads it next: it is in effect, the owner having gone",
               now["state"] == "active" and now["in_effect"], str(now))
            ck("... and the grants doc holds it", "restart acme/ops" in r.json()["project"], r.json()["project"])
            d = await grants_doc()
            ck("... settled by Tares, not by whoever read", d and d.get("updated_by") == "tares", str(d))
    finally:
        proc.terminate(); proc.wait()


try:
    asyncio.run(main())
finally:
    shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
