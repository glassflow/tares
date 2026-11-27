"""Two watches at once tell of each failed turn once (tares/crew/codex_round.py failed_turns, TR-1600).

Two processes, each a watch's Codex round, work at the same moment on two different stations whose last turns
failed. Each holds only its own station's lock, so what they share is the crew's files. They are made to meet
at the worst moment: both start the round together, and anything that reads and then writes stations.json
waits after its read until the other has read too, so each would write back what the other had not yet written.
Then:

 a. both stations' marks are there: each failed turn is kept as told of;
 b. each was told of once: one turn-failed event and one desk item per station;
 c. the next round, with both stations looked at, tells of nothing again: no event and no desk item more.

Run: python tests/test_codex_round_marks.py
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE / "factory"))
for _k in [k for k in os.environ if k.startswith(("TARES", "FACTORY"))]:
    del os.environ[_k]

from tares.crew import codex_round  # noqa: E402
from tares.crew import codex_station as cs  # noqa: E402
from tares.crew import state  # noqa: E402
from tares.crew.paths import CrewPaths  # noqa: E402
import launch_support as LS  # noqa: E402

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


TMP = Path(tempfile.mkdtemp(prefix="tcrm-"))
S1, S2 = "shop-build-api", "shop-build-web"
LS.make_crew(TMP, {"stations": {s: {"role": "builder", "runtime": "codex", "status": "active"} for s in (S1, S2)}})
paths = CrewPaths.at(TMP / "crew")
DESK = TMP / "desk.jsonl"     # the desk items both watches posted, one line each
MEET = TMP / "meet"
MEET.mkdir()

# One watch's round, in a process of its own: it sees only its station as a running one whose last turn failed.
WATCH = r'''
import json, os, sys, time
from pathlib import Path
root, home, only, desk, meet = sys.argv[1:6]
sys.path.insert(0, root)
from tares.crew import codex_round, state
from tares.crew import codex_station as cs
from tares.crew.paths import CrewPaths

def met(what, timeout=5.0):
    """Arrive at `what` and wait, a while at most, for the other watch to arrive there too."""
    Path(meet, f"{what}.{only}").touch()
    end = time.time() + timeout
    while len(list(Path(meet).glob(f"{what}.*"))) < 2 and time.time() < end:
        time.sleep(0.01)

cs.is_alive = lambda paths, station: station == only
cs.failed_turn = lambda paths, station: (f"turn-of-{station}", "the model is not supported")

real_load = state.stations_load
def put(station, values, paths=None):
    """stations_put as it is, with the read and the write apart: the other watch reads in between."""
    data = real_load(paths)
    met("read")
    data[station] = {**(data.get(station) or {}), **values}
    state.write_json(paths.stations, data)
state.stations_put = put

class Client:
    def tares_http(self, method, path, body):
        with open(desk, "a", encoding="utf-8") as f:
            f.write(json.dumps({"path": path, "station": body.get("station")}) + "\n")

met("start")
told = codex_round.failed_turns(Client(), CrewPaths.at(home))
print(json.dumps(told))
'''

watches = [subprocess.Popen([sys.executable, "-c", WATCH, str(ROOT), str(paths.home), s, str(DESK), str(MEET)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for s in (S1, S2)]
outs = [w.communicate(timeout=60) for w in watches]
told = [json.loads(o.strip().splitlines()[-1]) if o.strip() else None for o, _ in outs]


def marked(station):
    """The failed turn kept as told of for the station, wherever this version of the crew keeps it."""
    f = paths.station_file(station, "codex.failed-told")
    if f.is_file():
        return f.read_text(encoding="utf-8").strip()
    return (state.stations_load(paths).get(station) or {}).get(codex_round.FAILED_TOLD)


def events():
    return [json.loads(x) for x in paths.events.read_text(encoding="utf-8").splitlines()
            if x.strip() and json.loads(x).get("event") == "turn-failed"] if paths.events.is_file() else []


def desk():
    return [json.loads(x) for x in DESK.read_text(encoding="utf-8").splitlines() if x.strip()] if DESK.is_file() else []


print("a. two watches, two stations, at the same moment")
ck("each watch told of its own station", told == [[S1], [S2]], (told, [e for _, e in outs]))
ck("both stations' marks are kept", marked(S1) == f"turn-of-{S1}" and marked(S2) == f"turn-of-{S2}",
   (marked(S1), marked(S2), state.stations_load(paths)))
print("b. once each")
ck("one turn-failed event per station", sorted(e["station"] for e in events()) == [S1, S2], events())
ck("one desk item per station", sorted(d["station"] for d in desk()) == [S1, S2], desk())

print("c. the next round tells of nothing again")


class Client:
    def tares_http(self, method, path, body):
        with open(DESK, "a", encoding="utf-8") as f:
            f.write(json.dumps({"path": path, "station": body.get("station")}) + "\n")


cs.is_alive = lambda p, station: station in (S1, S2)
cs.failed_turn = lambda p, station: (f"turn-of-{station}", "the model is not supported")
again = codex_round.failed_turns(Client(), paths)
ck("no station is told of twice", again == [] and len(events()) == 2 and len(desk()) == 2, (again, events(), desk()))

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
