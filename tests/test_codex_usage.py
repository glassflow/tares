"""The Codex account's limits on the cell (tares/codex_usage.py and its routes, TR-1597).

 a. the mapping, with the real `tares.factory.pace_now`: the cases tests/factory/codex_probe/probe_pace.py ran
    by hand (room, slow at 86%, hold at 96%, the week at 91%, a full window until its reset, a full window whose
    reset has passed, no reading, an account with no windows, a window of another length, a reading older than
    30 minutes, pace off), the crew's thresholds at their edges (85, 90, 95), a window told by its length and not
    by its place, a further bucket that is shown and slows nothing, and what is kept of a reading (no account
    id, no address);
 b. the four readings, each said: ok, none, stale, unsupported;
 c. the routes: PUT /api/crew/codex-limits keeps a reading (an ingest key may send it, a read key may not);
    GET /api/crew/pace?runtime=codex is worked out from it and from nothing else, with a Claude plan at 99% on
    the same cell; without `runtime` the cell's own runtime decides; a Claude Code crew's pace is what it was.

The crew's side (reading the limits from a station and acting on them) is tests/test_crew_codex_pace.py.

Run: python tests/test_codex_usage.py
"""
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SCRATCH = Path(tempfile.mkdtemp(prefix="tares-codex-usage-"))
for _k in [k for k in os.environ if k.startswith(("TARES", "FACTORY", "FAKE_CLAUDE", "CLAUDE_PLUGIN_OPTION_"))]:
    del os.environ[_k]
os.environ.pop("TARESD_URL", None)
os.environ.update(
    HOME=str(SCRATCH / "home"), USERPROFILE=str(SCRATCH / "home"),
    PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring", TARES_HOME=str(SCRATCH / "tares-home"),
    TARES_DB=str(SCRATCH / "cell.duckdb"), TARES_CATALOG=str(SCRATCH / "none.yaml"),
    TARES_OTLP_GRPC_PORT="off", TARES_AUTH_TOKEN="root-token-codex-usage",
)
(SCRATCH / "home").mkdir()
ROOT_TOKEN = os.environ["TARES_AUTH_TOKEN"]

sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from tares import codex_usage as cu  # noqa: E402
from tares.claude_usage_store import PLAN_SETTING as CLAUDE_PLAN  # noqa: E402
from tares.factory import PACE_HOLD, PACE_SLOW, pace_now  # noqa: E402

P = F = 0
NOW = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)
ON = {"pace": True}


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def window(percent, mins, reset=3600, now=NOW):
    return {"usedPercent": percent, "windowDurationMins": mins,
            "resetsAt": int((now + timedelta(seconds=reset)).timestamp()) if reset is not None else None}


def limits(primary=None, secondary=None, primary_reset=3600, secondary_reset=86400, primary_mins=300, **more):
    rl = {"limitId": "codex", "limitName": None}
    if primary is not None:
        rl["primary"] = window(primary, primary_mins, primary_reset)
    if secondary is not None:
        rl["secondary"] = window(secondary, 10080, secondary_reset)
    return {"rateLimits": rl, **more}


class Mem:
    """A store with settings and nothing else."""

    def __init__(self):
        self.s = {}

    def get_setting(self, k):
        return self.s.get(k)

    def set_setting(self, k, v):
        self.s[k] = v


def paced(reading, taken=NOW, at=NOW, settings=ON, account=None, store=None):
    """What the cell answers at `at` for a reading that arrived at `taken`."""
    store = store or Mem()
    if reading is not None:
        cu.save(store, {"limits": reading, "account": account or {"type": "chatgpt", "planType": "plus"}}, "pat", taken)
    return cu.pace(store, settings, at, pace_now)


def has(got, want):
    return all(got.get(k) == v for k, v in want.items())


