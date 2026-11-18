"""Tell a person on Slack when something new waits for them (TR-1458).

Every minute the cell reads what waits for each person, the same list their home calls "Waiting
for you" (factory_api.waiting_for_person), and sends one direct message for each item that is new
since the last read: the decision, what it holds up, and a link to the feed. The person is found
in Slack by their email, through the cell's own bot token, so a cell with no Slack sends nothing.
Each person can switch it off for themselves (Settings, Slack); it is on until they do.

New means new to this reader. The first read of a person's list only records what is there, and
so does a read after the reader was away for more than an hour (Slack disconnected, the daemon
down): a gap never turns into a burst of old items. An item is recorded before its message goes,
so a crash cannot send it twice; a message that fails is logged and not sent again. More than a
few new items at once become their first few messages and one that says how many more wait.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from urllib.parse import quote

import httpx
from fastapi import Body, Request

from . import factory_owners as owners_mod
from . import slack as slack_mod
from .envelope import now_utc

log = logging.getLogger("tares.waiting_slack")

SETTING = "waiting_slack"                 # per person: f"{SETTING}:{person id}" is "on" or "off"
LAST_READ = "waiting_slack:last_read"     # when the reader last read everyone's list
TICK_SECONDS = 60
GAP = timedelta(hours=1)                  # away longer than this: what waits now is not new
MAX_PER_READ = 5                          # messages per person per read, then one "and N more"
LOOKUP_TTL = timedelta(hours=6)           # how long a Slack account found by email is kept
TIMEOUT = 15
SEEN = "*"                                # the item that says a person's list was read once

NO_SCOPE = ("the Slack app cannot look people up by email: add the users:read.email bot scope "
            "and reinstall it")


def is_on(store, person_id: str) -> bool:
    """The person's own choice; on until they switch it off."""
    return (store.get_setting(f"{SETTING}:{person_id}") or "").strip().lower() != "off"


def set_on(store, person_id: str, on: bool) -> None:
    store.set_setting(f"{SETTING}:{person_id}", "on" if on else "off")


def feed_link(item_id: str | None = None) -> str:
    """The feed, opened on `item_id` when given, or "" while the cell has no public address
    (slack.public_base: a link to 127.0.0.1 is worse than none)."""
    base = slack_mod.public_base()
    if not base:
        return ""
    return f"{base}/feed" + (f"?item={quote(item_id, safe='')}" if item_id else "")


def _ask(item: dict) -> str:
    return str(item.get("ask") or item.get("label") or "Something waits for you").strip()


def _link_block(link: str) -> dict:
    if link:
        return {"type": "actions", "elements": [{"type": "button", "text": {"type": "plain_text",
                "text": "Open the feed"}, "url": link, "action_id": "open_feed"}]}
    return slack_mod._context("Open Tares to answer it.")


def message(user: str, item: dict, link: str) -> dict:
    """One direct message for one item: the ask, what it holds up, the project, a link."""
    ask = _ask(item)
    lines = [f"*{slack_mod.escape_text(ask)}*"]
    if item.get("holds_up"):
        lines.append(f"Holds up {slack_mod.escape_text(str(item['holds_up']))}.")
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}}]
    project = (item.get("project") or {}).get("name")
    if project:
        blocks.append(slack_mod._context(slack_mod.escape_text(f"Waiting for you in {project}")))
    blocks.append(_link_block(link))
    return {"channel": user, "text": "Waiting for you: " + slack_mod.escape_text(ask), "blocks": blocks,
            "unfurl_links": False, "unfurl_media": False}


def more_message(user: str, n: int, link: str) -> dict:
    """What did not get a message of its own in this read, in one."""
    words = f"{n} more {'thing waits' if n == 1 else 'things wait'} for you"
    return {"channel": user, "text": words + ".",
            "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": words + "."}},
                       _link_block(link)],
            "unfurl_links": False, "unfurl_media": False}


def _parse(v) -> datetime | None:
    try:
        return datetime.fromisoformat(str(v)) if v else None
    except ValueError:
        return None


