"""A factory ticket's pull request as GitHub has it (TR-410): head, CI, the reviewer's verdict (the
`factory/review` commit status), merged or not. A ticket's PR state is a fact Tares reads from
GitHub, not a station's claim.

Tares reads it with the cell's own GitHub credentials (Settings, GitHub), trying each until one
can see the repo. No credential, or none that sees it: the PR keeps its link and says why it could
not be read. Reads are cached on the ticket for READ_EVERY seconds.
"""
from __future__ import annotations

import re
from datetime import datetime

import httpx

from .envelope import now_utc

READ_EVERY = 60
REVIEW_CONTEXT = "factory/review"
API = "https://api.github.com"

_PR_URL = re.compile(r"github\.com/([^/\s]+/[^/\s]+)/pull/(\d+)")

# swappable in tests: (method, url, token) -> (status, json)
_transport: httpx.AsyncBaseTransport | None = None


def parse_pr(ref: str) -> tuple[str, int] | None:
    """(owner/repo, number) of a PR URL or `owner/repo#12`; None otherwise."""
    ref = str(ref or "").strip()
    m = _PR_URL.search(ref)
    if m:
        return m.group(1), int(m.group(2))
    m = re.match(r"^([\w.-]+/[\w.-]+)#(\d+)$", ref)
    return (m.group(1), int(m.group(2))) if m else None


ERROR_BACKOFF = 300   # a read that failed is tried again after this long, not every minute


def stale(pr: dict | None) -> bool:
    if not pr or not pr.get("repo"):
        return False
    if pr.get("merged") and "changed_files" in pr:
        return False            # a merged PR does not change any more (read once for its size)
    at = pr.get("checked_at")
    try:
        when = datetime.fromisoformat(str(at)) if at else None
    except ValueError:
        when = None
    wait = ERROR_BACKOFF if pr.get("error") else READ_EVERY
    return when is None or (now_utc() - when).total_seconds() >= wait


def _ci(statuses: list[dict], runs: list[dict]) -> str:
    """One word for CI on a commit, the reviewer's own status left out: success, failure,
    pending, or none (no checks at all)."""
    states = [s.get("state") for s in statuses if s.get("context") != REVIEW_CONTEXT]
    for r in runs:
        if r.get("status") != "completed":
            states.append("pending")
        elif r.get("conclusion") in ("success", "neutral", "skipped"):
            states.append("success")
        else:
            states.append("failure")
    if not states:
        return "none"
    if any(s in ("failure", "error") for s in states):
        return "failure"
    if any(s == "pending" for s in states):
        return "pending"
    return "success"


def _status_state(state) -> str:
    """A commit status as success, failure or pending (`error` is a failure; a word GitHub may
    add later waits as pending, never as a pass)."""
    state = str(state or "")
    if state == "success":
        return "success"
    return "failure" if state in ("failure", "error") else "pending"


def _check_list(statuses: list[dict], runs: list[dict]) -> list[dict]:
    """Every check on a commit as {name, state}, one per commit status and check run, the
    reviewer's own status left out. The mapping is the one `_ci` uses: a run still going is
    pending, `success`, `neutral` and `skipped` pass, any other conclusion (`cancelled`,
    `timed_out`, `action_required`, `startup_failure`, `stale`, `failure`) fails."""
    out = []
    for s in statuses or []:
        if s.get("context") != REVIEW_CONTEXT:
            out.append({"name": str(s.get("context") or ""), "state": _status_state(s.get("state"))})
    for r in runs or []:
        if r.get("status") != "completed":
            state = "pending"
        elif r.get("conclusion") in ("success", "neutral", "skipped"):
            state = "success"
        else:
            state = "failure"
        out.append({"name": str(r.get("name") or ""), "state": state})
    return out


async def _tokens(store, repo: str) -> list[tuple[str, str]]:
    """[(token, API base)] for every credential that can give one; a GitHub Enterprise
    credential uses its own API."""
    from .github_credentials import get_token
    out = []
    for c in store.list_github_credentials():
        try:
            out.append((await get_token(store, c["name"], repo),
                        (c.get("api_url") or API).rstrip("/")))
        except Exception:
            continue
    return out


