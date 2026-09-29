"""Push dispatch — deliver a fired trigger to its subscribers, with retry/backoff.

The dispatch body carries the rendered trigger payload, so the agent boots already holding the
correlated timeline (zero reads to begin). At-least-once; subscribers dedupe on `dispatch_id`.

Three kinds of subscriber, ONE mechanism: an external agent's webhook (POST), a Tares agent
(run in-process), and a Slack channel. All three are ordinary subscription rows — a Tares
agent's URL uses the tares://agent/ scheme, a channel's uses slack://channel/ — so all three are
logged as deliveries and appear identically in the roster, a trigger's woken-agents list, and
recent firings. The in-process runs are dispatched without awaiting: an investigation takes
minutes and must not delay a webhook delivery on the same trigger.
"""
from __future__ import annotations

import asyncio
import uuid

import httpx

from . import slack
from .config import agent_name_from_url, slack_channel_from_url
from .envelope import now_utc


class Dispatcher:
    def __init__(self, store):
        self.store = store
        # Set by the daemon once the runtime exists — the in-process executor for Tares-agent
        # subscriptions. External-only deployments can leave it None.
        self.agents = None
        # Set by the daemon too: the live runtime, so a deferred trigger evaluation can read the
        # current catalog instead of the one captured when it was scheduled.
        self.runtime = None

    async def fire(self, trigger, key: str, payload: str,
                   parent_run_id: str | None = None) -> None:
        """`parent_run_id` is the run whose finding tripped this firing, when a trigger reads
        findings: the project timeline puts the firing under that run (TR-331)."""
        project = getattr(trigger, "project", None)
        subs = list(self.store.list_subscriptions(trigger.name))
        if project:
            # subscriptions to the whole project (an external agent that joined it, TR-336): every
            # trigger of it, the ones added after the subscription included. A URL subscribed both
            # ways is delivered to once.
            urls = {url for _sid, _t, url in subs}
            subs += [(s["subscription_id"], trigger.name, s["url"])
                     for s in self.store.list_project_subscriptions(project)
                     if s["url"] not in urls]
        woken_by = "schedule" if getattr(trigger.condition, "every", None) else "trigger"
        kind = trigger.emit.get("kind", trigger.name)
        dispatch_id = uuid.uuid4().hex
        body = {
            "dispatch_id": dispatch_id,
            "trigger": trigger.name,
            "project": project,
            "kind": kind,
            "key": key,
            "fired_at": now_utc().isoformat(),
            "payload": payload,
        }
        delivered = 0
        # Tares agents first, so the webhook body can name the runs this firing started.
        # Tares agent: run in-process. deliver() logs a pending delivery now and resolves it
        # (ok/error) when the run finishes, so `delivered` here is the synchronous count (external
        # only); list_dispatches computes the live total including agents.
        run_ids = []
        for sid, _trig, url in subs:
            agent_name = agent_name_from_url(url)
            if agent_name is not None and self.agents is not None:
                rid = self.agents.deliver(agent_name, sid, trigger.name, key, payload, dispatch_id,
                                          woken_by=woken_by)
                if rid:
                    run_ids.append(rid)
        body["run_ids"] = run_ids
        for sid, _trig, url in subs:
            if agent_name_from_url(url) is not None:
                continue
            channel = slack_channel_from_url(url)
            if channel is not None:
                ok, error = await self._slack_post(channel, trigger.name, key, payload,
                                                   body["fired_at"], dispatch_id)
            else:
                ok, error = await self._post(url, body)
            self.store.log_delivery(dispatch_id, sid, url, ok, error)   # per-agent delivery history
            if ok:
                delivered += 1
        # log every firing, even with zero subscribers — the UI shows what would have woken agents
        self.store.log_dispatch(dispatch_id, trigger.name, key, kind,
                                len(subs), delivered, payload,
                                project=project,
                                parent_run_id=parent_run_id)

    async def _post(self, url: str, body: dict, attempts: int = 5) -> tuple[bool, str | None]:
        """Deliver to one subscriber. Returns (ok, error): ok only on a 2xx. A 4xx is a definitive
        failure (bad secret / wrong path / gone) — recorded, not retried. 5xx and transport errors
        (unreachable, timeout, DNS) are retried with backoff; the last error is returned so the UI
        can say WHY a delivery shows 0, instead of a silent failure."""
        delay = 1.0
        error = None
        async with httpx.AsyncClient(timeout=10) as cx:
            for attempt in range(attempts):
                try:
                    r = await cx.post(url, json=body)
                    if 200 <= r.status_code < 300:
                        return True, None
                    error = f"HTTP {r.status_code}"
                    if r.status_code < 500:       # client error — won't self-heal, don't retry
                        return False, error
                except Exception as e:            # transport failure — unreachable / timeout / DNS
                    error = (type(e).__name__ + (f": {e}" if str(e).strip() else ""))[:200]
                if attempt < attempts - 1:        # no point sleeping after the final attempt
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 30)
        return False, error

    async def _slack_post(self, channel: str, trigger: str, key: str, payload: str,
                          fired_at: str | None = None, dispatch_id: str | None = None,
                          attempts: int = 5) -> tuple[bool, str | None]:
        """Deliver one firing to a Slack channel. Same contract as `_post` — (ok, error), same
        five attempts and the same capped exponential backoff — so a Slack subscription's delivery
        row reads identically to a webhook's.

        The difference is where the verdict comes from: `chat.postMessage` answers HTTP 200 with
        `{"ok": false, "error": ...}`, so the retry decision is made by `slack.classify` on the
        body rather than by the status code. A revoked token or a channel the bot isn't in fails on
        the FIRST attempt with that reason in the ledger, instead of five timeouts and no reason.
        """
        token, _origin = slack.resolve_token(self.store)
        if not token:
            # Not retryable and not transient: nothing about waiting 30s makes a token appear.
            return False, "slack: no bot token configured (set TARES_SLACK_BOT_TOKEN or add one under Settings)"
        msg = {"channel": channel,
               **slack.build_message(trigger, key, payload, fired_at, dispatch_id)}
        headers = {"authorization": f"Bearer {token}", "content-type": "application/json"}
        delay = 1.0
        error = None
        async with httpx.AsyncClient(timeout=10) as cx:
            for attempt in range(attempts):
                try:
                    r = await cx.post(f"{slack.API_BASE}/chat.postMessage",
                                      json=msg, headers=headers)
                    try:
                        data = r.json()
                    except Exception:
                        data = None
                    ok, error, retry = slack.classify(r.status_code, data)
                    if ok:
                        return True, None
                    if not retry:
                        return False, error
                    # Slack tells us how long to wait when it rate-limits; obeying it is the
                    # difference between backing off once and being throttled for the whole window.
                    if after := r.headers.get("retry-after"):
                        try:
                            delay = max(delay, min(float(after), 60.0))
                        except ValueError:
                            pass
                except Exception as e:            # transport failure — unreachable / timeout / DNS
                    error = ("slack: " + type(e).__name__
                             + (f": {e}" if str(e).strip() else ""))[:200]
                if attempt < attempts - 1:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 30)
        return False, error
