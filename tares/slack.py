"""Slack — the bot token, the outbound message, and the `/tares` slash command's replies.

Outbound (the dispatch sink): resolve the bot token, format a firing as Block Kit, post it.
Inbound (the slash command): parse `/tares ask …`, bound its cost, and format the answer that
goes back to Slack's `response_url`. Signature verification lives next door in `slack_verify.py`
— it is the security boundary and is kept separately reviewable.

This is the *generic* half of Tares's Slack support: a bot token that an operator configures
(env or console) and one `chat.postMessage` call. Nothing here knows about OAuth or hosted
installs; a self-hosted user gets the full feature by pasting a token.

Deliberately plain `httpx`, no `slack_sdk`: this is one HTTP call, and `Dispatcher` already owns
its client, its timeout and its retry policy. Adding an SDK for it would buy a dependency and a
second, divergent retry loop.

The one thing that needs care is Slack's error taxonomy. `chat.postMessage` answers **HTTP 200
with `{"ok": false, "error": "..."}`** for most failures, so the status code alone cannot decide
whether to retry — a revoked token would otherwise burn all five attempts and land in the ledger
as a timeout rather than as "invalid_auth".
"""
from __future__ import annotations

import os
import re
from urllib.parse import quote
import time
from collections import deque
from datetime import datetime, timezone

import httpx

API_BASE = os.getenv("TARES_SLACK_API_BASE", "https://slack.com/api").rstrip("/")
SETTING_KEY = "slack_bot_token"
ENV_VAR = "TARES_SLACK_BOT_TOKEN"

# Failures that will still be failures on the fifth attempt. Retrying these wastes ~30s of backoff
# and, worse, buries the real reason: the ledger must say "channel_not_found", not "timeout".
DEFINITIVE_ERRORS = {
    "invalid_auth", "not_authed", "account_inactive", "token_revoked", "token_expired",
    "no_permission", "missing_scope", "ekm_access_denied", "org_login_required",
    "channel_not_found", "not_in_channel", "is_archived", "restricted_action",
    "restricted_action_read_only_channel", "restricted_action_thread_only_channel",
    "msg_too_long", "no_text", "invalid_blocks", "invalid_blocks_format", "invalid_arguments",
    "team_access_not_granted", "as_user_not_supported",
}

# Section text caps at 3000 chars; leave room for the code fence we wrap the timeline in.
_MAX_SECTION = 2800


def resolve_token(store) -> tuple[str, str]:
    """(token, where-it-came-from). Environment wins over the console-stored value: an operator's
    deployment config is never silently overridden by something typed into a UI months earlier.
    Deliberately NOT the Anthropic key's order (resolve_key flipped to console-wins for hosted
    trials); the Slack token is infrastructure wiring, not a metered credential, so env-wins
    stays right here."""
    val = os.getenv(ENV_VAR, "").strip()
    if val:
        return val, f"env:{ENV_VAR}"
    stored = ((store.get_setting(SETTING_KEY) or "") if store is not None else "").strip()
    return (stored, "console") if stored else ("", "")


def public_base() -> str:
    """The instance's reachable address, or "". A link to 127.0.0.1 is worse than no link, so
    nothing is linked until the operator says the instance is reachable (TARES_PUBLIC_URL)."""
    return os.getenv("TARES_PUBLIC_URL", "").strip().rstrip("/")


def entity_url(key: str) -> str:
    """The entity's timeline in the console, or "" without a public address."""
    base = public_base()
    return f"{base}/explore?key={quote(key, safe='')}" if base and key else ""


def run_url(agent: str, run_id: str | None) -> str:
    """One run on its agent's page, opened and scrolled to, or "" without a public address."""
    base = public_base()
    if not base or not agent:
        return ""
    url = f"{base}/agents/{quote(agent, safe='')}?tab=runs"
    return url + (f"&run={quote(run_id, safe='')}" if run_id else "")


def dispatch_url(dispatch_id: str | None) -> str:
    base = public_base()
    return f"{base}/dispatches/{dispatch_id}" if base and dispatch_id else ""


