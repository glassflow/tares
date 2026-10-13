"""Linear (TR-408): one connection per cell, and the sync that keeps a project's tickets current.

When a project uses Linear, Linear owns its tickets. Sessions create and change them in Linear
with Linear's own tools; Tares keeps a record of each (tickets table, owner 'linear') and notices
changes by polling, since a local cell has no public address for Linear's webhooks. Each change
is also an event on the `linear` source, so it shows on the timeline and triggers can react.

The connection is either a personal API key or OAuth (authorization code with PKCE). Linear's
OAuth needs an OAuth application registered in Linear (its client id, from TARES_LINEAR_CLIENT_ID
or saved in Settings); with PKCE the client secret is optional. Access tokens last 24 hours and
are refreshed with the refresh token before they expire.

Everything here is read-only towards Linear.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx

from .envelope import Envelope, now_utc

API_URL = "https://api.linear.app/graphql"
TOKEN_URL = "https://api.linear.app/oauth/token"
REVOKE_URL = "https://api.linear.app/oauth/revoke"
AUTHORIZE_URL = "https://linear.app/oauth/authorize"
SCOPES = "read"

CONNECTION_SETTING = "linear_connection"
APP_SETTING = "linear_oauth_app"
SOURCE_NAME = "linear"
REFRESH_MARGIN = 300          # refresh an access token this many seconds before it expires
MAX_ISSUES = 1000             # per project per sync; a factory project is far smaller

# tests swap in an httpx.MockTransport with recorded responses
_transport: httpx.AsyncBaseTransport | None = None


class LinearError(RuntimeError):
    """Linear refused or failed; the message is meant for a person."""


def _client(timeout: float = 20) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, transport=_transport)


# ── the connection, kept in settings ─────────────────────────────────────────

def get_connection(store) -> dict | None:
    raw = store.get_setting(CONNECTION_SETTING)
    return json.loads(raw) if raw else None


def _save_connection(store, conn: dict | None) -> None:
    store.set_setting(CONNECTION_SETTING, json.dumps(conn) if conn else None)


def public_connection(store) -> dict:
    """What the console may see: never a token."""
    conn = get_connection(store)
    app = oauth_app(store)
    out = {"connected": bool(conn), "oauth_available": bool(app.get("client_id")),
           "client_id": app.get("client_id") or "",
           "client_id_from_env": bool(os.getenv("TARES_LINEAR_CLIENT_ID", "").strip())}
    if conn:
        out.update({"kind": conn.get("kind"), "account": conn.get("account") or {},
                    "connected_at": conn.get("connected_at"), "error": conn.get("error")})
    return out


def oauth_app(store) -> dict:
    """{client_id, client_secret}: the env wins over what Settings saved."""
    env_id = os.getenv("TARES_LINEAR_CLIENT_ID", "").strip()
    if env_id:
        return {"client_id": env_id,
                "client_secret": os.getenv("TARES_LINEAR_CLIENT_SECRET", "").strip()}
    raw = store.get_setting(APP_SETTING)
    return json.loads(raw) if raw else {}


def save_oauth_app(store, client_id: str, client_secret: str | None) -> None:
    cur = oauth_app(store) if not os.getenv("TARES_LINEAR_CLIENT_ID") else {}
    secret = cur.get("client_secret", "") if client_secret is None else client_secret
    store.set_setting(APP_SETTING, json.dumps({"client_id": client_id.strip(),
                                               "client_secret": (secret or "").strip()})
                      if client_id.strip() else None)


def disconnect(store) -> dict | None:
    conn = get_connection(store)
    _save_connection(store, None)
    return conn


async def revoke(conn: dict) -> None:
    """Best effort: an OAuth token is revoked at Linear; an API key is the user's to delete."""
    if conn.get("kind") != "oauth" or not conn.get("token"):
        return
    try:
        async with _client(10) as cx:
            await cx.post(REVOKE_URL, data={"token": conn["token"]},
                          headers={"Authorization": f"Bearer {conn['token']}"})
    except httpx.HTTPError:
        pass


def _auth_header(conn: dict) -> str:
    # Linear takes a personal API key as is, an OAuth token as a bearer token
    return conn["token"] if conn.get("kind") == "api_key" else f"Bearer {conn['token']}"


