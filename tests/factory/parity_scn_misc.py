"""Parity scenarios for a Tares that lacks a capability, and the one crew check
that no other scenario stands for ("orchestrator may spawn as tares factory"). See parity.py for the machinery."""
from parity import *  # noqa
import parity as P  # noqa  (underscore-prefixed helpers)

import ast


# --- a Tares that lacks a capability -------------------------------------------------------------------------------------------
# One stand-in Tares (fake_up_tares.py) plays an old cell, one with other capabilities and one that has the
# capability, in turn, its /health body swapped between the steps. The "real" body has the shape tares/daemon.py gives /health (status,
# auth_required, sources, pct_used, version, capabilities, uptime_seconds) and its capabilities are read from
# CAPABILITIES in tares/daemon.py itself, so the stand-in lists what a real cell lists.

COMPAT_MSG = "This needs a newer Tares on the cell (missing integrations). Upgrade Tares, then run this again."
COMPAT_MSGQ = "This needs a newer Tares on the cell (missing merge_queue). Upgrade Tares, then run this again."
OLD_CELL = {"status": "ok", "auth_required": False, "sources": [], "pct_used": None, "version": "1.0.0"}
OTHER_CELL = {"status": "ok", "auth_required": False, "sources": [], "pct_used": None, "capabilities": ["merge_queue"]}
NOMQ_CELL = {"status": "ok", "auth_required": False, "sources": [], "pct_used": None, "capabilities": ["integrations"]}


def _daemon_capabilities() -> list:
    for line in (ROOT / "tares" / "daemon.py").read_text(encoding="utf-8").splitlines():
        if line.startswith("CAPABILITIES"):
            return ast.literal_eval(line.split("=", 1)[1].strip())
    return []


def _real_cell() -> dict:
    return {"status": "ok", "auth_required": False, "sources": [], "pct_used": None, "version": "1.0.0",
            "capabilities": _daemon_capabilities(), "uptime_seconds": 1}


def _cell(body: dict, mark_name: str = ""):
    """A step's run_before: the stand-in now answers /health with this body (and keeps the launches so far)."""
    def before(r):
        if mark_name:
            r.marks[mark_name] = r.launches()
        r.stand_in.health = dict(body)
    return before


def _health_of_real_cell(r):
    """What a real cell's /health said, kept for the check before the step that runs against it."""
    r.stand_in.health = _real_cell()
    import urllib.request
    with urllib.request.urlopen(r.stand_in.url + "/health", timeout=5) as resp:
        r.marks["real_health"] = json.loads(resp.read().decode("utf-8"))
    r.marks["after_other"] = r.launches()


def _python_body(path: str, name: str) -> str:
    text = (ROOT / "tares" / "crew" / path).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(text, node) or ""
    return ""


def _never_asks_for_a_capability(r) -> bool:
    """views.cmd_status and lifecycle.down must exist, and neither may call need_capability."""
    parts = [_python_body("views.py", "cmd_status"), _python_body("lifecycle.py", "down")]
    return all(parts) and not any("need_capability" in p for p in parts)


