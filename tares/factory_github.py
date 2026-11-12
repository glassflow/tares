"""A factory ticket's pull request as GitHub has it (TR-410): head, CI, the reviewer's verdict (the
`factory/review` commit status, or a check run of that name, whoever posted it), merged or not. A ticket's PR state is a fact Tares reads from
GitHub, not a station's claim.

Tares reads it with the cell's own GitHub credentials (Settings, GitHub), trying each until one
can see the repo. No credential, or none that sees it: the PR keeps its link and says why it could
not be read. Reads are cached on the ticket for READ_EVERY seconds.
"""
from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import quote

import httpx

from .envelope import now_utc

READ_EVERY = 60
REVIEW_CONTEXT = "factory/review"
API = "https://api.github.com"

_PR_URL = re.compile(r"github\.com/([^/\s]+/[^/\s]+)/pull/(\d+)")

# swappable in tests: (method, url, token) -> (status, json)
_transport: httpx.AsyncBaseTransport | None = None

# API base -> the calls GitHub says are left in the current window, from the last response that
# carried the header (TR-856). The merge queue stands down while any value is below RATE_FLOOR.
rate_remaining: dict[str, int] = {}
RATE_FLOOR = 100


def note_rate(api: str, resp) -> None:
    """Remember `x-ratelimit-remaining` of a response; a response without the header, or with a
    value that is not a number, changes nothing."""
    try:
        raw = resp.headers.get("x-ratelimit-remaining")
        if raw is not None and str(raw).strip() != "":
            rate_remaining[api] = int(str(raw).strip())
    except Exception:
        pass


def rate_low() -> dict[str, int]:
    """The API bases whose remaining calls are below RATE_FLOOR."""
    return {a: n for a, n in rate_remaining.items() if n < RATE_FLOOR}


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
        if r.get("name") == REVIEW_CONTEXT:
            continue
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


def _more_than_read(total, got: list) -> bool:
    """GitHub says it holds more entries than the page it sent (`total_count` above what came)."""
    try:
        return int(total) > len(got)
    except (TypeError, ValueError):
        return False


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
        if r.get("name") == REVIEW_CONTEXT:
            continue
        if r.get("status") != "completed":
            state = "pending"
        elif r.get("conclusion") in ("success", "neutral", "skipped"):
            state = "success"
        else:
            state = "failure"
        out.append({"name": str(r.get("name") or ""), "state": state})
    return out


