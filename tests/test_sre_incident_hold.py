"""Taking an incident someone else holds changes nothing (TR-827).

"I'm on it" on an incident another person is on used to take it over without a word. Now the route
answers 409 and names who holds it, the holder stays, and the write is conditional on what the
incident was when it was read, so two people who take a free incident at the same moment end with
exactly one holder and the other is told who it is.

Covers: a free incident is taken; a held one answers 409 with the holder's name on the screen route
and on the engine route and keeps its holder and its log; the holder pressing it again is still
the old "already on this incident" 409; a take that finds the incident moved between its read and
its write (two takes at the same moment) leaves exactly one holder and one log line.

Run: .venv/bin/python tests/test_sre_incident_hold.py
"""
import asyncio
import os
import sys
import tempfile
import threading
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

TOKEN = "root-token-incident-hold"
TMP = tempfile.mkdtemp(prefix="tares-incident-hold-")
os.environ["TARES_DB"] = os.path.join(TMP, "t.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_EDITION"] = "sre"
os.environ["TARES_AUTH_TOKEN"] = TOKEN   # read when the daemon is imported

import httpx  # noqa: E402

from tares import incidents  # noqa: E402

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def eq(label, got, want):
    ck(label, got == want, f"got {got!r}, want {want!r}")


def H(tok=TOKEN):
    return {"Authorization": f"Bearer {tok}"}


async def main():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store
    async with app.router.lifespan_context(app):
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                               headers=H())

        def new_incident():
            return store.create_incident("service-a", None, datetime.now(timezone.utc))["id"]

        async def key(name):
            r = await cx.post("/api/keys", json={"name": name, "scopes": ["admin"]})
            assert r.status_code in (200, 201), r.text
            return H(r.json()["secret"])

        alex, kim = await key("alex-key"), await key("kim-key")

        def holder(iid):
            inc = store.get_incident(iid)
            return inc["state"], inc["owner_name"]

        print("== a free incident is taken ==")
        a = new_incident()
        r = await cx.post(f"/api/sre/incidents/{a}/on_it", headers=alex)
        eq("free: 200", r.status_code, 200)
        eq("free: the caller holds it", holder(a), ("on_it", "alex-key"))

        print("== one someone else holds is not changed ==")
        r = await cx.post(f"/api/sre/incidents/{a}/on_it", headers=kim)
        eq("held, screen route: 409 naming the holder", (r.status_code, r.json().get("detail")),
           (409, "alex-key is on it."))
        eq("held, screen route: the holder stays", holder(a), ("on_it", "alex-key"))
        r = await cx.post(f"/api/incidents/{a}/on_it", headers=kim)
        eq("held, engine route: 409 naming the holder", (r.status_code, r.json().get("detail")),
           (409, "alex-key is on it."))
        eq("held, engine route: the holder stays", holder(a), ("on_it", "alex-key"))
        eq("and the log has the one take", [e["action"] for e in store.incident_log(a)], ["on_it"])
        r = await cx.post(f"/api/sre/incidents/{a}/on_it", headers=alex)
        eq("the holder again: still the old answer", (r.status_code, r.json().get("detail")),
           (409, "Someone is already on this incident."))
        eq("the page says who is on it", (await cx.get(f"/api/sre/incidents/{a}", headers=kim)
                                          ).json()["who"]["name"], "alex-key")

        print("== two takes at the same moment end with one holder ==")
        b = new_incident()
        gate = threading.Barrier(2, timeout=5)
        seen = threading.local()
        real_get = store.get_incident

        def get_then_wait(iid):
            got = real_get(iid)
            if not getattr(seen, "waited", False):   # both have read it free before either writes
                seen.waited = True
                gate.wait()
            return got

        store.get_incident = get_then_wait
        out: dict = {}

        def take(name):
            try:
                out[name] = incidents.take(store, b, name, name, "console")
            except incidents.Held as e:
                out[name] = e

        try:
            ts = [threading.Thread(target=take, args=(n,)) for n in ("alex-key", "kim-key")]
            for t in ts:
                t.start()
            for t in ts:
                t.join(10)
        finally:
            store.get_incident = real_get
        won = [n for n, v in out.items() if isinstance(v, dict)]
        lost = [n for n, v in out.items() if isinstance(v, incidents.Held)]
        eq("exactly one take went through", len(won), 1)
        eq("the other was told who holds it", [str(out[n]) for n in lost],
           [f"{won[0]} is on it."] if won else [])
        eq("one holder, the winner", holder(b), ("on_it", won[0] if won else None))
        eq("one line in the log", [e["action"] for e in store.incident_log(b)], ["on_it"])
        await cx.aclose()


asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