async def _refresh(store, conn: dict) -> dict:
    app = oauth_app(store)
    if not conn.get("refresh_token") or not app.get("client_id"):
        raise LinearError("the Linear sign-in expired; connect Linear again in Settings")
    data = {"grant_type": "refresh_token", "refresh_token": conn["refresh_token"],
            "client_id": app["client_id"]}
    if app.get("client_secret"):
        data["client_secret"] = app["client_secret"]
    async with _client(15) as cx:
        r = await cx.post(TOKEN_URL, data=data)
    if r.status_code >= 400:
        raise LinearError("Linear refused to renew the sign-in; connect Linear again in "
                          f"Settings ({r.status_code})")
    tok = r.json()
    conn = {**conn, "token": tok["access_token"],
            "refresh_token": tok.get("refresh_token") or conn["refresh_token"],
            "expires_at": int(time.time()) + int(tok.get("expires_in") or 86400)}
    _save_connection(store, conn)
    return conn


async def query(store, gql: str, variables: dict | None = None, conn: dict | None = None) -> dict:
    """Run a GraphQL query with the cell's connection (or `conn`, to test one before saving).
    Refreshes an OAuth token that is about to expire. Raises LinearError."""
    saved = conn is None
    conn = conn or get_connection(store)
    if not conn:
        raise LinearError("Linear is not connected; connect it in Settings")
    if (saved and conn.get("kind") == "oauth"
            and int(conn.get("expires_at") or 0) - REFRESH_MARGIN < time.time()):
        conn = await _refresh(store, conn)
    try:
        async with _client() as cx:
            r = await cx.post(API_URL, json={"query": gql, "variables": variables or {}},
                              headers={"Authorization": _auth_header(conn),
                                       "Content-Type": "application/json"})
    except httpx.HTTPError as e:
        raise LinearError(f"cannot reach Linear: {type(e).__name__}") from e
    if r.status_code in (400, 401, 403) and "authentication" in r.text.lower():
        raise LinearError("Linear did not accept the credential; check the key or connect "
                          "again in Settings")
    if r.status_code >= 400:
        raise LinearError(f"Linear answered {r.status_code}: {r.text[:200]}")
    body = r.json()
    if body.get("errors"):
        msg = "; ".join(str(e.get("message")) for e in body["errors"])
        if "authentication" in msg.lower():
            raise LinearError("Linear did not accept the credential; check the key or connect "
                              "again in Settings")
        raise LinearError(f"Linear: {msg}")
    return body.get("data") or {}


_VIEWER = "query { viewer { id name email organization { name urlKey } } }"


async def _account(store, conn: dict) -> dict:
    v = (await query(store, _VIEWER, conn=conn)).get("viewer") or {}
    org = v.get("organization") or {}
    return {"name": v.get("name"), "email": v.get("email"), "org": org.get("name"),
            "url_key": org.get("urlKey")}


async def connect_api_key(store, key: str) -> dict:
    key = (key or "").strip()
    if not key:
        raise LinearError("paste a Linear API key (Linear: Settings, Security and access, "
                          "Personal API keys)")
    conn = {"kind": "api_key", "token": key}
    conn["account"] = await _account(store, conn)
    conn["connected_at"] = now_utc().isoformat()
    _save_connection(store, conn)
    return conn


# ── OAuth with PKCE: start returns the Linear page to send the browser to; the verifier stays
# in this process (never in the URL), keyed by a nonce inside the signed state ──

_pending: dict[str, tuple[str, str, float]] = {}   # nonce -> (verifier, redirect_uri, created)


def oauth_start(store, redirect_uri: str, sign_state) -> str:
    app = oauth_app(store)
    if not app.get("client_id"):
        raise LinearError("Linear sign-in needs a Linear OAuth application: create one in Linear "
                          "(Settings, API, OAuth applications) with the callback URL shown here, "
                          "and save its client id; or paste a personal API key instead")
    now = time.time()
    for n, (_, _, t) in list(_pending.items()):
        if now - t > 900:
            _pending.pop(n, None)
    nonce = secrets.token_urlsafe(12)
    verifier = secrets.token_urlsafe(48)
    _pending[nonce] = (verifier, redirect_uri, now)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()
                                         ).rstrip(b"=").decode()
    state = sign_state(store, {"op": "linear", "nonce": nonce})
    return AUTHORIZE_URL + "?" + urlencode({
        "client_id": app["client_id"], "redirect_uri": redirect_uri, "response_type": "code",
        "scope": SCOPES, "state": state, "prompt": "consent", "code_challenge": challenge,
        "code_challenge_method": "S256"})


