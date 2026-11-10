"""Linear (TR-408): one connection per cell, and the sync that keeps a project's tickets current.

When a project uses Linear, Linear owns its tickets. Sessions create and change them in Linear
with Linear's own tools; Tares keeps a record of each (tickets table, owner 'linear') and notices
changes by polling, since a local cell has no public address for Linear's webhooks. Each change
is also an event on the `linear` source, so it shows on the timeline and triggers can react.

The connection is one of two kinds:

* a personal API key, pasted in Settings (every install);
* on Tares Cloud, a token from the GlassFlow Linear app (TR-535..537). The control plane runs the
  sign-in, holds the app's secret, renews the token before its 24 hours run out and hands each
  new one to the cell (`set_cloud_connection`). The cell never renews it itself.

A connection from the cell's own Linear sign-in (kind 'oauth', before TR-538) is no longer used:
it shows as needing to be connected again.

The sync only reads from Linear. A ticket with a status change waiting to reach Linear
(a pending_writeback row, TR-845) keeps the status Tares gave it until the write lands.
The writes themselves are tares/integrations/builtin/tickets_linear.py.
"""
from __future__ import annotations

import asyncio
import json
import math
import time
from datetime import datetime, timezone

import httpx

from . import team as team_mod
from .envelope import Envelope, now_utc


API_URL = "https://api.linear.app/graphql"

CONNECTION_SETTING = "linear_connection"
SOURCE_NAME = "linear"
MAX_ISSUES = 1000             # per project per sync; a factory project is far smaller

RECONNECT = "connect Linear again in Settings"

# Linear's rate limit is waited out here, for every call: at most MAX_WAIT seconds each time (and
# at least 1), RATE_DEFAULT_WAIT when Linear names no wait, and RATE_RETRIES tries again at most.
MAX_WAIT = 60
RATE_DEFAULT_WAIT = 10
RATE_RETRIES = 3
MAX_PAGES = 100               # per list `pages` reads; far past what a factory project holds

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


def _usable(conn: dict | None) -> bool:
    return bool(conn and conn.get("token") and conn.get("kind") in ("api_key", "cloud"))


def public_connection(store) -> dict:
    """What the console may see: never a token. A connection that cannot be used any more (the
    cell's own sign-in from before TR-538, or a Tares Cloud one Linear refused to renew) is not
    connected, and says why."""
    conn = get_connection(store)
    out = {"connected": _usable(conn)}
    if conn:
        out.update({"kind": conn.get("kind"), "account": conn.get("account") or {},
                    "connected_at": conn.get("connected_at"), "error": conn.get("error")})
        if conn.get("kind") == "oauth":
            out["error"] = ("Linear sign-in from this workspace is no longer supported; "
                            "connect again with a personal API key")
    return out


def set_cloud_connection(store, body: dict) -> dict:
    """What the control plane hands over (TR-537): {token, expires_at, account} after a sign-in
    or a renewal, or {error, account} when Linear refused to renew it (TR-536). connected_at is
    kept across renewals."""
    prev = get_connection(store) or {}
    account = body.get("account") if isinstance(body.get("account"), dict) else {}
    keep = prev.get("connected_at") if prev.get("kind") == "cloud" else None
    if body.get("error"):
        conn = {"kind": "cloud", "token": None, "account": account or prev.get("account") or {},
                "error": str(body["error"])[:300], "connected_at": keep}
    else:
        token = str(body.get("token") or "").strip()
        if not token:
            raise LinearError("token is required (use DELETE to clear the connection)")
        conn = {"kind": "cloud", "token": token, "expires_at": int(body.get("expires_at") or 0),
                "account": account, "connected_at": keep or now_utc().isoformat()}
    _save_connection(store, conn)
    return conn


def disconnect(store) -> dict | None:
    conn = get_connection(store)
    _save_connection(store, None)
    return conn


def _auth_header(conn: dict) -> str:
    # Linear takes a personal API key as is, an OAuth token as a bearer token
    return conn["token"] if conn.get("kind") == "api_key" else f"Bearer {conn['token']}"


async def _sleep(seconds: float) -> None:
    """The wait before a rate limited request is sent again. Tests replace it."""
    await asyncio.sleep(seconds)


def _rate_limited(r: httpx.Response) -> bool:
    """True when Linear refused the request for its rate limit before running it: HTTP 429, or
    (Linear answers 400 or 200 as well) a GraphQL error whose extensions.code is RATELIMITED."""
    if r.status_code == 429:
        return True
    try:
        body = r.json()
    except ValueError:
        return False
    errors = body.get("errors") if isinstance(body, dict) else None
    if not isinstance(errors, list):
        return False
    return any(isinstance(e, dict) and isinstance(e.get("extensions"), dict)
               and str(e["extensions"].get("code") or "").upper() == "RATELIMITED"
               for e in errors)


