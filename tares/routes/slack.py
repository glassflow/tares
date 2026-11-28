"""Slack: the bot token, its team and the signing secret, the channel picker, the inbound endpoint
and posting to a channel."""
from __future__ import annotations

import asyncio
import json
import time
from urllib.parse import parse_qs

import httpx
from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from .. import factory_api, slack_verify
from .. import slack as slack_mod
from ..deps import Deps
from ..slack import SETTING_KEY as SLACK_TOKEN_SETTING
from ..slack import resolve_token as resolve_slack_token

SLACK_TEAM_SETTING = "slack_team"   # JSON {id, name}: the Slack team the bot token belongs to
SLACK_EVENTS_PATH = "/api/slack/events"
SLACK_CHANNELS_TTL = 60.0         # seconds a good channel list is served from the cell
SLACK_CHANNELS_FAIL_TTL = 5.0     # ... and a failed one at least (longer on a 429's Retry-After)


class SlackTokenIn(BaseModel):
    token: str  # same contract as AnthropicKeyIn: set a new one, or DELETE to clear


class SlackSigningSecretIn(BaseModel):
    secret: str  # the signing secret behind POST /api/slack/events; same write-only contract


class SlackChannelsChangedIn(BaseModel):
    event: str = ""      # the Slack event behind it (member_joined_channel, channel_rename, ...)
    channel: str = ""    # the channel it concerns; informational, the whole list is re-read


class SlackTeamIn(BaseModel):
    team_id: str     # the Slack workspace the bot token belongs to, e.g. T0123ABC
    team_name: str = ""



async def post_slack(deps: Deps, channel: str, text: str, title: str = "") -> dict:
    """Post markdown to a Slack channel as the cell's bot. LookupError when Slack is not
    connected, RuntimeError with Slack's reason when it does not take the message."""
    token, _origin = resolve_slack_token(deps.store)
    if not token:
        raise LookupError("Slack is not connected: add it under Settings, Slack")
    msg = {"channel": channel, "text": slack_mod.excerpt(text, 300) or title or "Tares",
           "blocks": ([slack_mod._header(title)] if title else []) + slack_mod._sections(text),
           "unfurl_links": False, "unfurl_media": False}
    try:
        async with httpx.AsyncClient(timeout=15) as cx:
            r = await cx.post(f"{slack_mod.API_BASE}/chat.postMessage", json=msg,
                              headers={"authorization": f"Bearer {token}"})
        try:
            data = r.json()
        except ValueError:
            data = None
    except httpx.HTTPError as e:
        raise RuntimeError(f"could not reach Slack: {type(e).__name__}") from None
    ok, error, _retry = slack_mod.classify(r.status_code, data)
    if not ok:
        raise RuntimeError(error or "Slack refused the message")
    return data or {}


