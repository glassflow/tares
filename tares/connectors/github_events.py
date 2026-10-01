"""The GitHub event contract: what a GitHub event looks like on a Tares timeline, whichever way it
arrived (the App's webhooks, `github_app`, or a personal token's polling, `github`).

Both connectors build events through these functions, so a pull request merged by webhook and the
same merge seen by polling carry the same event type, key, labels and text. A trigger written
against a token source keeps working when the person switches to the App.

* event_type: the GitHub event name (`pull_request`, `push`, `issues`, `issue_comment`,
  `pull_request_review`, `pull_request_review_comment`, `release`, `workflow_run`); polling adds
  `commit` for a single commit.
* key: pull request and issue events key `owner/repo#<number>` (GitHub numbers PRs and issues from
  one sequence); everything else keys `owner/repo`.
* labels: `repo`, `action` (a closed PR that merged reads `merged`), `number`, `author`, `branch`,
  `base`, `state`, `draft`, `sha`, `url`, `title`, `conclusion`, `release_tag`, `workflow`,
  `commits`. A label with no value for an event is simply absent.
"""
from __future__ import annotations

from datetime import datetime

from ..envelope import now_utc

STORED_EVENTS = ("pull_request", "pull_request_review", "pull_request_review_comment", "push",
                 "issues", "issue_comment", "release", "workflow_run")

# The labels the contract can set, for the console's pickers and the planner (PROVIDES).
CONTRACT_LABELS = [
    {"name": "repo", "help": "owner/name"},
    {"name": "action", "help": "opened | synchronize | closed | merged | reopened | "
                               "ready_for_review | submitted | published | completed | ..."},
    {"name": "number", "help": "pull request or issue number"},
    {"name": "author", "help": "who opened the PR or issue, or pushed (GitHub login)"},
    {"name": "branch", "help": "the PR's head branch, or the pushed branch"},
    {"name": "base", "help": "the branch a PR merges into"},
    {"name": "state", "help": "open | closed"},
    {"name": "draft", "help": "true when the PR is a draft"},
    {"name": "sha", "help": "head commit"},
    {"name": "url", "help": "link to the PR, issue, release or run on GitHub"},
    {"name": "title", "help": "PR or issue title"},
    {"name": "conclusion", "help": "workflow run result: success | failure | cancelled | ..."},
    {"name": "release_tag", "help": "release tag, e.g. v1.7.1"},
    {"name": "workflow", "help": "workflow name"},
    {"name": "commits", "help": "commits in a push"},
]


def parse_time(s) -> datetime | None:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _clean(d: dict) -> dict:
    """Drop absent values; render bools as true/false like the rest of the labels."""
    out = {}
    for k, v in d.items():
        if v is None or v == "":
            continue
        out[k] = ("true" if v else "false") if isinstance(v, bool) else v
    return out


def _login(obj: dict | None) -> str:
    return ((obj or {}).get("login") or "") if isinstance(obj, dict) else ""


def _short(s: str, n: int = 120) -> str:
    s = (s or "").strip().splitlines()[0] if s else ""
    return s if len(s) <= n else s[: n - 1] + "…"


def _ref_branch(ref: str) -> str:
    ref = ref or ""
    return ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref


def pr_fields(pr: dict, repo: str, action: str) -> dict:
    """The labels of a pull request event. `action` is normalized: closed + merged -> merged."""
    if action == "closed" and pr.get("merged"):
        action = "merged"
    head, base = pr.get("head") or {}, pr.get("base") or {}
    return _clean({"repo": repo, "action": action, "number": pr.get("number"),
                   "author": _login(pr.get("user")), "branch": head.get("ref"),
                   "base": base.get("ref"), "state": pr.get("state"),
                   "draft": bool(pr.get("draft")), "sha": head.get("sha"),
                   "url": pr.get("html_url"), "title": pr.get("title")})


def pr_text(f: dict) -> str:
    n, title, who = f.get("number"), _short(f.get("title", ""), 160), f.get("author", "")
    act = f.get("action", "")
    if act == "merged":
        return f"PR #{n} merged into {f.get('base', '')}: {title}"[:300]
    return f"PR #{n} {act.replace('_', ' ')}: {title}" + (f" ({who})" if who else "")


