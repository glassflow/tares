"""Runs per agent per day as a console setting (TR-325): the saved value, else
TARES_AGENT_DAILY_CAP, else 50; read at each run, so a change needs no restart. An agent's own
daily_cap (a project param, RIUS-1101) wins over all three.
"""
import asyncio
import os
import signal
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import clean_env

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tares.builtin_agents as ba
from tares.store import Store

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok   {label}")
    else:
        FAIL += 1; print(f"  FAIL {label}  {detail}")


class Settings:
    def __init__(self, **kv):
        self.kv = kv

    def get_setting(self, k):
        return self.kv.get(k)


class RunStore(Settings):
    """What the run path touches before the model is called."""

    def __init__(self, runs_today, frees_at=None, **kv):
        super().__init__(**kv)
        self.runs_today, self.frees_at, self.finished = runs_today, frees_at, None

    def agent_cap_frees_at(self, *a, **k):
        return self.frees_at

    def agent_runs_today(self, *a, **k):
        return self.runs_today

    def agent_cost_total(self, *a):
        # past the run cap the next gate is the budget; spent out, it stops the run before any
        # model call, so a "budget" refusal proves the run cap let it through
        return 1e9

    def finish_agent_run(self, run_id, status, **kw):
        self.finished = (status, kw.get("error"))


class Obs:
    def set_attribute(self, *a):
        pass


def unit():
    print("== where the cap comes from ==")
    os.environ.pop(ba.DAILY_CAP_ENV, None)
    check("default 50", ba.daily_cap(Settings()) == (50, "default"))
    os.environ[ba.DAILY_CAP_ENV] = "120"
    check("the environment over the default", ba.daily_cap(Settings()) == (120, "env"))
    check("the console over the environment",
          ba.daily_cap(Settings(agent_daily_cap="200")) == (200, "console"))
    check("a bad stored value is skipped", ba.daily_cap(Settings(agent_daily_cap="lots")) == (120, "env"))
    os.environ[ba.DAILY_CAP_ENV] = "0"
    check("an out-of-range env value is skipped", ba.daily_cap(Settings()) == (50, "default"))
    os.environ.pop(ba.DAILY_CAP_ENV, None)


def next_run():
    print("== when the next run is allowed (TR-671) ==")
    now = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
    words = lambda **d: ba.next_run_words(now + timedelta(**d), now)
    check("1h 12m", words(hours=1, minutes=12) == "next run allowed in 1h 12m", words(hours=1, minutes=12))
    check("rounds up to the minute", words(minutes=71, seconds=1) == "next run allowed in 1h 12m")
    check("whole hours", words(hours=3) == "next run allowed in 3h", words(hours=3))
    check("under an hour", words(minutes=5) == "next run allowed in 5m", words(minutes=5))
    check("a moment is a minute, not zero", words(seconds=10) == "next run allowed in 1m")
    check("nothing waits", ba.next_run_words(None, now) == "")

    store = Store(os.path.join(tempfile.mkdtemp(), "t.duckdb"))

    def run(status, ago, practice=False):
        rid = uuid.uuid4().hex
        store.start_agent_run(rid, "watcher", "spike", "d", "k", "h", practice=practice)
        store.finish_agent_run(rid, status)
        store.sc.execute("UPDATE agent_runs SET started_at = now() - ? * INTERVAL 1 MINUTE "
                          "WHERE id = ?", [ago, rid])
        return rid

    def left(cap, **kw):
        at = store.agent_cap_frees_at("watcher", cap, **kw)
        return None if at is None else round((at - datetime.now(timezone.utc)).total_seconds() / 60)

    check("no runs, nothing waits", left(2) is None)
    run("ok", 600)
    check("under the cap, nothing waits", left(2) is None)
    run("ok", 100)
    check("at the cap, it waits for the oldest run to be a day old", left(2) == 1440 - 600, str(left(2)))
    run("capped", 5)
    run("capped", 3)
    check("capped rows neither count nor move it", left(2) == 840, str(left(2)))
    run("failed", 50)
    check("over the cap, the run that brings the count under it", left(2) == 1440 - 100, str(left(2)))
    check("a cap of 1 waits for the third", left(1) == 1440 - 50, str(left(1)))
    mine = run("ok", 20, practice=True)
    check("a practice run counts unless health leaves it out",
          left(2) == 1440 - 50 and left(2, include_practice=False) == 1440 - 100,
          f"{left(2)} {left(2, include_practice=False)}")
    check("the run being checked is left out",
          left(2, exclude_run_id=mine) == 1440 - 100, str(left(2, exclude_run_id=mine)))
    store.con.close()