def dispatch_link(dispatch_id: str | None) -> str:
    """The `<url|label>` for one firing. A trigger alert is about the firing, not the entity: the
    dispatch page is the thing that answers "what actually fired, and what did it carry" — which is
    the question someone reading the alert in Slack has."""
    base = public_base()
    return f"<{base}/dispatches/{dispatch_id}|Open in Tares>" if base and dispatch_id else ""


# `[T-1734s]` on every event line. Correct, and what an agent wants; unreadable at a glance in a
# chat client. Rewritten in the SLACK COPY ONLY — the payload itself is the agent-facing contract
# (it goes out over MCP verbatim), so it keeps its exact seconds.
_AGE = re.compile(r"\[T-(\d+)s\]")


def _humanize_ages(text: str) -> str:
    def one(m: re.Match) -> str:
        s = int(m.group(1))
        if s < 90:
            return f"[{s}s ago]"
        if s < 5400:
            return f"[{round(s / 60)}m ago]"
        if s < 172800:
            return f"[{round(s / 3600)}h ago]"
        return f"[{round(s / 86400)}d ago]"
    return _AGE.sub(one, text)


def _slack_date(iso: str) -> str:
    """Slack's `<!date^…>` token, which renders in each READER's timezone. A raw ISO string with
    microseconds and a UTC offset is not a timestamp anyone reads in a chat client. Falls back to
    the original string if it can't be parsed — a wrong-looking date beats a broken token."""
    try:
        dt = datetime.fromisoformat(iso.strip().replace("Z", "+00:00"))
    except (TypeError, ValueError, AttributeError):
        return iso
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return f"<!date^{int(dt.timestamp())}^{{date_short_pretty}} at {{time}}|{iso}>"


def _truncate(text: str, limit: int = _MAX_SECTION) -> str:
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def build_message(trigger: str, key: str, payload: str, fired_at: str | None = None,
                  dispatch_id: str | None = None) -> dict:
    """Block Kit body for a fired trigger. `text` is always set as well — Slack uses it for the
    notification and for clients that can't render blocks, so a blocks-only message shows up as an
    empty push notification.

    `unfurl_links`/`unfurl_media` are off. The body carries every label on the event, so a source
    with a `host` or `url` label — web traffic, CDN logs, deploys nearly always have one — made
    Slack fetch that site and staple a preview card to the alert. It roughly doubled the height with
    nothing about the incident, read as though Tares were linking somewhere relevant, and meant
    alerting had the side effect of Slack fetching a customer's URLs.
    """
    # the trigger is the header, as an alert's name is on a Rius alert
    fields = [("Entity", key)]
    if fired_at:
        fields.append(("Fired", _slack_date(fired_at)))
    blocks: list[dict] = [_header(trigger), _fields(fields)]
    body = _humanize_ages((payload or "").strip())
    if body:
        blocks.append({"type": "section", "text": {
            "type": "mrkdwn", "text": "```" + _truncate(body, _MAX_PAYLOAD) + "```"}})
    actions = _buttons([("View firing in Tares", dispatch_url(dispatch_id), "primary")])
    if actions:
        blocks.append(actions)
    blocks.append(_context("Tares trigger" + (f" · {_slack_date(fired_at)}" if fired_at else "")))
    return {"text": f"{trigger} fired for {key}", "blocks": blocks,
            "unfurl_links": False, "unfurl_media": False}


# ── Block Kit pieces shared by every message Tares posts (TR-275) ────────────
_MAX_PAYLOAD = 1500      # a firing's payload excerpt; the full timeline is one click away
_MAX_EXCERPT = 600       # a finding's excerpt in the channel; the full note is in the thread


def _header(text: str) -> dict:
    # plain_text only, 150 characters at most, or Slack rejects the whole message
    return {"type": "header", "text": {"type": "plain_text", "text": _truncate(text, 150),
                                        "emoji": True}}