print("a. the mapping")
for name, r, want in (
        ("room on both windows: known, nothing slowed", limits(6, 20), {"known": True, "level": 0, "state": "ok", "reading": "ok"}),
        ("the 5-hour window at 86%: slow", limits(86, 20), {"level": 1, "state": "slow"}),
        ("the 5-hour window at 96%: hold", limits(96, 20), {"level": 2, "state": "hold"}),
        ("the week at 91%: slow", limits(10, 91), {"level": 1, "state": "slow"}),
        ("the 5-hour window full: at the limit, until its reset", limits(100, 20),
         {"level": 2, "at_limit": True, "state": "limit", "until": "2026-11-25T13:00:00Z"}),
        ("a full window whose reset time has passed counts as empty", limits(100, 20, primary_reset=-60),
         {"level": 0, "at_limit": False, "state": "ok"}),
        ("a window of another length is not taken for the 5-hour one", limits(99, None, primary_mins=60),
         {"known": True, "level": 0, "session_pct": None, "weekly_pct": None})):
    got = paced(r)
    ck(name, has(got, want), {k: got.get(k) for k in want})
got = paced(limits(99, None, primary_mins=60))
ck("and that window is shown, under a name of its own, marked as one that does not pace",
   [(x["kind"], x["percent"], x["paces"]) for x in got["limits"]] == [("codex_60m", 99, False)]
   and got["limits"][0]["label"] == "A window of 1 hour", got["limits"])
ck("pace off: the reading changes nothing", has(paced(limits(100, 100), settings={"pace": False}), {"state": "off", "level": 0, "on": False}))
for pct, mins_slot, want in ((PACE_SLOW["session"] - 1, "primary", 0), (PACE_SLOW["session"], "primary", 1),
                             (PACE_HOLD - 1, "primary", 1), (PACE_HOLD, "primary", 2),
                             (PACE_SLOW["weekly_all"] - 1, "secondary", 0), (PACE_SLOW["weekly_all"], "secondary", 1)):
    r = limits(pct, 5) if mins_slot == "primary" else limits(5, pct)
    ck(f"the crew's own thresholds: {'the 5-hour window' if mins_slot == 'primary' else 'the week'} at {pct}% is level {want}",
       paced(r)["level"] == want, paced(r)["level"])
ck("the thresholds are the ones a Claude Code crew has", (PACE_SLOW, PACE_HOLD) == ({"session": 85, "weekly_all": 90}, 95))
swapped = {"rateLimits": {"primary": window(20, 10080, 86400), "secondary": window(96, 300)}}
got = paced(swapped)
ck("a window is told by its length, not by its place: the 5-hour one in the second place still holds the crew",
   has(got, {"level": 2, "state": "hold", "session_pct": 96, "weekly_pct": 20}), got)
named = {"rateLimits": {"a": window(50, 10080, 86400), "b": window(86, 300), "limitId": "codex"}}
ck("nor by what the place is called", has(paced(named), {"level": 1, "session_pct": 86, "weekly_pct": 50}), paced(named))
more = limits(10, 20, rateLimitsByLimitId={
    "codex": {"limitId": "codex", "primary": window(10, 300), "secondary": window(20, 10080, 86400)},
    "codex_other": {"limitId": "codex_other", "limitName": "Another model", "primary": window(100, 300)}})
got = paced(more)
ck("a further bucket (the limit of one model) is shown and never slows the crew, and the main one is not listed twice",
   has(got, {"level": 0, "at_limit": False, "session_pct": 10})
   and [(x["kind"], x["paces"]) for x in got["limits"]]
   == [("session", True), ("weekly_all", True), ("codex_codex_other_300m", False)]
   and got["limits"][2]["label"] == "Another model, 5 hours", got["limits"])
kept = cu.compact_reading({"limits": {**limits(6, 20), "accountId": "acct-123"},
                           "account": {"type": "chatgpt", "planType": "plus", "email": "pat@example.com",
                                       "workspaceName": "Pat's workspace"}, "taken_at": "2026-11-25T12:00:00Z"})
ck("of a reading the windows and the account's type and plan are kept; no account id, address or workspace name",
   kept["account"] == {"type": "chatgpt", "plan": "plus"} and len(kept["windows"]) == 2
   and not any(x in json.dumps(kept) for x in ("acct-123", "pat@example.com", "workspace")), kept)
for junk in ({}, {"limits": "x"}, {"limits": {"rateLimits": [1, 2]}}, {"limits": {"rateLimits": {"primary": {"usedPercent": "9"}}}},
             {"limits": {"rateLimits": {"primary": {"usedPercent": True, "windowDurationMins": 300}}}}):
    ck(f"a reading that is not one keeps no window: {json.dumps(junk)[:50]}", cu.compact_reading(junk)["windows"] == [])
