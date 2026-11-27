"""What Codex's approval reviewer decided for a station is recorded once (tares/crew/codex_reviews.py, TR-1599).

The station's Codex folder holds conversation files shaped as Codex 0.171.0 writes them (measured by hand: the
reviewer's own conversation names the station's as its parent, its turn is started by `guardian_review`, the
planned action follows "Planned action JSON:" in its prompt and its last answer is the decision):

 a. a review that allowed and one that refused are each recorded once, as an event and a line, with the
    command as the station wrote it (without the guard's closed standard input), the risk and the reason;
 b. the station's own conversation, a helper agent's conversation and a file that is not JSON are not reviews,
    and nothing is recorded for them; a review still running is left, and recorded once it has its decision;
    one that ended without a decision is recorded as that;
 c. a second look, and a watch started again, record nothing twice: every name whose file is there stays kept,
    and one whose file is gone is dropped; a station on Claude Code is not looked at;
 d. the watch's Codex round runs this part after recording;
 f. a station's reviews are read and kept under its lock, so two watches at once record a review once.

Run: python tests/test_codex_reviews.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE / "factory"))
for _k in [k for k in os.environ if k.startswith(("TARES", "FACTORY"))]:
    del os.environ[_k]

from tares.crew import codex as cx  # noqa: E402
from tares.crew import codex_reviews as rv  # noqa: E402
from tares.crew import codex_round  # noqa: E402
from tares.crew.paths import CrewPaths  # noqa: E402
import launch_support as LS  # noqa: E402

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


TMP = Path(tempfile.mkdtemp(prefix="tcrv-"))
LS.make_crew(TMP, {"stations": {
    "shop-build-api": {"role": "builder", "runtime": "codex", "status": "active"},
    "shop-reviewer": {"role": "reviewer", "runtime": "claude_code", "status": "active"}}})
paths = CrewPaths.at(TMP / "crew")
MAIN = "01a2-main"


def write(station: str, name: str, lines: list) -> Path:
    d = cx.station_home(paths, station) / "sessions" / "2026" / "11" / "26"
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    return f


def meta(tid, parent=None):
    p = {"id": tid, "timestamp": "2026-11-26T14:13:10.049Z", "cwd": "/work/alpha"}
    if parent:
        p["parent_thread_id"] = parent
    return {"type": "session_meta", "payload": p}


def started(trigger):
    return {"type": "event_msg", "payload": {"type": "task_started", "turn_attribution": {"turn_trigger": trigger}}}


def user(*texts):
    return {"type": "response_item", "payload": {"type": "message", "role": "user",
                                                 "content": [{"type": "input_text", "text": t} for t in texts]}}


def answer(text):
    return {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                                 "content": [{"type": "output_text", "text": text}]}}


def action(script):
    return json.dumps({"command": ["/bin/zsh", "-lc", "exec </dev/null\n" + script], "cwd": "/work/alpha",
                       "justification": "push the branch", "sandbox_permissions": "require_escalated",
                       "tool": "exec_command"}, indent=2) + "\n"


def review(tid, script, outcome=None, why=""):
    lines = [meta(tid, MAIN), started("guardian_review"),
             user("The following is the Codex agent history whose request action you are assessing."),
             user("Assess the exact planned action below.", "Planned action JSON:\n", action(script),
                  ">>> APPROVAL REQUEST END\n")]
    if outcome:
        lines.append(answer(json.dumps({"risk_level": "low", "user_authorization": "high", "outcome": outcome,
                                        "rationale": why})))
    return lines


def events():
    try:
        return [json.loads(x) for x in paths.events.read_text(encoding="utf-8").splitlines() if x.strip()]
    except OSError:
        return []


said = []
S = "shop-build-api"

print("a. decisions are recorded once each")
write(S, "rollout-2026-11-26T15-00-00-main.jsonl", [meta(MAIN), started(None), user("work on T1"), answer("done")])
write(S, "rollout-2026-11-26T15-01-00-r1.jsonl",
      review("r1", "git push origin factory/t1", "allow", "Pushing factory/* branches is in the station's rules."))
write(S, "rollout-2026-11-26T15-02-00-r2.jsonl",
      review("r2", "touch /etc/x", "deny", "The policy forbids writes outside the workspace."))
got = rv.step(None, paths, said.append)
evs = [e for e in events() if e["event"] == "codex-review"]
ck("two reviews, oldest first, each with its command, outcome, risk and reason",
   [(s, r["outcome"], r["command"], r["risk"]) for s, r in got]
   == [(S, "allow", "git push origin factory/t1", "low"), (S, "deny", "touch /etc/x", "low")]
   and got[0][1]["why"].startswith("Pushing factory/*") and got[0][1]["parent"] == MAIN, got)
ck("an event and a line for each, saying allowed or refused and by its rules",
   len(evs) == 2 and all(e["station"] == S for e in evs)
   and evs[0]["detail"].startswith(f"Codex's reviewer allowed for {S}, by its rules: git push origin factory/t1 (risk low)")
   and evs[1]["detail"].startswith(f"Codex's reviewer refused for {S}, by its rules: touch /etc/x")
   and said == [e["detail"] for e in evs], (evs, said))

print("b. what is not a review, and a review still running")
write(S, "rollout-2026-11-26T15-03-00-helper.jsonl", [meta("h1", MAIN), started("spawn_agent"), user("look"), answer("ok")])
(cx.station_home(paths, S) / "sessions" / "2026" / "11" / "26" / "rollout-2026-11-26T15-04-00-junk.jsonl").write_text("not json\n")
pending = write(S, "rollout-2026-11-26T15-05-00-r3.jsonl", review("r3", "git fetch origin"))
said.clear()
got = rv.step(None, paths, said.append)
ck("nothing is recorded for the station's own conversation, a helper's, a broken file or a running review",
   got == [] and said == [] and len([e for e in events() if e["event"] == "codex-review"]) == 2, (got, said))
kept = json.loads(paths.station_file(S, rv.KEPT).read_text())
ck("the helper's conversation is kept as looked at; the running review is not",
   "rollout-2026-11-26T15-03-00-helper.jsonl#-" in kept and not [k for k in kept if k.startswith("rollout-2026-11-26T15-05-00-r3")]
   and not [k for k in kept if k.startswith("rollout-2026-11-26T15-00-00-main")], kept)
pending.write_text("".join(json.dumps(x) + "\n" for x in review("r3", "git fetch origin", "allow", "a fetch")),
                   encoding="utf-8")
got = rv.step(None, paths, said.append)
ck("once it has its decision, it is recorded", [r["command"] for _, r in got] == ["git fetch origin"], got)
timed = write(S, "rollout-2026-11-26T15-05-30-r4.jsonl", review("r4", "curl https://example.invalid"))
ck("one still running is not recorded", rv.step(None, paths, None) == [])
timed.write_text("".join(json.dumps(x) + "\n" for x in [*review("r4", "curl https://example.invalid"),
                                                         {"type": "event_msg", "payload": {"type": "task_complete"}}]),
                 encoding="utf-8")
said.clear()
got = rv.step(None, paths, said.append)
ck("one that ended without a decision is recorded as that, and as nothing allowed",
   [(r["outcome"], r["command"]) for _, r in got] == [("none", "curl https://example.invalid")]
   and said == [f"Codex's reviewer ended without a decision for {S}, so it was not allowed: curl https://example.invalid"],
   (got, said))

print("c. nothing twice; Claude Code stations are not looked at")
n = len(events())
ck("a second look records nothing", rv.step(None, paths, said.append) == [] and len(events()) == n)
gone = cx.station_home(paths, S) / "sessions" / "2026" / "11" / "26" / "rollout-2026-11-26T15-02-00-r2.jsonl"
gone.unlink()
write(S, "rollout-2026-11-26T15-07-00-r5.jsonl", review("r5", "git push origin factory/t2", "allow", "ok"))
rv.new(paths, S)
kept = json.loads(paths.station_file(S, rv.KEPT).read_text())
ck("every name whose file is there stays kept, however many; one whose file is gone is dropped",
   not [k for k in kept if k.startswith("rollout-2026-11-26T15-02-00-r2")] and len(kept) == 5
   and {k.split("#")[0] for k in kept} >= {"rollout-2026-11-26T15-01-00-r1.jsonl", "rollout-2026-11-26T15-07-00-r5.jsonl"},
   kept)
paths.station_file(S, rv.KEPT).unlink()
again = rv.new(paths, S)
ck("without its record a watch started again would record them again: the record is what keeps them once",
   len(again) == 4, again)
write("shop-reviewer", "rollout-2026-11-26T15-06-00-r9.jsonl", review("r9", "gh pr merge 1", "deny", "no"))
ck("a station on Claude Code is not looked at",
   all(s != "shop-reviewer" for s, _ in rv.step(None, paths, None)))

print("e. Codex goes on in the same review conversation for later requests: one file, several reviews")
two = review("r6", "git -C /w fetch origin", "allow", "a fetch")
two[1]["payload"]["turn_id"] = "t-1"
more = [{"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "t-1"}},
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "t-2",
                                          "turn_attribution": {"turn_trigger": "guardian_review"}}},
        user("Continue the same review conversation.", "Planned action JSON:\n", action("git add x && git commit -m y"))]
f6 = write(S, "rollout-2026-11-26T15-08-00-r6.jsonl", [*two, *more])
got = rv.new(paths, S)
ck("the first decision is recorded while the second is still running", [r["command"] for r in got] == ["git -C /w fetch origin"], got)
f6.write_text("".join(json.dumps(x) + "\n" for x in [*two, *more, answer(json.dumps({
    "risk_level": "medium", "user_authorization": "low", "outcome": "deny",
    "rationale": "It runs in the reports folder, not the worktree."}))]), encoding="utf-8")
got = rv.new(paths, S)
ck("the second decision in the same file is recorded too, once, and the first is not again",
   [(r["outcome"], r["command"], r["turn"]) for r in got] == [("deny", "git add x && git commit -m y", "t-2")]
   and rv.new(paths, S) == [], got)

print("f. two watches at once: a station's reviews are read and kept under its lock")
import subprocess  # noqa: E402
write(S, "rollout-2026-11-26T15-09-00-r7.jsonl", review("r7", "git push origin factory/t3", "allow", "ok"))
holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
lock = paths.lock_dir(S)
lock.mkdir(parents=True, exist_ok=True)
(lock / "pid").write_text(f"{holder.pid}\n")
skipped = rv.step(None, paths, None)
holder.kill()
holder.wait()
(lock / "pid").unlink()
lock.rmdir()
got = rv.step(None, paths, None)
ck("while another command holds the station's lock nothing of it is recorded; once free, the review is, once (TR-1600)",
   [s_ for s_, _ in skipped] == [] and [r["command"] for _, r in got] == ["git push origin factory/t3"]
   and rv.step(None, paths, None) == [], (skipped, got))

print("d. the watch's Codex round")
names = [n for n, _ in codex_round.PARTS]
ck("the reviews part runs right after recording, before the desk's",
   names[:3] == ["record", "reviews", "asks"] and dict(codex_round.PARTS)["reviews"] is rv.step, names)

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