async def enforcement():
    print("== the run path reads it at each run ==")
    ba.resolve_for_agent = lambda store, agent: (object(), "test", "anthropic", "")
    r = object.__new__(ba.AgentRunner)
    r.store = RunStore(runs_today=3, agent_daily_cap="3")
    status, err = await r._run_traced({"name": "a", "prompt": "p"}, "t", "k", "", "run_1", None,
                                      None, Obs(), ("k", {}))
    check("at the cap the run is capped", status == "capped" and "3 runs" in (err or ""), f"{status} {err}")
    check("and says where to raise it", "Settings, Agents" in (err or ""), err)
    r.store = RunStore(runs_today=3, agent_daily_cap="3",
                       frees_at=datetime.now(timezone.utc) + timedelta(hours=1, minutes=11, seconds=30))
    status, err = await r._run_traced({"name": "a", "prompt": "p"}, "t", "k", "", "run_1", None,
                                      None, Obs(), ("k", {}))
    check("and when the next run is allowed (TR-671)",
          status == "capped" and (err or "").endswith(", or wait: next run allowed in 1h 12m"), err)

    print("== an agent's own daily_cap (RIUS-1101) ==")
    os.environ.pop(ba.DAILY_CAP_ENV, None)

    async def run(agent, runs_today, **settings):
        r.store = RunStore(runs_today=runs_today, **settings)
        return await r._run_traced({"name": "a", "prompt": "p", "budget_usd": 1, **agent}, "t",
                                   "k", "", "run_1", None, None, Obs(), ("k", {}))

    status, err = await run({"daily_cap": 3}, 2)
    check("cap 3: run 3 in 24h goes ahead", status == "capped" and "budget" in (err or ""),
          f"{status} {err}")
    status, err = await run({"daily_cap": 3}, 3)
    check("cap 3: run 4 in 24h is refused", status == "capped" and "3 runs" in (err or ""),
          f"{status} {err}")
    check("and points at the project, not Settings", "daily_cap" in (err or "")
          and "Settings" not in (err or ""), err)
    status, err = await run({"daily_cap": 3}, 3, agent_daily_cap="200")
    check("the agent's cap wins over the console setting", "3 runs" in (err or ""), err)
    status, err = await run({"daily_cap": 100}, 60)
    check("a cap above 50 lets run 61 through", "budget" in (err or ""), f"{status} {err}")
    status, err = await run({}, 49)
    check("no agent cap: run 50 goes ahead", "budget" in (err or ""), f"{status} {err}")
    status, err = await run({"daily_cap": None}, 50)
    check("no agent cap: run 51 is refused at the default 50", "50 runs" in (err or ""),
          f"{status} {err}")


async def api():
    print("== the settings API on a running daemon ==")
    tmp = tempfile.mkdtemp()
    port = "8846"
    env = {**clean_env(), "TARES_DB": os.path.join(tmp, "t.duckdb"), "TARES_PORT": port,
           "TARES_MCP_PORT": "8847", "TARES_OTLP_GRPC_PORT": "off", "TARES_AGENT_DAILY_CAP": "80"}
    proc = subprocess.Popen([sys.executable, "-c", "from tares.cli import run_daemon; run_daemon()"],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    B = f"http://127.0.0.1:{port}"
    try:
        async with httpx.AsyncClient(timeout=10) as cx:
            for _ in range(60):
                try:
                    if (await cx.get(f"{B}/health")).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.5)
            s = (await cx.get(f"{B}/api/settings/agents")).json()
            check("starts from the environment", s["daily_cap"] == 80 and s["daily_cap_source"] == "env", str(s))
            r = await cx.put(f"{B}/api/settings/agents", json={"daily_cap": 200})
            check("saving 200 takes over", r.status_code == 200 and r.json()["daily_cap"] == 200
                  and r.json()["daily_cap_source"] == "console", r.text)
            for bad in (0, 10001, "many"):
                r = await cx.put(f"{B}/api/settings/agents", json={"daily_cap": bad})
                check(f"{bad!r} is refused with a plain message", r.status_code == 400
                      and "whole number from 1 to 10000" in r.text, r.text)
            r = await cx.put(f"{B}/api/settings/agents", json={"daily_cap": ""})
            check("clearing falls back to the environment", r.json()["daily_cap"] == 80
                  and r.json()["daily_cap_source"] == "env", r.text)
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    unit()
    next_run()
    asyncio.run(enforcement())
    asyncio.run(api())
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