def _review_of(statuses: list[dict], runs: list[dict]) -> dict | None:
    """The reviewer's verdict on a commit as {state, text}, from the `factory/review` commit
    status or the check run of that name, whichever was posted last (the person's `gh` posts the
    first, the cell's GitHub App the second). `state` is success, failure or pending; an `error`
    status is the reviewer failing to run, not a verdict, and a check run that is not finished
    waits as pending."""
    found = []
    for s in statuses or []:
        if s.get("context") == REVIEW_CONTEXT:
            found.append((str(s.get("updated_at") or s.get("created_at") or ""),
                          {"state": s.get("state"), "text": s.get("description")}))
    for r in runs or []:
        if r.get("name") != REVIEW_CONTEXT:
            continue
        if r.get("status") != "completed":
            state = "pending"
        elif r.get("conclusion") == "success":
            state = "success"
        else:
            state = "failure"
        found.append((str(r.get("completed_at") or r.get("started_at") or ""),
                      {"state": state, "text": (r.get("output") or {}).get("title")}))
    if not found:
        return None
    return max(found, key=lambda x: x[0])[1]   # the later one; equal stamps keep the first read


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
                note_rate(api, r)
                if r.status_code in (401, 403, 404):
                    limited = r.status_code == 403 and r.headers.get("x-ratelimit-remaining") == "0"
                    last_err = (f"GitHub's rate limit is spent; trying {repo}#{number} again later"
                                if limited else f"GitHub answered {r.status_code} for {repo}#{number}")
                    continue
                r.raise_for_status()
                pr = r.json()
                head = (pr.get("head") or {}).get("sha") or ""
                statuses, check_runs = [], []
                partial = False   # a CI read that did not give everything (see ci_partial below)
                try:   # CI and the verdict are extra: the PR itself is already read
                    st = await cx.get(f"{api}/repos/{repo}/commits/{head}/status",
                                      params={"per_page": 100})
                    runs = await cx.get(f"{api}/repos/{repo}/commits/{head}/check-runs",
                                        params={"per_page": 100})
                    if st.status_code == 200:
                        body = st.json()
                        statuses = body.get("statuses") or []
                        partial = partial or _more_than_read(body.get("total_count"), statuses)
                    else:
                        partial = True
                    if runs.status_code == 200:
                        body = runs.json()
                        check_runs = body.get("check_runs") or []
                        partial = partial or _more_than_read(body.get("total_count"), check_runs)
                    else:
                        partial = True
                except Exception:
                    partial = True
            review = _review_of(statuses, check_runs)
            out.update({
                "title": pr.get("title"), "branch": (pr.get("head") or {}).get("ref"),
                "head_repo": ((pr.get("head") or {}).get("repo") or {}).get("full_name"),
                "head": head, "state": pr.get("state"), "merged": bool(pr.get("merged")),
                "merge_commit": pr.get("merge_commit_sha") if pr.get("merged") else None,
                "merged_at": pr.get("merged_at"), "ci": _ci(statuses, check_runs),
                "ci_checks": _check_list(statuses, check_runs),
                # True when a status or check-run read failed or GitHub holds more than was read:
                # `ci` and `ci_checks` then show only part of the commit's checks
                "ci_partial": partial,
                # what the change was, for the project's GitHub page: the same read, no extra call
                "created_at": pr.get("created_at"), "closed_at": pr.get("closed_at"),
                "author": (pr.get("user") or {}).get("login"),
                "merged_by": (pr.get("merged_by") or {}).get("login"),
                "base": (pr.get("base") or {}).get("ref"), "draft": bool(pr.get("draft")),
                # TR-858: whether GitHub says the PR merges (True, False, or None while it works
                # it out) and why not (`dirty` is a conflict, `behind` is only out of date)
                "mergeable": pr.get("mergeable"), "mergeable_state": pr.get("mergeable_state"),
                "commits": pr.get("commits"), "changed_files": pr.get("changed_files"),
                "additions": pr.get("additions"), "deletions": pr.get("deletions"),
                # an `error` status is the reviewer failing to run, not a verdict
                "verdict": {"success": "pass", "failure": "changes",
                            "pending": "pending"}.get(review["state"]) if review else None,
                "verdict_head": head if review else None,
                "verdict_text": review["text"] if review else None,
                "url": pr.get("html_url") or out["url"], "error": None})
            return out
        except Exception as e:   # one credential failing is not the end
            last_err = f"could not read {repo}#{number}: {type(e).__name__}"
    out["error"] = last_err or "could not read it"
    return out


def _headers(tok: str) -> dict:
    return {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {tok}",
            "X-GitHub-Api-Version": "2022-11-28"}


async def merge_pr(store, repo: str, number: int, sha: str, method: str = "squash") -> dict:
    """Merge the pull request through GitHub, trying each credential in turn. `sha` is the head
    the caller evaluated: GitHub refuses (409) when the branch has moved on. Never raises.

    Returns {ok, status, message, no_write, rate_limited}: `ok` is a 200; `status` and `message`
    are GitHub's last answer (405, 409 and 422 end the search: the pull request itself is the
    problem, not the credential); `no_write` is true when no credential was allowed to (every
    answer was 401, 403 or 404, or there was no credential) and none of them was a spent rate
    limit; `rate_limited` is true when one was."""
    out = {"ok": False, "status": 0, "message": "", "no_write": False, "rate_limited": False}
    tokens = await _tokens(store, repo)
    refused = 0
    for tok, api in tokens:
        try:
            async with httpx.AsyncClient(timeout=15, transport=_transport,
                                         headers=_headers(tok)) as cx:
                r = await cx.put(f"{api}/repos/{repo}/pulls/{number}/merge",
                                 json={"merge_method": method, "sha": sha})
        except Exception as e:
            out["message"] = f"could not reach GitHub: {type(e).__name__}"
            continue
        note_rate(api, r)
        out["status"] = r.status_code
        try:
            body = r.json()
        except Exception:
            body = {}
        msg = str(body.get("message") or "") if isinstance(body, dict) else ""
        if r.status_code == 200:
            out.update(ok=True, message=msg, no_write=False, rate_limited=False)
            return out
        if r.status_code in (405, 409, 422):
            out.update(message=msg or f"GitHub answered {r.status_code}", no_write=False,
                       rate_limited=False)
            return out
        if r.status_code in (401, 403, 404, 429):
            if r.status_code in (403, 429) and (
                    r.headers.get("x-ratelimit-remaining") == "0" or r.headers.get("retry-after")
                    or "rate limit" in msg.lower()):
                out["rate_limited"] = True      # a spent limit, not a credential that cannot write
                out["message"] = msg or "GitHub's rate limit is spent"
                continue
            refused += 1
            out["message"] = msg or f"GitHub answered {r.status_code}"
            continue
        out["message"] = msg or f"GitHub answered {r.status_code}"
    out["no_write"] = (not tokens or refused == len(tokens)) and not out["rate_limited"]
    if not tokens:
        out["message"] = "no GitHub credential on this Tares"
    return out


