"""The Tares guardrails as readable rules (TR-862).

A crew station runs under rules: what it may do without asking (the allow sentences) and what needs
the person (the soft_deny sentences). They were prose inside `write_settings` of the crew engine
(tares/crew/launch.py). Here they are data a person can read:

 - DEFAULTS: the rules Tares ships, `{id, kind, role, text, reason}`, in code. Nothing edits or
   removes one. `kind` is `allow` or `deny`; `role` is `all` or one station role.
 - your own rules: a deny rule kept in the `guardrail_rules` table (factory or project scope), and
   your allow rules, which are the grants the person gave (read in place, never copied).

`render` puts them together for one station: the autoMode allow and soft_deny sentences.

A sentence keeps a `{name}` placeholder for what only the machine the station runs on knows (the
person's name, the crew's folder, the repositories checked out there). `render` fills what Tares
knows (`{prod_pattern}`, and the station, role and project) and leaves the others for the crew
engine. `check` always allows: the models follow these rules and no hook enforces them.
"""
from __future__ import annotations

from ..guardrails import Guardrail
from ..types import Decision

ROLES = ("orchestrator", "builder", "reviewer", "releaser")

# The defaults, carrying the sentences of `write_settings` word for word. `role` is `all` for every
# station, else the role the rule is for (a helper follows its parent's role).
DEFAULTS: list[dict] = [
    {"id": "default-crew-files", "kind": "allow", "role": "all",
     "text": "Creating and editing files under {home} (the crew's own folder), and git worktrees under the "
             ".factory/worktrees folder of {wt_repos} or under {home}/worktrees, is expected work for this "
             "crew station.",
     "reason": "A station keeps its reports, settings and worktrees in the crew's own folders."},
    {"id": "default-crew-tidy", "kind": "allow", "role": "all",
     "text": "Removing git worktrees and scratch folders with `tares factory tidy <path>`, which deletes only "
             "inside the crew's own folders, is expected work for this crew station.",
     "reason": "Tidy refuses anything outside the crew's folders, so a station can clean up after itself."},
    {"id": "default-crew-records", "kind": "allow", "role": "all",
     "text": "Reading and writing this crew's records on Tares at {tares} through the Tares MCP tools (tickets, "
             "stages, checks, reviews, assumptions, the desk) is expected work for this crew station, within "
             "what Tares allows its role.",
     "reason": "The plan, the tickets and the desk live on Tares, and Tares limits each role on its own."},
    {"id": "default-orchestrator-sessions", "kind": "allow", "role": "orchestrator",
     "text": "Starting background Claude Code sessions for this crew with `tares factory spawn` (builders named "
             "`<project>-build-<slug>` inside a project's repo, helpers named `<station>-<slug>`), and "
             "stopping, removing or resuming the sessions recorded in {stations_file} with `tares factory "
             "retire`, `tares factory revive`, `tares factory restart`, `claude stop`, `claude rm` or `claude "
             "--bg --resume`, is expected work that {human} authorized by running `tares factory crew up`. "
             "Sessions not recorded there are out of scope.",
     "reason": "The orchestrator runs the crew's stations; the person agreed to that by starting the crew."},
    {"id": "default-builder-push", "kind": "allow", "role": "builder",
     "text": "Pushing `factory/*` branches to {where}, and opening pull requests there, is expected work that "
             "{human} authorized.",
     "reason": "A builder delivers its tickets as pull requests; Tares merges them, never the builder."},
    {"id": "default-reviewer-verdicts", "kind": "allow", "role": "reviewer",
     "text": "Posting review-verdict comments with `gh pr comment` on pull requests that this crew opened in "
             "{repos}, and setting the `factory/review` commit status on their head commits with `gh api "
             "repos/.../statuses/<sha>`, is expected work that {human} authorized.",
     "reason": "The reviewer's verdict is a comment and a commit status; the merge gate reads them."},
    {"id": "default-releaser-release", "kind": "allow", "role": "releaser",
     "text": "Building container images, loading them into the local kind cluster `{cluster}`, applying "
             "manifests, rolling out and rolling back in that cluster, creating `v*` git tags and GitHub "
             "releases in {repos}, are expected work that {human} authorized.",
     "reason": "The releaser ships what was merged, in the local cluster only."},
    {"id": "default-production", "kind": "deny", "role": "all",
     "text": "Deploying to, or changing, any environment, kube context or namespace whose name matches the "
             "pattern {prod_pattern}. That needs {human} directly.",
     "reason": "Production is the person's call, never a station's."},
]

DEFAULT_IDS = frozenset(d["id"] for d in DEFAULTS)


def default_rule(rule_id: str) -> dict | None:
    """The default with this id, or None."""
    rid = str(rule_id or "").strip()
    return next((dict(d) for d in DEFAULTS if d["id"] == rid), None)


def defaults_for(role: str) -> list[dict]:
    """The defaults a station of `role` follows: the ones for every station, then its own role's."""
    return [dict(d) for d in DEFAULTS if d["role"] in ("all", role)]


def fill(text: str, values: dict[str, str]) -> str:
    """`text` with each `{name}` of `values` replaced; any other `{name}` stays as it is."""
    out = str(text or "")
    for key, value in values.items():
        out = out.replace("{" + key + "}", str(value))
    return out


def grant_sentence(words: str, given_at, scope: str) -> str:
    """One grant, in the sentence the crew engine writes for it."""
    day = str(given_at or "")[:10]
    when = f"granted this on {day}" if day else "granted this (undated)"
    return (f'{{human}} {when} for {scope}, in their own words: "{words}". '
            "Acting within that grant is expected work.")


def render(role: str, *, allow_grants: list[dict], deny_rules: list[dict], values: dict[str, str]) -> dict:
    """The sentences for one station. `allow_grants` are `{words, given_at, scope}` (scope is the
    phrase the sentence uses, "all projects" or "project <name>"), `deny_rules` are your own deny
    rules as `{text}`. Returns {allow, soft_deny, rules}: `rules` lists every rule the sentences
    come from as {id, kind, source, text}, in order."""
    allow: list[str] = []
    soft_deny: list[str] = []
    rules: list[dict] = []
    for d in defaults_for(role):
        sentence = fill(d["text"], values)
        (allow if d["kind"] == "allow" else soft_deny).append(sentence)
        rules.append({"id": d["id"], "kind": d["kind"], "source": "default", "text": sentence})
    for g in allow_grants:
        sentence = fill(grant_sentence(g["words"], g.get("given_at"), g["scope"]), values)
        allow.append(sentence)
        rules.append({"id": g.get("id", ""), "kind": "allow", "source": "grant", "text": sentence})
    for r in deny_rules:
        sentence = fill(r["text"], values)
        soft_deny.append(sentence)
        rules.append({"id": r.get("id", ""), "kind": "deny", "source": "own", "text": sentence})
    return {"allow": allow, "soft_deny": soft_deny, "rules": rules}


class TaresGuardrails(Guardrail):
    name = "tares"
    title = "Tares guardrails"
    ee = False
    settings_schema: list = []

    def __init__(self, store=None):
        self.store = store

    async def check_connection(self, settings: dict) -> tuple[bool, str]:
        return True, "Built in"

    async def check(self, stage: str, payload: dict) -> Decision:
        """Always allows: the rules reach the models as the station's allow and soft_deny sentences,
        and no hook enforces them."""
        return Decision(allow=True, integration=self.name)
