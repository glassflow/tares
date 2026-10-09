"""Built-in GitHub tools for Tares agents (TR-165).

An agent reads and writes repositories through GitHub's hosted MCP server, authenticated with a
stored GitHub credential (TR-348 showed it accepts a GitHub App's installation token, and writes
then show as the App). One thing that server cannot do is post a check run, which only a GitHub App
may write and which is the best place for an agent's verdict on a pull request: it sits in the PR's
checks, with notes on the lines it is about. That one tool is built in here.

Offered only when the agent names a GitHub credential (`github`) of an App kind (`app`, or
`app_broker` on Tares Cloud); a personal token cannot write check runs.
"""
from __future__ import annotations

import httpx

CHECK_RUN = "github_create_check_run"
CONCLUSIONS = ("success", "failure", "neutral", "action_required")
MAX_ANNOTATIONS = 50       # GitHub's limit per request

CHECK_RUN_DEF = {
    "name": CHECK_RUN,
    "description": (
        "Post your verdict on a pull request as a GitHub check run, shown in the PR's checks with "
        "notes on the lines it is about. Use the PR's head commit sha. `conclusion`: success "
        "(nothing to fix), failure (must be fixed), neutral (worth a look), action_required. "
        "Each annotation names a file path and line from the diff. The check is named after "
        "you; only repositories your GitHub credential covers. Returns the check run's URL."),
    "input_schema": {
        "type": "object",
        "properties": {
            "repo": {"type": "string", "description": "owner/name"},
            "sha": {"type": "string", "description": "the commit the check is about (PR head sha)"},
            "conclusion": {"type": "string", "enum": list(CONCLUSIONS)},
            "title": {"type": "string", "description": "one line shown next to the check"},
            "summary": {"type": "string", "description": "markdown body: the verdict and why"},
            "annotations": {"type": "array", "items": {"type": "object", "properties": {
                "path": {"type": "string"}, "start_line": {"type": "integer"},
                "end_line": {"type": "integer"},
                "level": {"type": "string", "enum": ["notice", "warning", "failure"]},
                "message": {"type": "string"}},
                "required": ["path", "start_line", "message"]}},
        },
        "required": ["repo", "sha", "conclusion", "summary"],
    },
}


def offered(store, agent: dict) -> bool:
    """The agent may post check runs: it names a GitHub credential, and that credential is an App."""
    name = (agent or {}).get("github")
    if not name:
        return False
    from .github_credentials import is_app
    return is_app(store.get_github_credential(name))


async def create_check_run(store, agent: dict, args: dict) -> str:
    """POST /repos/{repo}/check-runs with the agent's App credential. Raises ValueError with
    GitHub's reason (the model reads it and can fix the call)."""
    from .github_credentials import get_token, resolve_api_url
    a = args or {}
    repo = str(a.get("repo") or "").strip()
    sha = str(a.get("sha") or "").strip()
    conclusion = str(a.get("conclusion") or "").strip().lower()
    if "/" not in repo or not sha:
        raise ValueError("repo (owner/name) and sha are required")
    if conclusion not in CONCLUSIONS:
        raise ValueError(f"conclusion must be one of {', '.join(CONCLUSIONS)}")
    # Only a repository this credential covers (what the person connected), never one a prompt
    # in a pull request names: an injected "post success on acme/other" stays an error.
    from .github_credentials import list_repos_for
    cred_row = store.get_github_credential(agent.get("github") or "")
    if cred_row is None:
        raise ValueError("the agent's GitHub credential is gone (Settings > GitHub)")
    covered = {r["full_name"].lower() for r in await list_repos_for(cred_row)}
    if repo.lower() not in covered:
        raise ValueError(f"{repo} is not a repository this agent's GitHub credential covers")
    annotations = []
    for n in (a.get("annotations") or [])[:MAX_ANNOTATIONS]:
        if not isinstance(n, dict) or not n.get("path") or not n.get("message"):
            continue
        try:
            start = max(1, int(n.get("start_line") or 1))
            end = max(start, int(n.get("end_line") or start))
        except (TypeError, ValueError):
            raise ValueError("annotation start_line and end_line must be line numbers")
        annotations.append({"path": str(n["path"]), "start_line": start, "end_line": end,
                            "annotation_level": n.get("level") if n.get("level") in
                            ("notice", "warning", "failure") else "notice",
                            "message": str(n["message"])[:2000]})
    # named after the agent, never by the model: a check run cannot pose as a required status
    # check of another name (branch protection would read it as green)
    body = {"name": f"Tares: {agent.get('name') or 'agent'}"[:100], "head_sha": sha,
            "status": "completed",
            "conclusion": conclusion,
            "output": {"title": str(a.get("title") or f"Tares: {conclusion}")[:200],
                       "summary": str(a.get("summary") or "")[:60000],
                       **({"annotations": annotations} if annotations else {})}}
    cred = agent.get("github")
    token = await get_token(store, cred, repo)
    api = (resolve_api_url(store, cred) or "https://api.github.com").rstrip("/")
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(f"{api}/repos/{repo}/check-runs", json=body,
                         headers={"Accept": "application/vnd.github+json",
                                  "X-GitHub-Api-Version": "2022-11-28",
                                  "Authorization": f"Bearer {token}"})
    if r.status_code != 201:
        try:
            msg = (r.json() or {}).get("message") or ""
        except ValueError:
            msg = ""
        raise ValueError(f"GitHub returned {r.status_code} for the check run"
                         + (f": {msg}" if msg else "")
                         + ("; the App needs Checks: write" if r.status_code == 403 else ""))
    out = r.json() or {}
    return (f"check run posted: {out.get('html_url', '')} "
            f"({conclusion}, {len(annotations)} annotation(s))")