def router(deps: Deps) -> APIRouter:
    r = APIRouter()
    store = deps.store
    _err = deps.err

    # ── the Slack bot token: same contract as the Anthropic key ──────────────
    # One token per instance, behind the cell's Slack posts. It is a credential, so it is
    # write-only over the API exactly like the model key: the console can learn THAT one resolves
    # and where from, never what it is.
    @r.get("/api/settings/slack-bot-token")
    def get_slack_token():
        token, origin = resolve_slack_token(store)
        return {"configured": bool(token), "source": origin,
                "stored": bool(store.get_setting(SLACK_TOKEN_SETTING)),
                "env_overrides": origin.startswith("env:"), "team": _slack_team()}

    # ── the Slack team the bot token belongs to (TR-365) ─────────────────────
    # Not a secret: the control plane stores it next to the token it pushes, so Settings can say
    # which Slack workspace this one posts to. DELETE clears it, on disconnect.
    def _slack_team() -> dict | None:
        raw = store.get_setting(SLACK_TEAM_SETTING)
        if not raw:
            return None
        try:
            t = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(t, dict):
            return None
        return {"id": t.get("id") or "", "name": t.get("name") or ""}

    @r.put("/api/settings/slack-team")
    def set_slack_team(body: SlackTeamIn):
        team_id = body.team_id.strip()
        if not team_id:
            _err(ValueError("team_id is required (use DELETE to clear the team)"))
        store.set_setting(SLACK_TEAM_SETTING,
                          json.dumps({"id": team_id, "name": body.team_name.strip()}))
        return {"ok": True, "team": _slack_team()}

    @r.delete("/api/settings/slack-team")
    def clear_slack_team():
        store.set_setting(SLACK_TEAM_SETTING, None)
        return {"ok": True, "team": None}

    @r.put("/api/settings/slack-bot-token")
    def set_slack_token(body: SlackTokenIn):
        token = body.token.strip()
        if not token:
            _err(ValueError("token is required (use DELETE to remove the stored token)"))
        if not token.startswith("xox"):
            # Caught here rather than on the first failed delivery: a pasted webhook URL or signing
            # secret would otherwise sit in the settings table looking configured.
            _err(ValueError("that does not look like a Slack bot token; it should start with "
                            "'xoxb-' (OAuth & Permissions → Bot User OAuth Token)"))
        store.set_setting(SLACK_TOKEN_SETTING, token)
        _drop_channels_cache(forget=True)
        _, origin = resolve_slack_token(store)
        return {"ok": True, "source": origin,
                **({"note": "an environment token takes precedence and is still in use"}
                   if origin.startswith("env:") else {})}

    @r.delete("/api/settings/slack-bot-token")
    def clear_slack_token():
        store.set_setting(SLACK_TOKEN_SETTING, None)
        _drop_channels_cache(forget=True)
        token, origin = resolve_slack_token(store)
        return {"ok": True, "configured": bool(token), "source": origin}

    # ── the channels the console's picker offers ─────────────────────────────
    # Every public channel plus the private ones the bot is in, so that picking where something
    # goes never means digging a channel ID out of Slack's own UI.
    #
    # ALWAYS 200, never an exception: the console branches on `reason` and says why there is
    # nothing to pick (not connected, reconnect for the scope, Slack did not answer). A Slack
    # outage, or the very common token issued before `channels:read`/`groups:read` were
    # requested, must not blank the page.
    #
    # Cached in the cell: an open picker re-reads every few seconds, and conversations.list is
    # rate limited per workspace. A good list is kept a minute, a failure a few seconds (so Try
    # again soon asks Slack again), or as long as Slack's Retry-After says on a 429. When a re-read
    # fails while a good list is known, that list is served with `stale: true` rather than an
    # error: a picker that worked a minute ago must not turn into "Slack did not answer".
    # POST /api/slack/channels/changed drops it when Slack says a channel changed, so a channel
    # Tares was just added to shows up within one poll.
    #
    # `gen` counts the drops. A read captures it before asking Slack and stores its answer only if
    # no drop came in meanwhile; otherwise that answer predates the change and would hide it for
    # a minute. It still goes back to the caller that asked, just not into the cache.
    _channels = {"token": "", "expires": 0.0, "resp": None, "good": None, "gen": 0}
    _channels_lock = asyncio.Lock()       # one Slack read at a time; pickers poll together

    def _drop_channels_cache(forget: bool = False) -> None:
        """Make the next read ask Slack. `forget` also drops the last good list (the token
        changed, so that list may belong to another Slack)."""
        _channels["gen"] += 1
        _channels["expires"] = 0.0
        if forget:
            _channels.update(token="", resp=None, good=None)

    @r.get("/api/slack/channels")
    async def list_slack_channels():
        token, _origin = resolve_slack_token(store)
        if not token:
            return {"channels": [], "reason": "no_token"}
        async with _channels_lock:
            if _channels["token"] != token:      # not the token the kept list was read with
                _channels.update(token=token, expires=0.0, resp=None, good=None)
            if _channels["resp"] is not None and _channels["expires"] > time.monotonic():
                return _channels["resp"]
            gen = _channels["gen"]
            try:
                channels, reason, detail, retry = await slack_mod.list_channels(token)
            except Exception as e:                  # belt and braces: list_channels swallows too
                channels, reason, detail, retry = [], "error", f"{type(e).__name__}: {e}"[:200], None
            if reason is None:
                out = {"channels": channels, "reason": None}
                ttl = SLACK_CHANNELS_TTL
            else:
                good = _channels["good"]
                out = ({**good, "stale": True, **({"detail": detail} if detail else {})} if good
                       else {"channels": [], "reason": reason, **({"detail": detail} if detail else {})})
                ttl = max(SLACK_CHANNELS_FAIL_TTL, retry or 0.0)
            if _channels["gen"] == gen and _channels["token"] == token:
                _channels.update(expires=time.monotonic() + ttl, resp=out)
                if reason is None:
                    _channels["good"] = out
            return out

    @r.post("/api/slack/channels/changed")
    def slack_channels_changed(body: SlackChannelsChangedIn | None = None):
        """Tares Cloud forwards Slack's channel events here (member_joined_channel, channel_created,
        channel_rename, channel_archive); the next read of the list asks Slack again."""
        _drop_channels_cache()
        return {"ok": True}

    # ── the Slack signing secret: the credential that makes inbound Slack safe ──
    # Same write-only contract as the bot token. Without it POST /api/slack/events answers 503:
    # there is no configuration in which accepting an unverified inbound request is correct.
    @r.get("/api/settings/slack-signing-secret")
    def get_slack_signing_secret():
        secret, origin = slack_verify.resolve_secret(store)
        return {"configured": bool(secret), "source": origin,
                "stored": bool(store.get_setting(slack_verify.SETTING_KEY)),
                "env_overrides": origin.startswith("env:")}

    @r.put("/api/settings/slack-signing-secret")
    def set_slack_signing_secret(body: SlackSigningSecretIn):
        secret = body.secret.strip()
        if not secret:
            _err(ValueError("secret is required (use DELETE to remove the stored secret)"))
        if secret.startswith("xox"):
            # A bot token pasted into the wrong box would look configured and fail every signature.
            _err(ValueError("that is a bot token, not the signing secret; take the Signing Secret "
                            "from the Slack app's Basic Information page"))
        store.set_setting(slack_verify.SETTING_KEY, secret)
        _, origin = slack_verify.resolve_secret(store)
        return {"ok": True, "source": origin,
                **({"note": "an environment secret takes precedence and is still in use"}
                   if origin.startswith("env:") else {})}

    @r.delete("/api/settings/slack-signing-secret")
    def clear_slack_signing_secret():
        store.set_setting(slack_verify.SETTING_KEY, None)
        secret, origin = slack_verify.resolve_secret(store)
        return {"ok": True, "configured": bool(secret), "source": origin}

    # ── inbound Slack: the /tares slash command ───────────────────────────
    # The only route in this daemon that is public to the auth middleware AND accepts a body from
    # the internet, so it carries its own authentication: an HMAC over the raw bytes.
    _slack_workspace = slack_mod.workspace_slug(deps.cloud_links().get("slack_connect_url", ""))   # None self-hosted
    _ask_tasks: set = set()      # keeps background tasks referenced; asyncio only holds weak refs

    async def _slack_respond(response_url: str, body: dict) -> None:
        """Deliver one message to Slack's `response_url`. Best-effort with a couple of retries: the
        user is waiting, and there is nowhere else to put the answer if this fails."""
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=10) as cx:
                    r = await cx.post(response_url, json=body)
                if r.status_code < 400:
                    return
                if r.status_code < 500:
                    print(f"taresd: slack response_url rejected the answer: "
                          f"HTTP {r.status_code} {r.text[:200]}", flush=True)
                    return
            except Exception as e:
                print(f"taresd: slack response_url failed: {type(e).__name__}: {e}", flush=True)
            if attempt < 2:
                await asyncio.sleep(1.0 * (attempt + 1))

    async def _slack_post(token: str, method: str, body: dict) -> None:
        try:
            async with httpx.AsyncClient(timeout=15) as cx:
                r = await cx.post(f"{slack_mod.API_BASE}/{method}", json=body,
                                  headers={"authorization": f"Bearer {token}"})
            try:
                data = r.json()
            except Exception:
                data = None
            ok, error, _retry = slack_mod.classify(r.status_code, data)
            if not ok:
                print(f"taresd: slack {method}: {error}", flush=True)
        except Exception as e:
            print(f"taresd: slack {method} failed: {type(e).__name__}: {e}", flush=True)

    async def _slack_interaction(payload: dict) -> None:
        """A click on a button Tares posted that is not a link: an old message's button whose
        feature is gone. Say so to the clicker alone, through the payload's response_url. Never
        raises: it runs detached from the request."""
        try:
            response_url = str(payload.get("response_url") or "").strip()
            if response_url:
                await _slack_respond(response_url, {"response_type": "ephemeral",
                                                    "replace_original": False,
                                                    "text": slack_mod.GONE_NOTE})
        except Exception as e:   # noqa: BLE001 - nobody is waiting on this, so the log is the place
            print(f"taresd: slack interaction failed: {type(e).__name__}: {e}", flush=True)

    @r.post(SLACK_EVENTS_PATH, include_in_schema=False)
    async def slack_events(request: Request):
        """Slack's inbound endpoint: the URL-verification handshake, button clicks, and the
        `/tares` command, which only says that asking from Slack is gone (TR-1810).

        Two contracts shape this whole handler:

        · **The signature covers the RAW body.** Read `await request.body()` and parse afterwards —
          letting FastAPI decode and re-serialise changes the bytes and every HMAC fails.
        · **Slack allows 3 seconds to ACK.** Anything slower (the note to a button's clicker)
          runs in a background task after the ACK.
        """
        raw = await request.body()
        secret, _origin = slack_verify.resolve_secret(store)
        if not secret:
            # Never "accept and hope". Refusing to serve is the only safe failure here.
            return JSONResponse({"detail": "Slack is not configured on this instance: set "
                                           f"{slack_verify.ENV_VAR} (or add the signing secret "
                                           "under Settings) and try again"}, status_code=503)
        if (why := slack_verify.check(request.headers, raw, secret)) is not None:
            # The reason goes to the operator's log, not to the caller: telling a forger which part
            # of their forgery failed is free help.
            print(f"taresd: rejected an inbound Slack request ({why})", flush=True)
            return JSONResponse({"detail": "invalid Slack signature"}, status_code=401)

        ctype = request.headers.get("content-type", "")
        if "json" in ctype:
            try:
                body = json.loads(raw or b"{}")
            except ValueError:
                return JSONResponse({"detail": "malformed JSON"}, status_code=400)
            if body.get("type") == "url_verification":
                # Without this the Slack app can never be pointed at this daemon at all.
                return {"challenge": body.get("challenge", "")}
            # Event subscriptions are out of scope (slash command only) — ACK so Slack doesn't
            # retry, and say nothing.
            return {"ok": True}

        form = {k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace")).items()}
        if "payload" in form:
            # An interaction: someone clicked a button on a message Tares posted. A link button
            # (Open in Tares on a waiting item, View in Tares) already took the browser where it
            # points, so its click only needs the 200. Any other button is an old message's, whose
            # feature is gone: the clicker is told so, in the background. Slack wants its 200
            # within 3 seconds, and without it puts a warning triangle next to the button. Always
            # 200, whatever the payload holds.
            try:
                payload = json.loads(form["payload"])
            except ValueError:
                payload = None
            if isinstance(payload, dict) and payload.get("type") == "block_actions" \
                    and not slack_mod.is_link_click(payload) \
                    and not slack_mod.is_foreign_click(payload, _slack_workspace):
                task = asyncio.create_task(_slack_interaction(payload))
                _ask_tasks.add(task)
                task.add_done_callback(_ask_tasks.discard)
            elif not (isinstance(payload, dict) and payload.get("type") == "block_actions"):
                print("taresd: ignored a Slack interaction that is not a block action", flush=True)
            return Response(status_code=200)
        # `/tares ask` went with Ask (TR-1810): any command gets the same note, ephemeral
        thread_ts = (form.get("thread_ts") or "").strip() or None
        return slack_mod.build_error(slack_mod.ASK_GONE_NOTE, thread_ts)

    @r.post("/api/slack/post")
    async def slack_post(request: Request, body: dict = Body(...)):
        """{channel, text, title?}: post markdown to a Slack channel as the cell's bot (the
        standup's "Send to Slack"). A person's action: refused from a crew station."""
        if factory_api.caller(request)["station"]:
            _err(PermissionError("posting to Slack is the person's action, from the console"), 403)
        channel = str(body.get("channel") or "").strip()
        text = str(body.get("text") or "").strip()
        if not channel or not text:
            _err(ValueError("say which channel and what to post"))
        title = " ".join(str(body.get("title") or "").split())
        try:
            data = await post_slack(deps, channel, text, title)
        except LookupError as e:
            _err(e, 409)
        except RuntimeError as e:
            _err(e, 502)
        return {"ok": True, "channel": channel, "ts": data.get("ts")}

    return r