async def oauth_finish(store, code: str, state: str, verify_state) -> dict:
    data = verify_state(store, state)        # ValueError when forged or expired
    if data.get("op") != "linear" or not code:
        raise LinearError("this link is not a Linear sign-in callback")
    pend = _pending.pop(data.get("nonce") or "", None)
    if pend is None:
        raise LinearError("this sign-in was started before Tares restarted, or already used; "
                          "start again from Settings")
    verifier, redirect_uri, _ = pend
    app = oauth_app(store)
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
            "client_id": app.get("client_id", ""), "code_verifier": verifier}
    if app.get("client_secret"):
        form["client_secret"] = app["client_secret"]
    async with _client(15) as cx:
        r = await cx.post(TOKEN_URL, data=form)
    if r.status_code >= 400:
        raise LinearError(f"Linear refused the sign-in ({r.status_code}): {r.text[:200]}")
    tok = r.json()
    conn = {"kind": "oauth", "token": tok["access_token"],
            "refresh_token": tok.get("refresh_token"),
            "expires_at": int(time.time()) + int(tok.get("expires_in") or 86400)}
    conn["account"] = await _account(store, conn)
    conn["connected_at"] = now_utc().isoformat()
    _save_connection(store, conn)
    return conn


# ── Linear projects ───────────────────────────────────────────────────────────

_PROJECT_FIELDS = "id name url slugId"


def _project_ref(ref: str) -> str:
    """A project id, slug id, URL (…/project/<name>-<slugid>/…) or name, as given."""
    ref = (ref or "").strip()
    if "linear.app/" in ref and "/project/" in ref:
        slug = ref.split("/project/", 1)[1].split("/", 1)[0].split("?", 1)[0]
        return slug.rsplit("-", 1)[-1]
    return ref


async def find_project(store, ref: str) -> dict:
    """{id, name, url} of the Linear project `ref` names. Raises LinearError."""
    key = _project_ref(ref)
    if not key:
        raise LinearError("name the Linear project: its URL, id or name")
    try:
        p = (await query(store, f"query($id: String!) {{ project(id: $id) {{ {_PROJECT_FIELDS} }} }}",
                         {"id": key})).get("project")
        if p:
            return {"id": p["id"], "name": p["name"], "url": p["url"]}
    except LinearError as e:
        if "credential" in str(e) or "connected" in str(e) or "reach" in str(e):
            raise
    found = (await query(store, "query($n: String!) { projects(first: 5, filter: { name: "
                                f"{{ eqIgnoreCase: $n }} }}) {{ nodes {{ {_PROJECT_FIELDS} }} }} }}",
                         {"n": ref.strip()})).get("projects", {}).get("nodes") or []
    if not found:
        raise LinearError(f"no Linear project {ref!r} that this connection can see")
    p = found[0]
    return {"id": p["id"], "name": p["name"], "url": p["url"]}


async def list_projects(store, q: str = "") -> list[dict]:
    flt = ', filter: { name: { containsIgnoreCase: $q } }' if q.strip() else ""
    gql = (f"query{'($q: String!)' if flt else ''} {{ projects(first: 50{flt}, orderBy: updatedAt) "
           f"{{ nodes {{ {_PROJECT_FIELDS} }} }} }}")
    data = await query(store, gql, {"q": q.strip()} if flt else {})
    return [{"id": p["id"], "name": p["name"], "url": p["url"]}
            for p in (data.get("projects") or {}).get("nodes") or []]


# ── the sync ─────────────────────────────────────────────────────────────────

_ISSUES = """query($id: String!, $after: String) {
  project(id: $id) {
    issues(first: 50, after: $after, includeArchived: false) {
      nodes { id identifier title url sortOrder updatedAt state { name type }
              projectMilestone { id }
              inverseRelations(first: 20) { nodes { type issue { id } } } }
      pageInfo { hasNextPage endCursor }
    }
  }
}"""

