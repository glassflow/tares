"""Project: challenger workflow.

A user works in Claude Code on their laptop and marks a session as a challenger session (Claude
calls the `set_session_flow` MCP tool). From then on a second model, the Codex CLI running on the
laptop, challenges Claude's plan and every commit; the Tares plugin ships the whole exchange into
the `claude_code` source on one session timeline. Tares is the record and the after-session
brain, never the control point: nothing here decides whether Codex runs.

This template owns the Tares side: the `claude_code` source (adopted from the plugin, which creates
it on first run), a view per session, a trigger that fires when a challenger session ends, and a
Tares agent that reads the session and writes one finding: the session summary and up to five
memory proposals. Proposals live inside the finding; nothing is written to memory until a person
accepts one from the console, so only accepted memory ever exists.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from ..connectors.claude_code import ClaudeCodeConnector
from .base import PlannedObject, Template, ProjectError
from .registry import register

FLOW = "challenger"
SOURCE = "claude_code"           # the plugin's own name for the source, adopted as is
VIEW = "challenger_session"
ENDS_VIEW = "challenger_session_ends"
TRIGGER = "challenger_session_ended"
AGENT = "challenger_summarizer"
PROPOSALS_HEADING = "Memory proposals"
MAX_PROPOSALS = 5

PROMPT = """You are handed the end of one Claude Code session that ran as a challenger session: the user \
worked with Claude, and Codex (a second model on the user's laptop) challenged Claude's plan and \
every commit. Your key is the session id. Before writing anything, call `read` with \
{{"session": "<the key>"}} and window "24h" once to get the whole session: user prompts, Claude's \
turns, tool calls, the plan, each Codex challenge (challenge_plan / challenge_commit events with a \
verdict and findings), fix rounds, waived findings, commits, and token usage.

Write the session summary in plain sentences, no em dashes, under these headings:

Summary
What the user asked for and what was built (name the commits). Whether the goal was reached.

Plan
What the plan was, what Codex found in it, and how the plan changed as a result.

Challenges
For each reviewed commit: the verdict, what Codex caught, how Claude resolved it, how many rounds \
it took. Findings the user waived and the reason if the session shows one.

Cost
Tokens and cost if the timeline carries them, otherwise say they were not recorded.

{heading}
Up to {max_proposals} durable facts about this repository or this user's way of working that would save \
time in the next session. One per line, starting with "- ". Only facts the session supports, none \
about this session's specifics. Skip the heading if there are none.

Do not speculate beyond the evidence. The summary is your final message and nothing else.
""".format(heading=PROPOSALS_HEADING, max_proposals=MAX_PROPOSALS)


class ChallengerWorkflow(Template):
    key = "challenger_workflow"
    title = "Challenger workflow"
    description = ("Let a second model challenge Claude Code's plan and every commit on your laptop, "
                   "keep the whole exchange on one session timeline, and get a session summary "
                   "with memory proposals when the session ends.")
    tags = ()
    sentence = ("challenge Claude Code's plan and every commit on my laptop, and summarise "
                "each session when it ends")
    guide = {"label": "Challenger workflow guide",
             "url": "https://docs.glassflow.ai/tares/guides/challenger-workflow"}

    PARAMS = {
        "slack_channel": {"type": "string", "default": "", "label": "Slack channel",
                          "help": "post each session summary to this channel id (needs the Slack "
                                  "surface set up); empty = console only"},
        "model": {"type": "string", "default": "", "label": "Model",
                  "help": "model for the summarizer (empty = the instance default)"},
    }

    SETUP = [
        {"title": "Install the Tares plugin in Claude Code",
         "text": "The plugin streams every session into Tares and registers the tares MCP server. "
                 "One install, then /reload-plugins.",
         "command": "/plugin marketplace add glassflow/tares\n/plugin install tares@tares"},
        {"title": "Install the challenger",
         "text": "Codex runs on your laptop and is billed to your OpenAI account; Tares never calls it.",
         "command": "npm install -g @openai/codex\ncodex login"},
        {"title": "Give the summarizer a key", "check": "anthropic_key",
         "text": "The summarizer is a real agent: it needs an Anthropic key. Set one here or under "
                 "Settings > Anthropic."},
        {"title": "Start a challenger session",
         "text": "In any Claude Code session say \"make this a challenger session\" or type "
                 "/tares:challenger. Claude marks the session; the plan and every commit get "
                 "challenged from then on, and the summary lands here when the session ends."},
    ]

    ACTIONS = [
        {"name": "summarize", "label": "Summarize a session now",
         "help": "run the summarizer on one session without waiting for it to end (the session id "
                 "is the key on the claude_code timeline)",
         "params": {"session": {"label": "session id", "type": "string"}}},
    ]

    def _facts(self) -> dict:
        return {"you": ["install the Tares plugin and Codex on your laptop",
                        "say \"make this a challenger session\" at the start of a session",
                        "accept or reject the memory proposals after each session"],
                "tares": ["the full Claude and Codex exchange on one timeline per session",
                          "a trigger that fires when a challenger session ends",
                          "an agent that writes the session summary and memory proposals",
                          "accepted memory handed to Claude at the next session start"]}

    def describe(self) -> dict:
        d = super().describe()
        d["facts"] = self._facts()
        return d

    # ── params ───────────────────────────────────────────────────────────────
    def validate(self, params: dict) -> dict:
        p = super().validate(params)
        ch = str(p.get("slack_channel") or "").strip()
        if ch and not re.fullmatch(r"[A-Z][A-Z0-9]{6,}", ch):
            raise ProjectError("slack_channel must be a Slack channel id like C0123456789")
        p["slack_channel"] = ch
        p["model"] = str(p.get("model") or "").strip()
        return p

    # ── plan ─────────────────────────────────────────────────────────────────
    def plan(self, params: dict) -> list[PlannedObject]:
        objs = [
            # the same source the plugin creates on its first run (push is the connector's
            # default, so the stored config is empty either way): an existing one is adopted
            # unchanged rather than reconfigured
            PlannedObject("source", "source", {
                "name": SOURCE, "connector": "claude_code", "poll": "10s", "config": {}}),
            # the session timeline people and the agent read. The summarizer's finding lands on
            # the same session key and joins it through `read` (the findings source is internal,
            # provisioned by the daemon on the first finding, so a view must not name it).
            PlannedObject("view", "view", {
                "name": VIEW, "key_field": "session", "sources": [SOURCE]}),
            # the detection view: only the end-of-session line of a challenger session. The
            # plugin stamps `flow` on every line of a marked session and writes a session_end
            # line when the session closes.
            PlannedObject("view", "ends", {
                "name": ENDS_VIEW, "key_field": "session", "sources": [SOURCE],
                "filters": [{"field": "event_type", "op": "eq", "value": "session_end"},
                            {"field": "flow", "op": "eq", "value": FLOW}]}),
            PlannedObject("trigger", "trigger", {
                "name": TRIGGER, "view": ENDS_VIEW,
                "condition": {"aggregate": "count", "predicate": "> 0", "window": "5m",
                              "group_by": ["key_value"]},
                "emit": {"kind": "session_ended", "attach_view": True, "context_window": "24h"},
                "cooldown": "30m"}),
        ]
        agent = {"name": AGENT, "trigger": TRIGGER, "prompt": PROMPT, "enabled": True,
                 "max_rounds": 6}
        if params.get("model"):
            agent["model"] = params["model"]
        if params.get("slack_channel"):
            agent["slack_channel"] = params["slack_channel"]
        objs.append(PlannedObject("agent", "agent", agent))
        return objs

    # ── actions ──────────────────────────────────────────────────────────────
    def run_action(self, instance: dict, action: str, args: dict, store, runtime) -> dict:
        if action != "summarize":
            raise ProjectError(f"{self.key}: no action {action!r}")
        session = str(args.get("session") or "").strip()
        if not session:
            raise ProjectError("session id is required")
        agents = getattr(getattr(runtime, "dispatcher", None), "agents", None)
        if agents is None or not hasattr(agents, "run_now"):
            raise ProjectError("the agent runner is not available")
        run_id = agents.run_now(AGENT, TRIGGER, session, f"summarize session {session} on request")
        return {"session": session, "run_id": run_id,
                "message": f"summarizer started on session {session}; its finding appears under Runs"}

    # ── summary (the project page) ──────────────────────────────────────────
    def summary(self, instance: dict, store) -> dict:
        out = super().summary(instance, store)
        params = self.validate(instance["params"])
        sessions = recent_sessions(store) if any(
            x["source"] == SOURCE for x in store.event_stats()) else []
        repos = {x["session"]: x["repo"] for x in sessions}
        runs = out["runs"]
        for r in runs:
            finding = r.get("finding") or ""
            r["session"] = r.get("key")
            r["repo"] = repos.get(r.get("key"))
            r["proposals"] = parse_proposals(finding)
        decided = proposal_decisions(store)
        for r in runs:
            r["decisions"] = {str(i): decided.get((r["repo"], text))
                              for i, text in enumerate(r["proposals"])
                              if decided.get((r["repo"], text))}
        summarized = {r["session"]: r["id"] for r in reversed(runs) if r["status"] == "ok"}
        for x in sessions:
            x["run_id"] = summarized.get(x["session"])
        out["sessions"] = sessions
        out["names"] = {"view": VIEW, "ends_view": ENDS_VIEW, "trigger": TRIGGER, "agent": AGENT}
        out["panels"] = [{
            "title": "Where it delivers",
            "rows": [{"label": "Slack channel", "value": params["slack_channel"], "mono": True}],
        }] if params.get("slack_channel") else []
        out["cards"] = [{"label": "sessions summarized",
                         "value": len({r["session"] for r in runs if r["status"] == "ok"})}]
        return out


_HEADING_RE = re.compile(rf"^\s*#*\s*{PROPOSALS_HEADING}\s*:?\s*$", re.IGNORECASE | re.MULTILINE)


def parse_proposals(finding: str) -> list[str]:
    """The memory proposals section of a summarizer finding: the "- " lines after the heading, up
    to the next heading or the end. Empty when the agent skipped the section."""
    m = _HEADING_RE.search(finding or "")
    if not m:
        return []
    out = []
    for line in finding[m.end():].splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith(("- ", "* ")):
            out.append(s[2:].strip())
        elif out or not s.startswith(("-", "*")):
            break   # a new heading or prose ends the list
    return out[:MAX_PROPOSALS]


def has_challenger_line(payload) -> bool:
    """True when an ingest body carries a line stamped flow=challenger (the plugin marks every
    line of a marked session)."""
    items = payload if isinstance(payload, list) else [payload]
    return any(isinstance(o, dict) and o.get("flow") == FLOW for o in items)


def ensure_instance(engine) -> str | None:
    """Create the challenger_workflow project the first time a marked session ships, so saying
    "make this a challenger session" in Claude Code is the whole setup. Returns the new instance id,
    or None when one already exists."""
    if any(u.get("template") == "challenger_workflow" for u in engine.store.list_projects()):
        return None
    inst = engine.create("challenger_workflow", {})
    engine.store.log_project(inst["id"], "auto", "created on the first challenger session shipped by the plugin")
    return inst["id"]


REJECTED_TYPE = "rejected_proposal"   # stored as custom:rejected_proposal; the plugin only reads decisions


def proposal_decisions(store) -> dict:
    """{(repo, proposal text): "accepted" | "rejected"} from the memory source. Accept writes
    the proposal as a decision keyed by the repo; reject writes it as a rejected_proposal, so
    the choice is kept on Tares (not in one browser) without ever reaching Claude."""
    out = {}
    try:
        rows = store.recent_events(source="agent_memory", limit=2000)
    except Exception:
        return out
    for row in rows:
        text = (row.get("text") or "").strip()
        if not text:
            continue
        typ = row.get("event_type")
        state = ("accepted" if typ == "decision"
                 else "rejected" if typ == f"custom:{REJECTED_TYPE}" else None)
        if state:
            out.setdefault((row.get("key"), text), state)
    return out


SESSION_DAYS = 30
MAX_SESSIONS = 20
THREAD_TYPES = tuple(ClaudeCodeConnector.CHALLENGE_TYPES) + ("session_flow", "session_end")


def recent_sessions(store) -> list[dict]:
    """The challenger sessions of the last SESSION_DAYS, newest first: who and where, what Codex
    said about the plan and each commit, and the challenge thread (only the challenge events, so
    the Claude/Codex exchange reads on its own; the full session stays on the view)."""
    from ..views import _labels
    since = datetime.now(timezone.utc) - timedelta(days=SESSION_DAYS)
    rows = store.read_view_window([SOURCE], None, since, cap=5000, where={"flow": FLOW},
                                  include_payload=True)
    by_session: dict[str, dict] = {}
    for event_time, _source, text, labels, payload in rows:
        try:
            raw = json.loads(payload) if isinstance(payload, (str, bytes)) else (payload or {})
        except (TypeError, ValueError):
            raw = {}
        sid = str(raw.get("sessionId") or "")
        if not sid:
            continue
        typ = str(raw.get("type") or "")
        cwd = str(raw.get("cwd") or "")
        sess = by_session.setdefault(sid, {
            "session": sid, "repo": cwd.rstrip("/").rsplit("/", 1)[-1] or None,
            "branch": raw.get("gitBranch"), "started_at": _iso(event_time), "last_at": None,
            "ended": False, "events": 0, "plan_verdict": None, "plan_findings": None,
            "plan_blocking": None, "commits": [], "waived": 0,
            "thread": []})
        sess["events"] += 1
        sess["last_at"] = _iso(event_time)
        if raw.get("gitBranch"):
            sess["branch"] = raw["gitBranch"]
        if typ not in THREAD_TYPES:
            continue
        lbls = _labels(labels)
        if typ == "session_end":
            sess["ended"] = True
        elif typ == "challenge_plan":
            sess["plan_verdict"] = lbls.get("verdict")
            sess["plan_findings"] = lbls.get("finding_count")
            sess["plan_blocking"] = lbls.get("blocking_count")
        elif typ == "challenge_commit":
            sess["commits"].append({"sha": lbls.get("sha"), "verdict": lbls.get("verdict"),
                                    "round": lbls.get("round"),
                                    "findings": lbls.get("finding_count"),
                                    "blocking": lbls.get("blocking_count")})
        elif typ == "challenge_waived":
            sess["waived"] += 1
        entry = {"at": _iso(event_time), "event_type": typ, "text": text or "", "labels": lbls}
        ch = raw.get("challenge") if isinstance(raw.get("challenge"), dict) else {}
        if ch.get("findings"):
            # the full findings, from the lossless payload: the event text is capped and older
            # events carried only the first three findings in it
            entry["findings"] = [{"priority": f.get("priority"), "title": f.get("title"),
                                  "waived": bool(f.get("waived"))}
                                 for f in ch["findings"] if isinstance(f, dict)]
        sess["thread"].append(entry)
    out = sorted(by_session.values(), key=lambda x: x["last_at"] or "", reverse=True)
    return out[:MAX_SESSIONS]


def _iso(v):
    if v is None:
        return None
    return v.isoformat() if hasattr(v, "isoformat") else str(v)


register(ChallengerWorkflow())
