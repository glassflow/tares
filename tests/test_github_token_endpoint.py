"""GET /api/factory/github/token: a GitHub token for one repository, for the crew (TR-1422).

Run: .venv/bin/python tests/test_github_token_endpoint.py   (no network: GitHub and the token broker
are faked in-process)

Covers, against one in-process cell:
 - who may ask: a station key and a non-admin person's key may; a read key, a project key, no key and
   a key with no person may not; with auth off a plain request may, a browser's request may not;
 - a personal token: handed to an admin key and the cell's own token only; a non-admin key gets 403
   for a repository no App credential follows, and the cell calls nothing with the personal token;
 - which repository: one a visible project names on its hand-over or on a ticket (any case); not one
   seen only in a pull request link, not one of a private project the person is not on, and no call
   leaves the cell for any of those; a malformed one is 400;
 - which credential: github.com only; App credentials before personal tokens; one whose check fails
   does not hide a later one that follows the repository;
 - a broker credential serves a key that is not an admin only when its projects name every
   repository on that credential's list; admin keys and a self-held App do not depend on that list;
 - the answer for the three kinds, the permissions asked of the broker and of GitHub, the narrowing
   to one repository, the cache (apart from full-set tokens, refreshed under ten minutes), the
   errors (404, 403, 409, 502) and Cache-Control: no-store;
 - the identity to commit as: looked up once, kept when GitHub stops answering, looked up again when
   the personal token or the App changes, null when unknown;
 - nothing the cell prints, logs or answers holds a token, except the `token` field of this answer.
"""
import asyncio
import contextlib
import io
import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
sys.path.insert(0, ROOT)
from _env import scrub_os_environ
scrub_os_environ()

_TMP = tempfile.mkdtemp(prefix="tares-github-token-")
os.environ["TARES_HOME"] = os.path.join(_TMP, "on")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = ROOT_KEY = "root-secret"
os.environ["TARES_PUBLIC_URL"] = "https://cell.example.com"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

PASS = FAIL = 0


def check(label, cond, detail=""):
    """Printed past the capture below, so a failure's detail is not read back as a leak."""
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok   {label}", file=sys.__stdout__)
    else:
        FAIL += 1; print(f"  FAIL {label}  {detail}", file=sys.__stdout__)


def section(title):
    print(f"== {title} ==", file=sys.__stdout__)


PEM = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption()).decode()

ROUTE = "/api/factory/github/token"
BROKER_URL = "https://broker.test/api/github/token"
BROKER_SECRET = "broker-secret"
PERMS = {"contents": "write", "pull_requests": "write", "checks": "write", "metadata": "read"}
FIELDS = {"token", "expires_at", "kind", "login", "name", "email"}
# every fake token starts with one of these, which is what the leak check looks for
MARKS = ("ghs_", "ghu_", "github_pat")
PAT_ONE, PAT_TWO, PAT_USER = "github_pat_ONE111", "github_pat_TWO222", "ghu_THREE333"
PAT_ENT, PAT_STRAY = "github_pat_ENT444", "github_pat_STRAY555"
PAT_GAPS = "github_pat_GAPS666"     # its user answer holds a login and nothing else

INSTALLS = {101: {"login": "acme", "repos": ["acme/app", "acme/lib"]},
            202: {"login": "other", "repos": ["other/site"]}}
USERS = {PAT_ONE: {"login": "ada", "id": 11, "name": "Ada L", "email": "ada@example.com"},
         PAT_TWO: {"login": "bea", "id": 77, "name": None, "email": None},
         PAT_USER: {"login": "cyd", "id": 33, "name": "Cyd", "email": "cyd@example.com"},
         PAT_GAPS: {"login": "gil"}}
PUSH = {PAT_ONE: {"acme/app": True, "acme/lib": False, "acme/patonly": True},
        PAT_TWO: {"acme/app": True, "acme/patonly": True},
        PAT_USER: {"acme/app": True}, PAT_GAPS: {"acme/app": True}}
STATE: dict = {}
CALLS: list = []          # (method, host, path) of every request to the fake GitHub or broker
MINTS: list = []          # (who, body) of every token mint; body None when none was sent
MINTED_FOR: dict = {}     # installation token -> installation id
SEEN: list = []           # every response of this run, minus the one field that may hold a token
HANDED: list = []         # the tokens the route answered with
CAPTURED = io.StringIO()  # what the cell printed and logged


def reset_fake():
    STATE.update(exp_in=3600, broker="ok", app="ok", users="ok", user="ok", repo_status=None,
                 repo_raise=False, list_fail=set(), n=STATE.get("n", 0))


reset_fake()


def _expiry():
    return (datetime.now(timezone.utc) + timedelta(seconds=STATE["exp_in"])) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")


def _next(prefix):
    STATE["n"] += 1
    return f"{prefix}_{STATE['n']}"


def fake_broker(request: httpx.Request, bearer: str) -> httpx.Response:
    if bearer != BROKER_SECRET:
        return httpx.Response(401)
    body = json.loads(request.content)
    MINTS.append(("broker", body))
    mode = STATE["broker"]
    if mode == "missing":
        return httpx.Response(409, json={"detail": "app_permissions_missing"})
    if mode == "conflict":
        return httpx.Response(409, json={"detail": "something else", "token": "ghs_in_error_a"})
    if mode == "500":
        return httpx.Response(500, json={"token": "ghs_in_error_b", "message": "ghs_in_error_c"})
    if mode == "down":
        raise httpx.ConnectError("connection refused")
    if mode == "protocol":     # what the HTTP library raises for a header it cannot send
        raise httpx.LocalProtocolError(f"Illegal header value b'Bearer {PAT_STRAY}'")
    if mode == "no-token":
        return httpx.Response(200, json={"expires_at": _expiry(), "note": "ghs_in_error_d"})
    if mode == "text":
        return httpx.Response(200, text="ghs_in_error_e")
    answer = {"token": _next("ghs_broker"), "expires_at": _expiry()}
    MINTED_FOR[answer["token"]] = body["installation_id"]
    if mode == "ok":
        answer.update(app_slug="tares-cloud", bot_id=5550)
    elif mode == "nulls":
        answer.update(app_slug=None, bot_id=None)
    elif mode == "slug-only":
        answer.update(app_slug="tares-cloud", bot_id=None)
    return httpx.Response(200, json=answer)       # "absent": neither key