# a Linear project's milestones, in Linear's order (TR-411)
_MILESTONES = """query($id: String!) {
  project(id: $id) {
    projectMilestones(first: 100) { nodes { id name description sortOrder } }
  }
}"""


def blockers_of(issue: dict) -> list[str]:
    """The Linear ids of the issues that block this one (a "blocks" relation pointing at it)."""
    rel = ((issue.get("inverseRelations") or {}).get("nodes")) or []
    return [r["issue"]["id"] for r in rel
            if str(r.get("type") or "").lower() == "blocks" and (r.get("issue") or {}).get("id")]


async def fetch_milestones(store, linear_project_id: str) -> list[dict]:
    data = await query(store, _MILESTONES, {"id": linear_project_id})
    proj = data.get("project") or {}
    return list(((proj.get("projectMilestones") or {}).get("nodes")) or [])

# Linear's workflow state types -> a ticket's status
_STATUS = {"backlog": "todo", "unstarted": "todo", "triage": "todo", "started": "in_progress",
           "completed": "done", "canceled": "canceled", "duplicate": "canceled"}


def status_of(state: dict | None) -> str:
    return _STATUS.get(str((state or {}).get("type") or "").lower(), "todo")


async def fetch_issues(store, linear_project_id: str) -> list[dict]:
    out, after = [], None
    while len(out) < MAX_ISSUES:
        data = await query(store, _ISSUES, {"id": linear_project_id, "after": after})
        proj = data.get("project")
        if proj is None:
            raise LinearError("the linked Linear project is gone, or this connection can no "
                              "longer see it")
        page = proj["issues"]
        out.extend(page.get("nodes") or [])
        if not page["pageInfo"]["hasNextPage"]:
            break
        after = page["pageInfo"]["endCursor"]
    return out


def _env(project: dict, link: dict, issue: dict, change: str, text: str, detail: dict) -> Envelope:
    ident = issue.get("identifier") or issue.get("id")
    return Envelope(
        source=SOURCE_NAME, source_type="event_stream", key_value=ident,
        event_type=f"ticket_{change}", text=text, event_time=now_utc(),
        payload={"change": change, "ticket": ident, "project": project["name"],
                 "project_id": project["id"],
                 "linear_project": link.get("name"), "issue": issue, **detail},
        labels={"ticket": ident, "project": project["name"],
                "linear_project": link.get("name") or ""})


async def sync_project(store, uid: str) -> list[Envelope]:
    """Bring a project's Linear tickets up to date and return one event per change. Records
    the sync's time or error on the project's link. Raises LinearError (after recording it)."""
    project = store.get_project(uid)
    link = store.get_project_linear(uid)
    if project is None or not link:
        return []
    try:
        issues = await fetch_issues(store, link["id"])
        milestones = await fetch_milestones(store, link["id"])
    except LinearError as e:
        store.set_project_linear(uid, {**link, "error": str(e)})
        raise
    ms_ids = _sync_milestones(store, uid, milestones)
    have = {t["external_id"]: t for t in store.list_tickets(uid) if t["owner"] == "linear"}
    seen, out = set(), []
    for iss in issues:
        seen.add(iss["id"])
        status = status_of(iss.get("state"))
        pos = float(iss.get("sortOrder") or 0)
        ident = iss.get("identifier") or iss["id"]
        ms = ms_ids.get((iss.get("projectMilestone") or {}).get("id") or "")
        cur = have.get(iss["id"])
        if cur is None:
            store.create_ticket(uid, "linear", iss.get("title") or ident, status, pos,
                                external_id=iss["id"], identifier=ident, url=iss.get("url"),
                                milestone=ms)
            out.append(_env(project, link, iss, "created",
                            f"{ident} added in Linear: {iss.get('title')}", {"status": status}))
            continue
        changes: dict = {}
        if cur["title"] != iss.get("title"):
            changes["title"] = iss.get("title")
            out.append(_env(project, link, iss, "renamed",
                            f"{ident} renamed in Linear: {cur['title']} -> {iss.get('title')}",
                            {"from": cur["title"], "to": iss.get("title")}))
        if cur["status"] != status:
            changes["status"] = status
            out.append(_env(project, link, iss, "status",
                            f"{ident} moved to {(iss.get('state') or {}).get('name') or status} "
                            f"in Linear", {"from": cur["status"], "to": status}))
        if cur["position"] != pos:
            changes["position"] = pos
            out.append(_env(project, link, iss, "reordered", f"{ident} reordered in Linear",
                            {"from": cur["position"], "to": pos}))
        if cur["identifier"] != ident or cur["url"] != iss.get("url"):
            changes.update(identifier=ident, url=iss.get("url"))
        if cur["milestone"] != ms:
            changes["milestone"] = ms
        if changes:
            store.update_ticket(cur["id"], **changes)
    # blocking relations become dependencies, once every issue has its ticket
    by_ext = {t["external_id"]: t for t in store.list_tickets(uid) if t["owner"] == "linear"}
    for iss in issues:
        t = by_ext.get(iss["id"])
        if t is None:
            continue
        deps = [by_ext[b]["id"] for b in blockers_of(iss) if b in by_ext and b != iss["id"]]
        if sorted(deps) != sorted(t["depends_on"]):
            store.update_ticket(t["id"], depends_on=deps)
    for ext, cur in have.items():
        if ext in seen or cur["status"] == "canceled":
            continue
        # gone from the project in Linear (moved, deleted, archived): canceled here, not dropped,
        # so its working doc stays reachable
        store.update_ticket(cur["id"], status="canceled")
        out.append(_env(project, link,
                        {"id": ext, "identifier": cur["identifier"], "title": cur["title"]},
                        "removed", f"{cur['identifier']} left the Linear project",
                        {"from": cur["status"], "to": "canceled"}))
    store.set_project_linear(uid, {**link, "synced_at": now_utc().isoformat(), "error": None,
                                   "issues": len(issues)})
    return out


