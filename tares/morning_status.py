"""Morning status: the crew's standup, sent by the cell on weekday mornings (TR-825).

One time and one time zone per cell, off until someone turns it on. The report is the standup's
own facts and summary (tares/standup.py), with one line on top saying how much is waiting on the
team. It goes to a Slack channel, to workspace members by email (Tares Cloud only), or both, and
one failing never stops the other. It leaves out private projects, because a channel is shared.

The schedule is a pure function of the settings and a clock, so tests pass the clock in. The date
is recorded before anything is sent, so a crash or a slow model can never send the report twice.

The email goes through the control plane, authenticated with this cell's own TARES_AUTH_TOKEN. The
token is only ever put in the Authorization header; no failure text or log line carries it.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import threading
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from . import standup as standup_mod
from .envelope import now_utc

SETTING_KEY = "morning_status"
WINDOW = timedelta(hours=2)     # a restart at 09:30 still sends; a restart at 17:00 does not
DAY_HOURS, MONDAY_HOURS = 24, 72
TICK_SECONDS = 60
EMAIL_TIMEOUT = 30
MANUAL_SUMMARY_TIMEOUT = 45     # "Send one now" waits on it, behind the ingress's 60s

DEFAULTS = {"enabled": False, "to_slack": False, "to_email": False, "channel": "",
            "recipients": [], "time": "09:00", "timezone": "", "last_sent_date": "",
            "last_result": ""}
_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_MD_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_MD_HEAD = re.compile(r"^#{1,6}\s+")

# The timer and the settings routes both read, change and write the one setting, on different
# threads; this keeps either from writing back what the other just changed.
_lock = threading.Lock()


class EmailFailed(RuntimeError):
    """Why the email did not go, in words that are safe to show and to log."""


def email_available() -> bool:
    """Only a Tares Cloud cell can email: the control plane sets this address on it."""
    return bool(_workspace_url())


def _workspace_url() -> str:
    return (os.getenv("TARES_WORKSPACE_API_URL") or "").strip().rstrip("/")


def _safe(text: str) -> str:
    """The token out of a text about to be logged or shown, whatever that text was built from."""
    token = (os.getenv("TARES_AUTH_TOKEN") or "").strip()
    return text.replace(token, "[hidden]") if token else text


def load(store) -> dict:
    try:
        saved = json.loads(store.get_setting(SETTING_KEY) or "{}")
    except ValueError:
        saved = {}
    return {**DEFAULTS, **(saved if isinstance(saved, dict) else {})}


def _write(store, cfg: dict) -> None:
    store.set_setting(SETTING_KEY, json.dumps({k: cfg.get(k, DEFAULTS[k]) for k in DEFAULTS}))


def _local(cfg: dict, now: datetime) -> datetime:
    return now.astimezone(ZoneInfo(cfg["timezone"]))


def due(cfg: dict, now: datetime) -> bool:
    """On, a weekday in the cell's time zone, the set time passed but not by more than the window,
    and not sent yet today."""
    if not cfg["enabled"] or not cfg["timezone"]:
        return False
    local = _local(cfg, now)
    if local.weekday() >= 5 or cfg["last_sent_date"] == local.date().isoformat():
        return False
    hour, minute = (int(x) for x in cfg["time"].split(":"))
    at = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return at <= local < at + WINDOW


def report_range(cfg: dict, now: datetime) -> tuple[datetime, datetime]:
    """The last day; Monday's covers the weekend. In the cell's zone, so the heading names its days."""
    until = _local(cfg, now)
    return until - timedelta(hours=MONDAY_HOURS if until.weekday() == 0 else DAY_HOURS), until


def waiting(report: dict) -> int:
    """The questions waiting on someone, for the whole team (the report may go to a shared place)."""
    return (sum(len(p["needs_you"]) for p in report["projects"])
            + sum(q["needs_you"] for q in report["quiet"]))


def top_line(report: dict) -> str:
    n = waiting(report)
    # on a cell of several people the report names whom each item waits for (standup): "you"
    # would be whoever reads the shared place
    team = bool(report.get("team")) or any(
        "for" in d for p in report["projects"] for d in p["needs_you"] if isinstance(d, dict))
    if n == 0:
        return "Nothing is waiting on anyone." if team else "Nothing is waiting for you."
    if team:
        return "1 thing is waiting on a person." if n == 1 else f"{n} things are waiting on people."
    return "1 thing is waiting for you." if n == 1 else f"{n} things are waiting for you."


def plain_text(markdown: str) -> str:
    """The report for an email: the same lines without markdown's marks, links as their address."""
    out = []
    for line in markdown.splitlines():
        line = _MD_HEAD.sub("", _MD_LINK.sub(r"\1 (\2)", line)).replace("**", "")
        out.append(line)
    return "\n".join(out).strip() + "\n"


