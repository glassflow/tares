"""Scoped API keys — create/list/revoke via /api/keys, and scope enforcement across the surface:
read (reads, catalog reads, subscribe), ingest (/ingest, /v1/*, remember), admin (CRUD,
credentials, keys). Env tokens act as implicit root keys. Revoking a key removes its subscriptions.
"""
import asyncio, os, subprocess, sys
import httpx

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))

SEED = "/tmp/keys_catalog.yaml"
with open(SEED, "w") as fh:
    fh.write("""sources:
  - name: evt
    connector: webhook
    poll: 5s
    config: {}
triggers:
  - name: t_evt
    sources: [evt]
    key_field: key_value
    condition: {aggregate: max, field: value, predicate: "> 1.0", window: 1m}
""")
DB, PORT = "/tmp/keys.duckdb", "8806"
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


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


async def main():
    for p in (DB, DB + ".wal"):
        if os.path.exists(p):
            os.remove(p)
    env = {**os.environ, "TARES_DB": DB, "TARES_CATALOG": SEED, "TARES_PORT": PORT,
           "TARES_OTLP_GRPC_PORT": "off", "TARES_AUTH_TOKEN": AUTH}
    proc = subprocess.Popen([sys.executable, "-c", "from tares.cli import run_daemon; run_daemon()"],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    B = f"http://127.0.0.1:{PORT}"
    try:
        if not await _wait(f"{B}/health"):
            ck("daemon up", False); return
        async with httpx.AsyncClient() as cx:
            # ── key management (admin only) ──
            r = await cx.post(f"{B}/api/keys", json={"name": "agent", "scopes": ["read"]}, headers=H(AUTH))
            ck("create read key (root)", r.status_code == 201, r.text)
            read_key = r.json()["secret"]
            ck("secret has nvf_ prefix", read_key.startswith("nvf_"), read_key[:8])
            r2 = await cx.post(f"{B}/api/keys", json={"name": "producer", "scopes": ["ingest"]}, headers=H(AUTH))
            ing_key, ing_id = r2.json()["secret"], r2.json()["id"]
            r3 = await cx.post(f"{B}/api/keys", json={"name": "plugin", "scopes": ["ingest", "read"]}, headers=H(AUTH))
            both_key = r3.json()["secret"]

            ck("create with bad scope -> 400",
               (await cx.post(f"{B}/api/keys", json={"name": "x", "scopes": ["root"]}, headers=H(AUTH))).status_code == 400)
            ck("list keys hides secrets", "secret" not in str((await cx.get(f"{B}/api/keys", headers=H(AUTH))).json()["keys"]))
            ck("read key cannot manage keys -> 403",
               (await cx.get(f"{B}/api/keys", headers=H(read_key))).status_code == 403)

            # ── read scope ──
            ck("read key: /read ok",
               (await cx.post(f"{B}/read", json={"selector": {"key_value": "k"}},
                              headers=H(read_key))).status_code == 200)
            ck("read key: catalog ok", (await cx.get(f"{B}/catalog", headers=H(read_key))).status_code == 200)
            ck("read key: sources list ok", (await cx.get(f"{B}/api/sources", headers=H(read_key))).status_code == 200)
            ck("read key: create source denied -> 403",
               (await cx.post(f"{B}/api/sources", json={"name": "n", "connector": "webhook", "config": {}},
                              headers=H(read_key))).status_code == 403)
            # every write outside a short read list is admin (the read scope used to reach these)
            for m, path, body in (("POST", "/api/projects", {"template": "custom", "name": "x", "objects": []}),
                                  ("DELETE", "/api/projects/uc_nope", None),
                                  ("POST", "/api/mcp-servers", {"name": "m", "url": "http://x"}),
                                  ("POST", "/api/integrations/github", {"name": "g", "token": "t"}),
                                  ("PUT", "/api/ask/sessions/s1", {}),
                                  ("POST", "/api/projects/templates/custom/detect", {})):
                r = await cx.request(m, f"{B}{path}", json=body, headers=H(read_key))
                ck(f"read key: {m} {path} -> 403", r.status_code == 403, f"{r.status_code} {r.text[:120]}")
            ck("read key: cannot ingest -> 403 (authenticated, lacks ingest scope)",
               (await cx.post(f"{B}/ingest/evt", json={"m": 1}, headers=H(read_key))).status_code == 403)
            ck("read key: /read narrowed to sources ok",
               (await cx.post(f"{B}/read", json={"selector": {"key_value": "k"}, "sources": ["evt"]},
                              headers=H(read_key))).status_code == 200)
            sub = await cx.post(f"{B}/subscribe", json={"trigger": "t_evt", "url": "http://x/hook"},
                                headers=H(read_key))
            ck("read key: subscribe ok", sub.status_code == 200, sub.text)

            # ── ingest scope ──
            ck("ingest key: ingest ok",
               (await cx.post(f"{B}/ingest/evt", json={"m": 1}, headers=H(ing_key))).status_code == 202)
            ck("ingest key: remember ok",
               (await cx.post(f"{B}/remember", json={"key": "k", "content": "obs"}, headers=H(ing_key))).status_code == 202)
            ck("ingest key: /read denied -> 403",
               (await cx.post(f"{B}/read", json={"selector": {"key_value": "k"}},
                              headers=H(ing_key))).status_code == 403)
            ck("read+ingest key: both work",
               (await cx.post(f"{B}/ingest/evt", json={"m": 2}, headers=H(both_key))).status_code == 202
               and (await cx.get(f"{B}/catalog", headers=H(both_key))).status_code == 200)

            # ── env auth token is still root (admin) ──
            ck("env auth token: admin ok", (await cx.get(f"{B}/api/keys", headers=H(AUTH))).status_code == 200)
            ck("unknown token -> 401", (await cx.get(f"{B}/catalog", headers=H("nope"))).status_code == 401)

            # ── whoami ──
            w = (await cx.get(f"{B}/api/whoami", headers=H(read_key))).json()
            ck("whoami: name + scopes", w.get("name") == "agent" and w.get("scopes") == ["read"], str(w))

            # ── revocation kills access AND subscriptions ──
            r = await cx.post(f"{B}/api/keys", json={"name": "revoke-me", "scopes": ["read"]}, headers=H(AUTH))
            rk, rk_id = r.json()["secret"], r.json()["id"]
            await cx.post(f"{B}/subscribe", json={"trigger": "t_evt", "url": "http://gone/hook"}, headers=H(rk))
            subs_before = (await cx.get(f"{B}/api/subscriptions", headers=H(AUTH))).json()
            ck("revoke -> 200", (await cx.delete(f"{B}/api/keys/{rk_id}", headers=H(AUTH))).status_code == 200)
            ck("revoked key -> 401", (await cx.get(f"{B}/catalog", headers=H(rk))).status_code == 401)
            subs_after = (await cx.get(f"{B}/api/subscriptions", headers=H(AUTH))).json()
            gone = all("gone" not in str(s) for s in subs_after) and any("gone" in str(s) for s in subs_before)
            ck("revocation removed the key's subscriptions", gone,
               f"before={subs_before} after={subs_after}")
            ck("revoke twice -> 404", (await cx.delete(f"{B}/api/keys/{rk_id}", headers=H(AUTH))).status_code == 404)
            ck("revoke ingest key of producer works",
               (await cx.delete(f"{B}/api/keys/{ing_id}", headers=H(AUTH))).status_code == 200)
            ck("revoked producer cannot ingest -> 401",
               (await cx.post(f"{B}/ingest/evt", json={"m": 4}, headers=H(ing_key))).status_code == 401)
    finally:
        proc.terminate()
        proc.wait(timeout=10)

    print(f"\n{P} passed, {F} failed")
    raise SystemExit(1 if F else 0)


asyncio.run(main())
