"""Tares agents — the data plane reading its own data.

A Tares agent is a prompt attached to a trigger. It's a real agent (it reasons with an LLM),
configured inside Tares rather than connected over a webhook. When its trigger fires it's handed
the same correlated timeline the dispatch carries, may read a wider window or another entity, and
writes ONE finding back into Tares (plus an optional Slack post). Its own tools are two reads,
`read` and `stats` over its project's sources, both routed through Tares's own read path.

That closed tool set is the current boundary: a Tares agent reads and concludes, it does not act
on the customer's world (restarting, deploying, ticketing) — that's the customer's own agent's job.
The boundary is a property of this kind of agent, not the reason it's called something else.

Everything except the prompt is Tares's decision — model, round budget, token budget, the daily
cap. Making those configurable would turn a data-plane feature into an agent builder.

The dispatcher runs a Tares agent as a *subscriber* to its trigger (an internal subscription,
url = tares://agent/<name>), so it flows through the same dispatch, delivery log, roster and
recent-firings machinery as an external agent. This module is the in-process executor the dispatcher
calls for those subscriptions; it logs the run detail (rounds, finding) alongside the delivery.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import timedelta, timezone
import os
import time
import uuid

import httpx

from . import decision as _decision
from . import schedule as _schedule
from . import metrics
from . import results as _results
from . import skills as _skills
from . import tracing as _tracing
from .config import (FINDINGS_SOURCE, API_BASE, agent_url, parse_duration,
                     trigger_entity_label)
from .envelope import now_utc
from .models import ModelError, Provider, add_usage, empty_usage, tool_call_in_text, tool_message
from .pricing import price_usage
from .reads import resolve_read, resolve_sources_full
from .stats import stats_rows

MODEL = os.getenv("TARES_AGENT_MODEL", "claude-sonnet-4-6")
# The model choices the console offers per agent. The instance default (TARES_AGENT_MODEL) is
# always first; an agent stores "" to mean "follow the instance default", so changing the
# instance default moves every agent that never chose one.
AGENT_MODELS = list(dict.fromkeys(
    [MODEL, "claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5-20251001"]))
# Model-call rounds per run. The default is sized for read timeline + a read or two + a finding;
# an agent that also reaches external MCP servers (a diff, a file, a write) needs more, so its
# default is higher. Either can be overridden per agent (`max_rounds`, 1..24). One extra tools-off
# call is made when the budget runs out, so the real ceiling is max_rounds + 1 model calls.
MAX_ROUNDS = 6
MAX_ROUNDS_WITH_MCP = 12
MAX_ROUNDS_LIMIT = 24
MAX_TOKENS = 2048         # per model call
TOOL_TIMEOUT = 120        # per Anthropic request
# Cost ceiling. A trigger in a hot loop can fire far more often than anyone expects; without a cap
# the first surprise is the bill. Per-agent and per-day, counted from the run log.
# Runs per agent per day (TR-325): the console setting, else TARES_AGENT_DAILY_CAP, else this.
DAILY_RUN_CAP = 50
DAILY_CAP_SETTING = "agent_daily_cap"
DAILY_CAP_ENV = "TARES_AGENT_DAILY_CAP"
DAILY_CAP_MAX = 10000


def daily_cap(store) -> tuple[int, str]:
    """(runs per agent per day, where it came from: console | env | default). Read at each run,
    so a change in the console applies to the next run. A bad stored or env value is skipped."""
    get = getattr(store, "get_setting", None)
    for raw, source in (((get(DAILY_CAP_SETTING) if get else None) or "", "console"),
                        (os.getenv(DAILY_CAP_ENV, ""), "env")):
        try:
            n = int(str(raw).strip())
        except ValueError:
            continue
        if 1 <= n <= DAILY_CAP_MAX:
            return n, source
    return DAILY_RUN_CAP, "default"


def effective_daily_cap(agent: dict, store) -> tuple[int, str]:
    """(runs per rolling 24h this agent is held to, where it came from): the agent's own cap when
    set (a project param such as rius_rca's daily_cap), else the instance-wide daily_cap()."""
    own = agent.get("daily_cap")
    if own:
        return int(own), "agent"
    return daily_cap(store)
MAX_BOOTSTRAP_KEYS = 50    # a project bootstraps at most this many entities in one go
# Handoffs (TR-334): a chain stops after this many handoffs in a row; the next one is recorded
# as a capped run. The cooldown per (from, to, entity) lives in trigger_state under this name.
MAX_HANDOFF_DEPTH = 3
HANDOFF_STOPPED = f"handoff chain stopped at depth {MAX_HANDOFF_DEPTH}"


def handoff_state(from_agent: str, to_agent: str) -> str:
    return f"handoff:{from_agent}->{to_agent}"


def handoff_input(from_agent: str, verdict: str, key: str, label: str | None,
                  finding: str) -> str:
    """What a handed-off run is given: a line naming who handed off, the verdict and the entity,
    then the finding itself."""
    entity = f"{label}={key}" if label else key
    return (f'Handed off by {from_agent} with verdict "{verdict}" on {entity}.\n\n'
            f"{from_agent}'s finding:\n\n{finding}")


def _canonical_tool(name: str, tools: list) -> str | None:
    """The declared tool a model's name refers to: exact, else case-insensitive and trimmed."""
    raw = str(name or "")
    names = [t["name"] for t in tools]
    if raw in names:
        return raw
    folded = raw.strip().lower()
    return next((n for n in names if n.lower() == folded), None)


def effective_max_rounds(agent: dict) -> int:
    """The round cap a run is held to: the agent's own setting when set, else the default for its
    shape (higher when it reaches external MCP servers). A decision agent makes one call."""
    if agent.get("decision"):
        return 1
    own = agent.get("max_rounds")
    if own:
        return max(1, min(int(own), MAX_ROUNDS_LIMIT))
    return MAX_ROUNDS_WITH_MCP if agent.get("mcp_servers") else MAX_ROUNDS


# Starting points offered in the form. A preset only seeds the prompt box — the user edits freely,
# because we cannot know what sources they attached or what they need looked at.
PRESETS = {
    "what-changed": {
        "label": "What changed",
        "prompt": (
            "You are taking a first look at an entity in a system you monitor, because a condition "
            "you watch just tripped. You are handed the correlated timeline (logs, metrics, "
            "deploys, commits, alerts) for that entity at the moment it tripped; that is your "
            "evidence.\n\n"
            "Establish what is happening and what CHANGED just before it started. Connect the "
            "change to the signature in the evidence. If the timeline is too narrow, read a wider "
            "window (1h) once before concluding.\n\n"
            "Write a short note: 1) what is happening and since when, 2) the most likely "
            "explanation with the specific evidence lines that support it, 3) what you would look "
            "at next. Do not speculate beyond the evidence; if it is inconclusive, say so and say "
            "what you would need. Your final message is recorded on this entity's timeline."
        ),
    },
    "error-investigation": {
        "label": "Error investigation",
        "prompt": (
            "You are an SRE taking the first look when an error condition trips on a running "
            "service. You are handed the correlated timeline (logs, metrics, deploys, commits) for "
            "the affected entity; that is your primary evidence.\n\n"
            "Diagnose the most likely root cause: look for what changed (a deploy, a commit, a "
            "config value) just before the errors began, and connect it to the failure signature "
            "in the logs. If the timeline is insufficient, read a wider window (1h) once before "
            "concluding.\n\n"
            "Produce a tight incident note: 1) what is failing and since when, 2) most likely "
            "cause with the specific evidence lines, 3) suggested next action. Do not speculate "
            "beyond the evidence; say what you'd need if inconclusive. Your final message is "
            "recorded on this entity's timeline."
        ),
    },
    "summarize": {
        "label": "Summarize the window",
        "prompt": (
            "You are handed the correlated timeline for an entity at the moment a condition you "
            "watch tripped. Summarize what happened in that window in plain language: the notable "
            "events in order, which sources they came from, and anything that looks out of the "
            "ordinary. Do not diagnose beyond what the evidence supports. Your final message is "
            "recorded on this entity's timeline."
        ),
    },
    # The first level of a watch-then-escalate chain (TR-321): on a schedule trigger, cheap and
    # quiet unless something needs a closer look. Its `investigate` findings wake the second,
    # through a handoff on that verdict (TR-334) or a trigger over findings (TR-322).
    "triage": {
        "label": "Triage (on a schedule)",
        "concludes": True,
        "verdicts": [{"verdict": "investigate",
                      "when": "one entity looks like a problem and needs a closer look"}],
        "prompt": (
            "You watch a system on a schedule. Every few minutes you are handed a summary of "
            "the last window of the sources you watch: for each label, the count per value now "
            "against the window "
            "before, and a few recent lines. Most windows are normal. Your job is to say so "
            "quickly, or to flag the one entity that needs a closer look.\n\n"
            "Rule first: consider only values that grew at least 3x and by at least 50 events "
            "since the window before, or that are new with at least 50 events. Ignore routine "
            "noise such as uptime probes and health checks, and values that only went down or "
            "are gone. If nothing meets the rule, the window is normal.\n\n"
            "Then judgment: among the values that meet the rule, decide whether one looks like a "
            "problem (errors, 4xx or 5xx codes, failures, a sudden new source of traffic). If you "
            "need more detail, call stats with a `where` or a different label, or read a few "
            "lines; keep it to one or two calls.\n\n"
            "Always end with the conclude tool:\n"
            "- outcome no_op, with a one-line reason, when nothing needs a closer look;\n"
            "- outcome finding with verdict investigate when something does: key is the entity "
            "that looks off, label is the entity label the summary names (for example service), "
            "and summary names the numbers that moved (now, before, change) and why it looks "
            "like a problem. A finding with verdict investigate hands the entity off to the "
            "agent that takes a closer look.\n"
            "Name the entity, not the symptom. If what moved is a status code or another "
            "attribute, find which entity carries it (stats by the entity label with a `where` on "
            "that attribute, for example by service where code=404) and name that entity.\n"
            "Flag at most one entity per run, the most serious one."
        ),
    },
    # The first level again, judged by a decision model instead of a chat model (TR-324): the
    # prompt is what counts as a problem; the model gives it a probability per window and picks
    # the entity, and the threshold decides. Starts in shadow mode: it scores, it never wakes.
    "triage-decision": {
        "label": "Triage with a decision model (on a schedule)",
        "concludes": True,
        "verdicts": [{"verdict": "investigate",
                      "when": "one entity looks like a problem and needs a closer look"}],
        "decision": {"threshold": 0.5, "shadow": True},
        "prompt": (
            "Something in this window needs a closer look from an SRE: errors, failures, 4xx "
            "or 5xx codes, or a sudden new source of traffic, such as a value that grew at least "
            "3x and by at least 50 events, or that is new with at least 50 events. Routine noise "
            "such as uptime probes and health checks, and values that only went down, do not "
            "count."
        ),
    },
    # The second level (TR-322, TR-334): handed a triage finding marked investigate, not the
    # logs, so it fetches its own evidence.
    "rca-from-triage": {
        "label": "Root cause after a handoff",
        "concludes": True,
        "verdicts": [{"verdict": "rca", "when": "you found what is failing and why"},
                     {"verdict": "resolved", "when": "it was noise, or it is already over"}],
        "prompt": (
            "You are an SRE doing root-cause analysis. A triage agent flagged the entity you were "
            "woken for as worth a closer look and handed it to you; you are given its finding (the "
            "numbers that moved and why), not the logs. Fetch the evidence yourself: read the "
            "entity's timeline over "
            "the last hour, use stats to see which labels moved and since when, and use any other "
            "tools you have (an MCP server for traces, for example).\n\n"
            "Establish what is failing and since when, what changed just before it started, and "
            "the most likely cause, grounded in what the tools returned. If the evidence shows it "
            "is not a real problem, or it is already over, say so.\n\n"
            "Always end with the conclude tool: outcome finding; verdict rca, or resolved if it "
            "was noise or is already over; key and label exactly as the entity you were handed "
            "(for example service=checkout or path=/docs/x); summary is a short incident note "
            "that opens with one sentence "
            "stating your conclusion (it is what a chat notification shows), then: 1) what is "
            "failing and since when, 2) the most likely cause with the evidence, 3) the "
            "suggested next action."
        ),
    },
}

TOOL_DEFS = [
    {
        "name": "read",
        "description": "Read one correlated, time-ordered timeline for an entity across the "
                       "sources of your project (or the `sources` you name). The selector is a "
                       "{label: value} map (strict AND), e.g. {\"service\": \"checkout\"}. Use "
                       "this to widen the window or look at a different entity than the one you "
                       "were woken for.",
        "input_schema": {"type": "object", "properties": {
            "selector": {"type": "object", "description": "{label: value}, e.g. {\"service\": \"api\"}"},
            "window": {"type": "string", "description": "e.g. 15m, 1h, 24h", "default": "1h"},
            "sources": {"type": "array", "items": {"type": "string"},
                        "description": "optional: read only these sources"}},
            "required": ["selector"]},
    },
    {
        "name": "stats",
        "description": "Count events per value of one label across the sources of your project "
                       "(or the `sources` you name), for the last `window` and the window of the "
                       "same length before it, largest change first. Use it to see the shape of "
                       "a busy stream (which services, status codes or paths moved) instead of "
                       "reading lines.",
        "input_schema": {"type": "object", "properties": {
            "by": {"type": "string", "description": "the label to count per, e.g. service, "
                                                    "nginx_status_code, path"},
            "where": {"type": "object", "description": "{label: value} to narrow first, e.g. "
                                                       "{\"status_code_text\": \"404\"}"},
            "window": {"type": "string", "default": "30m"},
            "top": {"type": "integer", "description": "rows to return (max 50)", "default": 20},
            "sources": {"type": "array", "items": {"type": "string"},
                        "description": "optional: count only these sources"}},
            "required": ["by"]},
    },
]


# the counting table lives in stats.py so the schedule trigger (TR-320) hands over the same one
from .stats import STATS_MAX_TOP, stats_table  # noqa: E402,F401


# How a run ends on purpose (TR-318). Offered only to an agent whose prompt names it, so an
# existing agent never meets it: a model there choosing `no_op` on its own would silently drop
# a finding someone waits for (a write-back, a Slack post). Without it, the last text is the
# finding, as before.
CONCLUDE = "conclude"
CONCLUDE_DEF = {
    "name": CONCLUDE,
    "description": ("End the run. outcome `no_op`: nothing to hand on, no finding is recorded "
                    "(say why in `summary`). outcome `finding`: `summary` is recorded as the "
                    "finding, with `verdict` as a label, on `key` (default: the entity you were "
                    "woken for). Call it once, as your last step."),
    "input_schema": {"type": "object", "properties": {
        "outcome": {"type": "string", "enum": ["finding", "no_op"]},
        "summary": {"type": "string", "description": "the finding, or why there is none"},
        "verdict": {"type": "string", "description": "one word from your instructions, "
                                                     "e.g. investigate, resolved"},
        "key": {"type": "string", "description": "the entity the finding is about"},
        "label": {"type": "string", "description": "the label `key` is a value of, e.g. "
                                                   "service, path; default: the woken entity's"},
        "headline": {"type": "string", "description": "one line, at most 100 characters: what "
                                                      "you found, in the reader's terms, e.g. "
                                                      "Payment provider outage"},
        "next_step": {"type": "string", "description": "at most 300 characters: what a person "
                                                       "should do now; empty when nothing needs "
                                                       "doing"},
        "produced": {"type": "array", "items": {"type": "string"},
                     "description": "anything you produced that Tares cannot see from your "
                                    "tool calls, one short line each; usually leave empty"}},
        "required": ["outcome", "summary"]},
}


# Appended to the system prompt of an agent offered `conclude`: the project page shows the
# headline and the next step first, the note behind them.
CONCLUDE_GUIDANCE = (
    "\n\nWhen you call conclude, also give `headline`: one line of at most 100 characters "
    "saying what you found, in the reader's terms (for example: Payment provider outage). And "
    "give `next_step`: what a person should do now, in at most 300 characters; leave it empty "
    "when nothing needs doing.")
HEADLINE_MAX = 100
NEXT_STEP_MAX = 300


def for_projects(agent: dict, projects: list[str]) -> dict:
    """The agent as it runs for `projects` (P-TR-216: the projects whose wiring started the run;
    none given: the project that made it). `owned_by` becomes the first of them, the project the
    run is filed under first; `projects` is all of them (skills and sources come from them)."""
    ps = [p for p in dict.fromkeys(projects or []) if p] or [p for p in [agent.get("owned_by")] if p]
    return {**agent, "owned_by": ps[0] if ps else agent.get("owned_by"), "projects": ps}


def offers_conclude(agent: dict) -> bool:
    """The agent gets the conclude tool: it is set to (`concludes`), or its prompt names it (the
    older way, kept so prompts written for it work as they did)."""
    return bool(agent.get("concludes")) or CONCLUDE in (agent.get("prompt") or "")


def verdict_words(agent: dict) -> list[str]:
    """The verdicts the agent may give; empty: any word, or none."""
    return [str(v.get("verdict")) for v in agent.get("verdicts") or [] if v.get("verdict")]


def conclude_def(agent: dict) -> dict:
    """The conclude tool as this agent sees it: with verdicts set, `verdict` is a choice of
    exactly those words, each with what it means, and a finding must carry one."""
    words = verdict_words(agent)
    if not words:
        return CONCLUDE_DEF
    meanings = "; ".join(f"{v['verdict']}: {v['when']}" if v.get("when") else v["verdict"]
                         for v in agent.get("verdicts") or [])
    d = json.loads(json.dumps(CONCLUDE_DEF))
    d["input_schema"]["properties"]["verdict"] = {
        "type": "string", "enum": words,
        "description": f"required with a finding, exactly one of these: {meanings}"}
    return d


# Added to the system prompt of an agent set to conclude (`concludes`), so its own prompt only
# says how to do the job; the verdicts follow when it has them.
CONCLUDE_SETUP = (
    "\n\nEnd every run with the conclude tool, called once as your last step: outcome no_op, "
    "with a one-line reason as the summary, when there is nothing to hand on; outcome finding, "
    "with the finding as the summary, when there is.")


def conclude_instructions(agent: dict) -> str:
    """What the system prompt says about ending a run, for an agent offered conclude."""
    out = CONCLUDE_SETUP if agent.get("concludes") else ""
    if verdict_words(agent):
        out += ("\n\nA finding carries exactly one verdict, one of these words:\n"
                + "\n".join(f"- {v['verdict']}" + (f": {v['when']}" if v.get("when") else "")
                             for v in agent.get("verdicts") or []))
    return out + CONCLUDE_GUIDANCE


def _one_line(text, limit: int) -> str | None:
    """Whitespace folded to single spaces, cut to `limit` characters; None when empty."""
    t = " ".join(str(text or "").split())
    if len(t) > limit:
        t = t[:limit - 1].rstrip() + "\u2026"
    return t or None


def parse_conclude(args: dict, verdicts: list[str] | None = None) -> tuple[dict | None, str | None]:
    """The `conclude` call as an outcome, or the error the model gets back to try again.
    `verdicts`: the only ones a finding may carry, and it must carry one (none given: any)."""
    outcome = str(args.get("outcome") or "").strip().lower()
    summary = str(args.get("summary") or "").strip()
    if outcome not in ("finding", "no_op"):
        return None, "outcome must be finding or no_op"
    if outcome == "finding" and not summary:
        return None, "a finding needs a summary"
    verdict = str(args.get("verdict") or "").strip().lower() or None
    if verdicts and outcome == "finding" and verdict not in verdicts:
        return None, ((f"verdict {verdict!r} is not one of yours" if verdict
                       else "a finding needs a verdict")
                      + "; give exactly one of: " + ", ".join(verdicts))
    return {"outcome": outcome, "summary": summary,
            "verdict": verdict,
            "key": str(args.get("key") or "").strip() or None,
            "label": str(args.get("label") or "").strip() or None,
            "headline": _one_line(args.get("headline"), HEADLINE_MAX),
            "next_step": _one_line(args.get("next_step"), NEXT_STEP_MAX),
            "produced": args.get("produced") or []}, None


def prompt_hash(prompt: str) -> str:
    """Short, stable id for the prompt that produced a finding. Without it, editing a prompt makes
    every earlier finding unattributable to the wording that caused it."""
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]