async def post_email(subject: str, text: str, recipients: list[str]) -> None:
    """Ask the control plane to email the report. Raises EmailFailed with what to tell the person."""
    url = _workspace_url()
    token = (os.getenv("TARES_AUTH_TOKEN") or "").strip()
    if not url:
        raise EmailFailed("not available on this cell")
    if not token:
        raise EmailFailed("this cell has no key for Tares Cloud")
    try:
        async with httpx.AsyncClient(timeout=EMAIL_TIMEOUT) as cx:
            r = await cx.post(f"{url}/status-email", headers={"authorization": f"Bearer {token}"},
                              json={"subject": subject, "text": text, "recipients": recipients})
    except httpx.HTTPError as e:
        raise EmailFailed(f"could not reach Tares Cloud ({type(e).__name__})") from None
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.status_code in (401, 403):
        raise EmailFailed("Tares Cloud did not accept this cell's key")
    if r.status_code >= 300:
        detail = str(data.get("detail") or "") if isinstance(data, dict) else ""
        raise EmailFailed(_safe(detail)[:200] or f"Tares Cloud answered {r.status_code}")
    if not (isinstance(data, dict) and data.get("accepted")):
        raise EmailFailed("the mail service did not accept it")


def _clean(body: dict, stored: dict, people_emails: dict[str, str]) -> dict:
    """The settings to store. Raises ValueError with a plain message. The dates and the last result
    are never taken from a request."""
    for k in ("enabled", "to_slack", "to_email"):
        if k in body and not isinstance(body[k], bool):
            raise ValueError(f"{k} must be true or false")
    merged = {**stored, **{k: body[k] for k in ("enabled", "to_slack", "to_email", "channel",
                                                "recipients", "time", "timezone") if k in body}}
    time_, zone = str(merged["time"] or "").strip(), str(merged["timezone"] or "").strip()
    if not _TIME.match(time_):
        raise ValueError("the time must look like 09:00")
    try:
        ZoneInfo(zone)
    except Exception:
        raise ValueError(f"not a time zone: {zone or '(none)'}") from None
    channel = str(merged["channel"] or "").strip()
    if len(channel) > 200:
        raise ValueError("that channel name is too long")
    raw = merged["recipients"]
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        raise ValueError("recipients must be a list of email addresses")
    recipients: dict[str, str] = {}
    for r in raw:
        e = r.strip()
        if e:
            recipients[e.lower()] = e
    if merged["to_email"]:
        for lower in recipients:
            if lower not in people_emails:
                raise ValueError(f"{recipients[lower]} is not on this workspace")
        recipients = {k: people_emails[k] for k in recipients}
    out = {**DEFAULTS, **stored, "enabled": merged["enabled"], "to_slack": merged["to_slack"],
           "to_email": merged["to_email"], "channel": channel, "time": time_, "timezone": zone,
           "recipients": list(recipients.values())}
    out["last_sent_date"], out["last_result"] = stored["last_sent_date"], stored["last_result"]
    return out


def _person_emails(store) -> dict[str, str]:
    out = {}
    for p in store.list_people():
        e = (p.get("email") or "").strip()
        if e:
            out[e.lower()] = e
    return out


