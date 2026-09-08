"""Project: shared code context.

A team keeps its shared context (what each service does, its interfaces, configuration, how the
repos depend on each other) in one GitHub repository. This template watches commits across the
team's code repos and, whenever something lands, a Tares agent reads the diff and updates the
context repo so it never goes stale. The context repo is the source of truth; the agent writes
to it through GitHub's hosted MCP server, registered by the template with the same stored credential
the sources use.

Objects one instance owns (all names prefixed `ctx_<slug>_`): one `github` commits source per
source repo, one view keyed by repo, one trigger that fires on any new commit (batched per repo),
one MCP server (GitHub, toolsets repos + pull_requests), one Tares agent subscribed to the trigger.
"""
from __future__ import annotations

import re

from ..github_credentials import CREDENTIAL_PREFIX
from .base import PlannedObject, Template, ProjectError
from .registry import register

GITHUB_MCP_URL = "https://api.githubcopilot.com/mcp/"
MAX_REPOS = 50
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

TRIGGERS = {
    # only this one is offered today; "every merged PR" needs the github_webhook connector and
    # "daily" needs scheduled triggers, so both stay out of PARAMS until they exist
    "every_commit": {"label": "every commit to the branch (batched per repo)",
                     "window": "5m", "cooldown": "5m"},
}
WRITE_MODES = ("pull_request", "commit_to_branch")
LAYOUTS = ("existing", "per_repo")

PROMPT = """You maintain the shared context repository `{context_repo}` (branch `{context_branch}`, \
pages under `{context_dir}`) for the team. Its pages tell teammates and agents working in other repos \
what each repository does and how it is used. Your input is the attached timeline of recent commits \
to `{repo_hint}`; the source repositories are: {source_repos}.

Steps:
1. For each commit in the timeline call `github__get_commit` on its repository and sha to see the \
diff. If the diff comes back truncated, fetch the specific files you need with \
`github__get_file_contents` at that sha instead of guessing.
2. Decide whether the change alters anything a teammate or an agent working in another repo must \
know: public interfaces, APIs and contracts, config and environment variables, data schemas, \
deployment or runtime behaviour, dependencies between repos, conventions. Ignore pure refactors, \
tests, formatting and version bumps unless they change behaviour.
3. {layout_instructions}
4. When writing with `github__create_or_update_file`, pass the complete new file text in `content` \
(plain text, not base64), the file's current `sha` when it exists, `branch`, `path` and a `message`; \
if a call fails, read the error and fix the arguments rather than retrying the same call. \
Rewrite only the sections the change affects. Keep facts that still hold, drop what the diff \
makes false, add what is new. Every claim carries the short sha of the commit it comes from, like \
(abc1234). Keep the page short and factual; no filler.
5. {write_instructions}
6. If nothing needs updating, do not write anything; say why.

Finish with a finding for the timeline: what changed in the context repo and the pull request or \
commit link, or "no update needed" with the reason. Keep the finding under 12 lines. The finding is \
your final message and nothing else: do your reasoning through tool calls, and only write the \
finding once you have decided; never narrate what you are about to check.

Style, everywhere you write (pages, PR titles and bodies, the finding): plain sentences, no em \
dashes; use a comma, a colon or a full stop instead.

{layout_templates}"""

LAYOUT_EXISTING = (
    "The context repo already has its own pages under `{context_dir}` on `{context_branch}` (the "
    "team's layout, not one page per repository). First list that folder with "
    "`github__get_file_contents` on the folder path, read the index or README if there is one, then "
    "read the page or pages that cover what the commit changed. Look first for statements the change "
    "makes false or outdated (a version, a default, a name, a path, a 'still to do' note) and correct "
    "those where they are; only then add anything new. Update those pages in place, keeping "
    "their structure, headings and voice. Create a new page only when nothing existing covers the "
    "topic, name it like its neighbours, and add it to the index if the repo keeps one. Never create "
    "a parallel set of per-repository pages next to the team's own.")
LAYOUT_PER_REPO = (
    "Read `{context_path}<repo-name>.md` (repo-name is the part after the slash) with "
    "`github__get_file_contents` from `{context_repo}` on `{context_branch}`; if it does not exist, "
    "create it from the page template below. Read `{context_path}README.md` (the index) the same "
    "way and keep it listing every repository page.")