def _fields(pairs: list[tuple[str, str]]) -> dict:
    """A two-column grid of label and value, as Rius alerts show; empty values are left out."""
    return {"type": "section", "fields": [
        {"type": "mrkdwn", "text": f"*{label}*\n{_truncate(str(value), 1900)}"}
        for label, value in pairs if value not in (None, "")][:10]}


def _buttons(buttons: list[tuple[str, str, str | None]]) -> dict | None:
    """Link buttons; one without a URL (no public address) is left out, and none at all means no
    actions block rather than an empty one."""
    elems = []
    for i, (text, url, style) in enumerate(buttons):
        if not url:
            continue
        b = {"type": "button", "action_id": f"tares_link_{i}",
             "text": {"type": "plain_text", "text": text}, "url": url}
        if style:
            b["style"] = style
        elems.append(b)
    return {"type": "actions", "elements": elems} if elems else None


def _context(text: str) -> dict:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def classify(status: int, data: dict | None) -> tuple[bool, str | None, bool]:
    """(ok, error, retry) for one `chat.postMessage` response.

    Slack's answers come in three shapes and all three have to be told apart:
      · HTTP 200 + `{"ok": true}`                → delivered
      · HTTP 200 + `{"ok": false, "error": ...}` → the real failure mode. Definitive per
        DEFINITIVE_ERRORS; anything else there (internal_error, service_unavailable) is a
        transient Slack fault and retries, as does `ratelimited`.
      · HTTP 429 / 5xx                           → rate limit or outage, retry. Other 4xx are
        definitive, the same rule `Dispatcher._post` applies to a webhook.
    """
    if status == 429:
        return False, "slack: ratelimited", True
    if status >= 500:
        return False, f"slack: HTTP {status}", True
    if not isinstance(data, dict):
        # A 2xx that isn't JSON means we are not talking to Slack (a proxy, a captive portal).
        return False, f"slack: HTTP {status} (non-JSON response)", False
    if data.get("ok"):
        return True, None, False
    err = str(data.get("error") or "unknown_error")
    if err == "ratelimited":
        return False, "slack: ratelimited", True
    if err in DEFINITIVE_ERRORS or 400 <= status < 500:
        return False, f"slack: {err}{_error_detail(err, data)}", False
    return False, f"slack: {err}", True    # transient Slack-side fault — worth another attempt


_HINTS = {
    "invalid_auth": "the bot token is invalid or revoked",
    "token_revoked": "the bot token has been revoked",
    "account_inactive": "the bot token belongs to a deactivated workspace or app",
    "channel_not_found": "no such channel, or the bot cannot see it",
    "not_in_channel": "invite the bot to the channel first (/invite @Tares)",
    "is_archived": "the channel is archived",
    "missing_scope": "the bot token is missing the chat:write scope",
    "msg_too_long": "the message exceeded Slack's size limit",
}


def _error_detail(err: str, data: dict) -> str:
    hint = _HINTS.get(err)
    if not hint and err == "missing_scope" and data.get("needed"):
        hint = f"needs scope {data['needed']}"
    return f" ({hint})" if hint else ""


# ── the channel picker ────────────────────────────────────────────────────
# The console offers a list instead of a free-text box for the channel ID, which nobody can find
# without leaving the app. One page is 200 channels and a real workspace has more, so this
# paginates, but bounded: a cursor that never terminates would spin here forever.

_CHANNEL_PAGE = 200
_CHANNEL_MAX_PAGES = 10          # per listing: 2000 channels; past that a picker is the wrong UI


class _ListFailed(Exception):
    """One listing failed; carries the `(reason, detail, retry_after)` list_channels returns."""

    def __init__(self, reason: str, detail: str, retry_after: float | None = None):
        super().__init__(detail)
        self.reason, self.detail, self.retry_after = reason, detail, retry_after


def _retry_after(r: httpx.Response) -> float | None:
    """Slack's Retry-After on a 429, in seconds; None when absent or unreadable."""
    try:
        v = float(r.headers.get("retry-after", ""))
    except ValueError:
        return None
    return v if v >= 0 else None


