"""Runs per agent per day as a console setting (TR-325): the saved value, else
TARES_AGENT_DAILY_CAP, else 50; read at each run, so a change needs no restart.
"""
import asyncio
import os
import signal
import subprocess
import sys
import tempfile

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tares.builtin_agents as ba

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

    def __init__(self, runs_today, **kv):
        super().__init__(**kv)
        self.runs_today, self.finished = runs_today, None

    def agent_runs_today(self, *a, **k):
        return self.runs_today

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


async def enforcement():
    print("== the run path reads it at each run ==")
    ba.resolve_for_agent = lambda store, agent: (object(), "test", "anthropic", "")
    r = object.__new__(ba.AgentRunner)
    r.store = RunStore(runs_today=3, agent_daily_cap="3")
    status, err = await r._run_traced({"name": "a", "prompt": "p"}, "t", "k", "", "run_1", None,
                                      None, Obs(), ("k", {}))
    check("at the cap the run is capped", status == "capped" and "3 runs" in (err or ""), f"{status} {err}")
    check("and says where to raise it", "Settings, Agents" in (err or ""), err)


async def api():
    print("== the settings API on a running daemon ==")
    tmp = tempfile.mkdtemp()
    port = "8846"
    env = {**os.environ, "TARES_DB": os.path.join(tmp, "t.duckdb"), "TARES_PORT": port,
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
    asyncio.run(enforcement())
    asyncio.run(api())
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