def _wait_of(headers) -> float:
    """Seconds to wait after a rate limit answer: `Retry-After`, else until
    `X-RateLimit-Requests-Reset` (epoch milliseconds), else RATE_DEFAULT_WAIT; at least 1 and at
    most MAX_WAIT. A header that is not a number is the same as no header."""
    def number(raw) -> float | None:
        try:
            v = float(raw)
        except (TypeError, ValueError):
            return None
        return None if math.isnan(v) else v

    wait = number(headers.get("retry-after"))
    if wait is None:
        reset = number(headers.get("x-ratelimit-requests-reset"))
        wait = RATE_DEFAULT_WAIT if reset is None else reset / 1000 - time.time()
    return min(float(MAX_WAIT), max(1.0, wait))


async def query(store, gql: str, variables: dict | None = None, conn: dict | None = None) -> dict:
    """Run a GraphQL query with the cell's connection (or `conn`, to test one before saving).
    A rate limited request (HTTP 429, or a RATELIMITED error) is waited out and sent again, alone,
    at most RATE_RETRIES times: Linear refuses it before it runs, so even a mutation is not made
    twice. Anything else that goes wrong is not retried. Raises LinearError."""
    conn = conn or get_connection(store)
    if not conn:
        raise LinearError("Linear is not connected; connect it in Settings")
    if not _usable(conn):
        raise LinearError(conn.get("error") or f"the Linear connection stopped working; {RECONNECT}")
    if conn.get("kind") == "cloud" and int(conn.get("expires_at") or 0) < time.time():
        # Tares Cloud renews it hours ahead; an expired one means the renewal did not reach us
        raise LinearError(f"the Linear sign-in expired and was not renewed; {RECONNECT}")
    for attempt in range(RATE_RETRIES + 1):
        try:
            async with _client() as cx:
                r = await cx.post(API_URL, json={"query": gql, "variables": variables or {}},
                                  headers={"Authorization": _auth_header(conn),
                                           "Content-Type": "application/json"})
        except httpx.HTTPError as e:
            raise LinearError(f"cannot reach Linear: {type(e).__name__}") from e
        if not _rate_limited(r):
            break
        if attempt == RATE_RETRIES:
            raise LinearError("Linear is rate limiting this connection; try again in a few minutes")
        await _sleep(_wait_of(r.headers))
    if r.status_code in (400, 401, 403) and "authentication" in r.text.lower():
        raise LinearError("Linear did not accept the credential; check the key or connect "
                          "again in Settings")
    if r.status_code >= 400:
        raise LinearError(f"Linear answered {r.status_code}: {r.text[:200]}")
    try:
        body = r.json()
    except ValueError as e:
        raise LinearError(f"Linear answered {r.status_code} with something that is not JSON") from e
    if not isinstance(body, dict):
        raise LinearError(f"Linear answered {r.status_code} with something Tares cannot read")
    if body.get("errors"):
        errors = body["errors"] if isinstance(body["errors"], list) else [body["errors"]]
        msg = "; ".join(str(e.get("message") if isinstance(e, dict) else e) for e in errors)
        if "authentication" in msg.lower():
            raise LinearError("Linear did not accept the credential; check the key or connect "
                              "again in Settings")
        raise LinearError(f"Linear: {msg}")
    return body.get("data") or {}


async def pages(store, gql: str, variables: dict, path: tuple, missing: str | None = None):
    """The nodes of a paged Linear list, page by page, until Linear says there is no next page.
    `gql` takes `$after`; `path` names the connection inside the answer, for example
    ("project", "issues"). When the answer has no such connection nothing is yielded, or
    LinearError(`missing`) is raised when a message is given. Raises LinearError too when Linear
    promises another page and gives no cursor, or when the list runs past MAX_PAGES pages."""
    after = None
    for _ in range(MAX_PAGES):
        conn = await query(store, gql, {**variables, "after": after})
        for key in path:
            conn = conn.get(key) if isinstance(conn, dict) else None
        if not isinstance(conn, dict):
            if missing:
                raise LinearError(missing)
            return
        yield list(conn.get("nodes") or [])
        info = conn.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            return
        after = info.get("endCursor")
        if not after:
            raise LinearError("Linear says there is another page but gave no way to read it")
    raise LinearError(f"Linear has more than {MAX_PAGES} pages of this list; nothing was changed")


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
              projectMilestone { id } assignee { email name }
              inverseRelations(first: 20) { nodes { type issue { id } } } }
      pageInfo { hasNextPage endCursor }
    }
  }
}"""

# a Linear project's milestones, in Linear's order (TR-411)
_MILESTONES = """query($id: String!, $after: String) {
  project(id: $id) {
    projectMilestones(first: 100, after: $after) {
      nodes { id name description sortOrder }
      pageInfo { hasNextPage endCursor }
    }
  }
}"""


def blockers_of(issue: dict) -> list[str]:
    """The Linear ids of the issues that block this one (a "blocks" relation pointing at it)."""
    rel = ((issue.get("inverseRelations") or {}).get("nodes")) or []
    return [r["issue"]["id"] for r in rel
            if str(r.get("type") or "").lower() == "blocks" and (r.get("issue") or {}).get("id")]


async def fetch_milestones(store, linear_project_id: str) -> list[dict]:
    """Every milestone of the Linear project, in Linear's order, all pages."""
    out: list[dict] = []
    async for nodes in pages(store, _MILESTONES, {"id": linear_project_id},
                             ("project", "projectMilestones")):
        out.extend(nodes)
    return out