async def _list_pages(cx: httpx.AsyncClient, method: str, params: dict, headers: dict) -> list[dict]:
    """Every page of one conversations listing, raising _ListFailed on the first bad answer."""
    out: list[dict] = []
    cursor = ""
    for _ in range(_CHANNEL_MAX_PAGES):
        q = {**params, **({"cursor": cursor} if cursor else {})}
        r = await cx.get(f"{API_BASE}/{method}", params=q, headers=headers)
        try:
            data = r.json()
        except Exception:
            data = None
        if r.status_code == 429:
            # conversations.list is Tier 2 (about 20 a minute per workspace); Slack says how long
            # to wait, and asking again sooner only extends the wait
            wait = _retry_after(r)
            raise _ListFailed("error", "slack: rate limited (HTTP 429)"
                              + (f", try again in {wait:.0f}s" if wait is not None else ""), wait)
        if r.status_code >= 500:
            raise _ListFailed("error", f"slack: HTTP {r.status_code}")
        if not isinstance(data, dict):
            raise _ListFailed("error", f"slack: HTTP {r.status_code} (non-JSON response)")
        if not data.get("ok"):
            err = str(data.get("error") or "unknown_error")
            if err == "missing_scope":
                # Not _error_detail's hint: that one names chat:write, which is the scope this
                # token almost certainly *does* have. Two read scopes are in play, so the missing
                # one can be either: take Slack's `needed` when it says, name both when it doesn't.
                needed = str(data.get("needed") or "").strip() or "channels:read and groups:read"
                raise _ListFailed("missing_scope", (
                    f"slack: missing_scope (the bot token is missing {needed}; "
                    "reconnect Slack to grant it)"))
            raise _ListFailed("error", f"slack: {err}{_error_detail(err, data)}")
        out.extend(c for c in data.get("channels") or [] if isinstance(c, dict))
        cursor = str(((data.get("response_metadata") or {}).get("next_cursor") or "")).strip()
        if not cursor:
            break
        # Falling out of the loop with a cursor still set means the bound was hit. A partial
        # list is a usable picker; an error here would take the whole feature away.
    return out


async def list_channels(token: str, timeout: float = 10.0
                        ) -> tuple[list[dict], str | None, str | None, float | None]:
    """`(channels, reason, detail, retry_after)`: every public channel, plus the private ones the
    bot is in. `retry_after` is the seconds Slack asked to wait on a 429, else None.

    Two listings. `conversations.list` with `public_channel` gives every unarchived public channel
    of the workspace, the bot a member or not: a person picking where something should go expects
    to find any channel there, and `is_member` lets the console say when Tares still has to be
    added (posting to a public channel the bot is not in fails with `not_in_channel` unless the
    app holds `chat:write.public`). `users.conversations` with `private_channel` gives the private
    channels the bot was added to; Slack shows a bot no other private channel at all.

    Each channel is `{id, name, is_private, is_member}`, sorted by name. A private channel is
    always a member: it is only listed because the bot is in it.

    `reason` is None when the list is trustworthy (an empty list then means the workspace has no
    channel the bot can see), otherwise it names why the caller should not believe it:
    "no_token", "missing_scope", "error". Nothing raises: the console renders whatever this returns,
    and a Slack outage must degrade to a sentence rather than to a stack trace.

    `missing_scope` is called out separately because it is the *expected* failure: a token issued
    before `channels:read`/`groups:read` were requested has it, and the only fix is reconnecting.
    """
    if not (token or "").strip():
        return [], "no_token", None, None
    headers = {"authorization": f"Bearer {token.strip()}"}
    base = {"exclude_archived": "true", "limit": str(_CHANNEL_PAGE)}
    try:
        async with httpx.AsyncClient(timeout=timeout) as cx:
            public = await _list_pages(cx, "conversations.list",
                                       {**base, "types": "public_channel"}, headers)
            private = await _list_pages(cx, "users.conversations",
                                        {**base, "types": "private_channel"}, headers)
    except _ListFailed as f:
        return [], f.reason, f.detail, f.retry_after
    except Exception as e:
        return [], "error", ("slack: " + type(e).__name__
                             + (f": {e}" if str(e).strip() else ""))[:200], None
    seen: dict[str, dict] = {}
    for c, member in [(c, None) for c in public] + [(c, True) for c in private]:
        if not (c.get("id") and c.get("name")):
            continue
        # id, name, is_private and is_member only: a conversation object carries a few hundred
        # bytes of purpose, topic and membership the console never reads. `is_private` is absent
        # on some payloads; a channel that doesn't say it is private isn't.
        seen[str(c["id"])] = {"id": str(c["id"]), "name": str(c["name"]),
                              "is_private": bool(c.get("is_private")) or member is True,
                              "is_member": True if member else bool(c.get("is_member"))}
    out = sorted(seen.values(), key=lambda c: (c["name"], c["id"]))
    return out, None, None, None