REVIEW_TITLE_MAX = 100     # GitHub shows a check run's title in one line; the status text had this too
_VERDICT_CONCLUSION = {"pass": "success", "changes": "failure", "block": "failure"}
_VERDICT_WORDS = {"pass": "Pass", "changes": "Changes requested", "block": "Blocked"}


def review_comment(verdict: str, by: str, person: str, verified: str, not_verified: str,
                   findings: list[dict], head: str) -> str:
    """The PR comment for one review: who reviewed (named in text, no @-mention), the verdict,
    what was verified and what was not, and the findings with the blocking ones first."""
    who = f"{by} ({person}'s crew)" if person else by
    lines = [f"**{_VERDICT_WORDS.get(verdict, verdict)}** on `{str(head)[:7]}`", "",
             f"Reviewed by {who}.", ""]
    if verified:
        lines += ["**Verified**", "", str(verified).strip(), ""]
    if not_verified:
        lines += ["**Not verified**", "", str(not_verified).strip(), ""]
    rows = sorted(findings or [], key=lambda f: (not f.get("blocking"), str(f.get("severity") or "")))
    if rows:
        lines += ["**Findings**", ""]
        for i, f in enumerate(rows, 1):
            where = str(f.get("file") or "").strip()
            if where and f.get("line"):
                where += f":{f['line']}"
            tag = ", ".join(x for x in (str(f.get("severity") or "").strip(),
                                        "blocking" if f.get("blocking") else "", where) if x)
            lines.append(f"{i}. {str(f.get('text') or '').strip()}" + (f" ({tag})" if tag else ""))
        lines.append("")
    else:
        lines += ["No findings.", ""]
    return "\n".join(lines).rstrip() + "\n"


async def post_review(store, repo: str, number: int, head: str, verdict: str, comment: str,
                      title: str) -> dict:
    """Post one review to the pull request as the cell's GitHub App: the check run `factory/review`
    on `head` (the gate reads it like the commit status) and one comment. Only an App credential
    posts; a personal token is the person's own login and stays with the reviewer's `gh`. Never
    raises.

    Returns {posted_check, posted_comment, posted, why}: `posted` is true when both went up. When
    no App credential can write to the repo nothing is posted and `why` says so; when only one of
    the two went up `why` names the other, so the reviewer posts just that one with `gh`."""
    out = {"posted_check": False, "posted_comment": False, "posted": False, "why": ""}
    from .github_credentials import get_token, is_app
    creds = [c for c in store.list_github_credentials() if is_app(c)]
    if not creds:
        out["why"] = "no GitHub App credential on this Tares (Settings, GitHub)"
        return out
    conclusion = _VERDICT_CONCLUSION.get(verdict, "failure")
    refused = ""
    for c in creds:
        try:
            tok = await get_token(store, c["name"], repo)
        except Exception as e:
            refused = f"the App {c['name']!r} has no token for {repo}: {e}"
            continue
        api = (c.get("api_url") or API).rstrip("/")
        try:
            async with httpx.AsyncClient(timeout=15, transport=_transport,
                                         headers=_headers(tok)) as cx:
                r = await cx.post(f"{api}/repos/{repo}/check-runs", json={
                    "name": REVIEW_CONTEXT, "head_sha": head, "status": "completed",
                    "conclusion": conclusion,
                    "output": {"title": str(title)[:REVIEW_TITLE_MAX], "summary": comment[:60000]}})
                note_rate(api, r)
                if r.status_code not in (200, 201):
                    refused = (f"GitHub answered {r.status_code} to the App {c['name']!r} "
                               f"for the check run on {repo}")
                    continue
                out["posted_check"] = True
                r = await cx.post(f"{api}/repos/{repo}/issues/{number}/comments",
                                  json={"body": comment})
                note_rate(api, r)
                out["posted_comment"] = r.status_code in (200, 201)
        except Exception as e:
            refused = f"could not reach GitHub: {type(e).__name__}"
            if not out["posted_check"]:
                continue
        break
    out["posted"] = out["posted_check"] and out["posted_comment"]
    if out["posted"]:
        return out
    if out["posted_check"]:
        out["why"] = "the check run went up but the comment did not: post the comment with gh"
    elif out["posted_comment"]:
        out["why"] = "the comment went up but the check run did not: post the status with gh"
    else:
        out["why"] = refused or "no App credential could post to the repo"
    return out