# Linear's workflow state types -> a ticket's status
_STATUS = {"backlog": "todo", "unstarted": "todo", "triage": "todo", "started": "in_progress",
           "completed": "done", "canceled": "canceled", "duplicate": "canceled"}


def status_of(state: dict | None) -> str:
    return _STATUS.get(str((state or {}).get("type") or "").lower(), "todo")


async def fetch_issues(store, linear_project_id: str) -> list[dict]:
    """Every issue of the Linear project. Never a partial list: the sync cancels any ticket it
    did not see, so a list cut short would cancel tickets that are still in the project."""
    out, after = [], None
    while True:
        data = await query(store, _ISSUES, {"id": linear_project_id, "after": after})
        proj = data.get("project")
        if proj is None:
            raise LinearError("the linked Linear project is gone, or this connection can no "
                              "longer see it")
        page = proj["issues"]
        out.extend(page.get("nodes") or [])
        if not page["pageInfo"]["hasNextPage"]:
            return out
        if len(out) >= MAX_ISSUES:
            raise LinearError(f"the Linear project has more than {MAX_ISSUES} issues; "
                              "nothing was changed")
        after = page["pageInfo"]["endCursor"]


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
        if not issues and any(t["owner"] == "linear" for t in store.list_tickets(uid)):
            raise LinearError("Linear returned no issues for this project; nothing was changed")
    except LinearError as e:
        store.set_project_linear(uid, {**link, "error": str(e)})
        raise
    ms_ids = _sync_milestones(store, uid, milestones)
    have = {t["external_id"]: t for t in store.list_tickets(uid) if t["owner"] == "linear"}
    seen, out = set(), []
    people: dict = {}
    nameless = [i for i in issues if isinstance(i.get("assignee"), dict)
                and not str(i["assignee"].get("email") or "").strip()]
    team = team_mod.get()
    team.assignee_warn_nameless(project, len(nameless))
    for iss in issues:
        seen.add(iss["id"])
        status = status_of(iss.get("state"))
        mapped = team.assignee_from_linear(store, iss.get("assignee"), people)   # None: not synced
        pos = float(iss.get("sortOrder") or 0)
        ident = iss.get("identifier") or iss["id"]
        ms = ms_ids.get((iss.get("projectMilestone") or {}).get("id") or "")
        cur = have.get(iss["id"])
        if cur is None:
            who = {} if mapped is None else {"assignee_person_id": mapped[0],
                                             "assignee_raw": mapped[1]}
            store.create_ticket(uid, "linear", iss.get("title") or ident, status, pos,
                                external_id=iss["id"], identifier=ident, url=iss.get("url"),
                                milestone=ms, **who)
            if mapped is not None:
                team.assignee_sync_join(store, project, mapped[0], status)
            out.append(_env(project, link, iss, "created",
                            f"{ident} added in Linear: {iss.get('title')}", {"status": status}))
            continue
        changes: dict = {}
        if cur["title"] != iss.get("title"):
            changes["title"] = iss.get("title")
            out.append(_env(project, link, iss, "renamed",
                            f"{ident} renamed in Linear: {cur['title']} -> {iss.get('title')}",
                            {"from": cur["title"], "to": iss.get("title")}))
        moved = None
        if cur["status"] != status:
            # written apart from the rest: a status change of this ticket waiting to reach Linear
            # (TR-845) must not be undone by what Linear still says
            moved = _env(project, link, iss, "status",
                         f"{ident} moved to {(iss.get('state') or {}).get('name') or status} "
                         f"in Linear", {"from": cur["status"], "to": status})
            out.append(moved)
        if cur["position"] != pos:
            changes["position"] = pos
            out.append(_env(project, link, iss, "reordered", f"{ident} reordered in Linear",
                            {"from": cur["position"], "to": pos}))
        if cur["identifier"] != ident or cur["url"] != iss.get("url"):
            changes.update(identifier=ident, url=iss.get("url"))
        if cur["milestone"] != ms:
            changes["milestone"] = ms
        if mapped is not None and (cur["assignee_person_id"], cur["assignee_raw"]) != mapped:
            changes.update(assignee_person_id=mapped[0], assignee_raw=mapped[1])
            team.assignee_sync_join(store, project, mapped[0], status)
        why = f"Linear marked it {(iss.get('state') or {}).get('name') or status}"
        if changes:
            store.update_ticket(cur["id"], why=why, **changes)
        if moved is not None and not store.update_ticket(cur["id"], why=why, status=status,
                                                         unless_pending=True):
            out.remove(moved)      # a status change is waiting to reach Linear: Tares's stands
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
        store.update_ticket(cur["id"], status="canceled", why="it left the Linear project")
        store.pending_writeback_clear(cur["id"])     # nothing left in Linear to write to
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
           "set_cloud_connection", "find_project", "list_projects", "sync_project",
           "ensure_source", "status_of", "timezone"]