# ── inbound: the /tares slash command ─────────────────────────────────────
# Everything below serves `POST /api/slack/events`. It is deliberately pure — parsing, cost
# bounding and message shaping — so the endpoint itself is only plumbing: verify, ACK, answer.

USAGE = ("usage: `/tares ask <question>`; e.g. "
         "`/tares ask what happened to checkout-svc in the last hour?`")

# Cost ceiling, the same shape as `builtin_agents.DAILY_RUN_CAP`: a per-day count with an env
# override, so one enthusiastic channel cannot run up an unbounded model bill. Counted per
# (team, user) rather than per workspace — one person's loop must not lock out their colleagues.
DAILY_ASK_CAP = int(os.getenv("TARES_SLACK_DAILY_CAP", "50"))


class AskCap:
    """A rolling 24h cap per (team, user).

    Held in memory rather than in a table: unlike an agent run there is nothing to show in the
    console for an ask, and a restart resetting the counter is an acceptable cost bound — the
    ceiling exists to stop a runaway loop, not to bill anyone. Same knob shape as DAILY_RUN_CAP.
    """

    def __init__(self, cap: int = DAILY_ASK_CAP, window: float = 86400.0):
        self.cap, self.window, self._seen = cap, window, {}

    def take(self, team: str, user: str, now: float | None = None) -> bool:
        """Record one ask; False when this user is already at the cap (nothing is recorded then)."""
        now = time.time() if now is None else now
        q = self._seen.setdefault((team or "-", user or "-"), deque())
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.cap:
            return False
        q.append(now)
        return True


def parse_command(text: str, workspace: str | None = None) -> tuple[str, str | None]:
    """`(question, error)` for the `text` of a `/tares` slash command.

    `/tares ask <question>` is the documented form. A bare `/tares <question>` is accepted as
    the same thing — forgetting the subcommand is the overwhelmingly likely mistake, and answering
    it beats a lecture. Empty, or `help`, gets the usage line; nothing gets a stack trace.

    `workspace` is this instance's slug on Tares Cloud (see `workspace_slug`). When one Slack
    workspace is linked to several Tares workspaces, Tares Cloud asks for
    `/tares ask <workspace> <question>` and forwards the text unchanged (the signature covers it),
    so the first word after `ask` is dropped when it is exactly this slug. Anything else is part
    of the question, which keeps self-hosted and single-workspace asks as they were.
    """
    text = (text or "").strip()
    if not text or text.lower() in ("help", "-h", "--help", "?"):
        return "", USAGE
    head, _, rest = text.partition(" ")
    if head.lower() == "ask":
        rest = rest.strip()
        if workspace:
            first, _, tail = rest.partition(" ")
            if first.lower() == workspace.lower():
                rest = tail.strip()
        return (rest, None) if rest else ("", f"ask what? {USAGE}")
    return text, None


def workspace_slug(connect_url: str | None) -> str | None:
    """This workspace's slug on Tares Cloud: the `workspace` query parameter of
    TARES_SLACK_CONNECT_URL, which the control plane sets to
    `<console>/slack/install?workspace=<slug>`. None when it is unset (self-hosted) or carries no
    such parameter, and then `/tares ask` strips nothing."""
    from urllib.parse import parse_qs, urlsplit
    try:
        vals = parse_qs(urlsplit((connect_url or "").strip()).query).get("workspace") or []
    except ValueError:
        return None
    slug = (vals[0] if vals else "").strip()
    return slug or None