def fake_github(request: httpx.Request) -> httpx.Response:
    host, path = request.url.host, request.url.path
    auth = request.headers.get("authorization", "")
    bearer = auth.split(" ", 1)[1] if auth.startswith("Bearer ") else ""
    if host not in ("api.github.com", "broker.test"):
        return httpx.Response(599, text="unexpected host")
    CALLS.append((request.method, host, path))
    if host == "broker.test":
        return fake_broker(request, bearer)
    if path.startswith("/app/installations/"):
        iid = int(path.split("/")[3])
        if iid not in INSTALLS:
            return httpx.Response(404, json={"message": "Not Found"})
        if path.endswith("/access_tokens"):
            body = json.loads(request.content) if request.content else None
            MINTS.append((iid, body))
            if body and STATE["app"] == "422":
                return httpx.Response(422, json={"message": "The permissions requested are not "
                                                            "granted to this installation.",
                                                 "token": "ghs_in_error_f"})
            if body and STATE["app"] == "500":
                return httpx.Response(500, json={"token": "ghs_in_error_g"})
            token = _next(f"ghs_app_{iid}")
            MINTED_FOR[token] = iid
            return httpx.Response(201, json={"token": token, "expires_at": _expiry()})
        return httpx.Response(200, json={"id": iid, "repository_selection": "selected", "account": {
            "login": INSTALLS[iid]["login"], "type": "Organization"}})
    if path == "/installation/repositories":
        iid = MINTED_FOR[bearer]
        if iid in STATE["list_fail"]:
            return httpx.Response(500, json={"message": "Server Error"})
        return httpx.Response(200, json={"repositories": [
            {"full_name": r, "default_branch": "main", "private": True}
            for r in INSTALLS[iid]["repos"]]})
    if path.startswith("/users/"):
        if STATE["users"] == "raise":
            raise RuntimeError(f"boom while sending {bearer}")
        if STATE["users"] == "fail":
            return httpx.Response(503, json={"message": f"try later {bearer}"})
        return httpx.Response(200, json={"id": 9001, "login": path.split("/")[2]})
    if bearer not in USERS:
        return httpx.Response(401, json={"message": "Bad credentials"})
    if path == "/user":
        if STATE["user"] == "raise":
            raise RuntimeError(f"boom while sending {bearer}")
        if STATE["user"] == "fail":
            return httpx.Response(503, json={"message": f"try later {bearer}"})
        return httpx.Response(200, json=USERS[bearer])
    if path.startswith("/repos/"):
        if STATE["repo_raise"]:     # what the HTTP library raises for a header it cannot send
            raise httpx.LocalProtocolError(f"Illegal header value b'token {bearer}'")
        if STATE["repo_status"]:
            return httpx.Response(STATE["repo_status"], json={"message": f"no {bearer}"})
        repo = path[len("/repos/"):].lower()
        if repo not in PUSH[bearer]:
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(200, json={"full_name": repo, "permissions": {
            "pull": True, "push": PUSH[bearer][repo]}})
    return httpx.Response(404, json={"message": "Not Found"})


_RealClient = httpx.AsyncClient


class PatchedClient(_RealClient):
    def __init__(self, *a, **kw):
        if "transport" not in kw:
            kw["transport"] = httpx.MockTransport(fake_github)
        super().__init__(*a, **kw)


httpx.AsyncClient = PatchedClient

from tares import daemon, github_app, github_credentials
from tares.factory_api import NO_PERSON


class _Capture(logging.Handler):
    def emit(self, record):
        CAPTURED.write(self.format(record) + "\n")


def H(key, **more):
    return {"Authorization": f"Bearer {key}", **more}


def client(app, base_url="http://t"):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=base_url,
                             timeout=30)


def keep(r):
    """Remember a response for the leak check, without the route's own `token` field."""
    SEEN.append(str(dict(r.headers)))
    if r.request.url.path == ROUTE and r.status_code == 200:
        body = r.json()
        HANDED.append(body.pop("token", None))
        SEEN.append(json.dumps(body))
    else:
        SEEN.append(r.text)
    return r


async def ask(cx, repo, key=None, **headers):
    CALLS.clear(); MINTS.clear()
    hdrs = {**(H(key) if key else {}), **{k.replace("_", "-"): v for k, v in headers.items()}}
    return keep(await cx.get(ROUTE, params={} if repo is None else {"repo": repo}, headers=hdrs))


def code(r):
    try:
        return r.json().get("code")
    except ValueError:
        return None


def drop_credentials(store):
    for c in store.list_github_credentials():
        store.delete_github_credential(c["name"])
        github_credentials.forget_repos(c["name"])


async def add_broker(cx, repositories, name="cloud"):
    r = keep(await cx.post("/api/integrations/github", headers=H(ROOT_KEY), json={
        "name": name, "kind": "app_broker", "token_url": BROKER_URL,
        "broker_secret": BROKER_SECRET, "installation_id": 101, "account": "acme",
        "webhook_secret": "whsec", "repositories": repositories}))
    assert r.status_code == 201, r.text


