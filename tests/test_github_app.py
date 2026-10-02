"""The GitHub connector, App side and token side (TR-159, TR-161, TR-163, TR-164).

Run: .venv/bin/python tests/test_github_app.py   (no network: GitHub is faked in-process)

Covers, against a fake GitHub that checks the App's JWT with a generated RSA key:
* App auth: JWT claims, installation token minted once and reused, refreshed near expiry, the
  installation chosen by repo, a broker credential, named errors (not installed, wrong key).
* Signed state: round trip, tampering, expiry.
* Signature checks (webhook_verify): github, linear (with its replay window), hmac_sha256.
* The event contract: every stored event type, and a merged PR seen by webhook and by polling
  producing the same key, event type and labels.
* The daemon, with auth on: Create GitHub App (manifest -> callback -> credential + webhook source),
  the install callback, a signed delivery accepted with no Tares key, an unsigned or wrongly signed
  one refused (401, counted), ping / duplicate / unknown installation, uninstall removing the
  installation, a trigger filtered to `action=merged` firing once, the credential row never
  carrying the key or secrets, the token path's pull request polling (no backfill on first poll).
"""
import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone

os.environ["TARES_DB"] = "/tmp/tares-ghapp-test.duckdb"
os.environ["TARES_CATALOG"] = "/tmp/does-not-exist.yaml"
os.environ["TARES_AUTH_TOKEN"] = "root-secret"
os.environ["TARES_PUBLIC_URL"] = "https://cell.example.com"
os.environ["TARES_TRIGGER_DEBOUNCE_SECONDS"] = "0"

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok   {label}")
    else:
        FAIL += 1; print(f"  FAIL {label}  {detail}")


_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = _key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                         serialization.NoEncryption()).decode()
PUB = _key.public_key()
OTHER_PEM = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption()).decode()
APP_ID = "4242"
WEBHOOK_SECRET = "whsec-test"