async def delete_branch(store, repo: str, branch: str) -> bool:
    """Delete the merged pull request's branch. Best effort: a 422 (already gone, or protected)
    and every failure are ignored. True when GitHub deleted it."""
    if not branch:
        return False
    for tok, api in await _tokens(store, repo):
        try:
            async with httpx.AsyncClient(timeout=8, transport=_transport,
                                         headers=_headers(tok)) as cx:
                r = await cx.delete(f"{api}/repos/{repo}/git/refs/heads/{branch}")
        except Exception:
            continue
        note_rate(api, r)
        if r.status_code == 204:
            return True
        if r.status_code in (404, 422):
            return False
    return False


def _enc(part: str) -> str:
    return quote(str(part or ""), safe="")


def _int(v) -> int | None:
    try:
        return None if isinstance(v, bool) or v is None else int(v)
    except (TypeError, ValueError):
        return None


async def compare(store, repo: str, base: str, head: str) -> dict:
    """How `head` stands against `base`: GET /repos/{repo}/compare/{base}...{head}. Returns
    {ok, behind_by, ahead_by, files, error}: `behind_by` is how many commits `base` has that the
    head lacks (a branch that needs updating has behind_by above 0). `ok` is false, with `error`
    saying why, when GitHub gave no usable answer (no credential, rate limit, network, a body with
    no counts); `files` is the compare's files list (each {filename, status, patch}, `patch`
    missing for a binary or very large file) and [] when there is none. Never raises."""
    out = {"ok": False, "behind_by": None, "ahead_by": None, "files": [], "error": ""}
    if not str(base or "").strip() or not str(head or "").strip():
        out["error"] = "no base or head to compare"
        return out
    tokens = await _tokens(store, repo)
    if not tokens:
        out["error"] = "no GitHub credential on this Tares can read it (Settings, GitHub)"
        return out
    for tok, api in tokens:
        try:
            async with httpx.AsyncClient(timeout=15, transport=_transport,
                                         headers=_headers(tok)) as cx:
                r = await cx.get(f"{api}/repos/{repo}/compare/{_enc(base)}...{_enc(head)}")
        except Exception as e:
            out["error"] = f"could not reach GitHub: {type(e).__name__}"
            continue
        note_rate(api, r)
        if r.status_code != 200:
            out["error"] = f"GitHub answered {r.status_code} for the compare"
            continue
        try:
            body = r.json()
        except Exception:
            body = None
        if not isinstance(body, dict):
            out["error"] = "GitHub's compare answer could not be read"
            continue
        behind, ahead = _int(body.get("behind_by")), _int(body.get("ahead_by"))
        if behind is None:
            out["error"] = "GitHub's compare answer has no behind_by"
            continue
        files = body.get("files")
        out.update(ok=True, behind_by=behind, ahead_by=ahead,
                   files=[f for f in files if isinstance(f, dict)] if isinstance(files, list) else [],
                   error="")
        return out
    return out


