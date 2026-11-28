# Tares enterprise licence, see ee/LICENSE
"""The factory seed (TR-1604): a licensed factory store with as many rows as the real factory has.

Seeds one licensed cell with `factory_seed.seed`, then checks that every table holds exactly the count
SHAPE names (the events by source too), that it took under 4 minutes, and that Home, the Projects
list, the Crew pages, a project and two ticket pages answer 200 on it. TR-1607's time-target test builds on the same
seed.

Run: .venv/bin/python ee/tests/test_factory_seed.py
Postgres: TARES_TEST_PG=postgresql://... PYTHONPATH=tests/pg_harness .venv/bin/python ee/tests/test_factory_seed.py
"""
import asyncio
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import licensed   # scrubs the machine's TARES_* settings and puts the repo and tests/ on sys.path
licensed.install_test_root()
sys.path.insert(0, licensed.REPO)

import factory_seed as fs

LIMIT = 240.0     # seconds the seed may take, per backend


def _licence() -> str:
    kit = licensed.kit
    root_priv, _ = kit.keypair(kit.ROOT_PHRASE)
    iss_priv, iss_pub = kit.keypair(kit.ISSUER_PHRASE)
    cert = kit.issuer_cert(root_priv, iss_pub, "2099-01-01")
    return kit.sign(iss_priv, cert, "Test Org", 5, ("enterprise",), "2020-01-01T00:00:00Z",
                    "2099-01-01", "lic_ee_seed")


os.environ["TZ"] = "UTC"
time.tzset()
_TMP = tempfile.mkdtemp(prefix="tares-factory-seed-")
os.environ["TARES_HOME"] = os.path.join(_TMP, "api")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_LICENSE"] = _licence()
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-factory-seed"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_EDITION"] = "factory"

import httpx

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


async def main():
    from tares.daemon import make_app
    fs.freeze_clock()
    app = make_app()
    store = app.state.store
    root = {"Authorization": f"Bearer {TOKEN}"}
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=root,
                                     timeout=120) as cx:
            r = await cx.post("/api/keys", json={"name": "sam-laptop", "scopes": ["admin"],
                                                 "person": {"email": "sam-seed@example.com", "name": "Sam"}})
            assert r.status_code in (200, 201), r.text
            sam = {"Authorization": f"Bearer {r.json()['secret']}"}
        print(f"== seeding ({'postgres' if os.environ.get('TARES_TEST_PG') else 'duckdb'}) ==")
        t = time.time()
        made = fs.seed(store)
        took = time.time() - t
        print("== counts ==")
        want, have = fs.expected(), fs.counts(store)
        for name in sorted(want):
            print(f"  {name:<28} {have[name]:>9} {want[name]:>9}")
        wrong = {n: (have[n], want[n]) for n in want if have[n] != want[n]}
        ck(f"all {len(want)} counts match SHAPE ({fs.SHAPE['date']})", not wrong, str(wrong))
        ck(f"the seed took {took:.0f}s, under {LIMIT:.0f}s", took < LIMIT, f"{took:.0f}s")
        print("== pages ==")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=sam,
                                     timeout=300) as cx:
            for label, path in (
                    ("Home", "/api/home"),
                    ("Projects", "/api/factory/projects"),
                    ("Crew", "/api/crew"),
                    ("Crew screen", "/api/factory/crew"),
                    ("a station's sessions", f"/api/factory/crew/{made['crew_id']}/{made['station']}/sessions"),
                    ("a project", f"/api/factory/projects/{made['anchor_project']}"),
                    ("a ticket in it", f"/api/factory/projects/{made['anchor_project']}/tickets/{made['anchor_ticket']}"),
                    ("a ticket with reviews", f"/api/factory/projects/{made['big_project']}/tickets/{made['big_ticket']}")):
                t = time.time()
                resp = await cx.get(path)
                print(f"  {label:<24} {time.time() - t:6.1f}s")
                ck(f"GET {path} answers 200", resp.status_code == 200, f"{resp.status_code} {resp.text[:300]}")
    print(f"\n{P} passed, {F} failed")
    return F


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(main()) else 0)