# The credential and base URL resolvers moved to providers.py (TR-301); they are imported here
# so callers and tests that patch them on this module keep working.
from .providers import (DEFAULT_API_BASE, default_model_for, resolve_anthropic_headers,  # noqa: E402,F401
                        resolve_api_base, resolve_for_agent, resolve_provider)


class AgentRunner:
    """Runs Tares agents in-process on the daemon's event loop, one per firing.

    In-process is right for a single-user local install (the whole point is that the loop closes on
    `tares up`, with nothing to deploy), and wrong for a shared instance — one run holds a slot
    for minutes and there is no isolation. A shared deployment should connect an external agent over
    a normal webhook subscription instead.

    The dispatcher calls `deliver()` for each internal subscription on a firing. Because a run takes
    minutes, deliver() does NOT block the dispatch: it logs a pending delivery, spawns the run, and
    resolves the delivery (ok/error) plus the run detail when it finishes — so a slow agent never
    delays an external webhook on the same trigger.
    """

    def __init__(self, store, runtime, tracing=None):
        self.store = store
        self.runtime = runtime
        # One trace per run, exported to whatever backend the instance is configured for (see
        # tracing.py); off unless switched on, and never in the way of a run.
        self.tracing = tracing or _tracing.Tracing(store)
        self._tasks: set[asyncio.Task] = set()
        # The daemon's loop, so run_now() also works from a worker thread (a project action runs
        # in one); None when constructed outside a loop (tests building a runner by hand).
        try:
            self._event_loop = asyncio.get_running_loop()
        except RuntimeError:
            self._event_loop = None
        # (agent, key) currently running. Dispatch is at-least-once, so a retried delivery would
        # otherwise run the same investigation twice and pay for it twice.
        self._inflight: set[tuple[str, str]] = set()
        # the run going on for each (agent, key): a second project's firing for the same agent and
        # entity joins it instead of starting another (P-TR-216)
        self._inflight_run: dict[tuple[str, str], str] = {}
        # Runs live in this process, so nothing can still be running from a previous one: any row
        # left `running` is an orphan from a daemon that was killed mid-run. Left alone it stays
        # running forever and keeps counting toward the daily cap.
        reaped = store.reap_stale_agent_runs()
        if reaped:
            print(f"taresd: reaped {reaped} interrupted agent run(s)")

    # ── entry point (called by the dispatcher, once per agent a firing wakes) ─
    def deliver(self, agent_name: str, subscription_id: str, trigger_name: str, key: str,
                payload: str, dispatch_id: str, woken_by: str = "trigger",
                projects: list[str] | None = None) -> str | None:
        """Wake a Tares agent for one firing. Logs a pending delivery immediately, then runs the
        agent in the background. Never raises and never blocks — a run must not break the dispatch.
        Returns the id the run will have (the webhook body of the same firing names it), or None
        when no run starts: no such agent, or it is already running for this key."""
        agent = self.store.get_catalog_agent(agent_name)
        if agent is None:   # subscription outlived its definition (shouldn't happen) — nothing to run
            self.store.log_delivery(dispatch_id, subscription_id, agent_url(agent_name), False,
                                    "no such agent")
            return None
        # pending delivery: the firing already "reached" the agent; whether it concludes is async.
        self.store.log_delivery(dispatch_id, subscription_id, agent_url(agent_name), None)
        if (agent["name"], key) in self._inflight:   # at-least-once dedupe
            going = self._inflight_run.get((agent["name"], key))
            if going and projects:
                # another project woke it for the same entity: the run going on is for it too
                self.store.add_run_projects(going, projects)
            self.store.update_delivery(dispatch_id, subscription_id, True,
                                       "deduped (already running)")
            return None
        run_id = "run_" + uuid.uuid4().hex[:12]
        task = asyncio.create_task(self._guarded(agent, subscription_id, trigger_name, key,
                                                 payload, dispatch_id, woken_by, run_id=run_id,
                                                 projects=projects))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return run_id

    # ── manual and bootstrap runs (no firing behind them) ────────────────────
    def run_now(self, agent_name: str, trigger_name: str, key: str, payload: str,
                woken_by: str = "manual", parent_run_id: str | None = None,
                practice: bool = False, project: str | None = None) -> str | None:
        """Run an agent once outside a firing (a project bootstrapping its first pages, a manual
        re-run). Same run record, same caps and dedupe as a firing; no delivery row, since there is
        no dispatch. `woken_by` and `parent_run_id` are the run's lineage: manual, bootstrap, or a
        rerun of the parent. `practice` marks a run the guided setup's "Try it" started: it and the
        handoffs it leads to are shown as practice and never counted. Returns the run id, or None
        when the agent does not exist or is already running for this key."""
        agent = self.store.get_catalog_agent(agent_name, project)
        if agent is None or (agent_name, key) in self._inflight:
            return None
        # the project it was started from (a project page, its practice), else its maker
        agent = for_projects(agent, [project] if project else [])
        run_id = "run_" + uuid.uuid4().hex[:12]
        marker = (agent_name, key)
        self._inflight.add(marker)
        self._inflight_run[marker] = run_id
        async def go():
            t0 = time.monotonic()
            status = "failed"
            try:
                status, _error = await self._run(agent, trigger_name, key, payload, run_id, None)
            except Exception as e:
                detail = f"{type(e).__name__}: {str(e) or repr(e)}"
                self.store.finish_agent_run(run_id, "failed", error=detail[:500])
                print(f"[agent {agent_name}] {detail}")
            finally:
                self._inflight.discard(marker)
                self._inflight_run.pop(marker, None)
            metrics.agent_run(agent_name, status, time.monotonic() - t0)
            if status == "ok":
                self._hand_off(agent, run_id)

        try:
            self._spawn(go)
        except RuntimeError:
            self._inflight.discard(marker)
            self._inflight_run.pop(marker, None)
            raise
        self.store.start_agent_run(run_id, agent_name, trigger_name, "", key,
                                   prompt_hash(agent["prompt"]), effective_max_rounds(agent),
                                   woken_by=woken_by, parent_run_id=parent_run_id,
                                   project=agent.get("owned_by"), practice=practice)
        self.store.add_run_projects(run_id, agent["projects"])
        return run_id

    def attach_loop(self) -> None:
        """Remember the daemon's loop. Called at startup (the app is built before uvicorn's loop
        exists), so run_now() from a worker thread finds it."""
        self._event_loop = asyncio.get_running_loop()

    def _spawn(self, coro_fn) -> None:
        """Start `coro_fn()` as a task on the daemon's loop, from that loop or from a thread."""
        def start():
            task = asyncio.create_task(coro_fn())
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            if self._event_loop is None:
                raise RuntimeError("no event loop to run the agent on")
            self._event_loop.call_soon_threadsafe(start)
            return
        start()

    # ── handoffs (TR-334): a finding with a matching verdict starts another agent ──
    def _hand_off(self, agent: dict, run_id: str) -> None:
        """After a run ends ok: when it concluded a finding whose verdict one of the agent's
        handoffs names (case-insensitive), start that agent on the concluded entity. What does
        not start is still on record: a handoff inside its cooldown is a note on this run's
        results; a chain too deep, an agent already running for the entity, the daily cap and the
        budget each leave a capped run under this one. Never raises."""
        concluded = self.__dict__.get("_concluded", {}).pop(run_id, None)
        try:
            current = self.store.get_catalog_agent(agent["name"]) or agent
            run = self.store.get_agent_run(run_id)
            if not run or run.get("status") != "ok" or run.get("outcome") != "finding":
                return
            verdict = str(run.get("verdict") or "").strip().lower()
            if not verdict:
                return
            # the handoffs each project the run belongs to wires (P-TR-216); a target two
            # projects hand off to on the same verdict runs once, for both
            projects = self.store.run_projects(run_id) or [p for p in [run.get("project")] if p]
            by_target: dict[str, tuple[dict, list[str]]] = {}
            for p in projects:
                for h in self.store.list_handoffs(project=p, from_agent=agent["name"]):
                    if str(h.get("verdict") or "").strip().lower() == verdict:
                        h0, ps = by_target.setdefault(h["agent"], (h, []))
                        ps.append(p)
            if not by_target:
                return
            key, label = concluded or (run["key"], None)
            if not label:
                try:
                    label = self._entity_label(run.get("trigger") or current["trigger"])
                except Exception:
                    label = None
            notes = [n for n in (self._start_handoff(current, run, h, verdict, key, label, ps)
                                 for h, ps in by_target.values()) if n]
            if notes:
                self.store.set_run_results(run_id, _results.merge(
                    list(run.get("results") or [])
                    + [{"kind": "custom", "label": n} for n in notes]))
        except Exception as e:  # noqa: BLE001 — a handoff must never break the run it follows
            print(f"[agent {agent['name']}] handoff: {type(e).__name__}: {e}")

    def _handoff_depth(self, run: dict) -> int:
        """How many handoffs led to `run`: the handed-off runs on its line of parents, itself
        included."""
        depth, seen = 0, set()
        while run and run["id"] not in seen and len(seen) < 50:
            seen.add(run["id"])
            if run.get("woken_by") == "handoff":
                depth += 1
            pid = run.get("parent_run_id")
            run = self.store.get_agent_run(pid) if pid else None
        return depth

    def _start_handoff(self, agent: dict, parent: dict, h: dict, verdict: str, key: str,
                       label: str | None, projects: list[str] | None = None) -> str | None:
        """Start one handoff, or record why it did not start. Returns a note for the parent run's
        results when nothing was recorded as a run (cooldown, a target that is gone). The run
        belongs to `projects`: the projects whose wiring holds this handoff."""
        to = h["agent"]
        target = self.store.get_catalog_agent(to, (projects or [None])[0])
        if target is None:
            return f"handoff to {to} skipped: no such agent"
        target = for_projects(target, projects or [])
        state = handoff_state(agent["name"], to)
        cooldown = parse_duration(h.get("cooldown") or "30m")
        last = self.store.last_fired(state, key)
        if last is not None and last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if last is not None and cooldown > 0 \
                and (now_utc() - last).total_seconds() < cooldown:
            return f"handoff to {to} skipped: cooldown"
        self.store.set_fired(state, key, now_utc())
        # the target already running for this entity for another project: the run going on is
        # for this project too, not a second one (P-TR-216)
        going = self._inflight_run.get((to, key))
        if going and set(target["projects"]) - set(self.store.run_projects(going)):
            self.store.add_run_projects(going, target["projects"])
            return None
        run_id = "run_" + uuid.uuid4().hex[:12]
        trigger_name = target["trigger"]
        self.store.start_agent_run(run_id, to, trigger_name, "", key,
                                   prompt_hash(target["prompt"]), effective_max_rounds(target),
                                   woken_by="handoff", parent_run_id=parent["id"],
                                   project=target.get("owned_by"),
                                   # a practice run's handoff is practice too
                                   practice=bool(parent.get("practice")))
        self.store.add_run_projects(run_id, target["projects"])
        if self._handoff_depth(parent) >= MAX_HANDOFF_DEPTH:
            self.store.finish_agent_run(run_id, "capped", error=HANDOFF_STOPPED)
            metrics.agent_run(to, "capped", 0.0)
            return None
        marker = (to, key)
        if marker in self._inflight:
            self.store.finish_agent_run(
                run_id, "capped", error=f"not started: {to} is already running for {key}")
            metrics.agent_run(to, "capped", 0.0)
            return None
        payload = handoff_input(agent["name"], verdict, key, label, parent.get("finding") or "")
        self._inflight.add(marker)
        self._inflight_run[marker] = run_id

        async def go():
            t0 = time.monotonic()
            status = "failed"
            try:
                status, _error = await self._run(target, trigger_name, key, payload, run_id,
                                                 None, handoff=True)
            except Exception as e:
                detail = f"{type(e).__name__}: {str(e) or repr(e)}"
                self.store.finish_agent_run(run_id, "failed", error=detail[:500])
                print(f"[agent {to}] {detail}")
            finally:
                self._inflight.discard(marker)
                self._inflight_run.pop(marker, None)
            metrics.agent_run(to, status, time.monotonic() - t0)
            if status == "ok":
                self._hand_off(target, run_id)

        try:
            self._spawn(go)
        except RuntimeError as e:
            self._inflight.discard(marker)
            self._inflight_run.pop(marker, None)
            self.store.finish_agent_run(run_id, "failed", error=str(e))
        return None

    def bootstrap(self, agent_name: str, trigger_name: str, keys: list[str],
                  window: str = "7d", limit: int = 20, delay_s: float = 90.0,
                  concurrency: int = 10) -> None:
        """Schedule one run per key over a wide window of the trigger's sources, a little later (the sources
        behind a fresh project need their first poll before there is anything to read). Keys whose
        timeline is empty at that point are skipped, so an idle repo costs nothing. Runs go through
        run_now, so the daily cap and dedupe apply."""

        async def go():
            await asyncio.sleep(delay_s)
            sem = asyncio.Semaphore(max(1, concurrency))

            async def one(key: str):
                async with sem:
                    catalog = self.runtime.catalog
                    trig = next((t for t in catalog.triggers if t.name == trigger_name), None)
                    if trig is None:
                        return
                    payload, count, _rows = resolve_sources_full(
                        self.store, trig.sources, trig.name, key=key, window=window,
                        filters=trig.filters)
                    if not count:
                        print(f"[agent {agent_name}] bootstrap: no events for {key} yet, skipped")
                        return
                    rid = self.run_now(agent_name, trigger_name, key, payload,
                                       woken_by="bootstrap")
                    if rid:
                        print(f"[agent {agent_name}] bootstrap run {rid} for {key} ({count} events)")
                        # wait for the run so the semaphore really bounds concurrency
                        while (agent_name, key) in self._inflight:
                            await asyncio.sleep(2)

            await asyncio.gather(*(one(k) for k in keys[:MAX_BOOTSTRAP_KEYS]))

        task = asyncio.create_task(go())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _guarded(self, agent: dict, subscription_id: str, trigger_name: str, key: str,
                       payload: str, dispatch_id: str, woken_by: str = "trigger",
                       run_id: str | None = None, projects: list[str] | None = None) -> None:
        agent = for_projects(agent, projects or [])
        marker = (agent["name"], key)
        if marker in self._inflight:   # at-least-once dedupe
            going = self._inflight_run.get(marker)
            if going:
                self.store.add_run_projects(going, agent["projects"])
            self.store.update_delivery(dispatch_id, subscription_id, True, "deduped (already running)")
            return
        self._inflight.add(marker)
        run_id = run_id or "run_" + uuid.uuid4().hex[:12]
        self._inflight_run[marker] = run_id
        self.store.start_agent_run(run_id, agent["name"], trigger_name, dispatch_id, key,
                                   prompt_hash(agent["prompt"]), effective_max_rounds(agent),
                                   woken_by=woken_by, project=agent.get("owned_by"))
        self.store.add_run_projects(run_id, agent["projects"])
        t0 = time.monotonic()
        try:
            status, error = await self._run(agent, trigger_name, key, payload, run_id,
                                            dispatch_id)
        except Exception as e:
            detail = f"{type(e).__name__}: {str(e) or repr(e)}"
            self.store.finish_agent_run(run_id, "failed", error=detail[:500])
            status, error = "failed", detail[:500]
            print(f"[agent {agent['name']}] {detail}")
        finally:
            self._inflight.discard(marker)
            self._inflight_run.pop(marker, None)
        metrics.agent_run(agent["name"], status, time.monotonic() - t0)
        if status == "ok":
            self._hand_off(agent, run_id)
        # resolve the delivery: ok when the agent concluded ('ok'); 'empty'/'capped' are not
        # failures (it ran and declined to conclude, or hit the cap) but aren't a delivered finding
        # either — mark ok=false with the reason so the firing row is honest without crying wolf.
        self.store.update_delivery(dispatch_id, subscription_id, status == "ok",
                                   None if status == "ok" else error)

    async def _run(self, agent: dict, trigger_name: str, key: str, payload: str,
                   run_id: str, dispatch_id: str | None = None,
                   handoff: bool = False) -> tuple[str, str | None]:
        tracer = self.tracing.tracer_for(agent["name"])
        # Resolved before the span opens: the firing's delivery id is the session (TR-317).
        anchor = self._callback_anchor(agent, trigger_name, key)
        with _tracing.run_span(tracer, agent["name"],
                               session=self._session_id(agent, anchor, dispatch_id, run_id),
                               agent=agent["name"], attributes={
                "tares.run_id": run_id, "tares.dispatch_id": dispatch_id or "",
                "tares.trigger": trigger_name, "tares.key": key,
                **{f"tares.label.{k}": str(v) for k, v in anchor[1].items()
                   if v not in (None, "")}}) as obs:
            # The instance is on the resource (service.name) too, but a backend's span view
            # may not show resource attributes; on the root span it is always in reach.
            obs.set_attribute("tares.instance", _tracing.instance_name())
            obs.set_attribute("tares.agent", agent["name"])
            # what the run produced (TR-220), filled as evidence arrives and stored however it ends
            results: list = []
            loaded: list = []   # the skills the run loads (TR-332)
            try:
                status, error = await self._run_traced(agent, trigger_name, key, payload, run_id,
                                                       dispatch_id, tracer, obs, anchor, results,
                                                       skills_loaded=loaded,
                                                       **({"handoff": True} if handoff else {}))
            finally:
                if loaded:
                    self.store.set_run_skills(run_id, loaded)
                if results:
                    self.store.set_run_results(run_id, _results.merge(results))
                    obs.set_attribute("tares.results", len(_results.merge(results)))
            obs.set_attribute("tares.status", status)
            if status == "failed" and error:
                obs.error(error)
            elif error:
                obs.set_attribute("tares.note", error)
            return status, error

    @staticmethod
    def _session_id(agent: dict, anchor: tuple[str, dict], dispatch_id: str | None,
                    run_id: str) -> str:
        """session.id of a run: one run, one session (TR-317). The firing's delivery id when the
        agent reports one (`webhook_key_label`, the id Rius files the report under), else the
        trigger dispatch that woke it, else the run itself (a rerun or bootstrap run). Not the
        entity key: that would make one session of every run an agent ever made on a service."""
        label = (agent.get("webhook_key_label") or "").strip()
        if label and anchor[1].get(label) not in (None, ""):
            return str(anchor[1][label])
        return dispatch_id or run_id

    async def _run_traced(self, agent: dict, trigger_name: str, key: str, payload: str,
                          run_id: str, dispatch_id: str | None, tracer,
                          obs: _tracing.Observation,
                          anchor: tuple[str, dict] | None = None,
                          results: list | None = None,
                          skills_loaded: list | None = None,
                          handoff: bool = False) -> tuple[str, str | None]:
        results = [] if results is None else results
        started_at = now_utc()
        t0 = time.monotonic()
        # a decision model instead of a chat model (TR-324): one call, no provider, no daily cap
        decision = agent.get("decision") or {}
        if decision:
            provider, key_origin, provider_id, provider_note = None, "", "", ""
        else:
            provider, key_origin, provider_id, provider_note = resolve_for_agent(self.store, agent)
        if provider is None and not decision:
            msg = ("no model provider configured: add one under Settings, or set "
                   "ANTHROPIC_API_KEY before `tares up`")
            self.store.finish_agent_run(run_id, "failed", error=msg)
            obs.set_attribute(_tracing.SKIPPED_REASON, "no_provider")
            return "failed", msg
        # Count the runs BEFORE this one (its row is already inserted), so the cap fires at exactly
        # daily-cap runs rather than one over it.
        cap, source = effective_daily_cap(agent, self.store)
        if not decision and self.store.agent_runs_today(agent["name"], exclude_run_id=run_id) >= cap:
            where = "in its project's daily_cap" if source == "agent" else "under Settings, Agents"
            msg = f"cap of {cap} runs in the last 24h reached for this agent; raise it {where}"
            self.store.finish_agent_run(run_id, "capped", error=msg)
            obs.set_attribute(_tracing.SKIPPED_REASON, "daily_run_cap")
            return "capped", msg
        # The agent's own budget, when set: lifetime spend from the run log. Checked before any
        # model call, so a capped run costs nothing; the message says where to raise it.
        budget = agent.get("budget_usd")
        if budget and self.store.agent_cost_total(agent["name"]) >= float(budget):
            msg = (f"budget of ${float(budget):.2f} for this agent reached; "
                   f"raise or clear it under Configuration, Advanced")
            self.store.finish_agent_run(run_id, "capped", error=msg)
            obs.set_attribute(_tracing.SKIPPED_REASON, "budget")
            return "capped", msg

        # BEFORE the loop: the write-back's key names the firing this run answers for, and only
        # now is that unambiguous — the trigger has just fired on it. Resolved after the loop
        # instead, it named whichever firing had arrived most recently by then, which for a burst
        # inside one cooldown is a different alert from the one the agent wrote up (TR-294).
        callback_key, callback_labels = anchor or self._callback_anchor(agent, trigger_name, key)

        # Usage accumulates in a mutable dict rather than the loop's return value, so a run that
        # dies mid-loop still records the tokens it already paid for (the finally below).
        endpoint = _decision.entry(self.store, decision["endpoint"]) if decision else None
        if decision and endpoint is None:
            msg = (f"decision endpoint {decision['endpoint']!r} not found; add it under Settings, "
                   "Decision models, or pick another for this agent")
            self.store.finish_agent_run(run_id, "failed", error=msg)
            obs.set_attribute(_tracing.SKIPPED_REASON, "no_decision_endpoint")
            return "failed", msg
        model = (_decision.model_for(endpoint, decision) if decision
                 else agent.get("model") or default_model_for(self.store, provider_id))
        if not model and not decision:   # a custom decision endpoint may take no model name
            msg = (f"provider {provider_id!r} lists no models yet; pick one for this agent under "
                   "Configuration, or refresh the provider's models under Settings")
            self.store.finish_agent_run(run_id, "failed", error=msg)
            obs.set_attribute(_tracing.SKIPPED_REASON, "no_model")
            return "failed", msg
        if provider_note:
            print(f"[agent {agent['name']}] {provider_note}")
        usage = empty_usage()
        concluded: dict = {}   # filled by a `conclude` call (TR-318)
        if decision:
            return await self._decide(agent, trigger_name, key, payload, run_id, dispatch_id,
                                      obs, results, endpoint, decision, model,
                                      (callback_key, callback_labels), started_at, t0)
        obs.set_attribute("tares.provider", provider_id)
        try:
            finding, rounds, tool_calls, external_used, exhausted, partial = await self._loop(
                agent, trigger_name, key, payload, provider, model, usage, tracer, obs,
                concluded=concluded, produced=results, skills_loaded=skills_loaded,
                **({"handoff": True} if handoff else {}))
        finally:
            if usage["calls"]:
                cost = price_usage(provider.kind, model, usage)
                self.store.record_run_usage(
                    run_id, model, usage["input_tokens"], usage["output_tokens"],
                    usage["cache_creation_input_tokens"], usage["cache_read_input_tokens"], cost, provider=provider_id)
                self.store.record_model_usage(
                    "agent", agent["name"], run_id, model, usage["calls"],
                    usage["input_tokens"], usage["output_tokens"],
                    usage["cache_creation_input_tokens"], usage["cache_read_input_tokens"], cost,
                    key_source=key_origin, provider=provider_id)
                obs.set_attribute("tares.cost_usd", cost)
                obs.set_attribute("tares.model_calls", usage["calls"])
        if exhausted:
            # The round budget ran out before the model concluded. Keep whatever it said last as
            # a partial note (in `finding`, so it is visible) and say plainly what to do.
            cap = effective_max_rounds(agent)
            msg = f"round budget of {cap} exhausted before a conclusion; raise max rounds"
            if partial:
                obs.set_output(partial)
            self.store.finish_agent_run(run_id, "exhausted", rounds=rounds,
                                        tool_calls=tool_calls, finding=partial or None,
                                        error=msg, external_tools=external_used)
            return "exhausted", msg
        return await self._conclude(agent, trigger_name, key, run_id, dispatch_id, obs, results,
                                    concluded, finding, rounds, tool_calls, external_used,
                                    model, usage, price_usage(provider.kind, model, usage),
                                    (callback_key, callback_labels), started_at, t0)



    def _window_rows(self, trigger_name: str) -> tuple[str | None, list]:
        """(the entity label, its rows this window against the one before) for a schedule
        trigger: the options of a decision run's `entity` question. (None, []) for a trigger that
        fires on one entity, which is then the entity."""
        catalog = getattr(getattr(self, "runtime", None), "catalog", None)
        if catalog is None:
            return None, []
        trig = next((t for t in catalog.triggers if t.name == trigger_name), None)
        every = getattr(getattr(trig, "condition", None), "every", None)
        if trig is None or not every or not trig.sources:
            return None, []
        label = trigger_entity_label(trig, catalog.sources) or "key_value"
        window = f"{int(every)}s" if every % 60 else f"{int(every // 60)}m"
        rows = stats_rows(self.store, trig.sources, label, window, filters=trig.filters)["rows"]
        # a value that only went down or is gone is not a candidate
        return label, [r for r in rows if r["now"] > 0]

    async def _decide(self, agent: dict, trigger_name: str, key: str, payload: str, run_id: str,
                      dispatch_id: str | None, obs: _tracing.Observation, results: list,
                      endpoint: dict, settings: dict, model: str, anchor: tuple[str, dict],
                      started_at, t0: float) -> tuple[str, str | None]:
        """A decision-model run (TR-324): ask `problem` and `entity` about the window the agent
        was handed, store the probabilities, and conclude from the threshold."""
        obs.set_attribute("tares.decision.endpoint", endpoint["id"])
        obs.set_attribute("tares.model", model)
        # a schedule hands over a window: the entity is one of its label values, asked about.
        # A trigger that fired on one entity: that entity, only `problem` is asked.
        scheduled = self._is_scheduled(trigger_name)
        label, rows = None, []
        if scheduled:
            try:
                label, rows = self._window_rows(trigger_name)
            except Exception as e:   # the window could not be counted: no entity to name
                print(f"[agent {agent['name']}] decision: counting the window failed: {e}")
        else:
            catalog = getattr(getattr(self, "runtime", None), "catalog", None)
            trig = next((t for t in catalog.triggers if t.name == trigger_name), None) \
                if catalog else None
            label = trigger_entity_label(trig, catalog.sources) if trig else None
        state = payload
        used = settings.get("state") or _decision.DEFAULT_STATE
        if not scheduled:
            used = "timeline"
        settings = {**settings, "state": used}
        if used == "entities":
            # one JSON row per entity instead of the text summary (TR-401)
            try:
                state, erows = _schedule.entity_state(self.store, self.runtime.catalog,
                                                      self._trigger(trigger_name))
                qs, ids = _decision.entity_questions(agent.get("prompt") or "", label, erows)
            except Exception as e:
                msg = f"decision model: building the entity state failed: {type(e).__name__}: {e}"
                self.store.finish_agent_run(run_id, "failed", error=msg[:500])
                return "failed", msg[:500]
        else:
            qs, ids = _decision.questions(agent.get("prompt") or "", label if scheduled else None,
                                          rows)
        # what the model read and was asked, on the run's trace
        obs.set_input(json.dumps({"state": state, "questions": qs})[:60000])
        try:
            out = await _decision.decide(endpoint, model, state, qs)
            concluded, scores = _decision.outcome(out["answers"], settings, label, ids,
                                                  None if scheduled else key)
        except _decision.DecisionError as e:
            msg = f"decision model: {e}"
            self.store.finish_agent_run(run_id, "failed", error=msg[:500])
            return "failed", msg[:500]
        tokens = out["input_tokens"]
        cost = _decision.price(model, tokens)
        usage = {**empty_usage(), "calls": 1, "input_tokens": tokens}
        provider_id = f"decision:{endpoint['id']}"
        self.store.record_run_usage(run_id, model, tokens, 0, 0, 0, cost, provider=provider_id)
        self.store.record_model_usage("agent", agent["name"], run_id, model, 1, tokens, 0, 0, 0,
                                      cost, key_source="console", provider=provider_id)
        self.store.set_run_scores(run_id, scores)
        obs.set_attribute("tares.decision.problem", scores["problem"])
        obs.set_attribute("tares.decision.threshold", scores["threshold"])
        obs.set_attribute("tares.decision.escalate", scores["escalate"])
        obs.set_attribute("tares.decision.shadow", scores["shadow"])
        if scores.get("entity") is not None:
            obs.set_attribute("tares.decision.entity", str(scores["entity"]))
        if cost is not None:
            obs.set_attribute("tares.cost_usd", cost)
        if concluded.get("headline"):
            concluded["headline"] = concluded["headline"][:100]
        finding = concluded["summary"] if concluded["outcome"] == "finding" else None
        return await self._conclude(agent, trigger_name, key, run_id, dispatch_id, obs, results,
                                    concluded, finding, 1, 0, [], model, usage, cost, anchor,
                                    started_at, t0)

    async def _conclude(self, agent: dict, trigger_name: str, key: str, run_id: str,
                        dispatch_id: str | None, obs: _tracing.Observation, results: list,
                        concluded: dict, finding: str | None, rounds: int, tool_calls: int,
                        external_used: list, model: str, usage: dict, cost: float | None,
                        anchor: tuple[str, dict], started_at, t0: float) -> tuple[str, str | None]:
        """How a run ends once its model has spoken, chat or decision: no_op, a finding recorded
        (Slack, the write-back, the handoff after it), or no conclusion at all."""
        callback_key, callback_labels = anchor
        results.extend(_results.custom_results(concluded.get("produced")))
        if concluded.get("outcome") == "no_op":
            # A quiet success: nothing to hand on, so no finding, no Slack post, no write-back.
            # The reason stays on the run for whoever reads the runs table.
            obs.set_attribute("tares.outcome", "no_op")
            obs.set_output(concluded["summary"])
            self.store.finish_agent_run(run_id, "ok", rounds=rounds, tool_calls=tool_calls,
                                        finding=concluded["summary"] or None,
                                        external_tools=external_used, outcome="no_op",
                                        headline=concluded.get("headline"),
                                        next_step=concluded.get("next_step"))
            return "ok", None
        if not finding:
            msg = "the model returned no conclusion"
            if tool_calls == 0:
                # Most often a model that does not do tool calling, or a router alias pointing
                # at one: say so, since the run otherwise looks like the prompt was at fault.
                msg += ("; it called no tool either. Check that this model supports tool "
                        "calling, or pick another one for this agent")
            self.store.finish_agent_run(run_id, "empty", rounds=rounds, tool_calls=tool_calls,
                                        error=msg, external_tools=external_used)
            return "empty", msg

        verdict = concluded.get("verdict")
        # the entity a handoff starts the next agent on: the concluded key and its label
        self.__dict__.setdefault("_concluded", {})[run_id] = (concluded.get("key") or key,
                                                               concluded.get("label"))
        obs.set_output(finding)
        obs.set_attribute("tares.rounds", rounds)
        obs.set_attribute("tares.tool_calls", tool_calls)
        obs.set_attribute("tares.outcome", "finding")
        obs.set_attribute("tares.verdict", verdict)
        get_run = getattr(self.store, "get_agent_run", None)   # test doubles may not have it
        if get_run and (get_run(run_id) or {}).get("practice"):
            # a practice run (the guided setup's "Try it") stays inside Tares: its finding is
            # recorded, but no Slack post and no write-back webhook, whatever the agent has set
            agent = {**agent, "slack_channel": "", "slack_webhook": None, "webhook_url": ""}
        results.extend(await self._record(agent, trigger_name, concluded.get("key") or key,
                                          finding, verdict=verdict,
                                          label=concluded.get("label"),
                                          run_id=run_id, model=model,
                                          dispatch_id=dispatch_id,
                                          headline=concluded.get("headline"),
                                          next_step=concluded.get("next_step")) or [])
        self.store.finish_agent_run(run_id, "ok", rounds=rounds, tool_calls=tool_calls,
                                    finding=finding, external_tools=external_used,
                                    outcome="finding", verdict=verdict,
                                    headline=concluded.get("headline"),
                                    next_step=concluded.get("next_step"))
        if (agent.get("webhook_url") or "").strip():
            delivery, derr = await self._webhook(agent, {
                "event": "finding",
                "agent": agent["name"], "trigger": trigger_name,
                # `key` is the entity, unless the agent names a label to report instead: Rius
                # attributes reports by delivery id while the entity is the service (TR-285).
                # Both were resolved at run start — see _callback_anchor.
                "key": callback_key,
                # The firing's own labels, so a receiver can attribute the report on something
                # other than `key` alone and reject a mismatch rather than mis-file it silently.
                "labels": callback_labels,
                "finding": finding,
                "run_id": run_id, "dispatch_id": dispatch_id,
                "model": model,
                "rounds": rounds, "tool_calls": tool_calls,
                "usage": {k: v for k, v in usage.items() if k != "calls"},
                "cost_usd": cost,
                "started_at": started_at.isoformat(), "finished_at": now_utc().isoformat(),
                "duration_s": round(time.monotonic() - t0, 2),
                "prompt_hash": prompt_hash(agent["prompt"]),
            })
            self.store.set_run_delivery(run_id, delivery, derr)
            if delivery == "ok":
                results.append(_results.webhook_result(agent["webhook_url"]))
        return "ok", None

    def _callback_anchor(self, agent: dict, trigger_name: str, key: str) -> tuple[str, dict]:
        """The firing this run answers for: the `key` the write-back reports, and that firing's
        labels.

        The key is the entity, or the value of `webhook_key_label` on the firing when the agent
        names a label. MUST be called at run START: the trigger has just fired, so the newest
        event in the window is the firing the run was woken by. Called after the model loop
        instead, it named whichever firing had arrived most recently by then — for a burst inside
        one cooldown, a different alert from the one the agent wrote up (TR-294).

        Falls back to the entity and no labels when the label is on no event, so a wrong name
        never drops the field.
        """
        label = (agent.get("webhook_key_label") or "").strip()
        if not label:
            return key, {}
        try:
            catalog = self.runtime.catalog
            triggers = catalog.triggers
            trig = (triggers.get(trigger_name) if isinstance(triggers, dict)
                    else next((t for t in triggers if t.name == trigger_name), None))
            if trig is None or not trig.sources:
                return key, {}
            window = max(parse_duration(trig.condition.window or "15m"), 900.0)
            since = now_utc() - timedelta(seconds=window)
            rows = self.store.read_window(trig.sources, key, since, cap=1,
                                          filters=trig.filters)
            latest = max(rows, key=lambda r: r[0]) if rows else None
            if latest is None:
                return key, {}
            labels = latest[3]
            if isinstance(labels, str):
                labels = json.loads(labels or "{}")
            labels = labels or {}
            value = labels.get(label)
            return (str(value) if value not in (None, "") else key), labels
        except Exception as e:  # noqa: BLE001 — never lose the write-back over the key lookup
            print(f"[agent {agent['name']}] callback anchor from {label!r}: "
                  f"{type(e).__name__}: {e}")
            return key, {}

    # ── the bounded model loop ────────────────────────────────────────────────
    async def _loop(self, agent: dict, trigger_name: str, key: str, payload: str,
                    provider: Provider, model: str, usage: dict, tracer=None,
                    obs: _tracing.Observation | None = None, concluded: dict | None = None,
                    produced: list | None = None, skills_loaded: list | None = None,
                    handoff: bool = False,
                    ) -> tuple[str, int, int, list[str], bool, str]:
        # External tools: the agent's selected MCP servers, connected for the duration of this
        # run. A server that fails to connect is skipped (recorded below) — losing a tool server
        # must not lose the run.
        from .mcp_client import RemoteToolbox, resolve_servers
        selected = set(agent.get("mcp_servers") or [])
        servers = await resolve_servers(self.store, [m for m in self.store.list_mcp_servers()
                                                     if m["name"] in selected])
        async with RemoteToolbox(servers) as toolbox:
            for failure in toolbox.failures:
                print(f"[agent {agent['name']}] mcp connect failed; {failure}")
            return await self._loop_with(agent, trigger_name, key, payload, provider, toolbox,
                                         usage, tracer, obs, model=model, concluded=concluded,
                                         produced=produced, skills_loaded=skills_loaded,
                                         **({"handoff": True} if handoff else {}))

    async def _loop_with(self, agent: dict, trigger_name: str, key: str, payload: str,
                         provider: Provider, toolbox, usage: dict, tracer=None,
                         obs: _tracing.Observation | None = None, model: str = "",
                         concluded: dict | None = None, produced: list | None = None,
                         skills_loaded: list | None = None, handoff: bool = False,
                         ) -> tuple[str, int, int, list[str], bool, str]:
        """Returns (finding, rounds, tool_calls, external_tools_used, exhausted, partial_text).
        A `conclude` call ends the loop at the end of its round and fills `concluded`; each skill
        the model loads is appended to `skills_loaded` once."""
        concluded = {} if concluded is None else concluded
        skills_loaded = [] if skills_loaded is None else skills_loaded
        # The project's skills (TR-332): listed after the agent's own prompt, a body only when
        # the model loads it. None in the project: prompt and tools exactly as before.
        skills = self._project_skills(agent)
        skill_names = [sk["name"] for sk in skills]
        system = (agent["prompt"] + (conclude_instructions(agent) if offers_conclude(agent) else "")
                  + (_skills.prompt_section(skills) if skills else ""))
        # set to conclude: the run may not end without it (asked for it once more, below)
        must_conclude = bool(agent.get("concludes"))
        verdicts = verdict_words(agent)
        from . import github_tools as _gh_tools
        checks = _gh_tools.offered(self.store, agent)   # its GitHub App credential (TR-165)
        tools = (TOOL_DEFS + ([conclude_def(agent)] if offers_conclude(agent) else [])
                 + ([_skills.TOOL_DEF] if skills else []) + toolbox.tool_defs
                 + ([_gh_tools.CHECK_RUN_DEF] if checks else []))
        max_rounds = effective_max_rounds(agent)
        external_used: list[str] = []
        # The agent's own last conclusion for this entity, so a run builds on the previous one
        # instead of rediscovering it (worth real rounds for a context-maintaining agent). Capped:
        # it is a head start, not a second timeline.
        prior = self.store.last_finding(FINDINGS_SOURCE, agent["name"], key)
        prior_block = (f'Your finding from an earlier run on "{key}":\n\n{prior[:4000]}\n\n'
                       if prior else "")
        if handoff:
            # handed a finding by another agent (TR-334), not a timeline: it reads its own evidence
            content = (f"{prior_block}{payload}\n\n"
                       f"Take it from here, per your instructions. You hold the finding that was "
                       f"handed to you, not the evidence behind it; read what you need.")
        else:
            opening = (
                f'The schedule "{trigger_name}" ticked. The summary of its last '
                f"window:\n\n{payload}\n\n" if self._is_scheduled(trigger_name) else
                f'The condition "{trigger_name}" tripped for "{key}".\n\n'
                f"{prior_block}"
                f"The correlated timeline at that moment:\n\n{payload}\n\n")
            content = (f"{opening}"
                       f"Take a first look, per your instructions. You already hold the evidence "
                       f"above; read again only if you need a wider window or a different entity.")
        messages = [{"role": "user", "content": content}]
        rounds = tool_calls = 0
        last_text = ""
        if obs is not None:
            obs.set_input(messages[0]["content"])
        model = model or agent.get("model") or MODEL

        async def call(with_tools: bool, tool_choice: str | None = None):
            reply = await provider.complete(model=model, system=system, tools=tools,
                                            messages=messages, max_tokens=MAX_TOKENS,
                                            tools_allowed=with_tools, tracer=tracer,
                                            **({"tool_choice": tool_choice} if tool_choice else {}))
            add_usage(usage, reply.usage)
            return reply

        async def require_conclude(ask: str) -> bool:
            """An agent set to conclude stopped without it: ask once, with conclude the only
            choice, and once more if that call was not valid. True when it concluded."""
            messages.append({"role": "user", "content": ask})
            for _attempt in range(2):
                try:
                    reply = await call(True, CONCLUDE)
                except ModelError:
                    return False
                tc = next((c for c in reply.tool_calls if _canonical_tool(c.name, tools) == CONCLUDE),
                          None)
                if tc is None:
                    return False
                messages.append(reply.as_message())
                outcome, err = parse_conclude(tc.arguments or {}, verdicts)
                if not err:
                    concluded.update(outcome)
                    return True
                messages.append(tool_message([(tc.id, f"tool error: ValueError: {err}")]))
            return False

        for rounds in range(1, max_rounds + 1):
            reply = await call(True)
            text = reply.text.strip()
            calls = list(reply.tool_calls)
            if not calls:
                # A small model that wrote its tool call as JSON text instead of a real call:
                # accepting that as the finding would file the model's own instructions to
                # itself. Run the call it meant (TR-306). The assistant turn is then rebuilt from
                # text plus the call, so the provider sees a real call to answer.
                meant = tool_call_in_text(text, tools)
                if meant is not None:
                    meant.id = f"text_call_{rounds}"
                    calls = [meant]
                    messages.append({"role": "assistant", "content": "", "tool_calls": calls})
            if not (calls and calls[0].id.startswith("text_call_")):
                messages.append(reply.as_message())
            if text and not (calls and calls[0].id.startswith("text_call_")):
                last_text = text

            if not calls:
                if must_conclude and await require_conclude(
                        "End the run now with the conclude tool."):
                    return concluded["summary"], rounds, tool_calls + 1, external_used, False, ""
                return text, rounds, tool_calls, external_used, False, ""

            results = []
            for tc in calls:
                tool_calls += 1
                # A small model slips on names (Read, READ, "read "); match the declared tool
                # case-insensitively rather than fail the call over case (TR-306).
                name = _canonical_tool(tc.name, tools)
                with _tracing.tool_span(tracer, name, tc.arguments, call_id=tc.id) as tobs:
                    try:
                        if name is None:
                            raise ValueError(f"unknown tool {tc.name!r}; the tools are: "
                                             + ", ".join(t["name"] for t in tools))
                        if name == CONCLUDE:
                            outcome, err = parse_conclude(tc.arguments or {}, verdicts)
                            if err:
                                raise ValueError(err)
                            if not concluded:
                                concluded.update(outcome)
                            out = f"concluded: {outcome['outcome']}"
                        elif name == _skills.TOOL and skills:
                            wanted = str((tc.arguments or {}).get("name") or "").strip()
                            out = _skills.load_from(self.store, agent.get("projects")
                                                   or [agent.get("owned_by")], wanted, skill_names)
                            if wanted not in skills_loaded:
                                skills_loaded.append(wanted)
                        elif name == _gh_tools.CHECK_RUN and checks:
                            external_used.append(name)
                            out = await _gh_tools.create_check_run(self.store, agent, tc.arguments)
                            if produced is not None:
                                made = _results.from_tool_call(name, tc.arguments, out)
                                if made:
                                    produced.append(made)
                        elif toolbox.owns(name):
                            external_used.append(name)
                            out = await toolbox.call(name, tc.arguments)
                            # a pull request, a commit, a Slack post: read off the call (TR-220)
                            if produced is not None:
                                made = _results.from_tool_call(name, tc.arguments, out)
                                if made:
                                    produced.append(made)
                        else:
                            out = self._tool(agent["name"], name, tc.arguments)
                    except Exception as e:   # a tool error is evidence, not a crash
                        out = f"tool error: {type(e).__name__}: {e}"
                        tobs.tool_error(out)
                    else:
                        tobs.set_output(out)
                results.append((tc.id, out))
            messages.append(tool_message(results))
            if concluded:
                return concluded["summary"], rounds, tool_calls, external_used, False, ""

        # Budget exhausted with tool calls still pending. Ask once more, tools disabled, for a
        # conclusion from what it has (this is the +1 call). If it concludes, that is the
        # finding; if not, the run is `exhausted` and the last text is kept as a partial note.
        budget_note = ("You have used your round budget. Do not call any more tools. Conclude now "
                       "from the evidence you already have; if it is inconclusive, say what you "
                       "found so far and what you would look at next.")
        if must_conclude:
            # the conclusion is still the conclude tool, the one call it may make now
            if await require_conclude(budget_note + " Use the conclude tool."):
                return concluded["summary"], rounds, tool_calls + 1, external_used, False, ""
            return "", rounds, tool_calls, external_used, True, last_text
        messages.append({"role": "user", "content": budget_note})
        try:
            reply = await call(False)
        except ModelError:
            return "", rounds, tool_calls, external_used, True, last_text
        text = reply.text.strip()
        if text:
            return text, rounds, tool_calls, external_used, False, ""
        return "", rounds, tool_calls, external_used, True, last_text

    def _project_skills(self, agent: dict) -> list[dict]:
        """The skills of the projects the run is for (name, description), the first project's
        version of a name shared by two; an agent sees only those projects' know-how."""
        out, seen = [], set()
        for uid in agent.get("projects") or [p for p in [agent.get("owned_by")] if p]:
            for sk in self.store.list_skills(uid):
                if sk["name"] not in seen:
                    seen.add(sk["name"])
                    out.append(sk)
        return out

    def _trigger(self, trigger_name: str):
        return next((t for t in self.runtime.catalog.triggers if t.name == trigger_name), None)

    def _is_scheduled(self, trigger_name: str) -> bool:
        """Whether the run was woken by a schedule trigger (TR-320), which ticks for all its
        sources rather than trips for an entity."""
        try:
            trig = next((t for t in self.runtime.catalog.triggers if t.name == trigger_name), None)
            return bool(trig is not None and getattr(trig.condition, "every", None))
        except Exception:
            return False

    def _tool_sources(self, agent_name: str, args: dict) -> list[str]:
        """What a read or stats call covers: the `sources` the model named, else the sources of
        the agent's project. An agent sees its project, not the whole cell (an agent with no
        project on record, which only a hand-edited store has, falls back to every source)."""
        catalog = self.runtime.catalog
        named = args.get("sources")
        if named:
            if not isinstance(named, list) or not all(isinstance(x, str) for x in named):
                raise ValueError("sources must be a list of source names")
            unknown = sorted(set(named) - set(catalog.sources))
            if unknown:
                raise KeyError(f"unknown sources {unknown} (available: "
                               f"{', '.join(sorted(catalog.sources))})")
            return list(named)
        # the sources of every project that uses the agent (P-TR-216: it may serve several)
        uids = self.store.projects_using("agent", agent_name)
        if not uids:
            return sorted(catalog.sources)
        mine = {s for uid in uids for s in self.store.project_sources(uid)}
        return sorted(s for s in mine if s in catalog.sources)

    def _tool(self, agent_name: str, name: str, args: dict) -> str:
        """The two reads, in-process. No HTTP hop and no credential: a Tares agent IS Tares, so
        it reads through the same resolver the API serves rather than authenticating to itself."""
        catalog = self.runtime.catalog
        window = str(args.get("window") or "1h")
        if name == "read":
            selector = args.get("selector") or {}
            if not isinstance(selector, dict) or not selector:
                raise ValueError('read needs a selector, e.g. {"service": "checkout"}')
            payload, nrows, _sources, _rows = resolve_read(
                self.store, catalog, selector, window,
                sources=self._tool_sources(agent_name, args))
            self.store.log_query("r_" + uuid.uuid4().hex[:12], "(read)",
                                 ", ".join(f"{k}={v}" for k, v in selector.items()),
                                 window, nrows, f"agent:{agent_name}")
            return payload
        if name == "stats":
            by = str(args.get("by") or "").strip()
            if not by:
                raise ValueError('stats needs `by`, the label to count per, e.g. "service"')
            window = str(args.get("window") or "30m")
            out = stats_table(self.store, self._tool_sources(agent_name, args), by, window,
                              where=args.get("where") or None, top=args.get("top") or 20)
            self.store.log_query("s_" + uuid.uuid4().hex[:12], "(stats)", f"by {by}",
                                 window, 0, f"agent:{agent_name}")
            return out
        raise ValueError(f"unknown tool {name!r}")

    # ── the finding: an event, plus an optional Slack copy ────────────────────
    def _entity_label(self, trigger_name: str) -> str | None:
        """Which label the firing entity was identified by: the trigger's key_field, else the
        primary label of one of its sources. The finding must be stamped with the same axis as
        its evidence, or a label-native `read` for the entity won't return it."""
        catalog = self.runtime.catalog
        trig = next((t for t in catalog.triggers if t.name == trigger_name), None)
        if trig is None:
            return None
        return trigger_entity_label(trig, catalog.sources)

    async def _ingest_finding(self, agent_name: str, trigger_name: str, key: str, finding: str,
                              prompt_hash_: str, verdict: str | None = None,
                              label: str | None = None, run_id: str | None = None,
                              dispatch_id: str | None = None, project: str | None = None,
                              headline: str | None = None, next_step: str | None = None) -> None:
        """Store one finding as an event of the findings source. `project` is the project it was
        recorded in: a project's reads of the shared findings source see only its own."""
        if FINDINGS_SOURCE not in self.runtime.catalog.sources:
            # provisioned on the first finding, like the memory source — a fresh install has no
            # reason to carry an empty one. No `labels` config: the runner stamps them per event.
            self.store.upsert_catalog_source(FINDINGS_SOURCE, "finding", "finding", "5s", {})
            self.runtime.reload_catalog()
            print(f"taresd: auto-provisioned findings source {FINDINGS_SOURCE!r}")
        labels = {label: key} if label else {}
        if verdict:
            labels["verdict"] = verdict
        lineage = {k: v for k, v in (("run_id", run_id), ("dispatch_id", dispatch_id)) if v}
        labels.update(lineage)
        await self.runtime.ingest(FINDINGS_SOURCE, {
            "key": key, "finding": finding, "agent": agent_name, "trigger": trigger_name,
            "prompt_hash": prompt_hash_,
            **({"verdict": verdict} if verdict else {}),
            **({"headline": headline} if headline else {}),
            **({"next_step": next_step} if next_step else {}),
            **lineage,
            **({"project": project} if project else {}),
            "labels": labels,
        })

    async def record_external(self, project: str, agent_label: str, key: str, finding: str,
                              verdict: str | None = None, label: str | None = None,
                              headline: str | None = None, next_step: str | None = None) -> str:
        """An external agent's finding (TR-336): a run of kind external in the project, ended
        with this finding, plus the finding event every read and findings trigger sees, exactly
        like a Tares agent's. Returns the run id."""
        run_id = "run_" + uuid.uuid4().hex[:12]
        self.store.start_agent_run(run_id, agent_label, "", "", key, "", None,
                                   woken_by="external", project=project)
        self.store.add_run_projects(run_id, [project])
        await self._ingest_finding(agent_label, "", key, finding, "", verdict=verdict,
                                   label=label, run_id=run_id, project=project,
                                   headline=headline, next_step=next_step)
        self.store.finish_agent_run(run_id, "ok", finding=finding, outcome="finding",
                                    verdict=verdict, headline=headline, next_step=next_step)
        return run_id

    async def _record(self, agent: dict, trigger_name: str, key: str, finding: str,
                      verdict: str | None = None, label: str | None = None,
                      run_id: str | None = None, model: str | None = None,
                      dispatch_id: str | None = None, headline: str | None = None,
                      next_step: str | None = None) -> list:
        """Record the finding, then notify. Returns the notifications delivered, as results.
        The finding names the run and the firing it came from, in its payload and its labels, so
        a firing it trips later can be traced back to this run (TR-330)."""
        # `label` is the axis a concluded key belongs to when the agent names one; else the
        # woken entity's, so the finding carries the SAME axis as the evidence it was drawn from.
        label = label or self._entity_label(trigger_name)
        await self._ingest_finding(agent["name"], trigger_name, key, finding,
                                   prompt_hash(agent["prompt"]), verdict=verdict, label=label,
                                   run_id=run_id, dispatch_id=dispatch_id,
                                   project=agent.get("owned_by"), headline=headline,
                                   next_step=next_step)
        # Notification: the workspace bot posting to a channel is the primary path (one token,
        # picked from a list, no credential per agent); the per-agent incoming webhook stays as
        # the secondary/legacy path. An agent uses one — channel wins when both are set.
        channel = (agent.get("slack_channel") or "").strip()
        hook = agent.get("slack_webhook")
        # what the Slack message shows beside the note (TR-275)
        meta = {"verdict": verdict, "model": model, "run_id": run_id, "when": now_utc().isoformat()}
        if channel:
            if await self._slack_channel(agent["name"], channel, trigger_name, key, finding, meta):
                return [{"kind": "slack", "label": f"Slack {channel}"}]
        elif hook:
            if await self._slack(agent["name"], hook, trigger_name, key, finding, meta):
                return [{"kind": "slack", "label": "Slack webhook"}]
        return []

    async def _webhook(self, agent: dict, body: dict, attempts: int = 3) -> tuple[str, str | None]:
        """POST the finding plus its run metadata to the agent's write-back webhook — the machine
        counterpart of the Slack post, for feeding findings into the customer's own automation.

        The body carries only what the customer may already read via the API: the finding, the
        run's shape (rounds, tool calls, duration), the model name and a hash of the prompt —
        never a key or token. Auth is an optional bearer token sent as a header; it is never
        logged, and a delivery failing must never lose the finding (already stored).

        Returns (delivery, error) for the run: "ok"; "http <status>" for a client error, which
        is not retried; "failed" with the last error after the retries."""
        url = agent["webhook_url"].strip()
        headers = {"content-type": "application/json"}
        token = (agent.get("webhook_token") or "").strip()
        if token:
            headers["authorization"] = f"Bearer {token}"
        delay = 1.0
        async with httpx.AsyncClient(timeout=15) as cx:
            for attempt in range(attempts):
                try:
                    r = await cx.post(url, json=body, headers=headers)
                    if 200 <= r.status_code < 300:
                        return "ok", None
                    if r.status_code < 500:   # client error — won't self-heal, don't retry
                        print(f"[agent {agent['name']}] webhook: HTTP {r.status_code}")
                        return f"http {r.status_code}", r.text[:200]
                    err = f"HTTP {r.status_code}"
                except Exception as e:        # transport failure — unreachable / timeout / DNS
                    err = f"{type(e).__name__}: {str(e)[:120]}"
                if attempt < attempts - 1:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 10)
        print(f"[agent {agent['name']}] webhook: giving up after {attempts} attempts ({err})")
        return "failed", f"after {attempts} attempts: {err}"

    async def _slack_channel(self, agent_name: str, channel: str, trigger_name: str,
                             key: str, finding: str, meta: dict | None = None) -> bool:
        """Post the finding through the workspace bot (`chat.postMessage`): the entity, a grid of
        fields, the note and buttons in the channel (TR-275). A note too long for one message
        block gets an excerpt in the channel and the full note as a reply in the message's thread,
        so it cannot fill the channel; a shorter one is posted whole, with no thread. The credential is the one bot token the
        instance holds, and the target is a channel picked from a list.

        One attempt, verdict from `slack.classify` (Slack answers HTTP 200 with ok:false), failure
        printed — a notification failing must never lose the finding, which is already stored."""
        from . import slack as _slack_mod
        token, _origin = _slack_mod.resolve_token(self.store)
        if not token:
            print(f"[agent {agent_name}] slack: no bot token configured, channel post skipped")
            return False
        threaded = _slack_mod.needs_thread(finding)
        msg = _slack_mod.build_finding_message(agent_name, trigger_name, key, finding,
                                               full_note=not threaded, **(meta or {}))
        headers = {"authorization": f"Bearer {token}"}
        try:
            async with httpx.AsyncClient(timeout=15) as cx:
                r = await cx.post(f"{_slack_mod.API_BASE}/chat.postMessage",
                                  json={"channel": channel, **msg}, headers=headers)
                try:
                    data = r.json()
                except Exception:
                    data = None
                ok, error, _retry = _slack_mod.classify(r.status_code, data)
                if not ok:
                    print(f"[agent {agent_name}] slack: {error}")
                    return False
                ts = (data or {}).get("ts")
                if threaded and ts:
                    # The full note under the message. Failing here still leaves the finding in
                    # the channel with its excerpt and buttons, so the post counts as delivered.
                    r2 = await cx.post(f"{_slack_mod.API_BASE}/chat.postMessage", headers=headers,
                                       json={"channel": channel, "thread_ts": ts,
                                             "text": f"Full note from {agent_name} on {key}",
                                             "blocks": _slack_mod.build_finding_thread(finding),
                                             "unfurl_links": False, "unfurl_media": False})
                    try:
                        d2 = r2.json()
                    except Exception:
                        d2 = None
                    ok2, err2, _ = _slack_mod.classify(r2.status_code, d2)
                    if not ok2:
                        print(f"[agent {agent_name}] slack: thread reply: {err2}")
                return True
        except Exception as e:   # notification failing must never lose the finding
            print(f"[agent {agent_name}] slack: {type(e).__name__}: {e}")
            return False

    async def _slack(self, agent_name: str, hook: str, trigger_name: str, key: str,
                     finding: str, meta: dict | None = None) -> bool:
        """Slack carries the FULL finding, not a pointer. A local install has no reachable URL, and
        a link to 127.0.0.1 is worse than no link — so the message must stand alone. The text is the
        stored finding verbatim; a summary here would become a second, divergent record.

        A deep link is appended only when the instance knows it is reachable (TARES_PUBLIC_URL) —
        the same rule the slack:// dispatch sink applies, hence the shared helper.

        This is the ORIGINAL per-agent incoming-webhook path and stays as it is. The way forward is
        a `slack://channel/<id>` subscription (`tares/slack.py`), which is per-trigger, retried
        and logged in the delivery ledger; existing agents are not migrated.
        """
        # Incoming webhooks accept blocks too, so the finding renders the same way as on the
        # channel path instead of as raw markdown.
        # An incoming webhook cannot post into a thread, so the full note stays inline here.
        from .slack import build_finding_message
        msg = build_finding_message(agent_name, trigger_name, key, finding, full_note=True,
                                    **(meta or {}))
        try:
            async with httpx.AsyncClient(timeout=15) as cx:
                r = await cx.post(hook, json=msg)
                if r.status_code >= 300:
                    print(f"[agent {agent_name}] slack: HTTP {r.status_code} {r.text[:120]}")
                return r.status_code < 300
        except Exception as e:   # notification failing must never lose the finding
            print(f"[agent {agent_name}] slack: {type(e).__name__}: {e}")
            return False