STATE = {"minted": 0, "exp_in": 3600, "calls": [], "pulls": [], "broker": 0}
INSTALLS = {101: {"login": "acme", "repos": ["acme/app", "acme/lib"]},
            202: {"login": "other", "repos": ["other/site"]}}


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def fake_github(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    auth = request.headers.get("authorization", "")
    bearer = auth.split(" ", 1)[1] if auth.startswith("Bearer ") else ""
    STATE["calls"].append((request.method, path))
    if request.url.host == "broker.test":
        if bearer != "broker-secret":
            return httpx.Response(401)
        STATE["broker"] += 1
        return httpx.Response(200, json={"token": f"ghs_broker_{STATE['broker']}",
                                         "expires_at": _iso(datetime.now(timezone.utc)
                                                            + timedelta(hours=1))})
    if request.url.host not in ("api.github.com", "api.github.test"):
        return httpx.Response(599, text="unexpected host " + request.url.host)
    if request.method == "POST" and path.startswith("/app-manifests/"):
        code = path.split("/")[2]
        if code != "good-code":
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(201, json={
            "id": int(APP_ID), "slug": "tares-cell", "name": "Tares cell", "client_id": "Iv1.x",
            "client_secret": "csec", "pem": PEM, "webhook_secret": WEBHOOK_SECRET,
            "html_url": "https://github.com/apps/tares-cell", "owner": {"login": "acme"}})
    if path.startswith("/app/"):
        try:
            claims = jwt.decode(bearer, PUB, algorithms=["RS256"])
        except Exception:
            return httpx.Response(401, json={"message": "A JSON web token could not be decoded"})
        if claims.get("iss") != APP_ID:
            return httpx.Response(401, json={"message": "bad iss"})
        parts = path.strip("/").split("/")
        if path == "/app/installations":
            return httpx.Response(200, json=[{"id": i, "account": {"login": v["login"],
                                              "type": "Organization"},
                                              "repository_selection": "selected"}
                                             for i, v in INSTALLS.items()])
        if len(parts) >= 3 and parts[1] == "installations":
            iid = int(parts[2])
            if iid not in INSTALLS:
                return httpx.Response(404, json={"message": "Not Found"})
            if len(parts) == 3:
                return httpx.Response(200, json={"id": iid, "account": {
                    "login": INSTALLS[iid]["login"], "type": "Organization"},
                    "repository_selection": "selected"})
            if parts[3] == "access_tokens" and request.method == "POST":
                STATE["minted"] += 1
                exp = datetime.now(timezone.utc) + timedelta(seconds=STATE["exp_in"])
                return httpx.Response(201, json={"token": f"ghs_{iid}_{STATE['minted']}",
                                                 "expires_at": _iso(exp)})
        return httpx.Response(404, json={"message": "Not Found"})
    if not bearer.startswith("ghs_") and bearer != "tok-pat":
        return httpx.Response(401, json={"message": "Bad credentials"})
    if path == "/installation/repositories":
        iid = int(bearer.split("_")[1])
        return httpx.Response(200, json={"repositories": [
            {"full_name": r, "default_branch": "main", "private": True} for r in INSTALLS[iid]["repos"]]})
    if path == "/repos/acme/app":
        return httpx.Response(200, json={"default_branch": "main", "private": True})
    if path == "/repos/acme/app/commits":
        return httpx.Response(200, json=[])
    if path == "/repos/acme/app/check-runs" and request.method == "POST":
        body = json.loads(request.content)
        STATE.setdefault("checks", []).append(body)
        return httpx.Response(201, json={"id": 9, "html_url": "https://github.com/acme/app/runs/9"})
    if path == "/repos/acme/app/pulls":
        return httpx.Response(200, json=STATE["pulls"])
    return httpx.Response(404, json={"message": "Not Found"})


_RealClient = httpx.AsyncClient


class PatchedClient(_RealClient):
    def __init__(self, *a, **kw):
        if "transport" not in kw:
            kw["transport"] = httpx.MockTransport(fake_github)
        super().__init__(*a, **kw)


httpx.AsyncClient = PatchedClient
FIX = os.path.join(os.path.dirname(__file__), "fixtures", "github")


def fixture(name):
    with open(os.path.join(FIX, name)) as fh:
        return json.load(fh)


async def unit():
    from tares import github_app as ga
    from tares.webhook_verify import sign, verify
    from tares.connectors import github_events as gh

    print("== App JWT and installation tokens ==")
    tok = ga.app_jwt(APP_ID, PEM)
    claims = jwt.decode(tok, PUB, algorithms=["RS256"])
    now = time.time()
    check("JWT: iss = app id, iat 60s back, exp under 10 minutes",
          claims["iss"] == APP_ID and now - 70 < claims["iat"] < now - 50
          and claims["exp"] - now <= 600, str(claims))
    try:
        ga.app_jwt(APP_ID, "not a pem")
        check("bad PEM -> named error", False)
    except ValueError as e:
        check("bad PEM -> named error", "private key" in str(e), str(e))

    cred = {"name": "u1", "kind": "app", "api_url": "", "config": {
        "app_id": APP_ID, "private_key": PEM,
        "installations": [{"id": 101, "account": "acme"}, {"id": 202, "account": "other"}]}}
    ga.forget("u1")
    STATE["minted"] = 0
    t1 = await ga.installation_token(cred, 101)
    t2 = await ga.installation_token(cred, 101)
    check("token minted once and reused", t1 == t2 and STATE["minted"] == 1, f"{t1} {t2}")
    STATE["exp_in"] = 120            # under the refresh margin
    ga.forget("u1")
    a = await ga.installation_token(cred, 101)
    b = await ga.installation_token(cred, 101)
    check("near expiry -> refreshed", a != b, f"{a} {b}")
    STATE["exp_in"] = 900            # 15 minutes left: fine for a poll, not for an MCP run
    ga.forget("u1")
    p1 = await ga.installation_token(cred, 101)
    m1 = await ga.installation_token(cred, 101, min_life=1200)
    check("an MCP run asks for 20 minutes of life: a 15-minute token is replaced", p1 != m1, f"{p1} {m1}")
    STATE["exp_in"] = 3600
    ga.forget("u1")
    t = await ga.token_for(cred, "other/site")
    check("installation chosen by repo", t.startswith("ghs_202_"), t)
    try:
        await ga.token_for(cred, None)
        check("several installations, no repo -> named error", False)
    except ValueError as e:
        check("several installations, no repo -> named error", "several" in str(e), str(e))
    try:
        await ga.token_for({**cred, "config": {**cred["config"], "installations": []}}, "acme/app")
        check("not installed -> named error", False)
    except ValueError as e:
        check("not installed -> named error", "not installed" in str(e), str(e))
    wrong = {**cred, "name": "u2", "config": {**cred["config"], "private_key": OTHER_PEM}}
    try:
        await ga.installation_token(wrong, 101)
        check("wrong key -> 401 named", False)
    except ValueError as e:
        check("wrong key -> 401 named", "401" in str(e), str(e))
    broker = {"name": "b1", "kind": "app_broker", "api_url": "", "account": "acme",
              "config": {"token_url": "https://broker.test/api/github/token",
                         "broker_secret": "broker-secret", "installation_id": 101}}
    bt = await ga.token_for(broker, "acme/app")
    bt2 = await ga.token_for(broker, "acme/app")
    check("broker: token fetched once, cached", bt == bt2 and STATE["broker"] == 1, bt)

    print("== signed state ==")

    class S(dict):
        def get_setting(self, k): return self.get(k)
        def set_setting(self, k, v): self[k] = v
    st = S()
    s = ga.sign_state(st, {"op": "create", "cred": "x"})
    check("state round trip", ga.verify_state(st, s)["cred"] == "x")
    for label, bad in (("tampered", s[:-2] + ("00" if s[-2:] != "00" else "11")),
                       ("garbage", "nope")):
        try:
            ga.verify_state(st, bad)
            check(f"state {label} -> refused", False)
        except ValueError:
            check(f"state {label} -> refused", True)
    old = ga.STATE_TTL
    ga.STATE_TTL = -1
    expired = ga.sign_state(st, {"op": "create"})
    ga.STATE_TTL = old
    try:
        ga.verify_state(st, expired)
        check("expired state -> refused", False)
    except ValueError as e:
        check("expired state -> refused", "expired" in str(e), str(e))

    print("== signature checks ==")
    body = b'{"a":1}'
    check("github: good", verify("github", "s", body, {"X-Hub-Signature-256": sign("github", "s", body)}) is None)
    check("github: wrong secret", verify("github", "s", body, {"X-Hub-Signature-256": sign("github", "t", body)}))
    check("github: missing header", verify("github", "s", body, {}))
    check("github: no secret configured refuses", verify("github", "", body,
                                                        {"X-Hub-Signature-256": sign("github", "", body)}))
    lin = json.dumps({"webhookTimestamp": int(time.time() * 1000)}).encode()
    check("linear: good", verify("linear", "s", lin, {"Linear-Signature": sign("linear", "s", lin)}) is None)
    stale = json.dumps({"webhookTimestamp": int(time.time() * 1000) - 120_000}).encode()
    check("linear: stale timestamp refused",
          "60 seconds" in (verify("linear", "s", stale, {"Linear-Signature": sign("linear", "s", stale)}) or ""))
    check("hmac_sha256: named header, sha256= prefix accepted",
          verify("hmac_sha256", "s", body, {"X-Sig": "sha256=" + sign("hmac", "s", body)}, "X-Sig") is None)

    print("== the event contract ==")
    cases = {
        "pull_request_opened.json": ("pull_request", "acme/app#7", "opened"),
        "pull_request_merged.json": ("pull_request", "acme/app#7", "merged"),
        "pull_request_review.json": ("pull_request_review", "acme/app#7", "submitted"),
        "push.json": ("push", "acme/app", "pushed"),
        "issues.json": ("issues", "acme/app#9", "closed"),
        "issue_comment.json": ("issue_comment", "acme/app#9", "created"),
        "release.json": ("release", "acme/app", "published"),
        "workflow_run.json": ("workflow_run", "acme/app", "completed"),
    }
    for fname, (event, key, action) in cases.items():
        built = gh.build(event, fixture(fname))
        check(f"{fname}: key {key}, action {action}, text short",
              built["key"] == key and built["labels"].get("action") == action
              and built["labels"].get("repo") == "acme/app" and 0 < len(built["text"]) <= 300,
              f"{built['key']} {built['labels']} {built['text']!r}")
    merged = gh.build("pull_request", fixture("pull_request_merged.json"))
    check("merged PR text says merged into main",
          merged["text"].startswith("PR #7 merged into main"), merged["text"])
    wf = gh.build("workflow_run", fixture("workflow_run.json"))
    check("workflow run: conclusion label and failed text",
          wf["labels"].get("conclusion") == "failure" and "failed" in wf["text"], wf["text"])
    push = gh.build("push", fixture("push.json"))
    check("push to the default branch says so", push["labels"].get("on_default_branch") == "true",
          str(push["labels"]))

    print("== the `in` filter and GitHub trigger sentences ==")
    from tares.store import _filter_sql
    from tares.config import CatalogError, validate_filters
    sql, params = _filter_sql([{"field": "repo", "op": "in", "value": ["acme/app", "acme/lib"]}])
    check("in -> lower(label) IN (?, ?), case-insensitive",
          "lower(" in sql and "IN (?, ?)" in sql and params == ["acme/app", "acme/lib"], f"{sql} {params}")
    sql2, params2 = _filter_sql([{"field": "repo", "op": "in", "value": ["Acme/App"]}])
    check("a typed repo in another case still matches", params2 == ["acme/app"], str(params2))
    try:
        validate_filters([{"field": "repo", "op": "in", "value": "acme/app"}], "trigger 't'")
        check("in with a non-list value refused", False)
    except CatalogError:
        check("in with a non-list value refused", True)
    from tares import goal
    w = goal._github_event_words([{"field": "event_type", "op": "eq", "value": "pull_request"},
                                  {"field": "action", "op": "eq", "value": "merged"},
                                  {"field": "repo", "op": "in", "value": ["acme/app", "acme/lib"]}])
    check("a GitHub trigger is said by its event",
          w[0] == "a pull request is merged" and goal.filters_words(w[1])
          == " matching repo one of acme/app, acme/lib", str(w))


async def daemon():
    for p in (os.environ["TARES_DB"], os.environ["TARES_DB"] + ".wal"):
        if os.path.exists(p):
            os.remove(p)
    from tares.daemon import make_app
    from tares.webhook_verify import sign
    from urllib.parse import parse_qs, urlsplit
    app = make_app()
    root = {"Authorization": "Bearer root-secret"}
    async with app.router.lifespan_context(app):
        store = app.state.store
        runtime = app.state.runtime
        transport = httpx.ASGITransport(app=app)
        async with _RealClient(transport=transport, base_url="http://test", headers=root) as cx, \
                _RealClient(transport=transport, base_url="http://test") as anon:

            print("== Create GitHub App (manifest flow) ==")
            r = await cx.post("/api/integrations/github/apps", json={"name": "acme-app",
                                                                     "org": "acme"})
            check("create -> manifest + action", r.status_code == 200, r.text)
            body = r.json()
            man = json.loads(body["manifest"])
            check("action posts to the org's new-App page with a state",
                  body["action"].startswith("https://github.com/organizations/acme/settings/apps/new?state="),
                  body["action"])
            check("manifest: webhook to this cell's ingest, callbacks on the public URL",
                  man["hook_attributes"]["url"].startswith("https://cell.example.com/ingest/github_app-")
                  and man["redirect_url"] == "https://cell.example.com/api/integrations/github/apps/callback"
                  and man["default_permissions"]["checks"] == "write"
                  and "pull_request" in man["default_events"], json.dumps(man)[:300])
            check("no localhost warning on a public URL", body["warning"] is None, str(body["warning"]))
            state = parse_qs(urlsplit(body["action"]).query)["state"][0]
            ingest_key = man["hook_attributes"]["url"].rsplit("/", 1)[1]

            r = await anon.get("/api/integrations/github/apps/callback",
                               params={"code": "good-code", "state": "forged.state"})
            check("callback with a forged state -> back to settings with an error, nothing stored",
                  r.status_code == 303 and "error=" in r.headers["location"]
                  and store.get_github_credential("acme-app") is None, r.headers.get("location"))
            r = await anon.get("/api/integrations/github/apps/callback",
                               params={"code": "good-code", "state": state})
            check("callback (no Tares key) -> 303 to settings, created",
                  r.status_code == 303 and "event=created" in r.headers["location"],
                  r.headers.get("location"))
            cred = store.get_github_credential("acme-app")
            check("credential stored as kind app with key + webhook secret",
                  cred and cred["kind"] == "app" and cred["config"]["private_key"] == PEM
                  and cred["config"]["webhook_secret"] == WEBHOOK_SECRET
                  and cred["config"]["slug"] == "tares-cell", str(cred and cred["kind"]))
            src = runtime.catalog.sources.get("github")
            check("webhook source `github` created with the manifest's ingest key",
                  src is not None and src.connector == "github_app" and src.ingest_key == ingest_key,
                  str(src))
            r = await cx.get("/api/integrations/github")
            row = next(c for c in r.json()["credentials"] if c["name"] == "acme-app")
            raw = json.dumps(row)
            check("listed row: no key, no secrets, source named",
                  PEM[:40] not in raw and WEBHOOK_SECRET not in raw and "csec" not in raw
                  and row["key_configured"] and row["source"] == "github", raw[:300])

            print("== install ==")
            r = await cx.get("/api/integrations/github/acme-app/install")
            check("install link to the App's install page", r.status_code == 200
                  and r.json()["url"].startswith("https://github.com/apps/tares-cell/installations/new?state="),
                  r.text)
            r = await anon.get("/api/integrations/github/apps/installed",
                               params={"installation_id": 999, "setup_action": "install"})
            check("an installation that is not ours -> refused",
                  "error=" in r.headers["location"], r.headers.get("location"))
            r = await anon.get("/api/integrations/github/apps/installed",
                               params={"installation_id": 101, "setup_action": "install"})
            check("install callback, no state: on the App owner's account -> recorded",
                  "event=installed" in r.headers["location"]
                  and [i["id"] for i in store.get_github_credential("acme-app")["config"]["installations"]] == [101],
                  r.headers.get("location"))
            r = await anon.get("/api/integrations/github/apps/installed",
                               params={"installation_id": 202, "setup_action": "install"})
            check("install callback, no state, a stranger's account -> not added",
                  "error=" in r.headers["location"] and 202 not in
                  [i["id"] for i in store.get_github_credential("acme-app")["config"]["installations"]],
                  r.headers.get("location"))
            r = await cx.post("/api/integrations/github/acme-app/test")
            check("Test: resync keeps only known/owner installations, counts repos",
                  r.json().get("ok") and {i["id"] for i in r.json()["installations"]} == {101}
                  and r.json()["installations"][0]["repos"] == 2, r.text)
            link = (await cx.get("/api/integrations/github/acme-app/install")).json()["url"]
            st = parse_qs(urlsplit(link).query)["state"][0]
            r = await anon.get("/api/integrations/github/apps/installed",
                               params={"installation_id": 202, "setup_action": "install", "state": st})
            check("install callback with our state (Add an organization) -> added",
                  "event=installed" in r.headers["location"] and {101, 202} ==
                  {i["id"] for i in store.get_github_credential("acme-app")["config"]["installations"]},
                  r.headers.get("location"))
            r = await cx.get("/api/integrations/github/acme-app/repos")
            check("repos across installations", {x["full_name"] for x in r.json()["repos"]}
                  == {"acme/app", "acme/lib", "other/site"}, r.text)

            print("== deliveries ==")
            r = await cx.post("/api/triggers", json={
                "name": "pr_merged", "sources": ["github"],
                "condition": {"aggregate": "count", "predicate": "> 0", "window": "5m"},
                "filters": [{"field": "event_type", "op": "eq", "value": "pull_request"},
                            {"field": "action", "op": "eq", "value": "merged"}],
                "cooldown_seconds": 0})
            check("trigger on action=merged created", r.status_code in (200, 201), r.text)

            async def deliver(event, payload, delivery, secret=WEBHOOK_SECRET, signed=True):
                if "pull_request" in payload:     # inside the trigger's window, whatever the clock
                    payload = {**payload, "pull_request": {
                        **payload["pull_request"], "updated_at": _iso(datetime.now(timezone.utc))}}
                raw = json.dumps(payload).encode()
                h = {"X-GitHub-Event": event, "X-GitHub-Delivery": delivery,
                     "Content-Type": "application/json"}
                if signed:
                    h["X-Hub-Signature-256"] = sign("github", secret, raw)
                return await anon.post(f"/ingest/{ingest_key}", content=raw, headers=h)

            r = await deliver("ping", {"zen": "hi", "hook_id": 1}, "d-ping")
            check("ping, signed, no Tares key -> 202, nothing stored",
                  r.status_code == 202 and r.json()["ingested"] == 0, r.text)
            opened = fixture("pull_request_opened.json")
            r = await deliver("pull_request", opened, "d-1", signed=False)
            check("unsigned -> 401", r.status_code == 401, r.text)
            r = await deliver("pull_request", opened, "d-1", secret="wrong")
            check("wrong secret -> 401", r.status_code == 401 and "signature" in r.text, r.text)
            check("refusals counted on the source", runtime.rejected_deliveries.get("github") == 2,
                  str(runtime.rejected_deliveries))
            r = await deliver("pull_request", opened, "d-1")
            check("signed PR opened -> stored", r.status_code == 202 and r.json()["ingested"] == 1, r.text)
            r = await deliver("pull_request", opened, "d-1")
            check("same delivery id again -> skipped", r.json()["ingested"] == 0, r.text)
            foreign = {**opened, "installation": {"id": 555}}
            r = await deliver("pull_request", foreign, "d-x")
            check("unknown installation -> dropped", r.json()["ingested"] == 0, r.text)
            r = await deliver("pull_request", fixture("pull_request_merged.json"), "d-2")
            check("signed PR merged -> stored", r.json()["ingested"] == 1, r.text)
            await asyncio.sleep(0.3)
            rows = store.con.execute(
                "SELECT key_value, event_type, text, labels FROM events WHERE source = 'github' "
                "ORDER BY ingest_time").fetchall()
            check("events keyed per PR, contract labels present",
                  len(rows) == 2 and rows[1][0] == "acme/app#7"
                  and json.loads(rows[1][3]).get("action") == "merged"
                  and json.loads(rows[1][3]).get("base") == "main", str(rows))
            fired = store.con.execute(
                "SELECT count(*) FROM dispatch_log WHERE trigger = 'pr_merged'").fetchone()[0]
            check("trigger filtered to action=merged fired once", fired == 1, str(fired))

            r = await anon.post(f"/ingest/{ingest_key}", content=b"{}", headers={
                "X-GitHub-Event": "ping", "X-Hub-Signature-256": "sha256=éé".encode()})
            check("non-ASCII signature -> 401, not 500", r.status_code == 401, r.text)
            stranger = {"action": "created", "installation": {"id": 404, "account": {"login": "evil"}}}
            r = await deliver("installation", stranger, "d-evil")
            check("a stranger's install (signed, public App) is not added",
                  404 not in [i["id"] for i in store.get_github_credential("acme-app")["config"]["installations"]],
                  str(store.get_github_credential("acme-app")["config"]["installations"]))
            early = {**fixture("issues.json"), "installation": {"id": 303}}
            r = await deliver("issues", early, "d-early")
            check("an event before its installation is known -> dropped", r.json()["ingested"] == 0, r.text)
            r = await deliver("installation", {"action": "created", "installation": {
                "id": 303, "account": {"login": "acme"}}}, "d-inst303")
            r = await deliver("issues", early, "d-early")
            check("GitHub's redelivery of that event is stored, not deduped",
                  r.json()["ingested"] == 1, r.text)

            r = await deliver("installation", {"action": "deleted", "installation": {
                "id": 202, "account": {"login": "other"}}}, "d-un")
            check("uninstall delivery removes the installation",
                  r.json()["ingested"] == 0 and 202 not in
                  [i["id"] for i in store.get_github_credential("acme-app")["config"]["installations"]],
                  str(store.get_github_credential("acme-app")["config"]["installations"]))
            r = await cx.get("/api/integrations/github")
            row = next(c for c in r.json()["credentials"] if c["name"] == "acme-app")
            check("row shows deliveries and refusals",
                  row["deliveries"]["stored"] == 3 and row["deliveries"]["rejected_signature"] == 3,
                  str(row.get("deliveries")))

            print("== a manually entered App and a broker credential ==")
            r = await cx.post("/api/integrations/github", json={
                "name": "by-hand", "kind": "app", "app_id": APP_ID, "private_key": PEM,
                "webhook_secret": "s2", "installation_ids": [101]})
            check("app by hand -> 201 with its own source", r.status_code == 201
                  and r.json()["source"] == "github_by_hand", r.text)
            r = await cx.post("/api/integrations/github", json={
                "name": "cloud", "kind": "app_broker", "token_url": "https://broker.test/t",
                "broker_secret": "broker-secret", "installation_id": 101, "account": "acme",
                "webhook_secret": "s3"})
            check("broker credential -> 201, source + ingest key returned",
                  r.status_code == 201 and r.json()["ingest_key"], r.text)
            r = await cx.get("/api/integrations/github")
            row = next(c for c in r.json()["credentials"] if c["name"] == "cloud")
            check("broker row: no broker secret, one installation",
                  "broker-secret" not in json.dumps(row) and row["installations"][0]["id"] == 101,
                  json.dumps(row)[:300])

            print("== token path: pull request polling ==")
            r = await cx.post("/api/integrations/github", json={"name": "pat", "token": "tok-pat",
                                                                "api_url": "http://api.github.test"})
            from tares.config import SourceCfg
            from tares.connectors import build_connector
            cfg = SourceCfg(name="gh_poll", type="event_stream", connector="github",
                            poll_seconds=60, poll="60s",
                            config={"repo": "acme/app", "credential": "pat", "files": False,
                                    "labels": [{"name": "repo", "field": "repo", "primary": True}]})
            conn = build_connector(cfg, store)
            base = datetime.now(timezone.utc) - timedelta(minutes=5)
            STATE["pulls"] = [{"number": 3, "title": "Old PR", "state": "closed",
                               "created_at": _iso(base), "closed_at": _iso(base),
                               "merged_at": _iso(base), "updated_at": _iso(base),
                               "user": {"login": "bob"}, "head": {"ref": "x", "sha": "s3"},
                               "base": {"ref": "main"}, "html_url": "u3", "merged": True}]
            first = await conn.poll()
            check("first poll imports no old pull requests", first == [], str(first))
            later = datetime.now(timezone.utc) + timedelta(seconds=5)
            STATE["pulls"] = [{"number": 7, "title": "Add retry", "state": "closed",
                               "created_at": _iso(later), "closed_at": _iso(later + timedelta(seconds=1)),
                               "merged_at": _iso(later + timedelta(seconds=1)),
                               "updated_at": _iso(later + timedelta(seconds=1)),
                               "user": {"login": "alice"}, "head": {"ref": "feat/retry", "sha": "abc"},
                               "base": {"ref": "main"}, "html_url": "u7", "merged": True,
                               "draft": False}] + STATE["pulls"]
            envs = await conn.poll()
            acts = [(e.event_type, e.key_value, e.labels.get("action")) for e in envs]
            check("next poll: opened then merged, keyed per PR",
                  acts == [("pull_request", "acme/app#7", "opened"),
                           ("pull_request", "acme/app#7", "merged")], str(acts))
            again = await conn.poll()
            check("a third poll with nothing new is empty", again == [], str(again))
            from tares.connectors import github_events as gh
            hook = gh.build("pull_request", fixture("pull_request_merged.json"))
            polled = envs[-1]
            same = ("repo", "action", "number", "author", "branch", "base", "state")
            check("webhook and polling agree: event type, key and labels for the same merge",
                  polled.event_type == hook["event_type"] and polled.key_value == hook["key"]
                  and all(str(polled.labels.get(k)) == str(hook["labels"].get(k)) for k in same),
                  f"{polled.labels} vs {hook['labels']}")

            print("== upgrade: triggers on token sources keep meaning a commit ==")
            r = await cx.post("/api/sources", json={"name": "old_repo", "connector": "github",
                                                    "poll": "60s", "config": {"repo": "acme/app",
                                                                              "credential": "pat"}})
            check("token source created", r.status_code in (200, 201), r.text)
            for tname, filters in (("old_any", []), ("old_pr", [{"field": "event_type", "op": "eq",
                                                                  "value": "pull_request"}])):
                r = await cx.post("/api/triggers", json={
                    "name": tname, "sources": ["old_repo"], "filters": filters,
                    "condition": {"aggregate": "count", "predicate": "> 0", "window": "5m"}})
            store.set_setting("github_commit_filters_filled", None)
            from tares.projects import Engine
            n = Engine(store).fill_github_commit_filters()
            trigs = {t["name"]: t for t in store.list_catalog_triggers()}
            check("an unfiltered trigger on a token source gets event_type = commit",
                  trigs["old_any"]["filters"] == [{"field": "event_type", "op": "eq",
                                                   "value": "commit"}], str(trigs["old_any"]))
            check("a trigger already naming its event type is left alone; App triggers too",
                  trigs["old_pr"]["filters"][0]["value"] == "pull_request"
                  and trigs["pr_merged"]["filters"][0]["value"] == "pull_request" and n == 1,
                  f"{n} {trigs['old_pr']['filters']}")
            check("the upgrade runs once", Engine(store).fill_github_commit_filters() == 0)

            print("== agents on GitHub (TR-165) ==")
            r = await cx.post("/api/integrations/github/acme-app/mcp", json={"write": True})
            r2 = await cx.post("/api/integrations/github/acme-app/mcp", json={"write": True})
            r3 = await cx.post("/api/integrations/github/acme-app/mcp", json={"write": False})
            srv = store.get_mcp_server("github-acme-app")
            check("the credential's GitHub MCP server: made once, reused; a read-only one apart",
                  r.json()["server"] == r2.json()["server"] == "github-acme-app"
                  and r3.json()["server"] == "github-acme-app-read"
                  and srv["auth_value"] == "credential:github/acme-app"
                  and "X-MCP-Readonly" not in (srv.get("headers") or {})
                  and (store.get_mcp_server("github-acme-app-read").get("headers") or {}).get(
                      "X-MCP-Readonly") == "true", str(srv))
            r = await cx.post("/api/agents/builtin", json={
                "name": "pr_checker", "trigger": "pr_merged", "prompt": "Review the PR.",
                "mcp_servers": ["github-acme-app"], "github": "acme-app"})
            check("agent created with a GitHub credential", r.status_code == 201, r.text)
            r = await cx.post("/api/agents/builtin", json={
                "name": "bad_gh", "trigger": "pr_merged", "prompt": "x", "github": "nope"})
            check("an unknown credential is refused", r.status_code == 404, r.text)
            ag = store.get_catalog_agent("pr_checker")
            rows = (await cx.get("/api/agents/builtin")).json()["agents"]
            check("the agent's github is stored and listed",
                  ag["github"] == "acme-app" and next(a for a in rows if a["name"] == "pr_checker")["github"]
                  == "acme-app", str(ag.get("github")))
            r = await cx.put("/api/agents/builtin/pr_checker", json={
                "trigger": "pr_merged", "prompt": "Review the PR again.", "mcp_servers": ["github-acme-app"]})
            check("an update that does not mention github keeps it",
                  store.get_catalog_agent("pr_checker")["github"] == "acme-app", r.text)
            from tares import github_tools
            from tares.results import from_tool_call
            check("check runs offered for an App credential, not for a token",
                  github_tools.offered(store, ag) and not github_tools.offered(store, {"github": "pat"})
                  and not github_tools.offered(store, {"github": ""}))
            out = await github_tools.create_check_run(store, ag, {
                "repo": "acme/app", "sha": "abc", "conclusion": "failure", "summary": "two problems",
                "annotations": [{"path": "a.py", "start_line": 3, "message": "off by one", "level": "failure"},
                                {"path": "", "start_line": 1, "message": "dropped: no path"}]})
            posted = STATE["checks"][-1]
            check("a check run is posted with the App's token: conclusion, summary, valid notes only",
                  "runs/9" in out and posted["conclusion"] == "failure" and posted["head_sha"] == "abc"
                  and len(posted["output"]["annotations"]) == 1
                  and posted["output"]["annotations"][0]["annotation_level"] == "failure", out)
            res = from_tool_call("github_create_check_run", {"conclusion": "failure"}, out)
            check("the run records it as a check result with its link",
                  res == {"kind": "check", "label": "check run failure",
                          "url": "https://github.com/acme/app/runs/9"}, str(res))
            try:
                await github_tools.create_check_run(store, ag, {"repo": "acme/app", "sha": "abc",
                                                                "conclusion": "maybe", "summary": "x"})
                check("a bad conclusion is a tool error the model can fix", False)
            except ValueError as e:
                check("a bad conclusion is a tool error the model can fix", "conclusion" in str(e))


async def main():
    await unit()
    await daemon()
    print(f"\n{PASS} passed, {FAIL} failed")
    raise SystemExit(1 if FAIL else 0)


asyncio.run(main())