async def relist(cx, repositories, name="cloud"):
    r = keep(await cx.put(f"/api/integrations/github/{name}", headers=H(ROOT_KEY),
                          json={"name": name, "repositories": repositories}))
    assert r.status_code < 300, r.text


async def add_app(cx, store, slug="tares-cell", name="own"):
    r = keep(await cx.post("/api/integrations/github", headers=H(ROOT_KEY), json={
        "name": name, "kind": "app", "app_id": "4242", "private_key": PEM,
        "webhook_secret": "whsec", "installation_ids": [101, 202]}))
    assert r.status_code == 201, r.text
    if slug:       # the "Create GitHub App" flow stores the slug; one entered by hand has none
        set_slug(store, name, slug)


def set_slug(store, name, slug):
    store.update_github_credential_config(
        name, {**store.get_github_credential(name)["config"], "slug": slug})


async def add_pat(cx, token, name="pat"):
    r = keep(await cx.post("/api/integrations/github", headers=H(ROOT_KEY),
                           json={"name": name, "token": token}))
    assert r.status_code == 201, r.text


def projects(store):
    store.create_project("uc_team", "custom", "Team", {})
    store.set_handover("uc_team", [{"path": "/src/app", "name": "acme/app"}], None, "test",
                       datetime.now(timezone.utc))
    for repo in ("Acme/Lib", "acme/unfollowed", "acme/patonly", "other/site"):
        store.create_ticket("uc_team", "builder", f"work in {repo}", repo=repo)
    tid = store.create_ticket("uc_team", "builder", "a change elsewhere")
    store.update_ticket(tid, pr={"url": "https://github.com/acme/prlink/pull/1",
                                 "repo": "acme/prlink", "number": 1})


# ── scenarios ──────────────────────────────────────────────────────────────────

async def who_and_which(cx, store, k):
    section("no credential")
    r = await ask(cx, "acme/app", k["ann"])
    check("no credential stored: 404 no_credential",
          r.status_code == 404 and code(r) == "no_credential", f"{r.status_code} {r.text}")
    store.upsert_github_credential("ent", PAT_ENT, "token",
                                   "https://github.example.com/api/v3", "ent")
    r = await ask(cx, "acme/app", k["ann"])
    check("only a credential for another GitHub host: 404, no call",
          r.status_code == 404 and code(r) == "no_credential" and not CALLS, f"{r.text} {CALLS}")
    drop_credentials(store)

    named = ["acme/app", "acme/lib", "acme/secret"]
    await add_broker(cx, named)

    section("who may ask")
    r = await ask(cx, "acme/app", k["ann"], X_Tares_Station="crew-build-x",
                  X_Tares_Role="builder")
    check("a station (person key plus station headers) gets a token",
          r.status_code == 200 and r.json()["token"].startswith("ghs_broker_"), r.text)
    r = await ask(cx, "acme/app", k["ann"])
    check("a non-admin person's key gets a token", r.status_code == 200, r.text)
    r = await ask(cx, "acme/app", ROOT_KEY)
    check("the system credential gets a token", r.status_code == 200, r.text)
    r = await ask(cx, "acme/app", k["ann"], Origin="https://cell.example.com")
    check("with auth on, an Origin header changes nothing", r.status_code == 200, r.text)
    r = await ask(cx, "acme/app")
    check("no key: 401, no call", r.status_code == 401 and not CALLS, r.text)
    r = await ask(cx, "acme/app", k["bob_read"])
    check("a read key: 403, no call",
          r.status_code == 403 and "ingest" in r.text and not CALLS, r.text)
    r = await ask(cx, "acme/app", k["project"])
    check("a project key: 403, no call",
          r.status_code == 403 and "project" in r.text and not CALLS, r.text)
    r = await ask(cx, "acme/app", k["nobody"])
    check("a key with no person: 403 with the usual words, no call",
          r.status_code == 403 and r.json() == {"code": "repo_not_allowed", "detail": NO_PERSON}
          and not CALLS, r.text)
    r = await ask(cx, "acme/app", k["nobody"], X_Tares_Station="crew-build-x")
    check("station headers give a key with no person nothing", r.status_code == 403, r.text)

    section("which repository")
    r = await ask(cx, "ACME/App", k["ann"])
    check("a hand-over repository, in any case", r.status_code == 200, r.text)
    r = await ask(cx, "acme/lib", k["ann"])
    check("a ticket's repository, in any case", r.status_code == 200, r.text)
    r = await ask(cx, "https://github.com/acme/app.git", k["ann"])
    check("a github.com address of the repository is read as owner/name",
          r.status_code == 200, r.text)
    await relist(cx, ["acme/app", "acme/prlink", "acme/nowhere"])
    for repo, what in (("acme/prlink", "a repository seen only in a pull request link"),
                       ("acme/nowhere", "a followed repository no project names")):
        r = await ask(cx, repo, k["adm"])
        check(f"{what}, even for an admin key: 403, no call",
              r.status_code == 403 and code(r) == "repo_not_allowed" and not CALLS,
              f"{r.text} {CALLS}")
    await relist(cx, named)
    r = await ask(cx, "acme/secret", k["bob"])
    check("a private project the person is not on: 403, no call",
          r.status_code == 403 and code(r) == "repo_not_allowed" and not CALLS, f"{r.text} {CALLS}")
    r = await ask(cx, "acme/secret", k["ann"])
    check("a private project the person is on: a token", r.status_code == 200, r.text)
    r = await ask(cx, "acme/unfollowed", k["ann"])
    check("a repository the credential does not follow: 403, no token minted",
          r.status_code == 403 and code(r) == "repo_not_allowed" and not MINTS, f"{r.text} {MINTS}")
    for bad in (None, "", "  ", "acme", "acme/app/extra", "../app", "acme/..", "./app",
                "acme/a b", "acme/app?x=1", "https://example.com/acme/app"):
        r = await ask(cx, bad, k["ann"])
        check(f"malformed repo {bad!r}: 400, no call",
              r.status_code == 400 and r.json().get("detail") and not CALLS,
              f"{r.status_code} {r.text}")


