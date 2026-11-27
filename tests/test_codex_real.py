"""The real Codex runs, by hand (TR-1600): every probe in tests/factory/codex_probe/ against a signed-in Codex.

Not in CI (tests/ci_skip.txt): it needs a Codex login, starts real Codex sessions and spends a little of that
account's quota. CI runs the same behaviours without a login, against the fake app-server
(tests/factory/fake_codex.py) and the recorded wire (tests/factory/codex_probe/fixtures/), in:

  launch, the person's setup kept out     test_runtime_codex, test_crew_launch_codex
  turns, prompts, stop, revive, asks      test_crew_codex_station
  the guard and the proof that it ran     test_codex_guard, test_crew_codex_station
  MCP as the station                      test_crew_codex_station, test_crew_launch_codex
  recording, claims, cut-off commands     test_codex_record, test_factory_review
  pacing from the account's limits        test_crew_codex_pace, test_codex_usage
  messages between stations               test_crew_codex_mail
  the reviewer's decisions                test_codex_reviews
  planning and the second look            test_crew_codex_planning

    TARES_PROBE_CODEX=/path/to/codex .venv/bin/python tests/test_codex_real.py [--out DIR] [probe ...]

Each probe (see tests/factory/codex_probe/README.md) runs in turn with its own --out folder, and one line says
whether it passed; the exit code is 1 when one did not. An unknown probe or option, or an --out that is not a
folder, exits 2 before anything runs (tests/test_codex_real_runner.py checks that offline). A probe depends
on the model doing the steps it is asked for: one that fails says why in its results file, and is worth a
second run before it is believed. Run it with the checkout's interpreter and its folder first on PATH, so a
station's `tares` is this checkout's. Without TARES_PROBE_CODEX it says how to run it and fails.

probe_asks (TR-1593) is not run: it waits for a station to stop on an approval its sandbox needs, and since
TR-1599 a station is started with Codex's own reviewer, which decides such a request by the station's rules
(probe_review runs that). The desk still takes a station's questions in words, which test_crew_codex_station
covers. Run it by itself to look at the desk path of a station started before TR-1599.

The acceptance runs of the whole crew (a plan to a merge on a disposable cell and repository) are by hand too;
their results are on TR-1598, TR-1599 and TR-1600.
"""
import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROBES = HERE / "factory" / "codex_probe"
# (probe, what it runs against the real Codex); probe_evidence runs offline and is listed for completeness
ORDER = [
    ("probe_launch", "TR-1592: the crew's own start: the socket, a stale one, the person's setup kept out, an older Codex"),
    ("probe_station", "TR-1593: turns with nobody connected, a refused prompt, a stop and a revive in the same conversation"),
    ("probe_guard_on", "TR-1594: the guard's record at the start of a turn and for each call; a station without it stopped"),
    ("probe_guard", "TR-1576: halt mid-turn, wrong role, heavy commands, MCP tools, helpers, typed input, broken hooks"),
    ("probe_transport", "TR-1576: socket against stdio, a controller lost, two controllers, a crash, crew down"),
    ("probe_mcp_station", "TR-1596: Tares over MCP as the station, a wrong key, no key, the endpoint down"),
    ("probe_record", "TR-1595: recording by visits on a cell, claims matched against real commands"),
    ("probe_pace_station", "TR-1597: the account's own limits through the pace step and a cell"),
    ("probe_review", "TR-1599: Codex's reviewer with a station's rules, a grant through a restart, halt first"),
    ("probe_evidence", "TR-1576: Codex command records through the crew's match_check (offline)"),
]


def out_dir(text: str) -> Path:
    """--out: a folder, there or to be made; never empty, never a file."""
    if not text.strip():
        raise argparse.ArgumentTypeError("--out needs a folder")
    path = Path(text)
    if path.exists() and not path.is_dir():
        raise argparse.ArgumentTypeError(f"--out {text} is not a folder")
    return path


def parser() -> argparse.ArgumentParser:
    names = [n for n, _ in ORDER]
    p = argparse.ArgumentParser(prog="tests/test_codex_real.py", allow_abbrev=False,
                                description="Run the Codex probes against a signed-in Codex (TARES_PROBE_CODEX).")
    p.add_argument("--out", type=out_dir, help="the folder for each probe's results and log (default: a new "
                                               "temporary folder)")
    p.add_argument("probes", nargs="*", metavar="probe",
                   help="the probes to run, all when none is named: " + ", ".join(names))
    return p


def main(argv=None, run=subprocess.run, environ=None) -> int:
    """The selection is checked whole before any probe runs: an unknown name or option, or an --out that is
    not a folder, ends it with argparse's message and exit code 2, and nothing runs."""
    p = parser()
    args = p.parse_intermixed_args(sys.argv[1:] if argv is None else argv)   # names before or after --out
    unknown = [n for n in args.probes if n not in dict(ORDER)]
    if unknown:   # checked here, not by choices: an empty list against choices fails on some Pythons
        p.error(f"no such probe: {', '.join(unknown)} (the probes: {', '.join(n for n, _ in ORDER)})")
    environ = os.environ if environ is None else environ
    todo = [(n, what) for n, what in ORDER if not args.probes or n in args.probes]
    if not todo:   # never a pass for a run of nothing
        print("no probe was picked")
        return 2
    if not environ.get("TARES_PROBE_CODEX"):
        print("needs a signed-in Codex: TARES_PROBE_CODEX=/path/to/codex .venv/bin/python tests/test_codex_real.py\n"
              "(not in CI: tests/ci_skip.txt says why)")
        return 1
    out = args.out or Path(tempfile.mkdtemp(prefix="codex-real-"))
    out.mkdir(parents=True, exist_ok=True)
    env = {**environ, "PATH": os.pathsep.join([os.path.dirname(sys.executable), environ.get("PATH", "")])}
    env.pop("ANTHROPIC_BASE_URL", None)
    failed = []
    for name, what in todo:
        t = time.time()
        done = run([sys.executable, str(PROBES / f"{name}.py"), "--out", str(out / name)], env=env,
                   capture_output=True, text=True)
        (out / f"{name}.log").write_text((done.stdout or "") + (done.stderr or ""), encoding="utf-8")
        ok = done.returncode == 0
        failed += [] if ok else [name]
        print(f"  {'ok  ' if ok else 'FAIL'} {name} ({time.time() - t:.0f} s): {what}", flush=True)
    print(f"\n{len(todo) - len(failed)} passed, {len(failed)} failed; results and logs in {out}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
