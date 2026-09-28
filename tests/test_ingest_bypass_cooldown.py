"""X-Tares-Bypass-Cooldown on POST /ingest/{token}: an on-demand delivery wakes a trigger for a
key that is inside its cooldown (RIUS-687 — Rius asking for one alert firing's analysis now).

Run: .venv/bin/python tests/test_ingest_bypass_cooldown.py   (no external services needed)

Asserts the whole contract: the header fires a cooling-down key, its absence still skips, the
firing re-arms the cooldown so the next ordinary event waits again, only `1`/`true` (any case)
count, a trigger grouped by labels is cleared under its composite key and not just the stamped
one, and the header carries no authority of its own — without an ingest credential the request is
refused and the cooldown is untouched.
"""
import asyncio
import importlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB = "/tmp/tares-bypass-cooldown-test.duckdb"
GROUP_DB = "/tmp/tares-bypass-cooldown-group-test.duckdb"
AUTH_DB = "/tmp/tares-bypass-cooldown-auth-test.duckdb"
CATALOG = "/tmp/tares-bypass-cooldown-test.catalog.yaml"
TOKEN = "bypass-test-token"
os.environ["TARES_CATALOG"] = CATALOG
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
# The evaluation debounce would re-run these back-to-back ingests through a delayed catch-up task;
# the bypass is about the cooldown, so take the debounce out of the way and assert synchronously.
os.environ["TARES_TRIGGER_DEBOUNCE_SECONDS"] = "0"

import httpx

SOURCE, VIEW, TRIGGER = "alerts", "alerts_view", "alert_fired"
SERVICE = "checkout"
PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok   {label}")
    else:
        FAIL += 1; print(f"  FAIL {label}  {detail}")


def fresh_app(db: str, auth_token: str = ""):
    """A daemon on its own database. The auth token is read at import, so the module is reloaded."""
    for p in (db, db + ".wal", CATALOG):
        if os.path.exists(p):
            os.remove(p)
    os.environ["TARES_DB"] = db
    os.environ["TARES_AUTH_TOKEN"] = auth_token
    import tares.daemon as daemon
    importlib.reload(daemon)
    return daemon.make_app()


async def build_catalog(cx, headers=None):
    """A webhook source keyed by service, a view over it, a trigger with a 5 minute cooldown."""
    h = headers or {}
    r = await cx.post("/api/sources", headers=h, json={
        "name": SOURCE, "connector": "webhook", "poll": "5s",
        "config": {"event_type": "alert_firing", "text_template": "{rule} on {service}",
                   "labels": [{"name": "service", "field": "service", "primary": True},
                              {"name": "rule", "field": "rule"},
                              {"name": "delivery_id", "field": "delivery_id"}]}})
    assert r.status_code < 300, r.text
    r = await cx.post("/api/views", headers=h,
                      json={"name": VIEW, "key_field": "service", "sources": [SOURCE]})
    assert r.status_code < 300, r.text
    r = await cx.post("/api/triggers", headers=h, json={
        "name": TRIGGER, "view": VIEW, "cooldown": "5m",
        "condition": {"aggregate": "count", "predicate": "> 0", "window": "5m"}})
    assert r.status_code < 300, r.text
    srcs = {s["name"]: s for s in (await cx.get("/api/sources", headers=h)).json()}
    return srcs[SOURCE]["ingest_key"]


async def fire_count(cx, headers=None, trigger=TRIGGER) -> int:
    rows = (await cx.get("/api/activity/dispatches", headers=headers or {})).json()
    return sum(1 for d in rows if d["trigger"] == trigger)


def alert(n: int) -> dict:
    return {"service": SERVICE, "rule": "error rate > 25%", "delivery_id": f"dlv-{n:03d}"}


async def send(cx, key: str, n: int, bypass=None, headers=None):
    h = dict(headers or {})
    if bypass is not None:
        h["X-Tares-Bypass-Cooldown"] = bypass
    return await cx.post(f"/ingest/{key}", json=alert(n), headers=h)