async def broker(cx, store, k):
    section("broker credential")
    reset_fake()
    github_app.forget("cloud")
    r = await ask(cx, "acme/app", k["ann"])
    first = r.json()
    check("the answer holds exactly the six fields", set(first) == FIELDS, r.text)
    check("kind app_broker, committing as the App's bot",
          first["kind"] == "app_broker" and first["login"] == "tares-cloud[bot]"
          and first["name"] == "tares-cloud[bot]"
          and first["email"] == "5550+tares-cloud[bot]@users.noreply.github.com", r.text)
    expires = datetime.strptime(first["expires_at"], "%Y-%m-%dT%H:%M:%SZ") \
        .replace(tzinfo=timezone.utc)
    check("expires_at is the broker's, in UTC",
          3500 < (expires - datetime.now(timezone.utc)).total_seconds() <= 3600, first["expires_at"])
    check("Cache-Control: no-store", r.headers.get("cache-control") == "no-store", str(r.headers))
    check("a grant printed or logged as an object leaves its token out",
          "ghs_" not in repr(github_app.Grant("ghs_repr_1", 0.0, "tares-cloud", 5550)))
    check("the broker is sent the installation and exactly the crew's permissions",
          MINTS == [("broker", {"installation_id": 101, "permissions": PERMS})], str(MINTS))
    r = await ask(cx, "acme/lib", k["ann"])
    check("a cached token is served with the same identity, no new call",
          r.json() == first and not CALLS, f"{r.text} {CALLS}")

    cred = store.get_github_credential("cloud")
    MINTS.clear()
    full = await github_app.installation_token(cred, 101)
    check("a full-permission token is its own mint, sent no permissions",
          full != first["token"] and MINTS == [("broker", {"installation_id": 101})], str(MINTS))
    r = await ask(cx, "acme/app", k["ann"])
    MINTS.clear()
    check("... and neither replaces the other",
          r.json()["token"] == first["token"] and not CALLS
          and await github_app.installation_token(cred, 101) == full and not MINTS, r.text)
    github_app.forget("cloud")
    r = await ask(cx, "acme/app", k["ann"])
    check("forgetting the credential drops the narrowed token",
          r.json()["token"] != first["token"] and len(MINTS) == 1, r.text)
    MINTS.clear()
    check("... and the full one",
          await github_app.installation_token(cred, 101) != full and len(MINTS) == 1, str(MINTS))

    github_app.forget("cloud")
    STATE["exp_in"] = 590
    a = (await ask(cx, "acme/app", k["ann"])).json()["token"]
    b = (await ask(cx, "acme/app", k["ann"])).json()["token"]
    check("a cached token with under 600 s left is replaced", a != b and len(MINTS) == 1, str(MINTS))
    github_app.forget("cloud")
    STATE["exp_in"] = 700
    a = (await ask(cx, "acme/app", k["ann"])).json()["token"]
    b = (await ask(cx, "acme/app", k["ann"])).json()["token"]
    check("a cached token with more than 600 s left is served again", a == b and not CALLS)

    section("broker identity gaps")
    for mode, login, label in (("nulls", None, "null app_slug and bot_id"),
                               ("absent", None, "no app_slug or bot_id"),
                               ("slug-only", "tares-cloud[bot]", "a slug without a bot_id")):
        reset_fake(); STATE["broker"] = mode
        github_app.forget("cloud")
        r = await ask(cx, "acme/app", k["ann"])
        body = r.json()
        check(f"{label}: a token, the unknown fields null",
              r.status_code == 200 and body["token"] and body["login"] == login
              and body["name"] == login and body["email"] is None, r.text)

    section("broker errors")
    reset_fake(); STATE["broker"] = "missing"
    github_app.forget("cloud")
    r = await ask(cx, "acme/app", k["ann"])
    check("broker 409 app_permissions_missing: 409 with a code and words",
          r.status_code == 409 and code(r) == "app_permissions_missing"
          and "permission" in r.json().get("detail", ""), r.text)
    for mode, label, words in (("conflict", "another 409", "409"), ("500", "a 500", "500"),
                               ("down", "unreachable", "could not reach"),
                               ("protocol", "a request that cannot be sent", "LocalProtocolError"),
                               ("no-token", "no token in the answer", "no installation token"),
                               ("text", "an answer that is not JSON", "no installation token")):
        reset_fake(); STATE["broker"] = mode
        github_app.forget("cloud")
        r = await ask(cx, "acme/app", k["ann"])
        check(f"broker {label}: 502 naming the cause",
              r.status_code == 502 and words in r.json().get("detail", ""),
              f"{r.status_code} {r.text}")

    section("empty repository list")
    reset_fake()
    keep(await cx.put("/api/integrations/github/cloud", headers=H(ROOT_KEY),
                      json={"name": "cloud", "repositories": []}))
    r = await ask(cx, "acme/app", k["ann"])
    check("a broker credential that follows nothing: 403, no call",
          r.status_code == 403 and code(r) == "repo_not_allowed" and not CALLS, f"{r.text} {CALLS}")

    section("broker without an installation")
    reset_fake()
    keep(await cx.put("/api/integrations/github/cloud", headers=H(ROOT_KEY),
                      json={"name": "cloud", "repositories": ["acme/app"]}))
    config = store.get_github_credential("cloud")["config"]
    store.update_github_credential_config("cloud", {**config, "installation_id": None})
    r = await ask(cx, "acme/app", k["ann"])
    check("a followed repository and no installation: 502 saying so, no call",
          r.status_code == 502 and "no installation" in r.json().get("detail", "") and not CALLS,
          f"{r.status_code} {r.text} {CALLS}")
    drop_credentials(store)


