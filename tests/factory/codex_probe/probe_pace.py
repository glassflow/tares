"""Can the crew's pace step read a Codex account's limits? A by-hand probe (TR-1576), not in CI.

    TARES_PROBE_CODEX=/path/to/codex python tests/factory/codex_probe/probe_pace.py [--out DIR]

The watch slows the crew from a "plan" reading: `tares.factory.pace_now(settings, plan, now)`, where plan is
`{"limits": [{"kind": "session" | "weekly_all", "percent", "resets_at"}]}` (today filled from Claude Code's
status line and `/usage`). Codex's app-server answers `account/rateLimits/read` with a quota reading of its own
(`usedPercent`, `windowDurationMins`, `resetsAt`), which is not token usage. `plan_from_codex` below maps one
onto the other by the window's length, and the cases run the real `pace_now` on the result. One live reading
is taken too (it uses no model and costs nothing); account ids are left out of what is saved.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
from appserver import AppServer, codex_bin  # noqa: E402
from probe_guard import QUIET  # noqa: E402

from tares.factory import pace_now  # noqa: E402

# A window's length in minutes to the kind the pace step knows. Anything else is kept under its own name,
# which pace_now ignores, so an unknown window never slows or holds the crew by mistake.
WINDOW_KIND = {300: "session", 10080: "weekly_all"}
MAX_AGE = timedelta(minutes=30)   # an older reading is not trusted; the crew then paces as "unknown"


def plan_from_codex(reading: dict | None, observed_at: datetime, now: datetime) -> dict | None:
    """A Codex rate-limit reading as the plan dict pace_now takes, or None when there is nothing usable
    (no reading, an empty one, or one older than MAX_AGE)."""
    limits_in = (reading or {}).get("rateLimits") or {}
    if not limits_in or now - observed_at > MAX_AGE:
        return None
    limits = []
    for slot in ("primary", "secondary"):
        w = limits_in.get(slot)
        if not isinstance(w, dict) or not isinstance(w.get("usedPercent"), (int, float)):
            continue
        mins = w.get("windowDurationMins")
        kind = WINDOW_KIND.get(mins, f"codex_{mins}m")
        resets = w.get("resetsAt")
        resets_at = (datetime.fromtimestamp(resets, timezone.utc).isoformat().replace("+00:00", "Z")
                     if isinstance(resets, (int, float)) else None)
        limits.append({"kind": kind, "percent": w["usedPercent"], "resets_at": resets_at,
                       "source": "codex account/rateLimits/read", "observed_at": observed_at.isoformat()})
    return {"limits": limits} if limits else None


def reading(primary=None, secondary=None, primary_reset=3600, secondary_reset=86400, now=None, primary_mins=300):
    now = now or datetime.now(timezone.utc)
    rl = {}
    if primary is not None:
        rl["primary"] = {"usedPercent": primary, "windowDurationMins": primary_mins,
                         "resetsAt": int((now + timedelta(seconds=primary_reset)).timestamp())}
    if secondary is not None:
        rl["secondary"] = {"usedPercent": secondary, "windowDurationMins": 10080,
                           "resetsAt": int((now + timedelta(seconds=secondary_reset)).timestamp())}
    return {"rateLimits": rl}


def cases() -> list[dict]:
    now = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
    on = {"pace": True}
    rows = [
        ("room on both windows", reading(6, 20, now=now), now, {"known": True, "level": 0, "state": "ok"}),
        ("5-hour window at 86%: slow", reading(86, 20, now=now), now, {"level": 1, "state": "slow"}),
        ("5-hour window at 96%: hold", reading(96, 20, now=now), now, {"level": 2, "state": "hold"}),
        ("week at 91%: slow", reading(10, 91, now=now), now, {"level": 1, "state": "slow"}),
        ("5-hour window full: at the limit, until its reset", reading(100, 20, now=now), now,
         {"level": 2, "at_limit": True, "state": "limit", "until": "2026-11-23T13:00:00Z"}),
        ("a full window whose reset time has passed counts as empty", reading(100, 20, primary_reset=-60, now=now), now,
         {"level": 0, "at_limit": False, "state": "ok"}),
        ("no reading at all: unknown, nothing is slowed", None, now, {"known": False, "level": 0}),
        ("a reading with no windows (an API key account): unknown", {"rateLimits": {}}, now, {"known": False, "level": 0}),
        ("a window of another length is not taken for the 5-hour one", reading(99, None, now=now, primary_mins=60), now,
         {"level": 0, "session_pct": None}),
    ]
    out = []
    for name, r, at, want in rows:
        got = pace_now(on, plan_from_codex(r, at, at), at)
        out.append({"case": name, "pass": all(got.get(k) == v for k, v in want.items()),
                    "want": want, "got": {k: got.get(k) for k in ("known", "level", "state", "at_limit", "until", "session_pct", "weekly_pct")}})
    # stale: the same full reading, looked at 31 minutes after it was taken, is not trusted
    r = reading(100, 20, now=now)
    later = now + timedelta(minutes=31)
    got = pace_now(on, plan_from_codex(r, now, later), later)
    out.append({"case": "a reading older than 30 minutes is dropped: unknown", "pass": got["known"] is False and got["level"] == 0,
                "want": {"known": False, "level": 0}, "got": {k: got.get(k) for k in ("known", "level", "state")}})
    off = pace_now({"pace": False}, plan_from_codex(reading(100, 100, now=now), now, now), now)
    out.append({"case": "pace off: the reading changes nothing (as for Claude)", "pass": off["state"] == "off" and off["level"] == 0,
                "want": {"state": "off", "level": 0}, "got": {k: off.get(k) for k in ("state", "level")}})
    return out


def live() -> dict:
    work = tempfile.mkdtemp(prefix="tares-codex-pace-")
    app = AppServer.spawn(codex_bin(), args=QUIET, cwd=work)
    try:
        app.initialize()
        acct = (app.request("account/read", {"refreshToken": False}).get("account") or {})
        r = app.request("account/rateLimits/read")
    finally:
        app.close()
    now = datetime.now(timezone.utc)
    shape = {"account_type": acct.get("type"), "plan_type": acct.get("planType"),
             "rateLimits": {k: (r.get("rateLimits") or {}).get(k) for k in ("limitId", "primary", "secondary", "rateLimitReachedType")},
             "other_keys": sorted(k for k in r if k not in ("rateLimits", "accountId"))}
    return {"reading": shape, "plan": plan_from_codex(r, now, now),
            "pace": {k: v for k, v in pace_now({"pace": True}, plan_from_codex(r, now, now), now).items() if k != "note"}}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(os.getcwd(), "codex-probe-out"))
    ap.add_argument("--no-live", action="store_true")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    result = {"cases": cases()}
    if not args.no_live:
        try:
            result["live"] = live()
        except Exception as e:
            result["live"] = {"error": repr(e)[:400]}
    with open(os.path.join(args.out, "pace.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, indent=1, ensure_ascii=False)
    for c in result["cases"]:
        print(("PASS " if c["pass"] else "FAIL ") + c["case"], "" if c["pass"] else json.dumps(c["got"]))
    print("live:", json.dumps(result.get("live"))[:700])
    return 0 if all(c["pass"] for c in result["cases"]) else 1


if __name__ == "__main__":
    sys.exit(main())
