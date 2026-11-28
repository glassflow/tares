# Tares enterprise licence, see ee/LICENSE
"""The admin routes leave a private project alone (TR-998).

Lee leads Zephyrwatch, a private project that holds a source (zephyr-logs) with the service
zephyr-api, and a project key. Mia is on it as a member. Kim is a teammate with an admin key who
is not. A name Zephyrwatch owns answers Kim exactly like a name that does not exist, on every route
under Settings, Keys and Catalog (the AI SRE routes this test also covered went with the edition,
TR-1809):

  - the catalog export leaves out what Zephyrwatch holds, and an import neither overwrites nor
    clears it, and answers the same counts as for a new name;
  - the key list, and the key count on the settings page, leave out the project key, and revoking
    it, rebinding it or making one for Zephyrwatch answers as for a key or project that is not there.

Mia, Lee and the cell's own key still see and change all of it.

Run: .venv/bin/python ee/tests/test_private_admin_routes.py
"""
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import licensed   # scrubs the machine's TARES_* settings and puts the repo and tests/ on sys.path
licensed.install_test_root()

TMP = tempfile.mkdtemp(prefix="tares-private-admin-")
os.environ["TARES_DB"] = os.path.join(TMP, "t.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-private-admin"
os.environ["TARES_EDITION"] = "factory"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_LICENSE"] = licensed.test_licence()
os.environ["TARES_TRIGGER_DEBOUNCE_SECONDS"] = "0"
os.environ["TARES_SCHEDULE_CLOCK_SECONDS"] = "3600"
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-not-a-real-key"

import httpx
import yaml

from fixtures.sre_cell import build
from tares.connectors import source_type_for
from tares.envelope import Envelope

HIDDEN_REPO = "example-org/zephyr-repo"
P = F = 0
UTC = timezone.utc


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def alike(a, b, name_a, name_b) -> bool:
    """Two answers that differ only by the name asked about."""
    return (a.status_code == b.status_code
            and a.text.replace(name_a, "X") == b.text.replace(name_b, "X"))


async def main():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store
    root = {"Authorization": f"Bearer {TOKEN}"}
    keys, pid = {}, {}

    async def call(who, method, path, body=None, **params):
        hdr = root if who == "root" else keys[who]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                     headers=hdr) as cx:
            return await cx.request(method, path, json=body, params=params or None)

    async with app.router.lifespan_context(app):
        runtime = app.state.runtime
        base = datetime.now(UTC).replace(microsecond=0)
        for who in ("kim", "mia", "lee"):
            r = await call("root", "POST", "/api/keys", {
                "name": f"{who}-laptop", "scopes": ["admin"],
                "person": {"email": f"{who}-adm@example.com", "name": who.capitalize()}})
            keys[who] = {"Authorization": f"Bearer {r.json()['secret']}"}
            pid[who] = r.json()["person"]["id"]

        build(store, base)   # prod-otlp: edge-proxy and service-a, in the open project
        vault = "prj-zephyrwatch"
        store.create_project(vault, "custom", "Zephyrwatch", {}, goal="Keep zephyr-api healthy",
                             lead_person_id=pid["lee"])
        store.set_project_scope(vault, "private")
        store.add_project_member(vault, pid["mia"])
        store.upsert_catalog_source("zephyr-logs", source_type_for("otlp"), "otlp", "5s", {
            "labels": [{"name": "service", "field": "service.name", "primary": True}]})
        store.put_in_project("source", "zephyr-logs", vault)
        store.upsert_catalog_trigger("watch-zephyr", ["zephyr-logs"], {"every": "10m"}, {}, "10m")
        store.upsert_catalog_agent("zephyr-seer", "watch-zephyr", "Look at zephyr-api.",
                                   budget_usd=25.0, budget_period="month", concludes=True)
        store.put_in_project("trigger", "watch-zephyr", vault)
        store.put_in_project("agent", "zephyr-seer", vault)
        store.set_wake(vault, "watch-zephyr", "zephyr-seer", enabled=True)
        store.upsert_catalog_source("zephyr-gh", source_type_for("github_app"), "github_app", "1h", {
            "labels": [{"name": "repo", "field": "repo", "primary": True}]})
        store.put_in_project("source", "zephyr-gh", vault)
        runtime.reload_catalog()

        t = base - timedelta(minutes=3)
        store.append([Envelope(source="zephyr-logs", source_type=source_type_for("otlp"),
                               key_value="zephyr-api", event_type="INFO", text="zephyr handled 1",
                               event_time=t, payload={}, labels={"service": "zephyr-api"},
                               ingest_time=t)])
        store.append([Envelope(source="zephyr-gh", source_type=source_type_for("github_app"),
                               key_value=HIDDEN_REPO, event_type="commit", text="zephyr fix queue",
                               event_time=t, payload={}, labels={"repo": HIDDEN_REPO},
                               ingest_time=t)])

        # a project key for Zephyrwatch, made by the one who sees it
        r = await call("lee", "POST", "/api/keys", {"name": "zephyr-reader", "project": vault})
        ck("Lee makes a key for her private project", r.status_code == 201, r.text[:200])
        zkey = r.json()["id"]

        print("== the catalog export ==")
        for who in ("kim",):
            r = await call(who, "GET", "/api/catalog/export")
            low = r.text.lower()
            ck(f"{who}'s export names nothing of Zephyrwatch", r.status_code == 200
               and "zephyr" not in low and "prod-otlp" in low, f"{r.status_code} {r.text[:200]}")
            r = await call(who, "GET", "/api/catalog/export", sources="zephyr-logs")
            unk = await call(who, "GET", "/api/catalog/export", sources="no-such-source")
            ck(f"{who} asking for her source by name gets what an unknown name gets",
               "zephyr" not in r.text.lower() and r.status_code == unk.status_code
               and alike(r, unk, "zephyr-logs", "no-such-source"),
               f"{r.status_code} {r.text[:120]} vs {unk.status_code} {unk.text[:120]}")
        for who in ("mia", "lee", "root"):
            r = await call(who, "GET", "/api/catalog/export")
            ck(f"{who}'s export holds all of it", r.status_code == 200 and "zephyr-logs" in r.text
               and "watch-zephyr" in r.text and "zephyr-seer" in r.text, f"{r.status_code} {r.text[:200]}")

        print("== the catalog import ==")
        def doc(name, interval):
            return yaml.safe_dump({"sources": [{"name": name, "connector": "webhook",
                                                "poll": interval, "config": {}}]})

        def snapshot():
            return json.dumps({
                "src": {n: s.poll for n, s in runtime.catalog.sources.items()},
                "trg": sorted(t.name for t in runtime.catalog.triggers), "agt": sorted(a.name for a in runtime.catalog.agents),
                "own": store.projects_using("source", "zephyr-logs")}, sort_keys=True, default=str)
        kept = snapshot()
        r = await call("kim", "POST", "/api/catalog/import", {"yaml": doc("zephyr-logs", "9m")})
        new = await call("kim", "POST", "/api/catalog/import", {"yaml": doc("kim-logs", "9m")})
        ck("importing her source's name answers the counts a new name does", r.status_code == 200
           and alike(r, new, "zephyr-logs", "kim-logs"), f"{r.status_code} {r.text[:160]} vs {new.status_code} {new.text[:160]}")
        ck("and leaves the source and its owner as they were",
           json.loads(snapshot())["src"]["zephyr-logs"] == "5s"
           and json.loads(snapshot())["own"] == json.loads(kept)["own"], snapshot())
        r = await call("kim", "POST", "/api/catalog/import", {"yaml": doc("kim-logs", "9m"), "mode": "replace"})
        ck("a replace by Kim clears her own objects, not Zephyrwatch's", r.status_code == 200
           and "prod-otlp" not in runtime.catalog.sources and "zephyr-logs" in runtime.catalog.sources
           and "watch-zephyr" in [t.name for t in runtime.catalog.triggers]
           and "zephyr-seer" in [a.name for a in runtime.catalog.agents],
           f"{r.status_code} {r.text[:160]} {sorted(runtime.catalog.sources)}")
        ck("and the project's wiring survives", store.projects_using("source", "zephyr-logs") == kept_owner(kept),
           str(store.projects_using("source", "zephyr-logs")))
        ck("including the wake rows of the private project", store.list_wakes(vault, "watch-zephyr", "zephyr-seer"),
           str(store.list_wakes(vault)))
        def doc_in(project):
            return yaml.safe_dump({"sources": [{"name": "kim-logs", "connector": "webhook", "poll": "9m",
                                                "config": {}, "projects": [project]}]})
        r = await call("kim", "POST", "/api/catalog/import", {"yaml": doc_in(vault)})
        unk = await call("kim", "POST", "/api/catalog/import", {"yaml": doc_in("prj-no-such")})
        ck("an import naming her project answers as for a project that is not there", r.status_code == 400
           and alike(r, unk, vault, "prj-no-such"), f"{r.status_code} {r.text[:120]} vs {unk.status_code} {unk.text[:120]}")
        ck("and Zephyrwatch gained no source", "kim-logs" not in runtime.catalog.sources
           or vault not in store.projects_using("source", "kim-logs"), str(store.projects_using("source", "kim-logs")))
        r = await call("lee", "POST", "/api/catalog/import", {"yaml": doc("zephyr-logs", "7m")})
        ck("Lee's import of her own source lands", r.status_code == 200
           and runtime.catalog.sources["zephyr-logs"].poll == "7m", f"{r.status_code} {r.text[:160]}")

        print("== the keys ==")
        r = await call("kim", "GET", "/api/keys")
        ids = [k["id"] for k in r.json()["keys"]]
        ck("Kim's key list leaves out the project key", zkey not in ids and not any(
            k.get("project") == vault or "zephyr" in k["name"] for k in r.json()["keys"]), str(ids))
        ck("and still lists the others", keys["kim"]["Authorization"].split("_")[1] in ids,
           str(ids))
        for who in ("mia", "lee", "root"):
            r = await call(who, "GET", "/api/keys")
            ck(f"{who} sees the project key", zkey in [k["id"] for k in r.json()["keys"]], str(r.text[:200]))
        r = await call("kim", "DELETE", f"/api/keys/{zkey}")
        unk = await call("kim", "DELETE", "/api/keys/nokey000")
        ck("Kim revoking it answers as for a key that is not there",
           r.status_code == 404 and alike(r, unk, zkey, "nokey000"),
           f"{r.status_code} {r.text[:120]} vs {unk.status_code} {unk.text[:120]}")
        ck("and it is still active", (store.get_api_key(zkey) or {}).get("revoked_at") is None)
        r = await call("kim", "PUT", f"/api/keys/{zkey}/person", {"person_id": pid["kim"]})
        unk = await call("kim", "PUT", "/api/keys/nokey000/person", {"person_id": pid["kim"]})
        ck("binding it to herself answers as for a key that is not there",
           r.status_code == 404 and alike(r, unk, zkey, "nokey000"),
           f"{r.status_code} {r.text[:120]} vs {unk.status_code} {unk.text[:120]}")
        ck("and it is still bound to nobody", (store.get_api_key(zkey) or {}).get("person_id") is None)
        r = await call("kim", "POST", "/api/keys", {"name": "snoop", "project": vault})
        unk = await call("kim", "POST", "/api/keys", {"name": "snoop", "project": "prj-no-such"})
        ck("making a key for Zephyrwatch answers as for a project that is not there",
           r.status_code == unk.status_code and alike(r, unk, vault, "prj-no-such")
           and "secret" not in r.text, f"{r.status_code} {r.text[:120]} vs {unk.status_code} {unk.text[:120]}")
        ck("and none was made", not any(k["name"] == "snoop" for k in store.list_api_keys()))
        print("== the key count ==")
        live = [k for k in store.list_api_keys() if not k.get("revoked_at")]
        shared = [k for k in live if k.get("project") != vault]
        ck("the cell has a key for Zephyrwatch besides the others", len(live) == len(shared) + 1,
           f"{len(live)} {len(shared)}")
        r = await call("kim", "GET", "/api/factory/settings")
        ck("Kim's key count leaves out the project key",
           r.status_code == 200 and r.json()["people"]["keys"]["count"] == len(shared),
           f"{r.status_code} {r.text[:200]}")
        r = await call("lee", "GET", "/api/factory/settings")
        ck("Lee's count has it", r.status_code == 200 and r.json()["people"]["keys"]["count"] == len(live),
           f"{r.status_code} {r.text[:200]}")
        r = await call("mia", "PUT", f"/api/keys/{zkey}/person", {"person_id": pid["mia"]})
        ck("Mia, on the project, rebinds it", r.status_code == 200, f"{r.status_code} {r.text[:160]}")
        r = await call("lee", "DELETE", f"/api/keys/{zkey}")
        ck("Lee revokes it", r.status_code == 200 and (store.get_api_key(zkey) or {}).get("revoked_at"),
           f"{r.status_code} {r.text[:160]}")
        r = await call("lee", "DELETE", f"/api/keys/{zkey}")
        ck("and a second revoke is a plain 404", r.status_code == 404)

    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


def kept_owner(kept: str):
    return json.loads(kept)["own"]


asyncio.run(main())