async def phase_open():
    app = fresh_app(DB)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as cx:
            store = app.state.store
            key = await build_catalog(cx)

            print("== the first alert fires and arms the cooldown ==")
            r = await send(cx, key, 1)
            check("ingest -> 202", r.status_code == 202, r.text)
            check("trigger fired once", await fire_count(cx) == 1)
            armed = store.last_fired(TRIGGER, SERVICE)
            check("cooldown armed for the service", armed is not None)

            print("== without the header a cooling-down key is still skipped ==")
            await send(cx, key, 2)
            check("no second firing", await fire_count(cx) == 1)
            check("cooldown untouched", store.last_fired(TRIGGER, SERVICE) == armed)

            print("== with the header the cooling-down key fires ==")
            r = await send(cx, key, 3, bypass="true")
            check("ingest -> 202", r.status_code == 202, r.text)
            check("forced firing", await fire_count(cx) == 2)
            forced_at = store.last_fired(TRIGGER, SERVICE)
            check("cooldown re-recorded at the forced firing", forced_at is not None
                  and forced_at != armed, f"{armed} -> {forced_at}")

            print("== the forced firing re-arms the cooldown ==")
            await send(cx, key, 4)
            check("the next ordinary alert waits again", await fire_count(cx) == 2)
            check("cooldown still the forced firing's",
                  store.last_fired(TRIGGER, SERVICE) == forced_at)

            print("== only 1 and true, in any case, bypass ==")
            for val in ("", "false", "0", "yes", "true-ish"):
                await send(cx, key, 5, bypass=val)
                check(f"{val!r} does not bypass", await fire_count(cx) == 2)
            await send(cx, key, 6, bypass="1")
            check("'1' bypasses", await fire_count(cx) == 3)
            await send(cx, key, 7, bypass="  TRUE  ")
            check("'TRUE' bypasses (case and space insensitive)", await fire_count(cx) == 4)

            print("== a bypassed alert that is not cooling down fires once, not twice ==")
            await send(cx, key, 8, bypass="true")
            check("one firing for the one event", await fire_count(cx) == 5)


async def phase_group_by():
    """A trigger grouped by labels rather than the stamped key: the bypass has to clear the same
    composite key the evaluation loop cools down per."""
    app = fresh_app(GROUP_DB)
    grouped = "alert_fired_by_rule"
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as cx:
            store = app.state.store
            key = await build_catalog(cx)
            r = await cx.post("/api/triggers", json={
                "name": grouped, "view": VIEW, "cooldown": "5m",
                "condition": {"aggregate": "count", "predicate": "> 0", "window": "5m",
                              "group_by": ["service", "rule"]}})
            assert r.status_code < 300, r.text
            composite = f"service={SERVICE}, rule=error rate > 25%"

            print("== label group_by: the bypass clears the composite key ==")
            await send(cx, key, 1)
            check("the first alert fires the grouped trigger",
                  await fire_count(cx, trigger=grouped) == 1)
            check("cooled down under the composite key",
                  store.last_fired(grouped, composite) is not None)
            await send(cx, key, 2)
            check("without the header the group is skipped",
                  await fire_count(cx, trigger=grouped) == 1)
            await send(cx, key, 3, bypass="true")
            check("with the header the group fires",
                  await fire_count(cx, trigger=grouped) == 2)
            await send(cx, key, 4)
            check("and the cooldown is re-armed for the group",
                  await fire_count(cx, trigger=grouped) == 2)


async def phase_auth():
    app = fresh_app(AUTH_DB, auth_token=TOKEN)
    admin = {"Authorization": f"Bearer {TOKEN}"}
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as cx:
            store = app.state.store
            key = await build_catalog(cx, admin)

            print("== auth on: the header carries no authority of its own ==")
            await send(cx, key, 1, headers=admin)
            check("the first authenticated alert fires", await fire_count(cx, admin) == 1)
            armed = store.last_fired(TRIGGER, SERVICE)

            r = await send(cx, key, 2, bypass="true")
            check("bypass without a credential -> 401", r.status_code == 401, r.text)
            r = await send(cx, key, 3, bypass="true",
                           headers={"Authorization": "Bearer not-the-token"})
            check("bypass with a bad credential -> 401", r.status_code == 401, r.text)
            check("no firing from the refused requests", await fire_count(cx, admin) == 1)
            check("cooldown untouched by the refused requests",
                  store.last_fired(TRIGGER, SERVICE) == armed)

            r = await send(cx, key, 4, bypass="true", headers=admin)
            check("the same bypass with a credential -> 202", r.status_code == 202, r.text)
            check("and it fires", await fire_count(cx, admin) == 2)


async def main():
    await phase_open()
    await phase_group_by()
    await phase_auth()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
