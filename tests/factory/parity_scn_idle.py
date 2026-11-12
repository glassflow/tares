"""Ids of their own for the idle_wake and freeze scenarios that start stations.

A process that says `attach <id>` is visible to every process on the machine, and `claude.attached` looks at all of
them (psutil). The scenarios that attach the shared fake ids (aaaa0001, bbbb0002, bbbb0003,
cccc0004), or another crew's run of this harness, make a station that is idle here look "open in a terminal", and it is
not woken, restarted or upgraded. These scenarios keep their assertions as they are and run with ids nobody else uses.
"""
from parity import *  # noqa
import parity as P

# idle_wake_liveness: four stations, ids unique on the machine (the last four digits also make the fake pids differ)
LIVENESS_IDS = {"crew-orchestrator": "ee5a1a01", "crew-reviewer": "ee5a1a02", "crew-releaser": "ee5a1a03",
                "links-app-build-m1": "ee5a1a04"}


def _setup_liveness(tmp, env) -> None:
    P._setup_wake(tmp, env, LIVENESS_IDS)


# freeze_*: an orchestrator and a reviewer
FREEZE_IDS = {"aaaa0001": "ee5f1a01", "bbbb0002": "ee5f1a02"}


def _setup_freeze(tmp, env) -> None:
    P._fz_setup(tmp, env)
    crew, state = tmp / "crew", tmp / "claude"
    rows = json.loads((crew / "stations.json").read_text(encoding="utf-8"))
    for row in rows.values():
        row["id"] = FREEZE_IDS.get(row["id"], row["id"])
    (crew / "stations.json").write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    agents = json.loads((state / "agents.json").read_text(encoding="utf-8"))
    for row in agents:
        row["id"] = FREEZE_IDS.get(row["id"], row["id"])
        if "pid" in row:
            row["pid"] = 40000 + int(row["id"][-4:], 16)
    (state / "agents.json").write_text(json.dumps(agents, indent=2) + "\n", encoding="utf-8")


P.SCENARIOS["idle_wake_liveness"].setup = _setup_liveness
for _name in ("freeze_watch", "freeze_copywatch"):
    P.SCENARIOS[_name].setup = _setup_freeze