TEMPLATES_PER_REPO = """Page template for `{context_path}<repo-name>.md`:
# <repo-name>
Purpose: one paragraph.
## How to run
## Interfaces (APIs, events, CLIs)
## Configuration
## Data
## Depends on / used by
## Recent changes
- <date> <sha> one line

Index template for `{context_path}README.md`: a title, one sentence saying Tares keeps these pages \
current, then one line per repository linking its page.
"""
TEMPLATES_EXISTING = """When you do create a page, match the existing pages in shape; when in doubt, \
short sections with a purpose paragraph, how it is used, configuration, and a recent changes list \
with short shas.
"""

WRITE_PR = ("Write the changed pages with `github__create_or_update_file` on a branch named "
            "`tares/context-<repo-name>-<YYYYMMDD>` in `{context_repo}` (create the branch from "
            "`{context_branch}` with `github__create_branch` if it does not exist; if a pull request "
            "from that branch is already open, add your commit to the same branch and do not open "
            "another). Then open one pull request against `{context_branch}` with "
            "`github__create_pull_request`, titled `context: <repo-name>` and a body listing the "
            "commits (sha and first line) it reflects.")
WRITE_DIRECT = ("Write the changed pages with `github__create_or_update_file` directly on "
                "`{context_branch}` in `{context_repo}`, one commit per page, message "
                "`context: <repo-name> after <sha>`. Do not open a pull request.")


def _slug(text: str, n: int = 24) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", str(text or "").lower()).strip("_")
    return s[:n] or "project"


def _repo_name(repo: str) -> str:
    return repo.split("/", 1)[1]


def _norm_repo(value) -> str:
    r = str(value or "").strip().rstrip("/")
    if "://" in r:
        r = r.split("://", 1)[1]
    parts = r.split("/")
    if parts and "." in parts[0]:
        parts = parts[1:]
    r = "/".join(parts[:2])
    if r.endswith(".git"):
        r = r[:-4]
    if not _REPO_RE.match(r):
        raise ProjectError(f"repository must be owner/name (got {value!r})")
    return r