async def broker_list(cx, store, k):
    section("a broker credential's repository list and a key that is not an admin")
    reset_fake()
    await add_broker(cx, ["acme/app", "ACME/Secret"])
    github_app.forget("cloud")

    def served(r, kind="app_broker"):
        return r.status_code == 200 and r.json()["kind"] == kind and bool(r.json()["token"])

    def refused(r):
        return (r.status_code == 403 and code(r) == "repo_not_allowed" and "token" not in r.json()
                and "secret" not in r.text.lower() and not CALLS)

    r = await ask(cx, "acme/app", k["ann"])
    check("projects name every listed repository, in any case: a token", served(r), r.text)
    for label, more in (("a non-admin person's key", {}),
                        ("a station on that key", {"X_Tares_Station": "crew-build-x"})):
        r = await ask(cx, "acme/app", k["bob"], **more)
        check(f"projects name only some listed repositories, {label}: 403, no token, no call",
              refused(r), f"{r.status_code} {r.text} {CALLS}")
    for key, label in ((k["adm"], "an admin key"), (ROOT_KEY, "the cell's own token")):
        r = await ask(cx, "acme/app", key)
        check(f"{label} is served from the same list", served(r), r.text)

    await relist(cx, ["acme/app", "acme/lib"])
    r = await ask(cx, "acme/app", k["bob"])
    check("the same key once its projects name the whole list (hand-over and ticket): a token",
          served(r), r.text)

    for extra, label in (("acme/nowhere", "no project names"),
                         ("acme/prlink", "only a pull request link names")):
        await relist(cx, ["acme/app", extra])
        r = await ask(cx, "acme/app", k["ann"])
        check(f"a listed repository {label}: 403 for a non-admin key", refused(r),
              f"{r.status_code} {r.text} {CALLS}")
        r = await ask(cx, "acme/app", k["adm"])
        check(f"a listed repository {label}: an admin key is served", served(r), r.text)

    await relist(cx, ["acme/app", "acme/secret"])
    await add_pat(cx, PAT_ONE)
    r = await ask(cx, "acme/app", k["bob"])
    check("beside a personal token: still 403, the personal token never tried",
          refused(r) and "broker token" in r.json()["detail"], f"{r.status_code} {r.text} {CALLS}")
    await add_app(cx, store)
    r = await ask(cx, "acme/app", k["bob"])
    check("beside a self-held App: the App serves the non-admin key, the broker is not asked",
          served(r, "app") and not any(host == "broker.test" for _, host, _ in CALLS),
          f"{r.text} {CALLS}")
    r = await ask(cx, "acme/app", k["adm"])
    check("... and the admin key still gets the first stored App credential", served(r), r.text)
    drop_credentials(store)