async def update_branch(store, repo: str, number: int, expected_head: str) -> dict:
    """Merge the base branch into the pull request's branch:
    PUT /repos/{repo}/pulls/{n}/update-branch with `expected_head_sha`. Returns {ok, status,
    message, conflict, no_write, rate_limited}: `ok` is a 200 or 202; `conflict` is true for a 422 whose
    message mentions a conflict (the branch cannot be updated without a person); any other 422
    (the head moved, say) is not a conflict. Never raises."""
    out = {"ok": False, "status": 0, "message": "", "conflict": False, "no_write": False,
           "rate_limited": False}
    tokens = await _tokens(store, repo)
    refused = 0
    for tok, api in tokens:
        try:
            async with httpx.AsyncClient(timeout=15, transport=_transport,
                                         headers=_headers(tok)) as cx:
                r = await cx.put(f"{api}/repos/{repo}/pulls/{number}/update-branch",
                                 json={"expected_head_sha": expected_head})
        except Exception as e:
            out["message"] = f"could not reach GitHub: {type(e).__name__}"
            continue
        note_rate(api, r)
        out["status"] = r.status_code
        try:
            body = r.json()
        except Exception:
            body = {}
        msg = str(body.get("message") or "") if isinstance(body, dict) else ""
        if r.status_code in (200, 202):
            out.update(ok=True, message=msg, conflict=False, no_write=False, rate_limited=False)
            return out
        if r.status_code in (409, 422):
            out.update(message=msg or f"GitHub answered {r.status_code}",
                       conflict="conflict" in msg.lower(), no_write=False, rate_limited=False)
            return out
        if r.status_code in (401, 403, 404, 429):
            if r.status_code in (403, 429) and (
                    r.headers.get("x-ratelimit-remaining") == "0" or r.headers.get("retry-after")
                    or "rate limit" in msg.lower()):
                out["rate_limited"] = True
                out["message"] = msg or "GitHub's rate limit is spent"
                continue
            refused += 1
            out["message"] = msg or f"GitHub answered {r.status_code}"
            continue
        out["message"] = msg or f"GitHub answered {r.status_code}"
    out["no_write"] = bool(tokens) and refused == len(tokens) and not out["rate_limited"]
    if not tokens:
        out["message"] = "no GitHub credential on this Tares"
    return out


PR_FILES_PAGES = 10   # 100 files a page: a PR past 1,000 files is read as far as this


async def pr_files(store, repo: str, number: int) -> list[dict] | None:
    """The files of a pull request (GET /repos/{repo}/pulls/{n}/files, each {filename, status,
    patch}), every page up to PR_FILES_PAGES; [] for a PR with none, None when GitHub gave no
    answer (so a caller can tell "no files" from "could not read"). Never raises."""
    for tok, api in await _tokens(store, repo):
        files: list[dict] = []
        ok = True
        try:
            async with httpx.AsyncClient(timeout=15, transport=_transport,
                                         headers=_headers(tok)) as cx:
                for page in range(1, PR_FILES_PAGES + 1):
                    r = await cx.get(f"{api}/repos/{repo}/pulls/{number}/files",
                                     params={"per_page": 100, "page": page})
                    note_rate(api, r)
                    if r.status_code != 200:
                        ok = False
                        break
                    body = r.json()
                    if not isinstance(body, list):
                        ok = False
                        break
                    files += [f for f in body if isinstance(f, dict)]
                    if len(body) < 100:
                        break
        except Exception:
            ok = False
        if ok:
            return files
    return None


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
    unsure = False   # a credential gave no answer: a 404 from another is not enough to say "none"
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
                unsure = True
                continue
            body = r.json()
        except Exception:
            unsure = True
            continue
        if not isinstance(body, list):
            unsure = True       # the path is a file, not a folder: nothing to say
            continue
        if any(isinstance(f, dict) and f.get("type", "file") == "file" for f in body):
            found = True
            break
        found = False
    if found is False and unsure:
        found = None            # a 404 or an empty folder from one credential, no answer from another
    if found is not None:
        if len(_workflows) >= _WORKFLOWS_KEPT:
            _workflows.pop(next(iter(_workflows)))
        _workflows[key] = found
    return found


def branch_ref(branch: str) -> str | None:
    """The ticket ref in a factory branch name: `factory/T3` -> T3, `factory/ENG-12-x` -> ENG-12."""
    m = re.match(r"^factory/([A-Za-z]+-\d+|[Tt]\d+)\b", str(branch or ""))
    return m.group(1) if m else None