ck("a window with no length and no reset time is kept, shown, and does not pace",
   [(x["kind"], x["paces"], x["resets_at"]) for x in paced({"rateLimits": {"primary": {"usedPercent": 100}}})["limits"]]
   == [("codex_unknownm", False, None)])

print("b. the four readings")
got = paced(None)
ck("no reading: unknown, nothing is slowed, and it says none has arrived",
   has(got, {"known": False, "level": 0, "reading": "none", "source": "codex", "read_at": None, "limits": []})
   and "no reading of the Codex account's limits has arrived" in got["note"], got)
got = paced({"rateLimits": {}}, account={"type": "apiKey"})
ck("an account with no windows (an API key): unknown, said as unsupported, and it names the account",
   has(got, {"known": False, "level": 0, "reading": "unsupported"}) and "an API key account" in got["note"]
   and got["account"] == {"type": "apiKey", "plan": None}, got)
got = paced(limits(100, 20), taken=NOW, at=NOW + timedelta(minutes=31))
ck("a reading older than 30 minutes is not used: unknown, said as stale, with when it was taken",
   has(got, {"known": False, "level": 0, "at_limit": False, "reading": "stale", "limits": []})
   and "2026-11-25 12:00 UTC" in got["note"] and "older than 30 minutes" in got["note"], got)
got = paced(limits(100, 20), taken=NOW, at=NOW + timedelta(minutes=29))
ck("at 29 minutes it still counts", has(got, {"known": True, "level": 2, "at_limit": True, "reading": "ok"}), got)
ck("a reading's time is the cell's, whatever the crew's clock says",
   paced({**limits(96, 20)}, taken=NOW)["read_at"] == "2026-11-25T12:00:00Z")
ck("unknown never carries a level, a limit or a time", all(
    has(g, {"level": 0, "at_limit": False, "until": None, "state": "ok"})
    for g in (paced(None), paced({"rateLimits": {}}), paced(limits(100, 100), at=NOW + timedelta(hours=2)))))
ck("every reading is one of the four names", set(cu.READINGS) == {"ok", "none", "stale", "unsupported"})
store = Mem()
store.set_setting(CLAUDE_PLAN, json.dumps({"at": NOW.isoformat(), "person_id": "pat", "plan": {"limits": [
    {"kind": "session", "label": "Current session (5 hours)", "percent": 99,
     "resets_at": (NOW + timedelta(hours=1)).isoformat()}]}}))
ck("a Claude plan at 99% on the same cell is not a Codex reading: the Codex pace stays unknown",
   has(paced(None, store=store), {"known": False, "level": 0, "reading": "none"}))