_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)
_MD_HEAD = re.compile(r"^#{1,6}\s*(.+)$", re.M)
# A horizontal rule. Slack has no such thing, so `---` arrives as three literal dashes on a line of
# their own. Excludes anything containing a pipe, which is a table's separator row, not a rule.
_MD_RULE = re.compile(r"^[ \t]*([-*_])(?:[ \t]*\1){2,}[ \t]*$", re.M)
_MD_BULLET = re.compile(r"^([ \t]*)[-*][ \t]+(?=\S)", re.M)
# A table row: starts and ends with a pipe. The separator row is the one that is only dashes,
# colons, pipes and spaces — it carries alignment, which monospace output cannot express anyway.
_TBL_ROW = re.compile(r"^[ \t]*\|.*\|[ \t]*$")
_TBL_SEP = re.compile(r"^[ \t]*\|[\s\-:|]+\|[ \t]*$")
# Emphasis and code ticks inside a table cell: a code block renders them literally, so `**ok**`
# would read as asterisks. Stripped rather than converted — inside ``` there is nothing to convert to.
_CELL_NOISE = re.compile(r"(\*\*|__|`)")
_BLANKS = re.compile(r"\n{3,}")


def _cells(row: str) -> list[str]:
    return [_CELL_NOISE.sub("", c).strip() for c in row.strip().strip("|").split("|")]


def _table_to_code(rows: list[str]) -> str:
    """A markdown table as an aligned monospace block.

    Slack renders no tables at all — a pipe table arrives as raw pipes plus a `|---|---|` row, which
    on a three-column answer is most of the message. A code block is the only place Slack keeps
    columns lined up, so the table becomes text that is at least readable as a table.
    """
    grid = [_cells(r) for r in rows if not _TBL_SEP.match(r)]
    if not grid:
        return ""
    width = max(len(r) for r in grid)
    grid = [r + [""] * (width - len(r)) for r in grid]
    cols = [max(len(r[i]) for r in grid) for i in range(width)]
    lines = ["  ".join(c.ljust(cols[i]) for i, c in enumerate(r)).rstrip() for r in grid]
    if len(grid) > 1:                 # keep the header visually separate, without markdown's pipes
        lines.insert(1, "  ".join("-" * cols[i] for i in range(width)).rstrip())
    return "```\n" + "\n".join(lines) + "\n```"


_TABLE_CODE_WIDTH = 60   # a table this narrow stays an aligned code block; wider ones wrap badly


def _table_to_lines(rows: list[str]) -> str:
    """A wide markdown table as one bullet per row: the first cell in bold, then the rest. With two
    columns that reads as `• *Immediate*: No action needed`; with more, each value is named by its
    column. Nothing to align, so nothing wraps into a mess at any width (TR-275)."""
    grid = [_cells(r) for r in rows if not _TBL_SEP.match(r)]
    if not grid:
        return ""
    head, body = (grid[0], grid[1:]) if len(grid) > 1 else ([], grid)
    lines = []
    for row in body:
        first, rest = row[0], row[1:]
        if len(rest) == 1 or not head:
            tail = " ".join(c for c in rest if c)
        else:
            tail = " · ".join(f"{h}: {c}" if h else c
                              for h, c in zip(head[1:], rest) if c)
        lines.append((f"•  *{first}*" + (f": {tail}" if tail else "")) if first else f"•  {tail}")
    return "\n".join(lines)


def _render_table(rows: list[str]) -> str:
    code = _table_to_code(rows)
    widest = max((len(line) for line in code.splitlines()[1:-1]), default=0)
    return code if widest <= _TABLE_CODE_WIDTH else _table_to_lines(rows)


