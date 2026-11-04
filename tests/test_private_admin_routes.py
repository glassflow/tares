"""The admin routes leave a private project alone (TR-998).

Lee leads Zephyrwatch, a private project that holds a source (zephyr-logs) with the service
zephyr-api, and a project key. Mia is on it as a member. Kim is a teammate with an admin key who
is not. A name Zephyrwatch owns answers Kim exactly like a name that does not exist, on every route
under Settings, Keys and Catalog:

  - renaming, hiding or merging a service only Zephyrwatch feeds (PATCH /api/services, PUT
    /api/sre/services/{name}/settings) answers 404 and changes nothing;
  - the service-labels and via-labels settings take no source of Zephyrwatch's from her, and a
    save keeps the entries Zephyrwatch already has;
  - the catalog export leaves out what Zephyrwatch holds, and an import neither overwrites nor
    clears it, and answers the same counts as for a new name;
  - the key list, and the SRE People page, leave out the project key, and revoking it, rebinding
    it or making one for Zephyrwatch answers as for a key or project that is not there;
  - a repo only Zephyrwatch's source sent cannot be linked to a service by her.

Mia, Lee and the cell's own key still see and change all of it.

Run: .venv/bin/python tests/test_private_admin_routes.py
"""
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _env import scrub_os_environ
scrub_os_environ()
sys.path.insert(0, os.path.dirname(HERE))