def _sync_milestones(store, uid: str, milestones: list[dict]) -> dict[str, str]:
    """Linear's project milestones mirrored on the project (name and order from Linear; goal
    from its description until someone writes one on Tares; checks always Tares's). A milestone
    gone from Linear goes here too, its tickets keep going without one. Returns {Linear id:
    milestone id}."""
    have = {m["external_id"]: m for m in store.list_milestones(uid) if m["owner"] == "linear"}
    ids: dict[str, str] = {}
    for lm in milestones:
        name = (lm.get("name") or "").strip() or "Milestone"
        pos = float(lm.get("sortOrder") or 0)
        cur = have.get(lm["id"])
        if cur is None:
            # a Tares milestone of the same name (written before the link) becomes this one
            same = store.get_milestone(uid, name)
            if same and same["owner"] != "linear":
                store.update_milestone(same["id"], owner="linear", external_id=lm["id"],
                                       position=pos)
                ids[lm["id"]] = same["id"]
                continue
            ids[lm["id"]] = store.create_milestone(
                uid, name, " ".join(str(lm.get("description") or "").split())[:500], [], pos,
                owner="linear", external_id=lm["id"])
            continue
        ids[lm["id"]] = cur["id"]
        clash = store.get_milestone(uid, name)
        if cur["name"] != name and not (clash and clash["id"] != cur["id"]):
            store.update_milestone(cur["id"], name=name)   # a rename onto another's name waits
        if cur["position"] != pos:
            store.update_milestone(cur["id"], position=pos)
    for ext, cur in have.items():
        if ext not in ids:
            store.delete_milestone(cur["id"])
    return ids


def ensure_source(store) -> bool:
    """The cell's `linear` source exists; True when it was just made (the caller reloads)."""
    if any(s["name"] == SOURCE_NAME for s in store.list_catalog_sources()):
        return False
    store.upsert_catalog_source(SOURCE_NAME, "event_stream", "linear", "60s", {
        "labels": [{"name": "ticket", "field": "ticket", "primary": True},
                   {"name": "project", "field": "project"},
                   {"name": "linear_project", "field": "linear_project"}]})
    return True


def iso(dt) -> str:
    return dt.isoformat() if isinstance(dt, datetime) else str(dt or "")


__all__ = ["LinearError", "get_connection", "public_connection", "connect_api_key",
           "oauth_start", "oauth_finish", "find_project", "list_projects", "sync_project",
           "ensure_source", "status_of", "timezone"]