class SharedCodeContext(Template):
    key = "shared_code_context"
    title = "Shared code context"
    sentence = ("watch my code repos and keep one shared context repo up to date, opening a "
                "pull request when something changes")
    description = ("Keep one repository of shared context current: Tares watches commits across "
                   "your code repos and an agent updates the context repo when something changes.")
    guide = {"label": "Shared code context guide",
             "url": "https://docs.glassflow.ai/tares/guides/shared-code-context"}
    PARAMS = {
        "credential": {"type": "string", "required": True, "label": "GitHub credential",
                       "help": "name of a stored GitHub credential (Settings > GitHub); read on the "
                               "source repos, write on the context repo"},
        "source_repos": {"type": "list", "required": True, "label": "Source repositories",
                         "help": "the repositories whose commits feed the context, as "
                                 "[{repo: owner/name, branch: main}] (branch optional: the repo's "
                                 "default branch); 1 to 50",
                         "item": {"repo": {"type": "string", "required": True},
                                  "branch": {"type": "string"}}},
        "context_repo": {"type": "string", "required": True, "label": "Context repository",
                         "help": "owner/name of the repository the agent keeps up to date; not one "
                                 "of the source repos"},
        "context_branch": {"type": "string", "default": "main", "label": "Context branch",
                           "help": "branch of the context repo pull requests target"},
        "context_path": {"type": "string", "default": "", "label": "Context path",
                         "help": "folder inside the context repo where the pages live; empty or / "
                                 "means the root of the repo"},
        "layout": {"type": "string", "default": "existing", "label": "Page layout",
                   "options": [{"value": "existing",
                                "label": "keep the repo's existing pages and update them in place"},
                               {"value": "per_repo",
                                "label": "one page per source repository plus an index"}],
                   "help": "existing: the agent reads what is there and edits the page that covers "
                           "the change; per_repo: the agent keeps <repo-name>.md pages and a "
                           "README index under the path"},
        "trigger": {"type": "string", "default": "every_commit", "label": "Run when",
                    "options": [{"value": k, "label": v["label"]} for k, v in TRIGGERS.items()],
                    "help": "what wakes the agent"},
        "write_mode": {"type": "string", "default": "pull_request", "label": "Write as",
                       "options": [{"value": "pull_request", "label": "a pull request for review"},
                                   {"value": "commit_to_branch",
                                    "label": "commits straight to the context branch"}],
                       "help": "pull requests keep a human in the loop; commits keep the repo "
                               "current with no review"},
        "bootstrap": {"type": "boolean", "default": True, "label": "First look on start",
                      "help": "when the project starts, run the agent once per source repo over "
                              "the last 7 days of commits so the context repo starts current; turn "
                              "off if it already is or to save model spend"},
        "model": {"type": "string", "default": "", "label": "Model",
                  "help": "model for the agent (empty = the instance default)"},
        "max_rounds": {"type": "number", "default": 12, "label": "Max rounds",
                       "help": "model rounds per run; reading diffs and writing pages needs more "
                               "than the default 6"},
    }

    # ── params ───────────────────────────────────────────────────────────────
    def validate(self, params: dict) -> dict:
        p = super().validate(params)
        p["credential"] = str(p["credential"]).strip()
        raw = p.get("source_repos") or []
        if isinstance(raw, str):
            raw = [line for line in raw.replace(",", "\n").splitlines() if line.strip()]
        repos, seen = [], set()
        for item in raw:
            if isinstance(item, str):
                item = {"repo": item}
            if not isinstance(item, dict):
                raise ProjectError("source_repos entries must be {repo, branch}")
            repo = _norm_repo(item.get("repo"))
            if repo.lower() in seen:
                raise ProjectError(f"repository {repo} is listed twice")
            seen.add(repo.lower())
            repos.append({"repo": repo, "branch": str(item.get("branch") or "").strip()})
        if not repos:
            raise ProjectError("pick at least one source repository")
        if len(repos) > MAX_REPOS:
            raise ProjectError(f"at most {MAX_REPOS} source repositories per project")
        p["source_repos"] = repos
        p["context_repo"] = _norm_repo(p.get("context_repo"))
        if p["context_repo"].lower() in seen:
            raise ProjectError("the context repository cannot also be a source repository")
        p["context_branch"] = str(p.get("context_branch") or "main").strip() or "main"
        path = str(p.get("context_path") or "").strip().strip("/")
        p["context_path"] = (path + "/") if path else ""
        p["bootstrap"] = bool(p.get("bootstrap", True))
        if p.get("layout") not in LAYOUTS:
            raise ProjectError(f"layout must be one of {sorted(LAYOUTS)}")
        if p.get("trigger") not in TRIGGERS:
            raise ProjectError(f"trigger must be one of {sorted(TRIGGERS)}")
        if p.get("write_mode") not in WRITE_MODES:
            raise ProjectError(f"write_mode must be one of {list(WRITE_MODES)}")
        p["model"] = str(p.get("model") or "").strip()
        try:
            rounds = int(p.get("max_rounds") or 12)
        except (TypeError, ValueError):
            raise ProjectError("max_rounds must be a whole number between 1 and 24")
        if not 1 <= rounds <= 24:
            raise ProjectError("max_rounds must be between 1 and 24")
        p["max_rounds"] = rounds
        return p

    def preflight(self, params: dict, store) -> None:
        if store.get_github_credential(params["credential"]) is None:
            raise ProjectError(f"GitHub credential {params['credential']!r} not found; add it "
                               "under Settings > GitHub first")

    # ── plan ─────────────────────────────────────────────────────────────────
    def names(self, params: dict) -> dict:
        s = _slug(params.get("context_repo", ""))
        return {"prefix": f"ctx_{s}_", "view": f"ctx_{s}_repo_activity",
                "trigger": f"ctx_{s}_changes", "mcp": f"ctx_{s}_github",
                "agent": f"ctx_{s}_maintainer"}

    def source_name(self, params: dict, repo: str) -> str:
        return self.names(params)["prefix"] + _slug(repo, 48)

    def plan(self, params: dict) -> list[PlannedObject]:
        n = self.names(params)
        cred = params["credential"]
        objs: list[PlannedObject] = []
        source_names = []
        for item in params["source_repos"]:
            repo = item["repo"]
            name = self.source_name(params, repo)
            source_names.append(name)
            config = {"repo": repo, "credential": cred, "limit": 20,
                      "labels": [{"name": "repo", "field": "repo", "primary": True},
                                 {"name": "author", "field": "author"},
                                 {"name": "branch", "field": "branch"}]}
            if item.get("branch"):
                config["branch"] = item["branch"]
            objs.append(PlannedObject("source", f"source:{repo}", {
                "name": name, "connector": "github", "poll": "60s", "config": config}))
        objs.append(PlannedObject("view", "view", {
            "name": n["view"], "key_field": "repo", "sources": source_names}))
        trig = TRIGGERS[params["trigger"]]
        objs.append(PlannedObject("trigger", "trigger", {
            "name": n["trigger"], "view": n["view"],
            "condition": {"aggregate": "count", "predicate": "> 0", "window": trig["window"],
                          "group_by": ["key_value"]},
            "emit": {"kind": "code_change", "attach_view": True, "context_window": "30m"},
            "cooldown": trig["cooldown"]}))
        objs.append(PlannedObject("mcp_server", "mcp", {
            "name": n["mcp"], "url": GITHUB_MCP_URL, "auth_header": "Authorization",
            "auth_value": CREDENTIAL_PREFIX + cred,
            "headers": {"X-MCP-Toolsets": "repos,pull_requests"}}))
        agent = {"name": n["agent"], "trigger": n["trigger"], "prompt": self.render_prompt(params),
                 "mcp_servers": [n["mcp"]], "max_rounds": params["max_rounds"], "enabled": True}
        if params.get("model"):
            agent["model"] = params["model"]
        objs.append(PlannedObject("agent", "agent", agent))
        return objs

    def render_prompt(self, params: dict) -> str:
        write = WRITE_PR if params["write_mode"] == "pull_request" else WRITE_DIRECT
        fmt = {"context_repo": params["context_repo"], "context_branch": params["context_branch"],
               "context_path": params["context_path"],
               "context_dir": params["context_path"].rstrip("/") or "/"}
        per_repo = params.get("layout", "existing") == "per_repo"
        layout = (LAYOUT_PER_REPO if per_repo else LAYOUT_EXISTING).format(**fmt)
        templates = (TEMPLATES_PER_REPO if per_repo else TEMPLATES_EXISTING).format(**fmt)
        return PROMPT.format(
            repo_hint="one of the source repositories (the timeline says which)",
            source_repos=", ".join(f"`{r['repo']}`" for r in params["source_repos"]),
            write_instructions=write.format(**fmt), layout_instructions=layout,
            layout_templates=templates, **fmt)

    # ── summary (the project page) ──────────────────────────────────────────
    def summary(self, instance: dict, store) -> dict:
        out = super().summary(instance, store)
        params = instance["params"]
        n = self.names(params)
        # decorate the generic runs: the repo it ran for, the PR it opened, the bootstrap marker
        all_runs = store.list_agent_runs(n["agent"], limit=500)
        prs = {u for u in (_pr_link(r.get("finding")) for r in all_runs) if u}
        out["panels"] = [{
            "title": "Context repo",
            "rows": [
                {"label": "repo", "value": params["context_repo"], "mono": True,
                 "url": f"https://github.com/{params['context_repo']}"},
                {"label": "branch", "value": params["context_branch"], "mono": True},
                {"label": "path", "value": params["context_path"] or "/", "mono": True},
                {"label": "pages", "value": "one page per source repo plus an index"
                    if params.get("layout") == "per_repo" else "the repo's existing pages, updated in place"},
                {"label": "writes as", "value": "commits straight to the branch"
                    if params.get("write_mode") == "commit_to_branch" else "pull requests"},
            ]}]
        out["cards"] = [{"label": "pull requests", "value": f"{len(prs)} opened"}]
        return out

    # ── bootstrap: first pages right after Start ─────────────────────────────
    def after_create(self, instance: dict, store, runtime) -> None:
        """Run the agent once per source repo over the last 7 days of commits so the context repo
        gets its first pages now instead of at the next commit. Best effort: the sources need a
        first poll to have anything to read, so this schedules the runs a little later and skips
        repos with no commits yet."""
        if instance["params"].get("bootstrap") is False:
            return
        agents = getattr(getattr(runtime, "dispatcher", None), "agents", None)
        if agents is None or not hasattr(agents, "run_now"):
            return
        n = self.names(instance["params"])
        repos = [r["repo"] for r in instance["params"]["source_repos"]]
        agents.bootstrap(n["agent"], n["trigger"], n["view"], repos, window="7d", limit=20)


_PR_RE = re.compile(r"https://github\.com/[^\s)\]]+/(?:pull|commit)/[^\s)\]]+")


def _pr_link(text: str | None) -> str | None:
    m = _PR_RE.search(text or "")
    return m.group(0) if m else None


register(SharedCodeContext())