TMP = tempfile.mkdtemp(prefix="tares-private-admin-")
os.environ["TARES_DB"] = os.path.join(TMP, "t.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-private-admin"
os.environ["TARES_EDITION"] = "sre"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_TRIGGER_DEBOUNCE_SECONDS"] = "0"
os.environ["TARES_SCHEDULE_CLOCK_SECONDS"] = "3600"
os.environ["TARES_WATCH_SECONDS"] = "3600"
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-not-a-real-key"

import httpx
import yaml

from fixtures.sre_cell import build
from tares import sre, sre_home
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
        sre_home.clear_cache()

        # a project key for Zephyrwatch, made by the one who sees it
        r = await call("lee", "POST", "/api/keys", {"name": "zephyr-reader", "project": vault})
        ck("Lee makes a key for her private project", r.status_code == 201, r.text[:200])
        zkey = r.json()["id"]

        print("== Kim cannot rename, hide or merge a service Zephyrwatch feeds ==")
        before = json.dumps(store.service_settings(), sort_keys=True, default=str)
        for path, method in (("/api/services/{}", "PATCH"), ("/api/sre/services/{}/settings", "PUT")):
            for body in ({"display": "Renamed"} if method == "PATCH" else {"display_name": "Renamed"},
                         {"hidden": True}, {"merged_into": "service-a"}):
                label = f"{method} {path.split('{')[0]}.. {sorted(body)[0]}"
                hid = await call("kim", method, path.format("zephyr-api"), body)
                unk = await call("kim", method, path.format("no-such-service"), body)
                ck(f"{label}: the private name answers as an unknown one",
                   hid.status_code == 404 and alike(hid, unk, "zephyr-api", "no-such-service"),
                   f"{hid.status_code} {hid.text[:120]} vs {unk.status_code} {unk.text[:120]}")
        ck("and nothing was written",
           json.dumps(store.service_settings(), sort_keys=True, default=str) == before)
        r = await call("kim", "PATCH", "/api/services/service-a", {"merged_into": "zephyr-api"})
        unk = await call("kim", "PATCH", "/api/services/service-a", {"merged_into": "no-such-service"})
        ck("merging a shared service into the private one answers as into an unknown one",
           r.status_code == unk.status_code and alike(r, unk, "zephyr-api", "no-such-service"),
           f"{r.text} vs {unk.text}")
        await call("kim", "PATCH", "/api/services/service-a", {"merged_into": None})
        # Zephyrwatch's service is itself merged into another: the refusal must not echo that
        store.set_service("zephyr-api", "test", merged_into="edge-proxy")
        r = await call("kim", "PATCH", "/api/services/service-a", {"merged_into": "zephyr-api"})
        unk = await call("kim", "PATCH", "/api/services/service-a", {"merged_into": "no-such-service"})
        ck("and it is not refused for a merge of the private service", r.status_code == unk.status_code
           and "already merged" not in r.text, f"{r.status_code} {r.text[:160]} vs {unk.status_code} {unk.text[:160]}")
        await call("kim", "PATCH", "/api/services/service-a", {"merged_into": None})
        store.set_service("zephyr-api", "test", merged_into=None)
        # a chain of Zephyrwatch's own services ends in service-a: Kim's merge of service-a would
        # make it longer than the limit, though she sees none of the links
        chain = ["zephyr-w1", "zephyr-w2", "zephyr-w3", "zephyr-w4", "zephyr-w5"]
        tw = base - timedelta(minutes=2)
        for w in chain:
            store.append([Envelope(source="zephyr-logs", source_type=source_type_for("otlp"),
                                   key_value=w, event_type="INFO", text=f"{w} handled 1",
                                   event_time=tw, payload={}, labels={"service": w}, ingest_time=tw)])
        sre_home.clear_cache()
        for a, b in zip(chain[1:], chain):
            store.set_service(a, "test", merged_into=b)
        store.set_service(chain[0], "test", merged_into="service-a")
        r = await call("kim", "PATCH", "/api/services/service-a", {"merged_into": "edge-proxy"})
        ck("a merge that makes a chain too long through links she cannot see is refused",
           r.status_code == 400 and "chain of more than" in r.text and "zephyr" not in r.text
           and not (store.service_settings().get("service-a") or {}).get("merged_into"),
           f"{r.status_code} {r.text[:200]}")
        r = await call("kim", "PATCH", "/api/services/service-a", {"merged_into": "zephyr-w1"})
        ck("a merge that would close a loop through a link she cannot see is refused",
           r.status_code == 400 and "chain of more than" in r.text
           and not (store.service_settings().get("service-a") or {}).get("merged_into"),
           f"{r.status_code} {r.text[:200]}")
        r = await call("lee", "PATCH", "/api/services/service-a", {"merged_into": "edge-proxy"})
        ck("and Lee, who sees them, is refused the same", r.status_code == 400
           and "chain of more than" in r.text, f"{r.status_code} {r.text[:200]}")
        for w in chain:
            store.set_service(w, "test", merged_into=None)
        sre_home.clear_cache()
        r = await call("kim", "PATCH", "/api/services/service-a", {"display": "Service A"})
        ck("a shared service still takes a change", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
        await call("kim", "PATCH", "/api/services/service-a", {"display": None})

        print("== a repo link ==")
        for path, method, field in (("/api/services/{}", "PATCH", "repos"),
                                    ("/api/sre/services/{}/settings", "PUT", "repos")):
            hid = await call("kim", method, path.format("service-a"), {field: [HIDDEN_REPO]})
            unk = await call("kim", method, path.format("service-a"), {field: ["example-org/no-such-repo"]})
            ck(f"{method} {path.split('{')[0]}..: a repo only Zephyrwatch's source sent answers as an unseen one",
               hid.status_code == unk.status_code == 400
               and alike(hid, unk, HIDDEN_REPO, "example-org/no-such-repo"),
               f"{hid.status_code} {hid.text[:120]} vs {unk.status_code} {unk.text[:120]}")
        ck("and no link was made", HIDDEN_REPO not in json.dumps(store.service_settings(), default=str))
        r = await call("kim", "PATCH", "/api/services/service-a", {"repos": ["example-org/service-a"]})
        ck("a repo the caller sees still links", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
        await call("kim", "PATCH", "/api/services/service-a", {"repos": None})
        r = await call("lee", "PATCH", "/api/services/zephyr-api", {"repos": [HIDDEN_REPO]})
        ck("Lee links the private repo to her service", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
        await call("lee", "PATCH", "/api/services/zephyr-api", {"repos": None})

        print("== Mia, Lee and the cell's key can ==")
        for who, name in (("mia", "Mia's name"), ("lee", "Lee's name"), ("root", "Root's name")):
            r = await call(who, "PATCH", "/api/services/zephyr-api", {"display": name})
            ck(f"{who} renames it", r.status_code == 200
               and store.service_settings().get("zephyr-api", {}).get("display") == name,
               f"{r.status_code} {r.text[:160]}")
        r = await call("lee", "PUT", "/api/sre/services/zephyr-api/settings", {"display_name": None})
        ck("and clears it through the SRE settings", r.status_code == 200, f"{r.status_code} {r.text[:160]}")

        print("== the label settings ==")
        await call("lee", "PUT", "/api/settings/service-labels", {"zephyr-logs": "service"})
        await call("lee", "PUT", "/api/settings/via-labels", {"zephyr-logs": ["upstream"]})
        r = await call("kim", "PUT", "/api/settings/service-labels", {"zephyr-logs": "namespace"})
        unk = await call("kim", "PUT", "/api/settings/service-labels", {"no-such-source": "namespace"})
        ck("service-labels for her source answers as for an unknown source",
           r.status_code == 400 and alike(r, unk, "zephyr-logs", "no-such-source"),
           f"{r.status_code} {r.text[:120]} vs {unk.status_code} {unk.text[:120]}")
        r = await call("kim", "PUT", "/api/settings/service-labels", {"prod-otlp": "namespace"})
        ck("a save of hers keeps what Zephyrwatch had stored",
           r.status_code == 200 and json.loads(store.get_setting(sre.LABELS_SETTING) or "{}")
           .get("zephyr-logs") == "service" and "zephyr-logs" not in r.json(),
           f"{r.status_code} {r.text[:160]} {store.get_setting(sre.LABELS_SETTING)}")
        r = await call("kim", "PUT", "/api/settings/via-labels", {"zephyr-logs": ["other"]})
        unk = await call("kim", "PUT", "/api/settings/via-labels", {"no-such-source": ["other"]})
        stored = json.loads(store.get_setting(sre.VIA_SETTING) or "{}")
        ck("via-labels for her source changes nothing and answers as for an unknown one",
           r.status_code == unk.status_code == 200 and stored.get("zephyr-logs") == ["upstream"]
           and r.json().get("zephyr-logs") == ["other"] and unk.json().get("no-such-source") == ["other"],
           f"{r.status_code} {r.text[:120]} {stored}")
        r = await call("kim", "PUT", "/api/settings/via-labels", {"prod-otlp": ["other"]})
        stored = json.loads(store.get_setting(sre.VIA_SETTING) or "{}")
        ck("a via-labels save of hers keeps the Zephyrwatch entry",
           r.status_code == 200 and stored.get("zephyr-logs") == ["upstream"]
           and stored.get("prod-otlp") == ["other"], str(stored))
        r = await call("kim", "PUT", "/api/settings/via-labels", {"zephyr-gh": ["other"]})
        stored = json.loads(store.get_setting(sre.VIA_SETTING) or "{}")
        ck("a via-labels entry for a hidden source that had none is not written", r.status_code == 200
           and "zephyr-gh" not in stored, str(stored))
        r = await call("lee", "PUT", "/api/settings/service-labels", {"zephyr-logs": "namespace"})
        ck("Lee changes the label of her own source", r.status_code == 200
           and json.loads(store.get_setting(sre.LABELS_SETTING) or "{}").get("zephyr-logs") == "namespace",
           f"{r.status_code} {r.text[:160]}")

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
        print("== the SRE People page ==")
        r = await call("kim", "GET", "/api/sre/settings")
        names = [k["name"] for k in r.json()["people"]["keys"]]
        ck("Kim's People page leaves out the project key", r.status_code == 200 and "zephyr-reader" not in names
           and "kim-laptop" in names, str(names))
        r = await call("lee", "GET", "/api/sre/settings")
        ck("Lee's People page lists it", "zephyr-reader" in [k["name"] for k in r.json()["people"]["keys"]])
        print("== the Tools list and the key count ==")
        store.upsert_catalog_source("example-gh", source_type_for("github_app"), "github_app", "1h", {
            "labels": [{"name": "repo", "field": "repo", "primary": True}]})
        runtime.reload_catalog()
        r = await call("kim", "GET", "/api/sre/settings")
        tools = [t["name"] for t in r.json()["tools"]["rows"]]
        ck("Kim's Tools list leaves out the sources only Zephyrwatch holds",
           r.status_code == 200 and "zephyr-gh" not in tools and "example-gh" in tools
           and "zephyr" not in json.dumps(r.json()["tools"]), str(tools))
        r = await call("lee", "GET", "/api/sre/settings")
        tools = [t["name"] for t in r.json()["tools"]["rows"]]
        ck("Lee's Tools list has it", "zephyr-gh" in tools and "example-gh" in tools, str(tools))
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
        r = await call("kim", "PUT", "/api/sre/settings/people", {"revoke": zkey})
        unk = await call("kim", "PUT", "/api/sre/settings/people", {"revoke": "nokey000"})
        ck("Kim revoking it there answers as for a key that is not there",
           r.status_code == 404 and alike(r, unk, zkey, "nokey000"),
           f"{r.status_code} {r.text[:120]} vs {unk.status_code} {unk.text[:120]}")
        ck("and it is still active", (store.get_api_key(zkey) or {}).get("revoked_at") is None)
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
