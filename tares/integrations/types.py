"""Shared types for integrations: the one PR reference, verdicts, CI state, decisions, launch
specs, ticket references, and the form fields an integration declares for its settings."""
from __future__ import annotations

import enum
from dataclasses import dataclass, field

FIELD_KINDS = ("text", "secret", "url", "select", "bool", "mapping")
VERDICT_STATES = ("approved", "changes", "pending", "stale", "none")
CI_STATES = ("success", "failure", "pending", "none")


@dataclass
class Field:
    """One setting an integration asks for. `kind` is one of FIELD_KINDS. A `select` field lists
    its choices in `options`. A `mapping` field holds a dict from fixed keys (listed in
    `options`) to a text value, for example the statuses of a Jira workflow."""
    name: str
    label: str
    kind: str = "text"
    required: bool = False
    help: str = ""
    options: list[str] = field(default_factory=list)


@dataclass
class PRRef:
    """The one pull request reference every review, CI, guardrail and merge function takes.
    `repo` is `owner/name`, `head` is the commit SHA, `base` is the branch the PR merges into."""
    repo: str
    number: int
    head: str
    base: str = ""
    url: str = ""
    ticket: str = ""
    project_uid: str = ""


@dataclass
class Verdict:
    """A review gate's answer for one head. `state` is one of VERDICT_STATES; `stale` means the
    verdict was for an older head. `findings` is a list of plain sentences."""
    state: str
    head: str = ""
    by: str = ""
    summary: str = ""
    findings: list[str] = field(default_factory=list)
    url: str = ""


@dataclass
class CIState:
    """A CI source's answer for one head. `state` is one of CI_STATES; `failing` names the
    failing required checks."""
    state: str
    head: str = ""
    failing: list[str] = field(default_factory=list)
    url: str = ""


@dataclass
class Decision:
    """A guardrail's answer. `allow` False blocks the action with `reason`; with `warn_only`
    set the reason is shown and the action goes ahead. `integration` names who decided."""
    allow: bool
    reason: str = ""
    integration: str = ""
    warn_only: bool = False


@dataclass
class LaunchSpec:
    """What a runtime needs to start a station: the `command` (argument list), the environment
    `env`, the integration `settings` it was built from (never secrets in plain text), and the
    `placeholders` the caller fills in at launch."""
    command: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    settings: dict = field(default_factory=dict)
    placeholders: dict[str, str] = field(default_factory=dict)


@dataclass
class TicketRef:
    """A ticket in a ticket system: its id there, its url and its status."""
    id: str
    url: str = ""
    status: str = ""


class Status(str, enum.Enum):
    """The four stages every ticket system maps onto."""
    todo = "todo"
    in_progress = "in_progress"
    in_review = "in_review"
    done = "done"
