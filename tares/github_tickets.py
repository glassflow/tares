"""GitHub Issues as a project's tickets: the sync that keeps them current.

When a project uses GitHub Issues, GitHub owns its tickets. Sessions create and change the issues
with `gh`; Tares keeps a record of each (tickets table, owner 'github') and notices changes by
polling, since a local cell has no public address for GitHub's webhooks. A project is linked to
one repo and one issue label: every issue of the repo carrying that label is a ticket. Each change
is also an event on the `github_issues` source, so it shows on the timeline and triggers can
react.

Credentials are the cell's stored GitHub ones (Settings > GitHub), github.com only. Each is tried
until one can read the repo; the one that worked is remembered on the link.

Everything here is read-only towards GitHub.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import httpx

from .connectors.github import _normalize_repo
from . import ticket_assignee
from .envelope import Envelope, now_utc

API_URL = "https://api.github.com/graphql"
SOURCE_NAME = "github_issues"
MAX_ISSUES = 1000             # per project per sync; a factory project is far smaller
RATE_BACKOFF = 300            # seconds a project's sync waits after GitHub's rate limit answer
IN_PROGRESS = "in progress"   # the issue label that makes an open issue in_progress

# tests swap in an httpx.MockTransport with recorded responses
_transport: httpx.AsyncBaseTransport | None = None


class GitHubTicketsError(RuntimeError):
    """GitHub refused or failed; the message is meant for a person. `rate_limited` tells the
    caller to back off; `kind` is 'not_found', 'forbidden' or 'unauthorized' when GitHub said so
    about the credential, so the next one can be tried."""

    def __init__(self, message: str, rate_limited: bool = False, kind: str | None = None):
        super().__init__(message)
        self.rate_limited = rate_limited
        self.kind = kind


def default_issue_label(project_name: str) -> str:
    """The issue label a project gets unless the link names another; GitHub cuts labels at 50."""
    return f"tares:{project_name}"[:50]


# ── talking to GitHub ────────────────────────────────────────────────────────

# The milestones part is the same on every page; the caller keeps the first page's.
_QUERY = """query($owner: String!, $name: String!, $label: String!, $after: String) {
  rateLimit { remaining resetAt }
  repository(owner: $owner, name: $name) {
    nameWithOwner url
    milestones(first: 100, states: [OPEN, CLOSED], orderBy: {field: NUMBER, direction: ASC}) {
      nodes { id number title description } pageInfo { hasNextPage } }
    issues(first: 100, after: $after, labels: [$label], states: [OPEN, CLOSED],
           orderBy: {field: CREATED_AT, direction: ASC}) {
      nodes { id number title url state stateReason(enableDuplicate: true)
              labels(first: 30) { nodes { name } } milestone { id }
              assignees(first: 1) { nodes { login } }
              blockedBy(first: 50) { nodes { id } } }
      pageInfo { hasNextPage endCursor } } } }"""


async def _post(token: str, variables: dict) -> dict:
    """One page of `_QUERY`: its `data`. Raises GitHubTicketsError."""
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    try:
        async with httpx.AsyncClient(timeout=20, transport=_transport) as cx:
            r = await cx.post(API_URL, json={"query": _QUERY, "variables": variables},
                              headers=headers)
    except httpx.HTTPError as e:
        raise GitHubTicketsError(f"cannot reach GitHub: {type(e).__name__}") from e
    if r.status_code == 401:
        raise GitHubTicketsError("GitHub did not accept the credential", kind="unauthorized")
    if r.status_code == 429 or (r.status_code == 403 and (
            r.headers.get("x-ratelimit-remaining") == "0" or r.headers.get("retry-after"))):
        raise GitHubTicketsError("GitHub rate limit reached", rate_limited=True)
    if r.status_code >= 400:
        raise GitHubTicketsError(f"GitHub answered {r.status_code}")
    try:
        body = r.json()
    except ValueError as e:
        raise GitHubTicketsError("GitHub sent a reply that is not JSON") from e
    errors = body.get("errors") or []
    if errors:
        types = {str(e.get("type") or "") for e in errors}
        msg = "; ".join(str(e.get("message")) for e in errors)
        if "RATE_LIMITED" in types:
            raise GitHubTicketsError("GitHub rate limit reached", rate_limited=True)
        if "NOT_FOUND" in types:
            raise GitHubTicketsError(f"GitHub: {msg}", kind="not_found")
        if "FORBIDDEN" in types or ("issues" in msg.lower() and "permission" in msg.lower()):
            raise GitHubTicketsError(f"GitHub: {msg}", kind="forbidden")
        raise GitHubTicketsError(f"GitHub: {msg}")
    return body.get("data") or {}


async def _candidates(store, link: dict, repo: str) -> list[tuple[str, str]]:
    """[(credential name, token)] for the stored github.com credentials that can give a token,
    the one the link remembers first. Whether a token can read `repo` is not checked here."""
    from .github_credentials import get_token
    names = [c["name"] for c in store.list_github_credentials() if not c.get("api_url")]
    first = link.get("credential")
    if first in names:
        names.remove(first)
        names.insert(0, first)
    out = []
    for name in names:
        try:
            out.append((name, await get_token(store, name, repo)))
        except Exception:
            continue
    return out


def _need(obj, field: str):
    """`obj[field]`, or GitHubTicketsError when GitHub's answer lacks it (null or missing), so a
    malformed answer is never read as an empty list."""
    value = obj.get(field) if isinstance(obj, dict) else None
    if value is None:
        raise GitHubTicketsError(f"GitHub sent an answer without {field}")
    return value


async def fetch(store, link: dict) -> tuple[dict, list[dict], list[dict]]:
    """(repository info, GitHub milestones, issues carrying the link's issue label). The info
    names the `credential` that worked. All or nothing: any failure raises GitHubTicketsError
    and no partial list comes back."""
    try:
        repo = _normalize_repo(link.get("repo"))
    except ValueError as e:
        raise GitHubTicketsError(str(e)) from e
    owner, name = repo.split("/", 1)
    label = link["issue_label"]
    base = {"owner": owner, "name": name, "label": label}
    forbidden: GitHubTicketsError | None = None
    limited: GitHubTicketsError | None = None
    cred, token, data = None, None, None
    for cred_name, tok in await _candidates(store, link, repo):
        try:
            data = await _post(tok, {**base, "after": None})
        except GitHubTicketsError as e:
            if e.kind == "forbidden":
                forbidden = GitHubTicketsError(
                    f"the credential that sees {repo} cannot read its issues; give it "
                    "Issues: read", kind="forbidden")
            elif e.rate_limited:
                limited = e
            elif e.kind not in ("not_found", "unauthorized"):
                raise
            continue
        if data.get("repository"):
            cred, token = cred_name, tok
            break
    if cred is None:
        raise (forbidden or limited or GitHubTicketsError(
            f"no stored GitHub credential can read {repo}; add one in Settings > GitHub"))
    repository = first = data["repository"]
    milestones = _need(first, "milestones")
    if _need(milestones, "pageInfo").get("hasNextPage"):
        raise GitHubTicketsError(f"{repo} has more than 100 milestones")
    issues: list[dict] = []
    while True:
        page = _need(repository, "issues")
        issues.extend(_need(page, "nodes"))
        if len(issues) > MAX_ISSUES:
            raise GitHubTicketsError(f"more than {MAX_ISSUES} issues carry {label}")
        info_page = _need(page, "pageInfo")
        if not info_page.get("hasNextPage"):
            break
        data = await _post(token, {**base, "after": _need(info_page, "endCursor")})
        repository = data.get("repository")
        if not repository:
            raise GitHubTicketsError(f"GitHub stopped returning {repo} part way through")
    info = {"nameWithOwner": first.get("nameWithOwner") or repo, "url": first.get("url"),
            "credential": cred}
    return info, list(_need(milestones, "nodes")), issues


# ── the sync ─────────────────────────────────────────────────────────────────

def status_of(issue: dict) -> str:
    if issue.get("state") == "CLOSED":
        return "canceled" if issue.get("stateReason") in ("NOT_PLANNED", "DUPLICATE") else "done"
    names = [str(l.get("name") or "").strip().lower()
             for l in ((issue.get("labels") or {}).get("nodes")) or []]
    return "in_progress" if IN_PROGRESS in names else "todo"


def identifier_of(issue: dict) -> str:
    return f"GH-{issue['number']}"


def _env(project: dict, link: dict, issue: dict, change: str, text: str, detail: dict) -> Envelope:
    ident = issue.get("identifier") or identifier_of(issue)
    return Envelope(
        source=SOURCE_NAME, source_type="event_stream", key_value=ident,
        event_type=f"ticket_{change}", text=text, event_time=now_utc(),
        payload={"change": change, "ticket": ident, "project": project["name"],
                 "project_id": project["id"], "repo": link.get("repo"),
                 "issue_label": link.get("issue_label"), "issue": issue, **detail},
        labels={"ticket": ident, "project": project["name"], "repo": link.get("repo") or ""})


def _backing_off(link: dict) -> bool:
    try:
        until = datetime.fromisoformat(str(link.get("backoff_until")))
    except (TypeError, ValueError):
        return False
    return until > now_utc()


async def sync_project(store, uid: str) -> list[Envelope]:
    """Bring a project's GitHub tickets up to date and return one event per change. Records the
    sync's time or error on the project's link. Raises GitHubTicketsError (after recording it)."""
    project = store.get_project(uid)
    link = store.get_project_github(uid)
    if project is None or not link or _backing_off(link):
        return []
    label, repo = link["issue_label"], link["repo"]
    try:
        info, milestones, issues = await fetch(store, link)
    except GitHubTicketsError as e:
        now = store.get_project_github(uid)
        if now and (now["repo"], now["issue_label"]) == (repo, label):
            saved = {**now, "error": str(e)}
            if e.rate_limited:
                saved["backoff_until"] = (now_utc() + timedelta(seconds=RATE_BACKOFF)).isoformat()
            store.set_project_github(uid, saved)
        raise
    link = store.get_project_github(uid)
    if not link or (link["repo"], link["issue_label"]) != (repo, label):
        return []   # unlinked (or linked elsewhere) while GitHub was answering: write nothing
    have = {t["external_id"]: t for t in store.list_tickets(uid) if t["owner"] == "github"}
    if not issues and any(t["status"] in ("todo", "in_progress") for t in have.values()):
        # most likely the issue label was renamed or deleted; cancel nothing on a guess
        err = (f"no issues carry {label} in {repo} any more; was the issue label renamed?")
        store.set_project_github(uid, {**link, "error": err})
        raise GitHubTicketsError(err)
    ms_ids = _sync_milestones(store, uid, milestones, issues)
    seen, out = set(), []
    for iss in issues:
        seen.add(iss["id"])
        status = status_of(iss)
        pos = float(iss["number"])
        ident = identifier_of(iss)
        ms = ms_ids.get((iss.get("milestone") or {}).get("id") or "")
        who, raw = ticket_assignee.from_github(store, iss)
        cur = have.get(iss["id"])
        if cur is None:
            store.create_ticket(uid, "github", iss.get("title") or ident, status, pos,
                                external_id=iss["id"], identifier=ident, url=iss.get("url"),
                                milestone=ms, assignee_person_id=who, assignee_raw=raw)
            ticket_assignee.sync_join(store, project, who, status)
            out.append(_env(project, link, iss, "created",
                            f"{ident} added in GitHub: {iss.get('title')}", {"status": status}))
            continue
        changes: dict = {}
        if cur["title"] != iss.get("title"):
            changes["title"] = iss.get("title")
            out.append(_env(project, link, iss, "renamed",
                            f"{ident} renamed in GitHub: {cur['title']} -> {iss.get('title')}",
                            {"from": cur["title"], "to": iss.get("title")}))
        if cur["status"] != status:
            changes["status"] = status
            out.append(_env(project, link, iss, "status",
                            f"{ident} moved to {status.replace('_', ' ')} in GitHub",
                            {"from": cur["status"], "to": status}))
        if cur["position"] != pos:
            changes["position"] = pos
            out.append(_env(project, link, iss, "reordered", f"{ident} reordered in GitHub",
                            {"from": cur["position"], "to": pos}))
        if cur["identifier"] != ident or cur["url"] != iss.get("url"):
            changes.update(identifier=ident, url=iss.get("url"))
        if cur["milestone"] != ms:
            changes["milestone"] = ms
        if (cur["assignee_person_id"], cur["assignee_raw"]) != (who, raw):
            changes.update(assignee_person_id=who, assignee_raw=raw)
            ticket_assignee.sync_join(store, project, who, status)
        if changes:
            store.update_ticket(cur["id"], **changes)
    # "blocked by" becomes dependencies, once every issue has its ticket
    by_ext = {t["external_id"]: t for t in store.list_tickets(uid) if t["owner"] == "github"}
    for iss in issues:
        t = by_ext.get(iss["id"])
        if t is None:
            continue
        blockers = ((iss.get("blockedBy") or {}).get("nodes")) or []
        deps = [by_ext[b["id"]]["id"] for b in blockers
                if b.get("id") in by_ext and b["id"] != iss["id"] and b["id"] in seen]
        if sorted(deps) != sorted(t["depends_on"]):
            store.update_ticket(t["id"], depends_on=deps)
    for ext, cur in have.items():
        if ext in seen or cur["status"] == "canceled":
            continue
        # no longer carries the issue label (or deleted, or moved to another repo): canceled
        # here, not dropped, so its working doc stays reachable
        store.update_ticket(cur["id"], status="canceled")
        out.append(_env(project, link,
                        {"id": ext, "identifier": cur["identifier"], "title": cur["title"]},
                        "removed", f"{cur['identifier']} no longer carries {label}",
                        {"from": cur["status"], "to": "canceled"}))
    store.set_project_github(uid, {**link, "synced_at": now_utc().isoformat(), "error": None,
                                   "issues": len(issues), "credential": info["credential"],
                                   "backoff_until": None})
    return out


def _sync_milestones(store, uid: str, milestones: list[dict], issues: list[dict]) -> dict[str, str]:
    """The repo's GitHub milestones that a labeled issue uses, mirrored on the project (name and
    order from GitHub; goal from its description until someone writes one on Tares; checks always
    Tares's). A mirror stays while its GitHub milestone is in the repo, used or not; it goes
    when the milestone is gone, and its tickets keep going without one. Returns {GitHub id:
    milestone id}."""
    have = {m["external_id"]: m for m in store.list_milestones(uid) if m["owner"] == "github"}
    used = {(i.get("milestone") or {}).get("id") for i in issues}
    ids: dict[str, str] = {}
    for gm in sorted(milestones, key=lambda m: m["number"]):
        name = (gm.get("title") or "").strip() or "Milestone"
        pos = float(gm["number"])
        cur = have.get(gm["id"])
        if cur is None:
            if gm["id"] not in used:
                continue
            # a Tares milestone of the same name (written before the link) becomes this one
            same = store.get_milestone(uid, name)
            if same and same["owner"] != "github":
                store.update_milestone(same["id"], owner="github", external_id=gm["id"],
                                       position=pos)
                ids[gm["id"]] = same["id"]
                continue
            ids[gm["id"]] = store.create_milestone(
                uid, name, " ".join(str(gm.get("description") or "").split())[:500], [], pos,
                owner="github", external_id=gm["id"])
            continue
        ids[gm["id"]] = cur["id"]
        clash = store.get_milestone(uid, name)
        if cur["name"] != name and not (clash and clash["id"] != cur["id"]):
            store.update_milestone(cur["id"], name=name)   # a rename onto another's name waits
        if cur["position"] != pos:
            store.update_milestone(cur["id"], position=pos)
    gone = {m["id"] for m in milestones}
    for ext, cur in have.items():
        if ext not in gone:
            store.delete_milestone(cur["id"])
    return ids


def ensure_source(store) -> bool:
    """The cell's `github_issues` source exists; True when it was just made (the caller reloads)."""
    if any(s["name"] == SOURCE_NAME for s in store.list_catalog_sources()):
        return False
    store.upsert_catalog_source(SOURCE_NAME, "event_stream", "github_issues", "60s", {
        "labels": [{"name": "ticket", "field": "ticket", "primary": True},
                   {"name": "project", "field": "project"},
                   {"name": "repo", "field": "repo"}]})
    return True


__all__ = ["GitHubTicketsError", "default_issue_label", "fetch", "status_of", "identifier_of",
           "sync_project", "ensure_source"]