def build(event: str, payload: dict) -> dict | None:
    """One webhook delivery -> `{event_type, key, labels, text, event_time}`; None for an event that
    is not stored (lifecycle events are handled by the connector before this)."""
    p = payload or {}
    repo = ((p.get("repository") or {}).get("full_name")) or ""
    action = p.get("action") or ""
    sender = _login(p.get("sender"))
    labels: dict
    text: str
    when = None
    key = repo or "github"

    if event == "pull_request":
        pr = p.get("pull_request") or {}
        labels = pr_fields(pr, repo, action)
        text = pr_text(labels)
        key = f"{repo}#{pr.get('number')}"
        when = pr.get("updated_at")
    elif event in ("pull_request_review", "pull_request_review_comment"):
        pr = p.get("pull_request") or {}
        labels = pr_fields(pr, repo, action)
        if event == "pull_request_review":
            review = p.get("review") or {}
            state = (review.get("state") or "").lower().replace("_", " ")
            labels["author"] = _login(review.get("user")) or sender
            text = f"review on PR #{pr.get('number')}: {state or action} by {labels['author']}"
            when = review.get("submitted_at")
        else:
            comment = p.get("comment") or {}
            labels["author"] = _login(comment.get("user")) or sender
            text = (f"review comment on PR #{pr.get('number')} by {labels['author']}: "
                    f"{_short(comment.get('body', ''))}")
            when = comment.get("updated_at")
        key = f"{repo}#{pr.get('number')}"
    elif event == "issues":
        issue = p.get("issue") or {}
        labels = _clean({"repo": repo, "action": action, "number": issue.get("number"),
                         "author": _login(issue.get("user")), "state": issue.get("state"),
                         "url": issue.get("html_url"), "title": issue.get("title")})
        text = f"issue #{issue.get('number')} {action}: {_short(issue.get('title', ''), 160)}"
        key = f"{repo}#{issue.get('number')}"
        when = issue.get("updated_at")
    elif event == "issue_comment":
        issue = p.get("issue") or {}
        comment = p.get("comment") or {}
        what = "PR" if issue.get("pull_request") else "issue"
        labels = _clean({"repo": repo, "action": action, "number": issue.get("number"),
                         "author": _login(comment.get("user")) or sender,
                         "state": issue.get("state"), "url": comment.get("html_url"),
                         "title": issue.get("title")})
        text = (f"comment on {what} #{issue.get('number')} by {labels.get('author', '')}: "
                f"{_short(comment.get('body', ''))}")
        key = f"{repo}#{issue.get('number')}"
        when = comment.get("updated_at")
    elif event == "push":
        commits = p.get("commits") or []
        head = p.get("head_commit") or {}
        branch = _ref_branch(p.get("ref", ""))
        who = _login(p.get("pusher")) or (p.get("pusher") or {}).get("name") or sender
        labels = _clean({"repo": repo, "action": "pushed", "author": who, "branch": branch,
                         "sha": p.get("after"), "url": p.get("compare"),
                         "commits": len(commits)})
        n = len(commits)
        text = (f"push {n} commit{'s' if n != 1 else ''} to {branch} by {who}: "
                f"{_short(head.get('message', ''))}")
        when = head.get("timestamp")
    elif event == "release":
        rel = p.get("release") or {}
        labels = _clean({"repo": repo, "action": action, "release_tag": rel.get("tag_name"),
                         "author": _login(rel.get("author")) or sender,
                         "url": rel.get("html_url"), "title": rel.get("name")})
        text = f"release {rel.get('tag_name', '')} {action}"
        when = rel.get("published_at") or rel.get("created_at")
    elif event == "workflow_run":
        run = p.get("workflow_run") or {}
        conclusion = run.get("conclusion") or ""
        labels = _clean({"repo": repo, "action": action, "workflow": run.get("name"),
                         "conclusion": conclusion, "branch": run.get("head_branch"),
                         "sha": run.get("head_sha"), "url": run.get("html_url"),
                         "author": _login(run.get("actor")) or sender})
        status = {"success": "passed", "failure": "failed"}.get(conclusion, conclusion or action)
        text = f"workflow {run.get('name', '')} {status} on {run.get('head_branch', '')}"
        when = run.get("updated_at")
    else:
        labels = _clean({"repo": repo, "action": action, "author": sender})
        text = f"{event} {action}".strip() + (f" in {repo}" if repo else "")

    return {"event_type": event, "key": key, "labels": labels, "text": text[:300],
            "event_time": parse_time(when) or now_utc()}


def pr_poll_events(pr: dict, repo: str, since: datetime | None) -> list[tuple[str, datetime]]:
    """What a polled pull request says happened after `since`, without remembering earlier state:
    `opened` when it was created after `since`, `merged` (else `closed`) when it was closed after.
    A reopen cannot be told apart this way and is not reported. Oldest first."""
    out = []
    created, closed, merged = (parse_time(pr.get("created_at")), parse_time(pr.get("closed_at")),
                               parse_time(pr.get("merged_at")))
    if created and (since is None or created > since):
        out.append(("opened", created))
    if merged and (since is None or merged > since):
        out.append(("merged", merged))
    elif closed and (since is None or closed > since):
        out.append(("closed", closed))
    return out