scenario(Scenario(
    name="compat_capabilities",
    suite="compat",
    about="crew up stops with the upgrade message on a cell without the integrations capability; status and down never ask",
    stand_in={"health": OLD_CELL},
    prepare=trust_crew,
    steps=[
        Step(["crew", "up", "--yes"], stdin=""),                                                          # 0 old cell
        Step(["crew", "up", "--yes"], stdin="", run_before=_cell(OTHER_CELL, "after_old")),                # 1 other capabilities
        Step(["crew", "up"], stdin="L3-review\nno\nno\n2\n\n", run_before=_health_of_real_cell),           # 2 a cell that has it
        Step(["crew", "status"]),                                                                          # 3 the same cell
        Step(["crew", "status"], run_before=_cell(OLD_CELL)),                                              # 4 old cell
        Step(["crew", "down"]),                                                                            # 5 old cell
        Step(["crew", "status"], run_before=_cell(OTHER_CELL)),                                            # 6 other capabilities
        Step(["crew", "up", "--yes"], stdin="", run_before=_cell(NOMQ_CELL, "before_nomq")),               # 7 integrations, no merge_queue
        Step(["crew", "status"], run_before=_cell(NOMQ_CELL, "after_nomq")),                               # 8 the same cell
        Step(["crew", "down"]),                                                                            # 9 the same cell
    ],
    checks=[
        ("crew up against a cell with no capabilities stops with the upgrade message",
         lambda r: r.code(0) != 0 and COMPAT_MSG in r.out(0)),
        ("it starts nothing", lambda r: r.marks["after_old"] == 0),
        ("crew up against a cell with other capabilities stops the same way",
         lambda r: r.code(1) != 0 and COMPAT_MSG in r.out(1) and r.marks["after_other"] == 0),
        ("a real cell lists integrations on /health", lambda r: "integrations" in r.marks["real_health"]["capabilities"]),
        ("crew up against a cell with the capability proceeds",
         lambda r: r.code(2) == 0 and "needs a newer Tares" not in r.out(2) and "started crew-reviewer" in r.out(2)),
        ("crew status works against a cell with the capability",
         lambda r: r.code(3) == 0 and "crew-reviewer" in r.out(3)),
        ("crew status works against a cell without the capability",
         lambda r: r.code(4) == 0 and "needs a newer Tares" not in r.out(4)),
        ("crew down works against a cell without the capability",
         lambda r: r.code(5) == 0 and "needs a newer Tares" not in r.out(5)
         and re.search(r"stopped|down", r.out(5)) is not None),
        ("crew status works against a cell with other capabilities",
         lambda r: r.code(6) == 0 and "needs a newer Tares" not in r.out(6)),
        ("crew up against a cell without merge_queue stops and names it",
         lambda r: r.code(7) != 0 and COMPAT_MSGQ in r.out(7) and r.marks["after_nomq"] == r.marks["before_nomq"]),
        ("crew status works against a cell without merge_queue",
         lambda r: r.code(8) == 0 and "needs a newer Tares" not in r.out(8)),
        ("crew down works against a cell without merge_queue",
         lambda r: r.code(9) == 0 and "needs a newer Tares" not in r.out(9)),
        ("a real cell lists merge_queue on /health", lambda r: "merge_queue" in r.marks["real_health"]["capabilities"]),
        ("the CLI never asks for a capability in status or down", _never_asks_for_a_capability),
    ],
    covers=["crew up against a cell with no capabilities stops with the upgrade message",
            "it starts nothing",
            "crew up against a cell with other capabilities stops the same way",
            "a real cell lists integrations on /health",
            "crew up against a cell with the capability proceeds",
            "crew status works against a cell with the capability",
            "crew status works against a cell without the capability",
            "crew down works against a cell without the capability",
            "crew status works against a cell with other capabilities",
            "crew up against a cell without merge_queue stops and names it",
            "crew status works against a cell without merge_queue",
            "crew down works against a cell without merge_queue",
            "a real cell lists merge_queue on /health",
            "the CLI never asks for a capability in status or down"],
))


# --- the orchestrator's permission to run `tares factory spawn` --------------------------------------
# parity.py's launch_settings_crew checks it under a longer label ("... too (TR-1069)"); this scenario keeps the
# shorter label, on the same driver and the same assertion.

scenario(Scenario(
    name="launch_settings_spawn",
    suite="crew",
    about="the orchestrator may run both `factory spawn` and `tares factory spawn`",
    driver=LAUNCH_DRIVER,
    steps=[lstep("write_settings", "crew-orchestrator", "orchestrator", "", "", "",
                 world=lworld("L3-review", grants=LAUNCH_GRANT))],
    checks=[("orchestrator may spawn as tares factory",
             lambda r: "Bash(tares factory spawn *)" in station_json(r, "crew-orchestrator")["permissions"]["allow"])],
    covers=["orchestrator may spawn as tares factory"],
))