class WaitingSlack:
    """The reader. `waiting_for(person, now)` is factory_api.waiting_for_person; `send` and
    `lookup` default to Slack's API and are handed in by tests."""

    def __init__(self, store, waiting_for, *, send=None, lookup=None, clock=now_utc):
        self.store, self.waiting_for, self.clock = store, waiting_for, clock
        self._send = send or self._post
        self._lookup = lookup or self._lookup_email
        self._users: dict[str, tuple[str | None, datetime]] = {}
        self._logged: set[str] = set()
        self.problem: str | None = None        # a cell-wide reason nobody is found (Settings shows it)
        self.not_found: set[str] = set()       # people whose email Slack does not know

    # ── Slack ──
    async def _post(self, token: str, msg: dict) -> tuple[bool, str | None]:
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as cx:
                r = await cx.post(f"{slack_mod.API_BASE}/chat.postMessage", json=msg,
                                  headers={"authorization": f"Bearer {token}"})
            try:
                data = r.json()
            except ValueError:
                data = None
        except httpx.HTTPError as e:
            return False, f"could not reach Slack: {type(e).__name__}"
        ok, error, _retry = slack_mod.classify(r.status_code, data)
        return ok, error

    async def _lookup_email(self, token: str, email: str) -> tuple[str | None, str | None]:
        """(Slack user id, None), (None, None) when Slack knows no one by this email, or
        (None, Slack's error) when it could not say."""
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as cx:
                r = await cx.get(f"{slack_mod.API_BASE}/users.lookupByEmail", params={"email": email},
                                 headers={"authorization": f"Bearer {token}"})
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            return None, f"could not reach Slack: {type(e).__name__}"
        if data.get("ok"):
            return (data.get("user") or {}).get("id"), None
        if data.get("error") == "users_not_found":
            return None, None
        return None, str(data.get("error") or f"HTTP {r.status_code}")

    async def slack_user(self, token: str, email: str, now: datetime) -> tuple[str | None, str | None]:
        """The person's Slack user id by email, kept for LOOKUP_TTL (a miss too, so a person who
        is not in the workspace is not looked up every minute)."""
        key = email.strip().lower()
        hit = self._users.get(key)
        if hit and now - hit[1] < LOOKUP_TTL:
            return hit[0], None
        uid, err = await self._lookup(token, key)
        if err is None:
            self._users[key] = (uid, now)
        return uid, err

    def _log_once(self, key: str, text: str) -> None:
        if key not in self._logged:
            self._logged.add(key)
            log.warning("%s", text)
            print(f"[tares] waiting on Slack: {text}", flush=True)

    # ── one read ──
    def people(self) -> list[dict]:
        """Everyone who could be told: on the cell, someone (not the unclaimed `local`), with an email."""
        return [p for p in self.store.list_people()
                if not p.get("removed_at") and owners_mod.claimed(p) and (p.get("email") or "").strip()]

    async def read_once(self) -> dict:
        """Read every person's list once and send what is new. Returns {sent, people} for tests
        and the log; {skipped} when Slack is not connected."""
        token = slack_mod.resolve_token(self.store)[0]
        if not token:
            return {"skipped": "slack is not connected", "sent": 0}
        now = self.clock()
        last = _parse(self.store.get_setting(LAST_READ))
        away = last is None or now - last > GAP
        self.store.set_setting(LAST_READ, now.isoformat())
        sent, problem = 0, None
        for p in self.people():
            try:
                n, problem_here = await self._read_person(token, p, now, away)
            except asyncio.CancelledError:
                raise
            except Exception as e:      # one person's list never stops the others'
                self._log_once(f"read:{p['id']}:{type(e).__name__}",
                               f"could not read what waits for {p['id']}: {type(e).__name__}: {e}")
                continue
            sent += n
            problem = problem or problem_here
        self.problem = problem
        return {"sent": sent}

    async def _read_person(self, token: str, p: dict, now: datetime, away: bool) -> tuple[int, str | None]:
        pid = p["id"]
        items = await asyncio.to_thread(self.waiting_for, p, now)
        told = await asyncio.to_thread(self.store.told_items, pid)
        new = [i for i in items if i["id"] not in told]
        if SEEN not in told or away or not is_on(self.store, pid):
            # what waits now was not new to this reader: recorded, never sent
            await asyncio.to_thread(self.store.mark_told, pid, [SEEN, *[i["id"] for i in new]], False, now)
            return 0, None
        if not new:
            return 0, None
        user, err = await self.slack_user(token, p["email"], now)
        if err == "missing_scope":
            self._log_once("missing_scope", NO_SCOPE)
            await asyncio.to_thread(self.store.mark_told, pid, [i["id"] for i in new], False, now)
            return 0, NO_SCOPE
        if err:
            # Slack could not say who they are this time: the items stay new for the next read
            self._log_once(f"lookup:{err}", f"could not find {pid} on Slack: {err}")
            return 0, None
        if user is None:
            self.not_found.add(pid)
            await asyncio.to_thread(self.store.mark_told, pid, [i["id"] for i in new], False, now)
            return 0, None
        self.not_found.discard(pid)
        own, rest = new[:MAX_PER_READ], new[MAX_PER_READ:]
        if len(rest) == 1:
            own, rest = new, []       # "1 more thing" is no shorter than the thing itself
        # recorded first: a crash between the two can lose a message, never send one twice
        await asyncio.to_thread(self.store.mark_told, pid, [i["id"] for i in new], True, now)
        sent = 0
        for item in own:
            ok, err = await self._send(token, message(user, item, feed_link(item["id"])))
            sent += 1 if ok else 0
            if not ok:
                self._log_once(f"send:{err}", f"could not message {pid} about {item['id']}: {err}")
        if rest:
            ok, err = await self._send(token, more_message(user, len(rest), feed_link()))
            sent += 1 if ok else 0
            if not ok:
                self._log_once(f"send:{err}", f"could not message {pid}: {err}")
        return sent, None

    async def run(self, stop: asyncio.Event) -> None:
        """Read every TICK_SECONDS until `stop`. The first read waits one tick, so a daemon's
        start does not reach Slack at once. Never stops on an error."""
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=TICK_SECONDS)
                return
            except asyncio.TimeoutError:
                pass
            try:
                await self.read_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._log_once(f"read:{type(e).__name__}", f"read failed: {type(e).__name__}: {e}")

    def status_for(self, person: dict | None) -> dict:
        """What Settings shows the person: their choice, whether Slack is connected, whether the
        reader could find them there (None until it tried), and a cell-wide problem. `claimed` is
        false for the unclaimed `local` of a cell of one, whom the reader never messages."""
        pid = (person or {}).get("id")
        connected = bool(slack_mod.resolve_token(self.store)[0])
        found = None
        if pid and pid in self.not_found:
            found = False
        elif person and self._users.get((person.get("email") or "").strip().lower(), (None,))[0]:
            found = True
        return {"on": bool(pid) and is_on(self.store, pid), "connected": connected,
                "claimed": bool(person) and owners_mod.claimed(person),
                "has_email": bool((person or {}).get("email")), "found": found,
                "problem": self.problem if connected else None}


def register(app, store, h, reader: WaitingSlack) -> None:
    """GET and PUT /api/me/slack: the caller's own switch for these messages, and what Settings
    says about it. A credential with no person has nothing to switch."""
    def person_of(request: Request) -> dict:
        person = h.current_person(request)
        if not person or not person.get("id"):
            h.err(ValueError("this key is no person, so nothing waits for it on Slack"), 400)
        return store.get_person(person["id"]) or person

    @app.get("/api/me/slack")
    def get_my_slack(request: Request):
        """Whether the caller is told on Slack when something new waits for them (on until they
        switch it off), whether Slack is connected, and whether Slack knows their email."""
        return reader.status_for(person_of(request))

    @app.put("/api/me/slack")
    def put_my_slack(request: Request, body: dict = Body(...)):
        """Switch the caller's messages on or off: {"on": true|false}."""
        if not isinstance(body.get("on"), bool):
            h.err(ValueError("send on as true or false"), 400)
        p = person_of(request)
        set_on(store, p["id"], body["on"])
        return reader.status_for(p)