async def read_pr(store, repo: str, number: int, prev: dict | None = None) -> dict:
    """The PR as GitHub has it now, merged into what was known (`prev`). Never raises: a read
    that fails says so in `error` and keeps the last known state."""
    out = {**(prev or {}), "repo": repo, "number": number,
           "url": (prev or {}).get("url") or f"https://github.com/{repo}/pull/{number}",
           "checked_at": now_utc().isoformat()}
    tokens = await _tokens(store, repo)
    if not tokens:
        out["error"] = "no GitHub credential on this Tares can read it (Settings, GitHub)"
        return out
    last_err = ""
    for tok, api in tokens:
        headers = {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {tok}",
                   "X-GitHub-Api-Version": "2022-11-28"}
        try:
            async with httpx.AsyncClient(timeout=8, transport=_transport, headers=headers) as cx:
                r = await cx.get(f"{api}/repos/{repo}/pulls/{number}")
                if r.status_code in (401, 403, 404):
                    limited = r.status_code == 403 and r.headers.get("x-ratelimit-remaining") == "0"
                    last_err = (f"GitHub's rate limit is spent; trying {repo}#{number} again later"
                                if limited else f"GitHub answered {r.status_code} for {repo}#{number}")
                    continue
                r.raise_for_status()
                pr = r.json()
                head = (pr.get("head") or {}).get("sha") or ""
                statuses, check_runs = [], []
                try:   # CI and the verdict are extra: the PR itself is already read
                    st = await cx.get(f"{api}/repos/{repo}/commits/{head}/status",
                                      params={"per_page": 100})
                    runs = await cx.get(f"{api}/repos/{repo}/commits/{head}/check-runs",
                                        params={"per_page": 100})
                    statuses = (st.json().get("statuses") or []) if st.status_code == 200 else []
                    check_runs = (runs.json().get("check_runs") or []) if runs.status_code == 200 else []
                except Exception:
                    pass
            review = next((s for s in statuses if s.get("context") == REVIEW_CONTEXT), None)
            out.update({
                "title": pr.get("title"), "branch": (pr.get("head") or {}).get("ref"),
                "head": head, "state": pr.get("state"), "merged": bool(pr.get("merged")),
                "merge_commit": pr.get("merge_commit_sha") if pr.get("merged") else None,
                "merged_at": pr.get("merged_at"), "ci": _ci(statuses, check_runs),
                "ci_checks": _check_list(statuses, check_runs),
                # what the change was, for the project's GitHub page: the same read, no extra call
                "created_at": pr.get("created_at"), "closed_at": pr.get("closed_at"),
                "author": (pr.get("user") or {}).get("login"),
                "merged_by": (pr.get("merged_by") or {}).get("login"),
                "base": (pr.get("base") or {}).get("ref"), "draft": bool(pr.get("draft")),
                "commits": pr.get("commits"), "changed_files": pr.get("changed_files"),
                "additions": pr.get("additions"), "deletions": pr.get("deletions"),
                # an `error` status is the reviewer failing to run, not a verdict
                "verdict": {"success": "pass", "failure": "changes",
                            "pending": "pending"}.get(review.get("state")) if review else None,
                "verdict_head": head if review else None,
                "verdict_text": review.get("description") if review else None,
                "url": pr.get("html_url") or out["url"], "error": None})
            return out
        except Exception as e:   # one credential failing is not the end
            last_err = f"could not read {repo}#{number}: {type(e).__name__}"
    out["error"] = last_err or "could not read it"
    return out


# (repo, ref) -> whether the commit has workflow files; a commit's files never change, so an
# answer is kept (the oldest goes first once the dict is full)
_workflows: dict[tuple[str, str], bool] = {}
_WORKFLOWS_KEPT = 256


async def has_workflows(store, repo: str, ref: str) -> bool | None:
    """Whether `repo` has a GitHub Actions workflow file at `ref` (a commit): True for at least
    one file in `.github/workflows`, False for an empty or missing folder, None when GitHub
    gave no answer (no credential, rate limit, network). Only a True or False is remembered."""
    repo, ref = str(repo or "").strip(), str(ref or "").strip()
    if not repo or not ref:
        return None
    key = (repo, ref)
    if key in _workflows:
        return _workflows[key]
    found: bool | None = None
    for tok, api in await _tokens(store, repo):
        headers = {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {tok}",
                   "X-GitHub-Api-Version": "2022-11-28"}
        try:
            async with httpx.AsyncClient(timeout=8, transport=_transport, headers=headers) as cx:
                r = await cx.get(f"{api}/repos/{repo}/contents/.github/workflows",
                                 params={"ref": ref})
            if r.status_code == 404:
                found = False   # or this credential cannot see the repo: another may know better
                continue
            if r.status_code != 200:
                continue
            body = r.json()
        except Exception:
            continue
        if not isinstance(body, list):
            continue            # the path is a file, not a folder: nothing to say
        if any(isinstance(f, dict) and f.get("type", "file") == "file" for f in body):
            found = True
            break
        found = False
    if found is not None:
        if len(_workflows) >= _WORKFLOWS_KEPT:
            _workflows.pop(next(iter(_workflows)))
        _workflows[key] = found
    return found


def branch_ref(branch: str) -> str | None:
    """The ticket ref in a factory branch name: `factory/T3` -> T3, `factory/ENG-12-x` -> ENG-12."""
    m = re.match(r"^factory/([A-Za-z]+-\d+|[Tt]\d+)\b", str(branch or ""))
    return m.group(1) if m else None