def _count(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


class MorningStatus:
    """The feature, built once by make_app() from the daemon's own standup and Slack helpers.

    `report(since, until)`      the standup facts, without private projects
    `summarise(report, timeout)` -> (summary or None, why not); the facts stand alone without it
    `post_slack(channel, text, title)`, raising with a message to show
    `slack_connected()`         whether the cell has a Slack connection
    `post_email(subject, text, recipients)`, raising EmailFailed
    `clock()`                   the current time, aware; tests give their own
    """

    def __init__(self, store, report, summarise, post_slack, slack_connected,
                 post_email=post_email, clock=now_utc):
        self.store = store
        self.report = report
        self.summarise = summarise
        self.post_slack = post_slack
        self.slack_connected = slack_connected
        self.post_email = post_email
        self.clock = clock

    # ── the page ─────────────────────────────────────────────────────────────
    def page(self) -> dict:
        cfg = load(self.store)
        return {k: cfg[k] for k in ("enabled", "to_slack", "to_email", "channel", "recipients",
                                    "time", "timezone", "last_result")} | {
            "email_available": email_available()}

    def _check_destinations(self, cfg: dict) -> None:
        """At least one place the report can really go, or ValueError saying what is missing."""
        if cfg["to_email"] and not email_available():
            raise ValueError("email is not available on this cell")
        slack, mail = cfg["to_slack"], cfg["to_email"]
        if not slack and not mail:
            raise ValueError("choose where to send it: Slack, Email, or both")
        slack_ok = slack and bool(cfg["channel"]) and self.slack_connected()
        mail_ok = mail and bool(cfg["recipients"])
        if slack_ok or mail_ok:
            return
        if slack and not cfg["channel"]:
            raise ValueError("pick a Slack channel")
        if slack and not self.slack_connected():
            raise ValueError("Slack is not connected")
        raise ValueError("pick at least one person to email")

    def save(self, body: dict) -> dict:
        people = _person_emails(self.store)
        with _lock:
            cfg = _clean(body, load(self.store), people)
            if cfg["enabled"]:
                self._check_destinations(cfg)
            elif cfg["to_email"] and not email_available():
                raise ValueError("email is not available on this cell")
            _write(self.store, cfg)
        return self.page()

    # ── sending ──────────────────────────────────────────────────────────────
    def _note_result(self, line: str) -> None:
        with _lock:
            cfg = load(self.store)
            cfg["last_result"] = line
            _write(self.store, cfg)

    async def _send(self, cfg: dict, now: datetime, summary_timeout: int | None) -> dict:
        """Build one report and try every destination. Records and answers what happened."""
        when = _local(cfg, now).strftime("%A %H:%M")
        try:
            since, until = report_range(cfg, now)
            report = await asyncio.to_thread(self.report, since, until)
            summary = None
            try:
                summary, _why = await self.summarise(report, summary_timeout)
            except Exception as e:  # noqa: BLE001 — the report stands without the summary
                print(f"[tares] morning status: summary failed: {type(e).__name__}", flush=True)
            top = top_line(report)
            markdown = f"{top}\n\n{standup_mod.markdown(report, summary)}"
        except Exception as e:  # noqa: BLE001 — record it; the page says what happened
            print(f"[tares] morning status: report failed: {type(e).__name__}", flush=True)
            line = f"Not sent {when}. The report could not be put together."
            self._note_result(line)
            return {"ok": False, "result": line}
        sent, failed = [], []
        if cfg["to_slack"] and cfg["channel"]:
            try:
                await self.post_slack(cfg["channel"], markdown, "Morning status")
                sent.append("Slack")
            except Exception as e:  # noqa: BLE001 — email still goes
                failed.append(f"Slack failed: {_safe(str(e))[:200]}")
        if cfg["to_email"] and cfg["recipients"]:
            # someone removed from the workspace since the save is left out, not an error
            people = _person_emails(self.store)
            still = [people[r.lower()] for r in cfg["recipients"] if r.lower() in people]
            try:
                if not still:
                    raise EmailFailed("nobody picked is on this workspace any more")
                await self.post_email(f"Morning status: {top.rstrip('.')}",
                                      plain_text(markdown), still)
                sent.append(_count(len(still), "person", "people"))
            except EmailFailed as e:
                failed.append(f"Email failed: {_safe(str(e))}")
            except Exception as e:  # noqa: BLE001 — Slack's answer is already in
                failed.append(f"Email failed: {type(e).__name__}")
        for f in failed:
            print(f"[tares] morning status: {f}", flush=True)
        fails = " ".join(f"{f}." for f in failed)
        if sent:
            line = f"Last sent {when} to {' and '.join(sent)}." + (f" {fails}" if fails else "")
        else:
            line = f"Not sent {when}. {fails or 'Nowhere to send it.'}"
        self._note_result(line)
        return {"ok": bool(sent), "result": line}

    async def send_now(self) -> dict:
        """One report on the saved settings, whether or not it is switched on. It does not count as
        today's: the scheduled one still goes."""
        cfg = load(self.store)
        if not cfg["timezone"]:
            raise ValueError("save a time zone first")
        self._check_destinations(cfg)
        return await self._send(cfg, self.clock(), MANUAL_SUMMARY_TIMEOUT)

    async def tick(self, now: datetime | None = None) -> bool:
        """One pass of the timer: sends when due, and says whether it did."""
        now = now or self.clock()
        with _lock:
            cfg = load(self.store)
            if not due(cfg, now):
                return False
            cfg["last_sent_date"] = _local(cfg, now).date().isoformat()   # before anything is sent
            _write(self.store, cfg)
        await self._send(cfg, now, None)
        return True

    async def run(self, stop: asyncio.Event) -> None:
        """The cell's own timer, so the report goes out with everyone's laptop shut."""
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=TICK_SECONDS)
                return
            except asyncio.TimeoutError:
                pass
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — the timer never stops on an error
                print(f"[tares] morning status: timer failed: {type(e).__name__}", flush=True)
