#!/usr/bin/env python3
"""Status line for tares-factory stations: a short line for whoever attaches, and a report to Tares.

Claude Code runs this after each reply with the session's state on stdin (the model, the context
window, its cost estimate, and on a Pro or Max plan the 5-hour and weekly limits). The factory sets
it in each station's settings. It prints one line, then sends a `session_status` line to Tares's
claude_code source when something changed (or a minute passed), so the Crew page can show how full
each station's window is, how near it is to compacting (FACTORY_AUTOCOMPACT, the --autocompact it was
started with), and how much of the plan is left. Best effort: it never fails the session,
and waits at most two seconds for Tares.
"""
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ship import _post, config, now_iso  # noqa: E402

RESEND = 60   # seconds after which an unchanged report is sent again, so Tares knows it is current
KEEP = ("model", "context_window", "cost", "rate_limits", "exceeds_200k_tokens", "session_name",
        "version")


def human(n) -> str:
    n = n or 0
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}k" if n >= 1e3 else str(n)


def render(d: dict) -> str:
    parts = [((d.get("model") or {}).get("display_name") or "Claude")]
    cw = d.get("context_window") or {}
    pct, size = cw.get("used_percentage"), cw.get("context_window_size")
    if pct is not None and size:
        parts.append(f"context {pct}% of {human(size)}")
    rl = d.get("rate_limits") or {}
    for key, label in (("five_hour", "5h"), ("seven_day", "week")):
        p = (rl.get(key) or {}).get("used_percentage")
        if p is not None:
            parts.append(f"{label} {p:.0f}%")
    return " · ".join(parts)


def changed(data_dir: str, sid: str, report: dict) -> bool:
    """True when the report differs from the last one sent for the session, or that is old."""
    sig = hashlib.sha1(json.dumps({k: report.get(k) for k in KEEP if k != "cost"},
                                  sort_keys=True, default=str).encode()).hexdigest()
    path = os.path.join(data_dir, "sessions",
                        hashlib.sha1(sid.encode()).hexdigest()[:16] + ".status")
    try:
        last_sig, last_at = open(path).read().split()
        if last_sig == sig and time.time() - float(last_at) < RESEND:
            return False
    except (OSError, ValueError):
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(f"{sig} {time.time()}")
    return True


def main() -> None:
    try:
        d = json.load(sys.stdin)
    except Exception:
        print("Claude")
        return
    print(render(d))
    sys.stdout.flush()
    sid = d.get("session_id")
    if not sid:
        return
    cfg = config()
    if not changed(cfg["data_dir"], sid, d):
        return
    status = {k: d.get(k) for k in KEEP if k in d}
    compact = os.environ.get("FACTORY_AUTOCOMPACT", "")
    if compact.isdigit():   # where the station compacts, when the factory started it with a limit
        status["autocompact_tokens"] = int(compact)
    line = {"type": "session_status", "sessionId": sid, "timestamp": now_iso(),
            "version": "tares-plugin", "status": status}
    _post(cfg["base"] + "/ingest/claude_code", (json.dumps(line) + "\n").encode(), cfg["headers"],
          timeout=2)


if __name__ == "__main__":
    try:
        main()
    except Exception:   # a status line must never get in the session's way
        pass
