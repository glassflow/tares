"""tares.crew.regrant: the pieces of the watch's regrant step that need no running station (TR-960).

The whole step, against bash, is in the parity scenarios `watch_regrant_*` (tests/factory/parity.py). This script
checks what they cannot say alone: the fingerprint is the cksum bash uses, a change already acted on or an
unreadable Tares is never a change, the prompt quotes only the added grants, and the step is registered with
the watch under the name the skeleton reserves for it.

Run: python tests/test_crew_regrant.py
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tares.crew import regrant, watch  # noqa: E402
from tares.crew.client import grants_sum  # noqa: E402
from tares.crew.errors import CrewError  # noqa: E402
from tares.crew.paths import CrewPaths  # noqa: E402

P = F = 0


def check(label, ok):
    global P, F
    if ok:
        P += 1
        print(f"  ok   {label}")
    else:
        F += 1
        print(f"  FAIL {label}")


class Client:
    """The part of TaresClient the fingerprint reads: the grants text, or an error."""

    def __init__(self, text=None):
        self.text = text

    def grants_read(self, project=""):
        if self.text is None:
            raise CrewError("no Tares")
        return self.text


def crew(rows):
    tmp = Path(tempfile.mkdtemp(prefix="regrant-"))
    (tmp / "stations.json").write_text(json.dumps(rows), encoding="utf-8")
    return CrewPaths.at(tmp)


A = '- "may label pull requests" (2026-02-03)\n'
B = '- "may merge in shop" (2026-02-04)\n'

# the fingerprint
check("the fingerprint is the cksum of the grants and a newline",
      regrant.grants_now(crew({"s": {}}), Client(A), "s") == str(grants_sum(A)))
check("a grants text with extra trailing newlines gives the same fingerprint",
      regrant.grants_now(crew({"s": {}}), Client(A + "\n\n"), "s") == str(grants_sum(A)))
check("a Tares that does not answer gives no fingerprint", regrant.grants_now(crew({"s": {}}), Client(None), "s") is None)

# what counts as a change
was = str(grants_sum(A))
paths = crew({"s": {"grants": was}})
check("unchanged grants are no change", not regrant.regranted(paths, Client(A), "s"))
check("grants that differ are a change", regrant.regranted(paths, Client(A + B), "s"))
check("a Tares that does not answer is no change", not regrant.regranted(paths, Client(None), "s"))
paths = crew({"s": {"grants": was, "grants_tried": str(grants_sum(A + B))}})
check("a change already acted on is not acted on again", not regrant.regranted(paths, Client(A + B), "s"))
check("a different change after it is", regrant.regranted(paths, Client(B), "s"))
paths = crew({"s": {}})
check("a station started before grants were tracked is never regranted", not regrant.regranted(paths, Client(A + B), "s"))

# when a restart is due
paths = crew({"s": {"grants": was, "sessionId": "abc"}})
check("a station with a session and new grants is due", regrant.regrant_due(paths, Client(A + B), "s"))
paths = crew({"s": {"grants": was}})
check("no session to resume: not due", not regrant.regrant_due(paths, Client(A + B), "s"))
paths = crew({"s": {"grants": was, "sessionId": "abc"}})
(paths.home / "stations").mkdir()
paths.done_file("s").write_text("", encoding="utf-8")
check("a station that is done is not due", not regrant.regrant_due(paths, Client(A + B), "s"))

# what the prompt quotes
check("added grants are the lines not already whole lines of the kept file", regrant.added_grants(A + B, A) == B.rstrip("\n"))
check("a kept line that only contains a new line does not hide it",
      regrant.added_grants("- a b\n- a\n", "- a b\n") == "- a")
check("without a kept file every grant is added", regrant.added_grants(A + B, None) == A + B)
check("a kept file with no final newline still counts its last line", regrant.added_grants(A + B, A.rstrip("\n")) == B.rstrip("\n"))
check("nothing added gives nothing", regrant.added_grants(A, A) == "")
p = regrant._prompt("Kiran", "- x", "read the rules")
check("the prompt with rules quotes them and says to run the refused command again",
      "Kiran allowed new permissions in Tares" in p and "\n- x\n" in p and "run it again now" in p)
p = regrant._prompt("Kiran", "", "read the rules")
check("the prompt without rules says what changed and points at read_grants",
      "changed what the crew is allowed in Tares" in p and "read_grants shows them" in p)

# registered with the skeleton
check("the watch runs this step under the name the skeleton reserves", watch._STEPS.get("regrant") is regrant.step)
check("that name is one of the skeleton's", "regrant" in watch.STEP_NAMES)

print(f"{P} passed, {F} failed")
sys.exit(1 if F else 0)