async def own_app(cx, store, k):
    section("self-held App")
    reset_fake()
    await add_app(cx, store)
    r = await ask(cx, "ACME/LIB", k["ann"])
    body = r.json()
    check("kind app, committing as the App's bot",
          r.status_code == 200 and set(body) == FIELDS and body["kind"] == "app"
          and body["token"].startswith("ghs_app_101_") and body["login"] == "tares-cell[bot]"
          and body["name"] == "tares-cell[bot]"
          and body["email"] == "9001+tares-cell[bot]@users.noreply.github.com"
          and body["expires_at"], r.text)
    check("no-store", r.headers.get("cache-control") == "no-store")
    narrowed = [b for _, b in MINTS if b]
    check("the mint asks for the crew's permissions on that one repository, as GitHub names it",
          narrowed == [{"permissions": PERMS, "repositories": ["lib"]}], str(MINTS))
    check("the token that lists repositories is minted with no body, as before",
          (101, None) in MINTS, str(MINTS))
    check("the bot is looked up",
          ("GET", "api.github.com", "/users/tares-cell[bot]") in CALLS, str(CALLS))
    r2 = await ask(cx, "acme/lib", k["ann"])
    check("a second request calls nothing", r2.json() == body and not CALLS, f"{r2.text} {CALLS}")
    r3 = await ask(cx, "acme/app", k["ann"])
    check("another repository gets its own narrowed token",
          r3.json()["token"] != body["token"]
          and MINTS == [(101, {"permissions": PERMS, "repositories": ["app"]})], str(MINTS))
    full = await github_app.installation_token(store.get_github_credential("own"), 101)
    check("the full token is none of the narrowed ones",
          full not in (body["token"], r3.json()["token"]))

    STATE["users"] = "fail"
    github_app.forget("own")
    r = await ask(cx, "acme/lib", k["ann"])
    check("the cached identity survives a GitHub that no longer answers the lookup",
          r.status_code == 200 and r.json()["email"] == body["email"]
          and r.json()["login"] == body["login"], r.text)
    listed = keep(await cx.get("/api/integrations/github", headers=H(ROOT_KEY))).text
    check("the credential list does not show the kept identity",
          "commit_identity" not in listed and "9001" not in listed, listed)

    STATE["exp_in"] = 590
    github_app.forget("own")
    a = (await ask(cx, "acme/lib", k["ann"])).json()["token"]
    b = (await ask(cx, "acme/lib", k["ann"])).json()["token"]
    check("a narrowed App token with under 600 s left is replaced", a != b, str(MINTS))

    reset_fake(); STATE["users"] = "raise"
    set_slug(store, "own", "tares-new")
    r = await ask(cx, "acme/lib", k["ann"])
    check("a changed App slug drops the old identity; a failed lookup gives nulls and a token",
          r.status_code == 200 and r.json()["token"] and r.json()["login"] is None
          and r.json()["name"] is None and r.json()["email"] is None, r.text)
    reset_fake()
    r = await ask(cx, "acme/lib", k["ann"])
    check("the next lookup fills it in", r.json()["login"] == "tares-new[bot]"
          and r.json()["email"] == "9001+tares-new[bot]@users.noreply.github.com", r.text)
    set_slug(store, "own", "")
    r = await ask(cx, "acme/lib", k["ann"])
    check("an App with no slug: a token, null identity, no lookup",
          r.status_code == 200 and r.json()["login"] is None and r.json()["email"] is None
          and not any(p.startswith("/users/") for _, _, p in CALLS), f"{r.text} {CALLS}")

    section("self-held App without an installation")
    reset_fake()
    config = store.get_github_credential("own")["config"]
    store.update_github_credential_config("own", {**config, "installations": []})
    github_app.forget("own")
    r = await ask(cx, "acme/app", k["ann"])
    check("an App with no installation: 403, no call",
          r.status_code == 403 and code(r) == "repo_not_allowed" and not CALLS, f"{r.text} {CALLS}")
    store.update_github_credential_config("own", config)

    section("self-held App errors")
    reset_fake(); STATE["app"] = "422"
    github_app.forget("own")
    r = await ask(cx, "acme/app", k["ann"])
    check("GitHub 422 on the mint: 409 app_permissions_missing",
          r.status_code == 409 and code(r) == "app_permissions_missing"
          and r.json().get("detail"), r.text)
    STATE["app"] = "500"
    r = await ask(cx, "acme/app", k["ann"])
    check("GitHub 500 on the mint: 502", r.status_code == 502 and "500" in r.text,
          f"{r.status_code} {r.text}")
    reset_fake()
    r = await ask(cx, "acme/unfollowed", k["ann"])
    check("a repository in no installation: 403",
          r.status_code == 403 and code(r) == "repo_not_allowed", r.text)
    STATE["list_fail"] = {101}
    github_app.forget("own")
    r = await ask(cx, "other/site", k["ann"])
    check("one installation that cannot be listed does not hide another",
          r.status_code == 200 and r.json()["token"].startswith("ghs_app_202_"), r.text)
    github_app.forget("own")
    r = await ask(cx, "acme/app", k["ann"])
    check("none follows and a listing failed: 502, not 403",
          r.status_code == 502 and "own" in r.json().get("detail", ""),
          f"{r.status_code} {r.text}")

    section("a failing App and a personal token that follows")
    await add_pat(cx, PAT_ONE)
    github_app.forget("own")
    r = await ask(cx, "acme/app", k["adm"])
    check("the later credential serves", r.status_code == 200 and r.json()["kind"] == "pat", r.text)
    github_app.forget("own")
    CALLS.clear()
    r = await ask(cx, "acme/app", k["ann"])
    check("... but not a non-admin key: 502 from the App, the personal token never tried",
          r.status_code == 502 and "token" not in r.json()
          and "/repos/acme/app" not in [p for _, _, p in CALLS], f"{r.status_code} {r.text}")
    drop_credentials(store)


