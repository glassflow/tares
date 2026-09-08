"""Tares agents — the data plane reading its own data.

A Tares agent is a prompt attached to a trigger. It's a real agent (it reasons with an LLM),
configured inside Tares rather than connected over a webhook. When its trigger fires it's handed
the same correlated timeline the dispatch carries, may read a wider window or another entity, and
writes ONE finding back into Tares (plus an optional Slack post). Today it has exactly two tools,
`query` and `read`, both routed through Tares's own read path — it cannot reach anything else.

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
import os
import time
import uuid

import httpx

from . import tracing as _tracing
from .config import FINDINGS_SOURCE, API_BASE, agent_url
from .envelope import now_utc
from .pricing import cost_usd
from .slack import deep_link as _slack_deep_link
from .views import resolve_query_full, resolve_read

MODEL = os.getenv("TARES_AGENT_MODEL", "claude-sonnet-4-6")
# The model choices the console offers per agent. The instance default (TARES_AGENT_MODEL) is
# always first; an agent stores "" to mean "follow the instance default", so changing the
# instance default moves every agent that never chose one.
AGENT_MODELS = list(dict.fromkeys(
    [MODEL, "claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5-20251001"]))
# Model-call rounds per run. The default is sized for read timeline + a query or two + a finding;
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
DAILY_RUN_CAP = int(os.getenv("TARES_AGENT_DAILY_CAP", "50"))
MAX_BOOTSTRAP_KEYS = 50    # a project bootstraps at most this many entities in one go

def effective_max_rounds(agent: dict) -> int:
    """The round cap a run is held to: the agent's own setting when set, else the default for its
    shape (higher when it reaches external MCP servers)."""
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
}

TOOL_DEFS = [
    {
        "name": "read",
        "description": "Read one correlated, time-ordered timeline for an entity across ALL "
                       "sources. The selector is a {label: value} map (strict AND), e.g. "
                       "{\"service\": \"checkout\"}. Use this to widen the window or look at a "
                       "different entity than the one you were woken for.",
        "input_schema": {"type": "object", "properties": {
            "selector": {"type": "object", "description": "{label: value}, e.g. {\"service\": \"api\"}"},
            "window": {"type": "string", "description": "e.g. 15m, 1h, 24h", "default": "1h"}},
            "required": ["selector"]},
    },
    {
        "name": "query",
        "description": "Read a timeline through a saved view (narrower than `read`: only that "
                       "view's sources and filters). Select the entity by `key` or by `where`.",
        "input_schema": {"type": "object", "properties": {
            "view": {"type": "string"},
            "key": {"type": "string"},
            "where": {"type": "object"},
            "window": {"type": "string", "default": "1h"}},
            "required": ["view"]},
    },
]


def prompt_hash(prompt: str) -> str:
    """Short, stable id for the prompt that produced a finding. Without it, editing a prompt makes
    every earlier finding unattributable to the wording that caused it."""
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]


def resolve_anthropic_headers(store) -> tuple[dict[str, str], str]:
    """(header, where-it-came-from). The console-stored key wins over the env `ANTHROPIC_API_KEY`:
    the user's own key takes over from whatever the deployment shipped the moment they save one.
    That order is load-bearing for hosted trials — an operator-provided key in the env must yield
    to the customer's key instantly, so their spend lands on their key, not the trial's. Deleting
    the stored key falls back to the env key (if the deployment still carries one)."""
    stored = (store.get_setting("anthropic_key") or "").strip()
    auth_token = os.getenv("ANTHROPIC_AUTH_TOKEN", "").strip()
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    headers = {"anthropic-version": "2023-06-01",}
    if stored:
        headers["x-api-key"] = stored
        key_origin = "console"
    elif auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
        key_origin = "env:ANTHROPIC_AUTH_TOKEN"
    elif api_key:
        headers["x-api-key"] = api_key
        key_origin = "env:ANTHROPIC_API_KEY"
    else:
        return {}, ""

    return headers, key_origin


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
        # Runs live in this process, so nothing can still be running from a previous one: any row
        # left `running` is an orphan from a daemon that was killed mid-run. Left alone it stays
        # running forever and keeps counting toward the daily cap.
        reaped = store.reap_stale_agent_runs()
        if reaped:
            print(f"taresd: reaped {reaped} interrupted agent run(s)")

    # ── entry point (called by the dispatcher, once per internal subscription) ─
    def deliver(self, agent_name: str, subscription_id: str, trigger_name: str, key: str,
                payload: str, dispatch_id: str) -> None:
        """Wake a Tares agent for one firing. Logs a pending delivery immediately, then runs the
        agent in the background. Never raises and never blocks — a run must not break the dispatch."""
        agent = self.store.get_catalog_agent(agent_name)
        if agent is None:   # subscription outlived its definition (shouldn't happen) — nothing to run
            self.store.log_delivery(dispatch_id, subscription_id, agent_url(agent_name), False,
                                    "no such agent")
            return
        # pending delivery: the firing already "reached" the agent; whether it concludes is async.
        self.store.log_delivery(dispatch_id, subscription_id, agent_url(agent_name), None)
        task = asyncio.create_task(self._guarded(agent, subscription_id, trigger_name, key,
                                                 payload, dispatch_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ── manual and bootstrap runs (no firing behind them) ────────────────────
    def run_now(self, agent_name: str, trigger_name: str, key: str, payload: str) -> str | None:
        """Run an agent once outside a firing (a project bootstrapping its first pages, a manual
        re-run). Same run record, same caps and dedupe as a firing; no delivery row, since there is
        no dispatch. Returns the run id, or None when the agent does not exist or is already
        running for this key."""
        agent = self.store.get_catalog_agent(agent_name)
        if agent is None or (agent_name, key) in self._inflight:
            return None
        run_id = "run_" + uuid.uuid4().hex[:12]
        marker = (agent_name, key)
        self._inflight.add(marker)
        async def go():
            try:
                await self._run(agent, trigger_name, key, payload, run_id, None)
            except Exception as e:
                detail = f"{type(e).__name__}: {str(e) or repr(e)}"
                self.store.finish_agent_run(run_id, "failed", error=detail[:500])
                print(f"[agent {agent_name}] {detail}")
            finally:
                self._inflight.discard(marker)

        try:
            self._spawn(go)
        except RuntimeError:
            self._inflight.discard(marker)
            raise
        self.store.start_agent_run(run_id, agent_name, trigger_name, "", key,
                                   prompt_hash(agent["prompt"]), effective_max_rounds(agent))
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

    def bootstrap(self, agent_name: str, trigger_name: str, view_name: str, keys: list[str],
                  window: str = "7d", limit: int = 20, delay_s: float = 90.0,
                  concurrency: int = 10) -> None:
        """Schedule one run per key over a wide window of the view, a little later (the sources
        behind a fresh project need their first poll before there is anything to read). Keys whose
        timeline is empty at that point are skipped, so an idle repo costs nothing. Runs go through
        run_now, so the daily cap and dedupe apply."""

        async def go():
            await asyncio.sleep(delay_s)
            sem = asyncio.Semaphore(max(1, concurrency))

            async def one(key: str):
                async with sem:
                    catalog = self.runtime.catalog
                    if view_name not in catalog.views:
                        return
                    payload, count, _rows = resolve_query_full(self.store, catalog, view_name,
                                                               key=key, window=window)
                    if not count:
                        print(f"[agent {agent_name}] bootstrap: no events for {key} yet, skipped")
                        return
                    rid = self.run_now(agent_name, trigger_name, key, payload)
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
                       payload: str, dispatch_id: str) -> None:
        marker = (agent["name"], key)
        if marker in self._inflight:   # at-least-once dedupe
            self.store.update_delivery(dispatch_id, subscription_id, True, "deduped (already running)")
            return
        self._inflight.add(marker)
        run_id = "run_" + uuid.uuid4().hex[:12]
        self.store.start_agent_run(run_id, agent["name"], trigger_name, dispatch_id, key,
                                   prompt_hash(agent["prompt"]), effective_max_rounds(agent))
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
        # resolve the delivery: ok when the agent concluded ('ok'); 'empty'/'capped' are not
        # failures (it ran and declined to conclude, or hit the cap) but aren't a delivered finding
        # either — mark ok=false with the reason so the firing row is honest without crying wolf.
        self.store.update_delivery(dispatch_id, subscription_id, status == "ok",
                                   None if status == "ok" else error)

    async def _run(self, agent: dict, trigger_name: str, key: str, payload: str,
                   run_id: str, dispatch_id: str | None = None) -> tuple[str, str | None]:
        tracer = self.tracing.tracer_for(agent["name"])
        with _tracing.run_span(tracer, agent["name"], session=key, attributes={
                "tares.run_id": run_id, "tares.dispatch_id": dispatch_id or "",
                "tares.trigger": trigger_name, "tares.key": key}) as obs:
            # The instance is on the resource (service.name) too, but a backend's span view
            # may not show resource attributes; on the root span it is always in reach.
            obs.set_attribute("tares.instance", _tracing.instance_name())
            obs.set_attribute("tares.agent", agent["name"])
            status, error = await self._run_traced(agent, trigger_name, key, payload, run_id,
                                                   dispatch_id, tracer, obs)
            obs.set_attribute("tares.status", status)
            if status == "failed" and error:
                obs.error(error)
            elif error:
                obs.set_attribute("tares.note", error)
            return status, error

    async def _run_traced(self, agent: dict, trigger_name: str, key: str, payload: str,
                          run_id: str, dispatch_id: str | None, tracer,
                          obs: _tracing.Observation) -> tuple[str, str | None]:
        started_at = now_utc()
        t0 = time.monotonic()
        headers, key_origin = resolve_anthropic_headers(self.store)
        if not headers:
            msg = "no Anthropic key: set ANTHROPIC_API_KEY before `tares up`, or add a key under Settings"
            self.store.finish_agent_run(run_id, "failed", error=msg)
            return "failed", msg
        # Count the runs BEFORE this one (its row is already inserted), so the cap fires at exactly
        # DAILY_RUN_CAP runs rather than one over it.
        if self.store.agent_runs_today(agent["name"], exclude_run_id=run_id) >= DAILY_RUN_CAP:
            msg = f"cap of {DAILY_RUN_CAP} runs in the last 24h reached for this agent"
            self.store.finish_agent_run(run_id, "capped", error=msg)
            return "capped", msg
        # The agent's own budget, when set: lifetime spend from the run log. Checked before any
        # model call, so a capped run costs nothing; the message says where to raise it.
        budget = agent.get("budget_usd")
        if budget and self.store.agent_cost_total(agent["name"]) >= float(budget):
            msg = (f"budget of ${float(budget):.2f} for this agent reached; "
                   f"raise or clear it under Configuration, Advanced")
            self.store.finish_agent_run(run_id, "capped", error=msg)
            return "capped", msg

        # Usage accumulates in a mutable dict rather than the loop's return value, so a run that
        # dies mid-loop still records the tokens it already paid for (the finally below).
        model = agent.get("model") or MODEL
        usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0,
                 "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
        try:
            finding, rounds, tool_calls, external_used, exhausted, partial = await self._loop(
                agent, trigger_name, key, payload, headers, usage, tracer, obs)
        finally:
            if usage["calls"]:
                cost = cost_usd(model, usage["input_tokens"], usage["output_tokens"],
                                usage["cache_creation_input_tokens"],
                                usage["cache_read_input_tokens"])
                self.store.record_run_usage(
                    run_id, model, usage["input_tokens"], usage["output_tokens"],
                    usage["cache_creation_input_tokens"], usage["cache_read_input_tokens"], cost)
                self.store.record_model_usage(
                    "agent", agent["name"], run_id, model, usage["calls"],
                    usage["input_tokens"], usage["output_tokens"],
                    usage["cache_creation_input_tokens"], usage["cache_read_input_tokens"], cost,
                    key_source=key_origin)
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
        if not finding:
            msg = "the model returned no conclusion"
            self.store.finish_agent_run(run_id, "empty", rounds=rounds, tool_calls=tool_calls,
                                        error=msg, external_tools=external_used)
            return "empty", msg

        obs.set_output(finding)
        obs.set_attribute("tares.rounds", rounds)
        obs.set_attribute("tares.tool_calls", tool_calls)
        await self._record(agent, trigger_name, key, finding)
        self.store.finish_agent_run(run_id, "ok", rounds=rounds, tool_calls=tool_calls,
                                    finding=finding, external_tools=external_used)
        if (agent.get("webhook_url") or "").strip():
            await self._webhook(agent, {
                "event": "finding",
                "agent": agent["name"], "trigger": trigger_name, "key": key,
                "finding": finding,
                "run_id": run_id, "dispatch_id": dispatch_id,
                "model": model,
                "rounds": rounds, "tool_calls": tool_calls,
                "usage": {k: v for k, v in usage.items() if k != "calls"},
                "cost_usd": cost_usd(model, usage["input_tokens"], usage["output_tokens"],
                                     usage["cache_creation_input_tokens"],
                                     usage["cache_read_input_tokens"]),
                "started_at": started_at.isoformat(), "finished_at": now_utc().isoformat(),
                "duration_s": round(time.monotonic() - t0, 2),
                "prompt_hash": prompt_hash(agent["prompt"]),
            })
        return "ok", None

    # ── the bounded model loop ────────────────────────────────────────────────
    async def _loop(self, agent: dict, trigger_name: str, key: str, payload: str,
                    headers: dict, usage: dict, tracer=None,
                    obs: _tracing.Observation | None = None,
                    ) -> tuple[str, int, int, list[str], bool, str]:
        # External tools: the agent's selected MCP servers, connected for the duration of this
        # run. A server that fails to connect is skipped (recorded below) — losing a tool server
        # must not lose the run.
        from .mcp_client import RemoteToolbox, resolve_servers
        selected = set(agent.get("mcp_servers") or [])
        servers = resolve_servers(self.store, [m for m in self.store.list_mcp_servers()
                                               if m["name"] in selected])
        async with RemoteToolbox(servers) as toolbox:
            for failure in toolbox.failures:
                print(f"[agent {agent['name']}] mcp connect failed; {failure}")
            return await self._loop_with(agent, trigger_name, key, payload, headers, toolbox,
                                         usage, tracer, obs)

    async def _loop_with(self, agent: dict, trigger_name: str, key: str, payload: str,
                         headers: dict, toolbox, usage: dict, tracer=None,
                         obs: _tracing.Observation | None = None,
                         ) -> tuple[str, int, int, list[str], bool, str]:
        """Returns (finding, rounds, tool_calls, external_tools_used, exhausted, partial_text)."""
        tools = TOOL_DEFS + toolbox.tool_defs
        max_rounds = effective_max_rounds(agent)
        external_used: list[str] = []
        # The agent's own last conclusion for this entity, so a run builds on the previous one
        # instead of rediscovering it (worth real rounds for a context-maintaining agent). Capped:
        # it is a head start, not a second timeline.
        prior = self.store.last_finding(FINDINGS_SOURCE, agent["name"], key)
        prior_block = (f'Your finding from an earlier run on "{key}":\n\n{prior[:4000]}\n\n'
                       if prior else "")
        messages = [{
            "role": "user",
            "content": (
                f'The condition "{trigger_name}" tripped for "{key}".\n\n'
                f"{prior_block}"
                f"The correlated timeline at that moment:\n\n{payload}\n\n"
                f"Take a first look, per your instructions. You already hold the evidence above; "
                f"read again only if you need a wider window or a different entity."),
        }]
        rounds = tool_calls = 0
        last_text = ""
        if obs is not None:
            obs.set_input(messages[0]["content"])
        async with httpx.AsyncClient(timeout=TOOL_TIMEOUT) as cx:
            async def call(with_tools: bool) -> dict:
                # The tools stay declared on the final call (a conversation holding tool_use
                # blocks must define them); tool_choice none is what disables them.
                body = {"model": agent.get("model") or MODEL,
                        "max_tokens": MAX_TOKENS, "system": agent["prompt"],
                        "tools": tools, "messages": messages}
                if not with_tools:
                    body["tool_choice"] = {"type": "none"}
                with _tracing.generation(tracer, body["model"], messages,
                                         {"max_tokens": MAX_TOKENS}) as gen:
                    r = await cx.post(
                        f"{API_BASE}/v1/messages",
                        headers=headers,
                        json=body)
                    if r.status_code >= 400:
                        raise RuntimeError(f"anthropic {r.status_code}: {r.text[:300]}")
                    msg = r.json()
                    # Defensive get: stubs (tests) may answer without a usage block.
                    u = msg.get("usage") or {}
                    usage["calls"] += 1
                    for field in ("input_tokens", "output_tokens",
                                  "cache_creation_input_tokens", "cache_read_input_tokens"):
                        usage[field] += int(u.get(field) or 0)
                    gen.set_output(msg.get("content") or [])
                    gen.set_usage(u)
                    gen.set_response_model(msg.get("model"))
                    gen.set_finish_reason(msg.get("stop_reason"))
                return msg

            for rounds in range(1, max_rounds + 1):
                msg = await call(True)
                messages.append({"role": "assistant", "content": msg["content"]})
                text = "\n".join(b.get("text", "") for b in msg["content"]
                                 if b.get("type") == "text").strip()
                if text:
                    last_text = text

                uses = [b for b in msg["content"] if b.get("type") == "tool_use"]
                if not uses:
                    return text, rounds, tool_calls, external_used, False, ""

                results = []
                for u in uses:
                    tool_calls += 1
                    name = str(u["name"])
                    with _tracing.tool_span(tracer, name, u.get("input") or {}) as tobs:
                        try:
                            if toolbox.owns(name):
                                external_used.append(name)
                                out = await toolbox.call(name, u.get("input") or {})
                            else:
                                out = self._tool(agent["name"], name, u.get("input") or {})
                        except Exception as e:   # a tool error is evidence, not a crash
                            out = f"tool error: {type(e).__name__}: {e}"
                            tobs.tool_error(out)
                        else:
                            tobs.set_output(out)
                    results.append({"type": "tool_result", "tool_use_id": u["id"], "content": out})
                messages.append({"role": "user", "content": results})

            # Budget exhausted with tool calls still pending. Ask once more, tools disabled, for a
            # conclusion from what it has (this is the +1 call). If it concludes, that is the
            # finding; if not, the run is `exhausted` and the last text is kept as a partial note.
            messages.append({"role": "user", "content": (
                "You have used your round budget. Do not call any more tools. Conclude now from "
                "the evidence you already have; if it is inconclusive, say what you found so far "
                "and what you would look at next.")})
            try:
                msg = await call(False)
            except RuntimeError:
                return "", rounds, tool_calls, external_used, True, last_text
            text = "\n".join(b.get("text", "") for b in msg["content"]
                             if b.get("type") == "text").strip()
            if text:
                return text, rounds, tool_calls, external_used, False, ""
        return "", rounds, tool_calls, external_used, True, last_text

    def _tool(self, agent_name: str, name: str, args: dict) -> str:
        """The two reads, in-process. No HTTP hop and no credential: a Tares agent IS Tares, so
        it reads through the same resolver the API serves rather than authenticating to itself."""
        catalog = self.runtime.catalog
        window = str(args.get("window") or "1h")
        if name == "read":
            selector = args.get("selector") or {}
            if not isinstance(selector, dict) or not selector:
                raise ValueError('read needs a selector, e.g. {"service": "checkout"}')
            payload, nrows, _sources, _rows = resolve_read(self.store, catalog, selector, window)
            self.store.log_query("r_" + uuid.uuid4().hex[:12], "(read)",
                                 ", ".join(f"{k}={v}" for k, v in selector.items()),
                                 window, nrows, f"agent:{agent_name}")
            return payload
        if name == "query":
            view = str(args.get("view") or "")
            if view not in catalog.views:
                raise KeyError(f"unknown view {view!r} (available: {', '.join(catalog.views)})")
            key = args.get("key")
            where = args.get("where") or None
            payload, nrows, _rows = resolve_query_full(self.store, catalog, view, key, window,
                                                       where=where)
            self.store.log_query("q_" + uuid.uuid4().hex[:12], view, str(key or where or ""),
                                 window, nrows, f"agent:{agent_name}")
            return payload
        raise ValueError(f"unknown tool {name!r}")

    # ── the finding: an event, plus an optional Slack copy ────────────────────
    def _entity_label(self, trigger_name: str) -> str | None:
        """Which label the firing entity was identified by — the key field of the trigger's view,
        else the primary label of one of that view's sources. The finding must be stamped with the
        same axis as its evidence, or a label-native `read` for the entity won't return it."""
        catalog = self.runtime.catalog
        trig = next((t for t in catalog.triggers if t.name == trigger_name), None)
        view = catalog.views.get(trig.view) if trig else None
        if view is None:
            return None
        if view.key_field:
            return view.key_field
        for src_name in view.sources:
            src = catalog.sources.get(src_name)
            for spec in (src.config.get("labels") or []) if src else []:
                if spec.get("primary"):
                    return spec.get("name")
        return None

    async def _record(self, agent: dict, trigger_name: str, key: str, finding: str) -> None:
        if FINDINGS_SOURCE not in self.runtime.catalog.sources:
            # provisioned on the first finding, like the memory source — a fresh install has no
            # reason to carry an empty one. No `labels` config: the runner stamps them per event.
            self.store.upsert_catalog_source(FINDINGS_SOURCE, "finding", "finding", "5s", {})
            self.runtime.reload_catalog()
            print(f"taresd: auto-provisioned findings source {FINDINGS_SOURCE!r}")

        label = self._entity_label(trigger_name)
        await self.runtime.ingest(FINDINGS_SOURCE, {
            "key": key, "finding": finding, "agent": agent["name"], "trigger": trigger_name,
            "prompt_hash": prompt_hash(agent["prompt"]),
            "labels": {label: key} if label else {},
        })
        # Notification: the workspace bot posting to a channel is the primary path (one token,
        # picked from a list, no credential per agent); the per-agent incoming webhook stays as
        # the secondary/legacy path. An agent uses one — channel wins when both are set.
        channel = (agent.get("slack_channel") or "").strip()
        hook = agent.get("slack_webhook")
        if channel:
            await self._slack_channel(agent["name"], channel, trigger_name, key, finding)
        elif hook:
            await self._slack(agent["name"], hook, trigger_name, key, finding)

    async def _webhook(self, agent: dict, body: dict, attempts: int = 3) -> None:
        """POST the finding plus its run metadata to the agent's write-back webhook — the machine
        counterpart of the Slack post, for feeding findings into the customer's own automation.

        The body carries only what the customer may already read via the API: the finding, the
        run's shape (rounds, tool calls, duration), the model name and a hash of the prompt —
        never a key or token. Auth is an optional bearer token sent as a header; it is never
        logged, and a delivery failing must never lose the finding (already stored)."""
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
                        return
                    if r.status_code < 500:   # client error — won't self-heal, don't retry
                        print(f"[agent {agent['name']}] webhook: HTTP {r.status_code}")
                        return
                    err = f"HTTP {r.status_code}"
                except Exception as e:        # transport failure — unreachable / timeout / DNS
                    err = f"{type(e).__name__}: {str(e)[:120]}"
                if attempt < attempts - 1:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 10)
        print(f"[agent {agent['name']}] webhook: giving up after {attempts} attempts ({err})")

    async def _slack_channel(self, agent_name: str, channel: str, trigger_name: str,
                             key: str, finding: str) -> None:
        """Post the finding through the workspace bot (`chat.postMessage`). Same message shape as
        the webhook path — the full finding, standing alone — but the credential is the one bot
        token the instance already holds, and the target is a channel picked from a list.

        One attempt, verdict from `slack.classify` (Slack answers HTTP 200 with ok:false), failure
        printed — a notification failing must never lose the finding, which is already stored."""
        from . import slack as _slack_mod
        token, _origin = _slack_mod.resolve_token(self.store)
        if not token:
            print(f"[agent {agent_name}] slack: no bot token configured, channel post skipped")
            return
        msg = _slack_mod.build_finding_message(agent_name, trigger_name, key, finding,
                                               _slack_deep_link(key))
        try:
            async with httpx.AsyncClient(timeout=15) as cx:
                r = await cx.post(f"{_slack_mod.API_BASE}/chat.postMessage",
                                  json={"channel": channel, **msg},
                                  headers={"authorization": f"Bearer {token}"})
                try:
                    data = r.json()
                except Exception:
                    data = None
                ok, error, _retry = _slack_mod.classify(r.status_code, data)
                if not ok:
                    print(f"[agent {agent_name}] slack: {error}")
        except Exception as e:   # notification failing must never lose the finding
            print(f"[agent {agent_name}] slack: {type(e).__name__}: {e}")

    async def _slack(self, agent_name: str, hook: str, trigger_name: str, key: str,
                     finding: str) -> None:
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
        from .slack import build_finding_message
        msg = build_finding_message(agent_name, trigger_name, key, finding, _slack_deep_link(key))
        try:
            async with httpx.AsyncClient(timeout=15) as cx:
                r = await cx.post(hook, json=msg)
                if r.status_code >= 300:
                    print(f"[agent {agent_name}] slack: HTTP {r.status_code} {r.text[:120]}")
        except Exception as e:   # notification failing must never lose the finding
            print(f"[agent {agent_name}] slack: {type(e).__name__}: {e}")
