"""Does a Codex builder take its operating rules from its start, and know what it does not have (TR-1598)? A
by-hand probe, not in CI: it needs a signed-in `codex` and spends one small turn.

    TARES_PROBE_CODEX=/path/to/codex python tests/factory/codex_probe/probe_instructions.py [--out DIR]

A builder station is started by the product code (`tares.crew.codex.start`) from the spec the product's Codex
runtime builds, in a throwaway crew home, with `{ROLE_MANUAL}` filled as `launch.launch_plan` fills it: the
kit's rules as a Codex station reads them (`tares.crew.role_text`). The cell is not in the loop. Its first turn
asks one thing, with no tool call: which tools its instructions say it does not have. What is checked:

  * the conversation was started with developer instructions that hold the identity and the whole manual
    (about 34,000 characters), so Codex takes a text of that size;
  * the turn ended, and the answer names the schedule tool and the station messages as things it lacks;
  * nothing was written into the folder it works in (no AGENTS.md), and the person's own Codex files are as
    they were.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

import probe_launch  # noqa: E402
import probe_station  # noqa: E402

STATION = "probe-build-api"
probe_launch.STATION = probe_station.STATION = STATION   # the helpers of both ask about this station

from appserver import codex_bin  # noqa: E402
from probe_launch import EFFORT, MODEL, Crew, path_without_claude, personal_state  # noqa: E402

from tares.crew import codex as cx  # noqa: E402
from tares.crew import codex_station as cs  # noqa: E402
from tares.crew import role_text  # noqa: E402
from tares.crew.errors import CrewError  # noqa: E402
from tares.integrations.builtin.runtime_codex import RuntimeCodex  # noqa: E402

KIT = os.path.join(ROOT, "claude-plugin")
ASK = ("This is a check of your instructions, not work: call no tool and read no file. Answer in one line: which "
       "tools or abilities do your instructions say a station on your runtime does not have?")


class BuilderCrew(Crew):
    def plan(self, lean: bool = True) -> dict:
        spec = RuntimeCodex().launch_spec("builder", STATION, {
            "crew": "probe", "crew_autonomy": "L3-review", "human": "the person", "model": MODEL, "effort": EFFORT,
            "tares_url": "http://127.0.0.1:9", "mcp_url": "http://127.0.0.1:9/mcp",
            "rules": {"allow": ["Work in {where} is expected work for this station."], "soft_deny": []}})
        manual = role_text.manual(KIT, role_text.CODEX, "builder")
        values = {"CREW_HOME": self.home, "TOKEN_FILE": os.path.join(self.home, ".tares-token"),
                  "PLUGIN_DIR_SHELL": KIT, "REPOS": "the probe's folder", "WT_REPOS": "the probe's folder",
                  "ROLE_MANUAL": "\n\n" + manual.rstrip(), **cx.machine_values(self.paths, STATION)}
        return cx.plan({"command": spec.command, "env": spec.env, "settings": spec.settings}, values)

    def start(self, plan=None, codex=None, prompt: str = "", resume: str = ""):
        plan = plan or self.plan()
        with open(os.path.join(self.home, "stations", f"{STATION}.settings.json"), "w", encoding="utf-8") as f:
            json.dump({"env": plan["env"], **plan["settings"]}, f)   # as launch.write_settings leaves it
        try:
            cx.start(self.client, STATION, plan, self.work, prompt,
                     {"role": "builder", "model": MODEL, "effort": EFFORT, "cwd": self.work, "sessionId": resume},
                     codex=codex or codex_bin())
            return None, self.row()
        except CrewError as e:
            return str(e), self.row()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(os.getcwd(), "codex-probe-out"))
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    before = personal_state()
    c = BuilderCrew()
    c.client.env["PATH"] = os.environ["PATH"] = path_without_claude()
    plan = c.plan()
    told = plan["settings"]["thread"]["developerInstructions"]
    answer, status, reached, took = "", "", False, 0.0
    try:
        err, row = c.start(plan, prompt=ASK)
        if not err:
            reached, _, took = probe_station.wait_for(c.paths, "done", 180)
            thread = cs._recorded_thread(c.paths, STATION)
            with cs.visit(c.paths, STATION) as wire:
                found = cs._thread(wire, thread, turns=True)[0] or {}
            turn = (found.get("turns") or [{}])[-1]
            status = str(turn.get("status") or "")
            texts = [i.get("text") or "" for i in turn.get("items") or [] if i.get("type") == "agentMessage"]
            answer = texts[-1] if texts else ""
            tools = [i.get("type") for i in turn.get("items") or [] if i.get("type") in ("commandExecution", "mcpToolCall")]
        else:
            tools = []
        low = answer.lower()
        result = {
            "codex": cx.codex_version(codex_bin()), "model": MODEL, "effort": EFFORT, "start_error": err,
            "developer_instructions_chars": len(told), "turn_status": status, "seconds": took, "answer": answer[:600],
            "checks": {
                "the start gave the conversation the identity and the whole manual": err is None
                    and told.startswith("You are a tares-factory station: role builder") and len(told) > 30000
                    and role_text.named(told) == [],
                "the turn ended": reached and status == "completed",
                "it called no tool": tools == [],
                "the answer says it has no schedule of its own": any(w in low for w in ("schedule", "tick", "cron", "timer")),
                "the answer says it cannot message another station": "messag" in low,
                "nothing was written into the folder it works in": sorted(os.listdir(c.work)) == [".git"],
                "the person's own Codex files are as they were": personal_state() == before,
            }}
    finally:
        try:
            cs.stop(c.paths, STATION)
        except CrewError:
            pass
        c.end(wait=3)
        c.remove()
    result["pass"] = all(result["checks"].values())
    with open(os.path.join(args.out, "instructions.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, indent=1, ensure_ascii=False)
    print("PASS" if result["pass"] else "FAIL", "a Codex builder takes its rules from its start")
    for k, v in result["checks"].items():
        print(f"      {'ok ' if v else 'NO '} {k}")
    print("      answer:", answer[:400])
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