async def personal(cx, store, k):
    section("personal token")
    reset_fake()
    await add_pat(cx, PAT_ONE)
    r = await ask(cx, "acme/app", k["adm"], X_Tares_Station="crew-build-x")
    check("kind pat: the stored token, no expiry, the user's name and email",
          r.status_code == 200 and r.json() == {
              "token": PAT_ONE, "expires_at": None, "kind": "pat", "login": "ada",
              "name": "Ada L", "email": "ada@example.com"}, r.text)
    check("no-store", r.headers.get("cache-control") == "no-store")
    check("GitHub is asked whether the token can push, then who it is",
          [p for _, _, p in CALLS] == ["/repos/acme/app", "/user"], str(CALLS))
    r = await ask(cx, "acme/app", ROOT_KEY)
    check("the cell's own token gets the personal token",
          r.status_code == 200 and r.json()["token"] == PAT_ONE, r.text)
    for label, more in (("a non-admin person's key", {}),
                        ("a station on a non-admin key", {"X_Tares_Station": "crew-build-x"})):
        r = await ask(cx, "acme/app", k["ann"], **more)
        check(f"only a personal token, {label}: 403, no token, no call",
              r.status_code == 403 and code(r) == "repo_not_allowed" and "token" not in r.json()
              and "admin keys only" in r.json()["detail"] and not CALLS,
              f"{r.status_code} {r.text} {CALLS}")
    STATE["user"] = "fail"
    r = await ask(cx, "acme/app", k["adm"])
    check("the identity is kept: no second lookup",
          r.json()["login"] == "ada" and [p for _, _, p in CALLS] == ["/repos/acme/app"],
          f"{r.text} {CALLS}")
    r = await ask(cx, "acme/lib", k["adm"])
    check("a repository the token cannot push to: 403",
          r.status_code == 403 and code(r) == "repo_not_allowed", r.text)
    r = await ask(cx, "other/site", k["adm"])
    check("a repository the token cannot see: 403",
          r.status_code == 403 and code(r) == "repo_not_allowed", r.text)
    for status in (401, 500):
        STATE["repo_status"] = status
        r = await ask(cx, "acme/app", k["adm"])
        check(f"GitHub {status} on the check: 502 naming it",
              r.status_code == 502 and str(status) in r.json().get("detail", ""),
              f"{r.status_code} {r.text}")
    reset_fake(); STATE["repo_raise"] = True
    r = await ask(cx, "acme/app", k["adm"])
    check("a check that cannot be sent: 502 naming the error's type, not its text",
          r.status_code == 502 and "LocalProtocolError" in r.json().get("detail", "")
          and "Illegal header" not in r.text, f"{r.status_code} {r.json().get('detail', '')[:60]}")

    section("personal token rotation")
    reset_fake()
    keep(await cx.put("/api/integrations/github/pat", headers=H(ROOT_KEY),
                      json={"name": "pat", "token": PAT_TWO}))
    r = await ask(cx, "acme/app", k["adm"])
    check("a rotated token is looked up again; no name or public email gives login and noreply",
          r.json() == {"token": PAT_TWO, "expires_at": None, "kind": "pat", "login": "bea",
                       "name": "bea", "email": "77+bea@users.noreply.github.com"}, r.text)
    keep(await cx.put("/api/integrations/github/pat", headers=H(ROOT_KEY),
                      json={"name": "pat", "token": PAT_USER}))
    STATE["user"] = "raise"
    r = await ask(cx, "acme/app", k["adm"])
    check("a failed lookup after a rotation never serves the old identity",
          r.status_code == 200 and r.json()["token"] == PAT_USER and r.json()["login"] is None
          and r.json()["name"] is None and r.json()["email"] is None, r.text)
    STATE["user"] = "fail"
    r = await ask(cx, "acme/app", k["adm"])
    check("... nor on a refusal", r.json()["login"] is None, r.text)
    reset_fake()
    r = await ask(cx, "acme/app", k["adm"])
    check("it is filled in once GitHub answers", r.json()["login"] == "cyd", r.text)
    listed = keep(await cx.get("/api/integrations/github", headers=H(ROOT_KEY))).text
    check("the credential list does not show it",
          "commit_identity" not in listed and "cyd@example.com" not in listed, listed)

    section("personal token identity gaps")
    keep(await cx.put("/api/integrations/github/pat", headers=H(ROOT_KEY),
                      json={"name": "pat", "token": PAT_GAPS}))
    r = await ask(cx, "acme/app", k["adm"])
    check("no email and no id: a token and the login, email null",
          r.status_code == 200 and r.json() == {
              "token": PAT_GAPS, "expires_at": None, "kind": "pat", "login": "gil",
              "name": "gil", "email": None}, r.text)
    r = await ask(cx, "acme/app", k["adm"])
    check("an identity with no email is not kept: looked up again",
          [p for _, _, p in CALLS] == ["/repos/acme/app", "/user"], str(CALLS))
    USERS[PAT_GAPS] = {"id": 5}
    r = await ask(cx, "acme/app", k["adm"])
    check("a user answer with an id and no login: a token, null identity",
          r.status_code == 200 and r.json()["token"] == PAT_GAPS and r.json()["login"] is None
          and r.json()["name"] is None and r.json()["email"] is None, r.text)
    keep(await cx.put("/api/integrations/github/pat", headers=H(ROOT_KEY),
                      json={"name": "pat", "token": PAT_USER}))

    section("which credential")
    await add_pat(cx, PAT_ONE, name="a-pat")
    r = await ask(cx, "acme/app", k["adm"])
    check("two personal tokens that follow: the first stored one",
          r.json()["token"] == PAT_ONE, r.text)
    await add_broker(cx, ["acme/app"], name="zz-cloud")
    r = await ask(cx, "acme/app", k["adm"])
    check("an App credential comes before personal tokens",
          r.json()["kind"] == "app_broker", r.text)
    r = await ask(cx, "acme/app", k["ann"])
    check("a non-admin key gets the App token beside stored personal tokens",
          r.status_code == 200 and r.json()["kind"] == "app_broker", r.text)
    CALLS.clear()
    r = await ask(cx, "acme/patonly", k["ann"])
    check("a repository only a personal token follows, non-admin key: 403, no call",
          r.status_code == 403 and code(r) == "repo_not_allowed" and "token" not in r.json()
          and not CALLS, f"{r.status_code} {r.text} {CALLS}")
    r = await ask(cx, "acme/patonly", k["adm"])
    check("a repository the App does not follow falls to a personal token",
          r.json()["kind"] == "pat" and r.json()["token"] == PAT_ONE, r.text)
    STATE["repo_status"] = 500
    r = await ask(cx, "acme/lib", k["adm"])
    check("none follows and a check failed: 502", r.status_code == 502,
          f"{r.status_code} {r.text}")
    reset_fake()


async def echo_routes(cx):
    """Other routes that describe credentials, settings or the cell, for the leak check."""
    for path in ("/health", "/api/integrations/github", "/api/settings", "/api/keys",
                 "/api/catalog/export", "/api/sources", "/api/projects", "/api/projects/uc_team",
                 "/api/factory/integrations", "/api/factory/integrations/effective", "/metrics",
                 "/api/integrations/github/pat/repos"):
        keep(await cx.get(path, headers=H(ROOT_KEY)))
    for name in ("pat", "a-pat", "zz-cloud"):
        keep(await cx.post(f"/api/integrations/github/{name}/test", headers=H(ROOT_KEY)))