# ---------------------------------------------------------------------------------------------
async def main():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store
    hdr = {"Authorization": f"Bearer {ROOT_TOKEN}"}
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=hdr) as root:
            print("c. the routes")
            await root.put("/api/crew/settings", json={"pace": True})
            now = datetime.now(timezone.utc)
            body = {"limits": {**limits(96, 20), "accountId": "acct-123"},
                    "account": {"type": "chatgpt", "planType": "plus", "email": "pat@example.com"},
                    "taken_at": "2026-11-25T12:00:00Z"}
            body["limits"]["rateLimits"]["primary"]["resetsAt"] = int((now + timedelta(hours=1)).timestamp())
            body["limits"]["rateLimits"]["secondary"]["resetsAt"] = int((now + timedelta(days=2)).timestamp())
            r = await root.get("/api/crew/pace", params={"runtime": "codex"})
            ck("before any reading a Codex crew's pace is unknown, and says so",
               r.status_code == 200 and has(r.json(), {"on": True, "known": False, "reading": "none", "source": "codex"}), r.text[:300])
            r = await root.put("/api/crew/codex-limits", json=body)
            ck("a reading is taken, and the answer says what was kept",
               r.status_code == 200 and r.json() == {"windows": 2, "account": {"type": "chatgpt", "plan": "plus"}}, r.text[:300])
            raw = store.get_setting(cu.PLAN_SETTING) or ""
            ck("what the cell keeps has no account id and no address", "acct-123" not in raw and "pat@example.com" not in raw, raw[:200])
            r = await root.get("/api/crew/pace", params={"runtime": "codex"})
            got = r.json()
            ck("the pace of a Codex crew is worked out from it: hold at 96%",
               has(got, {"known": True, "level": 2, "state": "hold", "session_pct": 96, "weekly_pct": 20, "reading": "ok",
                         "source": "codex"}) and [x["kind"] for x in got["limits"]] == ["session", "weekly_all"]
               and "5-hour window is at 96%" in got["note"], r.text[:500])
            r = await root.get("/api/crew/pace")
            ck("a Claude Code crew on the same cell is not paced from it: no plan reading, as before",
               has(r.json(), {"on": True, "known": False, "level": 0}) and "source" not in r.json(), r.text[:300])
            r = await root.get("/api/crew/pace", params={"runtime": "claude_code"})
            ck("and the same when it names its runtime", has(r.json(), {"known": False, "level": 0}) and "source" not in r.json())
            store.set_setting(CLAUDE_PLAN, json.dumps({"at": now.isoformat(), "person_id": None, "plan": {"limits": [
                {"kind": "session", "label": "Current session (5 hours)", "percent": 99,
                 "resets_at": (now + timedelta(hours=1)).isoformat()}]}}))
            claude = (await root.get("/api/crew/pace")).json()
            ck("a Claude plan at 99% holds the Claude Code crew", has(claude, {"known": True, "level": 2, "session_pct": 99}), claude)
            await root.put("/api/crew/codex-limits", json={"limits": limits(6, 20), "account": {"type": "chatgpt"}})
            got = (await root.get("/api/crew/pace", params={"runtime": "codex"})).json()
            ck("and leaves the Codex crew alone: its own reading says there is room",
               has(got, {"known": True, "level": 0, "state": "ok", "session_pct": 6}), got)
            await root.put("/api/crew/codex-limits", json={"limits": {"rateLimits": {}}, "account": {"type": "apiKey"}})
            got = (await root.get("/api/crew/pace", params={"runtime": "codex"})).json()
            ck("an API key account's reading: unknown, unsupported, never Claude's 99%",
               has(got, {"known": False, "level": 0, "reading": "unsupported", "session_pct": None}), got)
            r = await root.put("/api/factory/integrations/runtime", json={"name": "codex", "settings": {}})
            await root.put("/api/crew/codex-limits", json=body)
            got = (await root.get("/api/crew/pace")).json()
            crew = (await root.get("/api/crew")).json()["pace"]
            ck("with the cell's runtime set to codex, the pace asked without a runtime is the Codex one, on /api/crew too",
               r.status_code == 200 and has(got, {"source": "codex", "level": 2, "session_pct": 96})
               and has(crew, {"source": "codex", "level": 2}), (got, crew))
            got = (await root.get("/api/crew/pace", params={"runtime": "claude_code"})).json()
            ck("and a Claude Code crew still on that cell names its runtime and gets its own", has(got, {"level": 2, "session_pct": 99})
               and "source" not in got, got)
            await root.put("/api/factory/integrations/runtime", json={"name": "claude_code", "settings": {}})
            r = await root.get("/api/crew/pace", params={"runtime": "gemini"})
            ck("a runtime that is not one is refused", r.status_code == 400, r.text[:200])
            for junk in ({}, {"limits": [1]}, {"limits": "full"}):
                r = await root.put("/api/crew/codex-limits", json=junk)
                ck(f"a reading without limits is refused: {junk}", r.status_code == 400, r.text[:200])
            rk = (await root.post("/api/keys", json={"name": "reader", "scopes": ["read"]})).json()["secret"]
            ik = (await root.post("/api/keys", json={"name": "crew", "scopes": ["ingest", "read"]})).json()["secret"]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as cx:
            r = await cx.put("/api/crew/codex-limits", json=body, headers={"Authorization": f"Bearer {rk}"})
            ck("a read key may not send a reading", r.status_code == 403, (r.status_code, r.text[:200]))
            r = await cx.put("/api/crew/codex-limits", json=body, headers={"Authorization": f"Bearer {ik}"})
            ck("an ingest key may, as it may send a Claude plan reading", r.status_code == 200, (r.status_code, r.text[:200]))
            r = await cx.put("/api/crew/codex-limits", json=body)
            ck("no key at all is refused", r.status_code in (401, 403), r.status_code)
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


asyncio.run(main())