def _extract_tables(text: str) -> tuple[str, list[str]]:
    """Replace each markdown table with a placeholder, returning the rendered blocks separately.

    Done before the emphasis and link substitutions so those never rewrite a table's contents —
    inside a code block their output would be literal asterisks and angle brackets.
    """
    out, tables, run = [], [], []

    def flush():
        # One row and a separator is a table; a single pipe-ish line is prose and stays prose.
        if len(run) >= 2 and any(_TBL_SEP.match(r) for r in run):
            out.append(f"\x00TBL{len(tables)}\x00")
            tables.append(_render_table(run))
        else:
            out.extend(run)
        run.clear()

    for line in (text or "").splitlines():
        if _TBL_ROW.match(line):
            run.append(line)
            continue
        flush()
        out.append(line)
    flush()
    return "\n".join(out), tables


def to_mrkdwn(text: str) -> str:
    """Markdown (what the model writes) → Slack mrkdwn (what Slack renders).

    Slack's dialect is close enough to markdown to be misleading: `**bold**` is literal asterisks,
    `[a](b)` is literal brackets, `## heading` is a literal hash, `---` is three dashes, and a pipe
    table is the raw pipes. All of those were showing up verbatim in `/tares ask` answers — the
    assistant is told to use small tables where they help, so the table case is not an edge case.
    """
    text, tables = _extract_tables(text)
    text = _MD_LINK.sub(r"<\2|\1>", text)
    text = _MD_BOLD.sub(r"*\1*", text)
    text = _MD_HEAD.sub(r"*\1*", text)
    text = _MD_RULE.sub("", text)
    text = _MD_BULLET.sub(r"\1•  ", text)
    # A dropped rule leaves the blank line either side of it, so the gap doubles. Collapse any run
    # of blank lines back to one — nothing in Slack needs more than a paragraph break.
    text = _BLANKS.sub("\n\n", text)
    for i, block in enumerate(tables):
        text = text.replace(f"\x00TBL{i}\x00", block)
    return text


def build_finding_message(agent_name: str, trigger: str, key: str, finding: str, *,
                          verdict: str | None = None, model: str | None = None,
                          run_id: str | None = None, when: str | None = None,
                          full_note: bool = False) -> dict:
    """Block Kit body for a Tares agent's finding (TR-275): the entity as the header, a grid of
    fields, a short excerpt of the note, and buttons into Tares.

    A note that fits in one message block is posted whole (`full_note=True`), and Slack folds it
    behind "Show more". A longer one, like a multi-section incident note, gets an excerpt here and
    the full note in the message's thread (`build_finding_thread`), so it cannot fill the channel.
    The incoming-webhook path cannot thread, so it always posts the note whole. `text` is the
    notification fallback."""
    # the entity is the header, so the fields say who wrote the note and why
    fields = [("Agent", agent_name), ("Trigger", f"`{trigger}`"), ("Verdict", verdict),
              ("Model", model)]
    if when:
        fields.append(("When", _slack_date(when)))
    blocks: list[dict] = [_header(key), _fields(fields)]
    if full_note:
        blocks += _sections(finding)
    else:
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
                                                    "text": excerpt(finding) or "_(no note)_"}})
    actions = _buttons([("View in Tares", run_url(agent_name, run_id), "primary"),
                        ("Open timeline", entity_url(key), None)])
    if actions:
        blocks.append(actions)
    blocks.append(_context("Tares agent finding" + ("" if full_note else " · full note in the thread")))
    return {"text": f"{agent_name} on {key}: {excerpt(finding, 200)}",
            "blocks": blocks, "unfurl_links": False, "unfurl_media": False}


def needs_thread(finding: str) -> bool:
    """Whether the note is too long for one message block: then the channel gets an excerpt and
    the full note goes in the thread. A note that fits in one block is posted whole, and Slack's
    own "Show more" folds it; a thread there only repeated what the channel already showed."""
    return len(_sections(finding)) > 1


def build_finding_thread(finding: str) -> list[dict]:
    """The full note, as the thread reply under a finding."""
    return _sections(finding)