async def auth_on():
    app = daemon.make_app()
    async with app.router.lifespan_context(app):
        store = app.state.store
        async with client(app) as cx:
            health = keep(await cx.get("/health")).json()
            check("/health capabilities include github_token",
                  "github_token" in health["capabilities"], str(health["capabilities"]))

            async def key(name, **body):
                r = await cx.post("/api/keys", headers=H(ROOT_KEY), json={"name": name, **body})
                assert r.status_code < 300, r.status_code
                return r.json()["secret"]

            # straight into the store: adding people over HTTP is limited by the edition's seats
            ann = store.create_person("Ann", "ann@example.com")["id"]
            bob = store.create_person("Bob", "bob@example.com")["id"]
            projects(store)
            store.create_project("uc_priv", "custom", "Private", {})
            store.create_ticket("uc_priv", "builder", "private work", repo="acme/secret")
            store.set_project_scope("uc_priv", "private")
            store.add_project_member("uc_priv", ann)
            k = {"ann": await key("ann", scopes=["ingest"], person_id=ann),
                 "adm": await key("ann-admin", scopes=["admin"], person_id=ann),
                 "bob": await key("bob", scopes=["ingest"], person_id=bob),
                 "bob_read": await key("bob-read", scopes=["read"], person_id=bob),
                 "nobody": await key("nobody", scopes=["ingest"]),
                 "project": await key("project", project="uc_team")}
            await who_and_which(cx, store, k)
            await broker(cx, store, k)
            await broker_list(cx, store, k)
            await own_app(cx, store, k)
            await personal(cx, store, k)
            await echo_routes(cx)


async def auth_off():
    section("auth off")
    reset_fake()
    daemon.AUTH_TOKEN = ""
    # a folder of its own: the session store is a file beside the database, so the first daemon's
    # projects would still be there
    daemon.DATA_DIR = os.path.join(tempfile.mkdtemp(prefix="off-", dir=_TMP), "off")
    app = daemon.make_app()
    async with app.router.lifespan_context(app):
        store = app.state.store
        async with client(app, "http://127.0.0.1:8787") as cx:
            projects(store)
            store.create_project("uc_priv", "custom", "Private", {})
            store.create_ticket("uc_priv", "builder", "private work", repo="acme/secret")
            store.set_project_scope("uc_priv", "private")
            drop_credentials(store)
            keep(await cx.post("/api/integrations/github", json={
                "name": "off-cloud", "kind": "app_broker", "token_url": BROKER_URL,
                "broker_secret": BROKER_SECRET, "installation_id": 101, "webhook_secret": "whsec",
                "repositories": ["acme/app", "acme/secret"]}))
            r = await ask(cx, "acme/app")
            check("a plain request gets a token",
                  r.status_code == 200 and r.json()["token"]
                  and r.headers.get("cache-control") == "no-store", r.text)
            r = await ask(cx, "acme/secret")
            check("with auth off every project counts", r.status_code == 200, r.text)
            for headers, label in (({"Origin": "https://example.com"}, "an Origin header"),
                                   ({"Origin": "http://t"}, "the cell's own Origin"),
                                   ({"Origin": "null"}, "Origin: null"),
                                   ({"Sec_Fetch_Site": "same-origin"}, "Sec-Fetch-Site: same-origin"),
                                   ({"Sec_Fetch_Site": "none"}, "Sec-Fetch-Site: none"),
                                   ({"Sec_Fetch_Site": "cross-site"}, "Sec-Fetch-Site: cross-site")):
                r = await ask(cx, "acme/app", **headers)
                check(f"a browser request ({label}): 403, no token, no call",
                      r.status_code == 403 and code(r) == "repo_not_allowed" and "token" not in r.json()
                      and not CALLS, f"{r.status_code} {r.text}")
            for host in ("localhost:8787", "LocalHost", "[::1]:8787", "192.168.1.20:8787"):
                r = await ask(cx, "acme/app", Host=host)
                check(f"called as {host}: a token", r.status_code == 200 and r.json()["token"],
                      f"{r.status_code} {r.text}")
            for host in ("cell.example.com", "t:8787", "localhost.example.com",
                         "127.0.0.1.example.com", "cell.example.com@127.0.0.1", "[::1",
                         "[cell.example.com]", "localhost:cell", ""):
                r = await ask(cx, "acme/app", Host=host)
                check(f"called as {host or 'an empty Host'}: 403, no token, no call",
                      r.status_code == 403 and code(r) == "repo_not_allowed" and "token" not in r.json()
                      and not CALLS, f"{r.status_code} {r.text}")


async def main():
    handler = _Capture()
    handler.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    logging.captureWarnings(True)
    with contextlib.redirect_stdout(CAPTURED), contextlib.redirect_stderr(CAPTURED):
        await auth_on()
        await auth_off()

    section("no token outside the answer's token field")
    printed, answered = CAPTURED.getvalue(), "\n".join(SEEN)
    check("the run handed out tokens of every fake family",
          all(any(str(t).startswith(m) for t in HANDED) for m in MARKS), str(len(HANDED)))
    check("the capture saw the cell's own lines about GitHub",
          "could not look up who to commit as" in printed and "token refreshed" in printed,
          printed[-2000:])
    for mark in MARKS:
        where = [line for line in printed.splitlines() if mark in line]
        check(f"nothing printed or logged holds {mark}", not where, str(where[:5]))
        at = answered.find(mark)
        check(f"no response holds {mark}", at < 0, answered[max(0, at - 200):at + 80])
    check("neither holds the broker secret",
          BROKER_SECRET not in printed and BROKER_SECRET not in answered)


asyncio.run(main())
print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