_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def excerpt(text: str, limit: int = _MAX_EXCERPT) -> str:
    """The part of a note worth reading in the channel, as mrkdwn: the note's own summary paragraph
    when it has one ("Summary: ..."), else its first paragraph that says something (not a heading,
    not a rule, not a lead-in ending in a colon), cut at a sentence near `limit` characters."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]

    def plain(p: str) -> str:
        return re.sub(r"[*_#`>]", "", p).strip()

    def useful(p: str) -> bool:
        first = p.splitlines()[0]
        return not (_MD_HEAD.match(first) or _MD_RULE.match(first) or _TBL_ROW.match(first)
                    or first.startswith("```") or plain(p).endswith(":"))

    pick = next((p for p in paras if plain(p).lower().startswith("summary")), None)
    pick = pick or next((p for p in paras if useful(p)), paras[0] if paras else "")
    out = to_mrkdwn(pick).strip()
    if len(out) <= limit:
        return out
    cut, n = "", 0
    for sentence in _SENTENCE_END.split(out):
        if n + len(sentence) > limit and cut:
            break
        cut += (" " if cut else "") + sentence
        n += len(sentence) + 1
    return _truncate(cut, limit)


def _sections(text: str) -> list[dict]:
    """Split a long note across section blocks: one section caps at 3000 characters, and an
    over-long block makes Slack reject the whole message (`invalid_blocks`) rather than truncate.

    Splits at line boundaries and never inside a code block: a cut there left each half with an
    unmatched ``` that Slack showed literally (TR-275). A code block too long for one section is
    closed at the cut and reopened in the next."""
    out: list[dict] = []
    cur: list[str] = []
    in_fence = False

    def emit(close: bool) -> None:
        body = "\n".join(cur).strip("\n")
        if close:
            body += "\n```"
        if body.strip() and body.strip() != "```":
            out.append({"type": "section", "text": {"type": "mrkdwn", "text": body}})

    for line in to_mrkdwn(text).strip().splitlines():
        # a single line longer than a section is cut hard; rare, and better than a rejection
        while len(line) > _MAX_SECTION - 10:
            cur.append(line[:_MAX_SECTION - 10])
            line = line[_MAX_SECTION - 10:]
            emit(in_fence)
            cur = ["```"] if in_fence else []
        if sum(len(x) + 1 for x in cur) + len(line) + 4 > _MAX_SECTION:
            emit(in_fence)
            cur = ["```"] if in_fence else []
        cur.append(line)
        if line.count("```") % 2:
            in_fence = not in_fence
    emit(False)
    return out or [{"type": "section", "text": {"type": "mrkdwn", "text": "_(no answer)_"}}]


def build_answer(question: str, answer: str, thread_ts: str | None = None,
                 in_channel: bool = True) -> dict:
    """The `response_url` body carrying an answer back to Slack.

    `replace_original` clears the "thinking…" ACK so a channel is left with the answer alone, and
    `text` is always set for the notification and for clients that can't render blocks.
    """
    blocks = [{"type": "context", "elements": [
        {"type": "mrkdwn", "text": f":mag: *{to_mrkdwn(question)[:180]}*"}]}]
    blocks += _sections(answer)
    body = {"response_type": "in_channel" if in_channel else "ephemeral",
            "replace_original": True,
            "text": _truncate(answer.strip() or "(no answer)", 500),
            "blocks": blocks,
            # Same reason as build_message: an answer that mentions one of the user's hostnames
            # should not make Slack go and fetch it.
            "unfurl_links": False, "unfurl_media": False}
    if thread_ts:
        # Invoked inside a thread: the answer belongs in that thread, not adrift in the channel.
        body["thread_ts"] = thread_ts
    return body


def build_error(message: str, thread_ts: str | None = None) -> dict:
    """A failure the user can act on, ephemeral so a broken setup isn't broadcast to the channel.

    Every failure mode goes through here. A slash command that answers with silence is
    indistinguishable from an app that is down, which is the worst outcome of all.
    """
    body = {"response_type": "ephemeral", "replace_original": True,
            "text": message,
            "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": message}}]}
    if thread_ts:
        body["thread_ts"] = thread_ts
    return body
