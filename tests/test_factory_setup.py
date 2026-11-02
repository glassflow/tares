"""The question box `tares factory crew up` shows in a terminal (tares/factory_setup.py): the form as
a state machine (keys in, lines out), the settings it asks, the trust check of the crew's folder,
and what `setup` saves on Tares, against a stub of its two routes.

Run: .venv/bin/python tests/test_factory_setup.py
"""
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tares import factory_setup as fs
from tares.factory_setup import DOWN, ENTER, ESC, LEFT, RIGHT, UP, BACKSPACE, Form

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def keys(form, *ks):
    for k in ks:
        form.key(k)
    return form


def text(form, words):
    for ch in words:
        form.key(ch)


ALL = ["autonomy", "releaser", "challenger", "builders_max"]

print("== the box: recommended answers by just pressing Enter ==")
f = Form(fs.crew_questions(ALL))
ck("five tabs: four settings and the grants", [q.tab for q in f.questions]
   == ["Merging", "Releasing", "Codex check", "Builders", "Grants"])
first = f.lines(80, color=False)
ck("the tabs say what is still open", first[0].count("☐") == 5 and "Done" in first[0], first[0])
ck("the question is said in plain words", any("Who merges pull requests?" in l for l in first), first)
ck("the recommended option is marked and preselected",
   any(l.startswith(" ❯ 1. The crew, once its reviewer approves and tests pass   (recommended)") for l in first),
   first)
ck("each option says what it means", any("Every merge waits for your yes" in l for l in first))
ck("the keys are named", "Enter to select · ↑/↓ to move · ←/→ between questions · Esc to cancel" in first[-1],
   first[-1])
keys(f, ENTER, ENTER, ENTER, ENTER, ENTER)
ck("Enter on each takes the recommendation and moves on", f.on_review, f.at)
review = f.lines(80, color=False)
ck("the review lists every answer", any("Merging      The crew, once its reviewer" in l for l in review)
   and any("Grants       Nothing more" in l for l in review), review)
ck("the review saves or cancels", any("❯ 1. Save and start the crew" in l for l in review)
   and any("2. Cancel" in l for l in review))
keys(f, ENTER)
ck("Enter on the review saves", f.result == "save")
ck("the values saved", f.values() == {"autonomy": "L4-ship", "releaser": "no", "challenger": "no",
                                      "builders_max": "2", "grant": ""}, f.values())

print("== moving around ==")
f = Form(fs.crew_questions(ALL))
keys(f, DOWN, ENTER)
ck("↓ then Enter picks the second option", f.value("autonomy") == "L3-review")
keys(f, LEFT)
ck("← goes back to the question answered", f.at == 0 and f.cursor["autonomy"] == 1)
keys(f, UP, ENTER)
ck("and the answer can be changed", f.value("autonomy") == "L4-ship")
keys(f, RIGHT)
ck("→ moves on without answering", f.at == 2 and "releaser" not in f.answers)
keys(f, "2")
ck("a number picks that option and moves on", f.value("challenger") == "yes" and f.at == 3)
keys(f, UP)
ck("↑ wraps round", f.cursor["builders_max"] == 3, f.cursor)
keys(f, ESC)
ck("Esc cancels", f.result == "cancel")

print("== a releaser brings the question of how it ships ==")
f = Form(fs.crew_questions(ALL))
keys(f, ENTER, "2")
ck("Shipping comes right after Releasing", [q.tab for q in f.questions][:3]
   == ["Merging", "Releasing", "Shipping"] and f.questions[f.at].tab == "Shipping")
keys(f, ENTER)
ck("its recommendation is a local kind cluster", f.value("release_profile") == "kind")
keys(f, LEFT, LEFT, "1")
ck("changing back to no releaser takes it away", "release_profile" not in [q.key for q in f.questions]
   and "release_profile" not in f.values(), f.values())

print("== a grant in the person's own words ==")
f = Form(fs.crew_questions(ALL))
keys(f, RIGHT, RIGHT, RIGHT, RIGHT)
keys(f, DOWN, ENTER)
ck("choosing to add one opens a line to type in", f.typing == "")
text(f, "builders may bump patch versionz")
keys(f, BACKSPACE)
text(f, "s")
ck("the line shows what is typed", any("> builders may bump patch versions" in l for l in f.lines(80, False)))
keys(f, ENTER)
ck("Enter keeps it and moves on", f.on_review and f.value("grant") == "builders may bump patch versions",
   f.values())
ck("the review quotes it", any('"builders may bump patch versions"' in l for l in f.lines(80, False)))
f = Form(fs.crew_questions(["autonomy"]))
keys(f, RIGHT, DOWN, ENTER, ENTER)
ck("an empty line takes nothing and stays on the question", f.at == 1 and f.typing is None
   and "grant" not in f.answers)
keys(f, DOWN, ENTER)
text(f, "x")
keys(f, ESC)
ck("Esc while typing goes back to the list, it doesn't cancel", f.result is None and f.typing is None)

print("== the trust box: one question, no review ==")
q = fs.Question("trust", "Crew folder", "Open Claude Code there now to trust it?",
                [fs.Option("yes", "Yes, open it"), fs.Option("no", "Not now")])
f = Form(single=True, questions=[q])
lines = f.lines(80, False)
ck("no tabs for one question", "☐" not in "".join(lines) and "←/→" not in lines[-1], lines)
keys(f, ENTER)
ck("answering it finishes the box", f.result == "save" and f.value("trust") == "yes")

print("== what is asked ==")
ck("nothing set: the four settings", fs.missing_settings({}) == ALL)
ck("all set: nothing", fs.missing_settings({"autonomy": "L4-ship", "releaser": False, "challenger": False,
                                            "builders_max": 2}) == [])
ck("an old L5 level is asked again", fs.missing_settings({"autonomy": "L5-dark", "releaser": False,
                                                          "challenger": False, "builders_max": 2})
   == ["autonomy"])
ck("a releaser without its shipping asks that", fs.missing_settings(
    {"autonomy": "L4-ship", "releaser": True, "challenger": False, "builders_max": 2})
   == ["release_profile"])

print("== the crew's folder: trusted as Claude Code records it ==")
tmp = Path(tempfile.mkdtemp(prefix="tares-setup-"))
home = tmp / "factory"
home.mkdir()
old_home = os.environ.get("HOME")
os.environ["HOME"] = str(tmp)
try:
    ck("no record: not trusted", not fs.home_trusted(home))
    (tmp / ".claude.json").write_text(json.dumps({"projects": {str(home.resolve()): {
        "hasTrustDialogAccepted": True}}}))
    ck("recorded under its real path: trusted", fs.home_trusted(home))

    print("== setup saves the answers on Tares ==")
    seen = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _reply(self, body):
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            seen.append(("GET", self.path, None, self.headers.get("Authorization")))
            self._reply({"autonomy": None, "releaser": None, "challenger": None, "builders_max": None})

        def do_PUT(self):
            n = int(self.headers.get("Content-Length") or 0)
            seen.append((self.command, self.path, json.loads(self.rfile.read(n)), self.headers.get("Authorization")))
            self._reply({})

        do_POST = do_PUT

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}"

    def scripted(*ks):
        def run(form):
            for k in ks:
                if form.result:
                    break
                form.key(k)
            return form
        return run

    real = fs.run_form
    try:
        fs.run_form = scripted(ENTER, "2", ENTER, ENTER, "3", DOWN, ENTER, *"keep it small", ENTER, ENTER)
        ok = fs.setup(url, "tok", home, "claude")
        puts = [s for s in seen if s[0] == "PUT"]
        posts = [s for s in seen if s[0] == "POST"]
        ck("setup goes on after saving", ok is True)
        ck("the settings are saved as the factory reads them", puts and puts[0][2] == {
            "autonomy": "L4-ship", "releaser": True, "challenger": False, "builders_max": 3,
            "release_profile": "kind", "prod_pattern": fs.PROD_PATTERN}, puts)
        ck("the grant is saved in the person's words, for every project",
           posts and posts[0][2] == {"words": "keep it small", "scope": "all"}, posts)
        ck("with the token", all(s[3] == "Bearer tok" for s in seen), seen)
        seen.clear()
        fs.run_form = scripted(ESC)
        ck("a cancelled box saves nothing and stops", fs.setup(url, "", home, "claude") is False
           and not [s for s in seen if s[0] != "GET"], seen)
        ck("no Tares to ask: setup leaves it to the factory CLI to say so",
           fs.setup("http://127.0.0.1:9", "", home, "claude") is True)
    finally:
        fs.run_form = real
        srv.shutdown()
finally:
    if old_home is not None:
        os.environ["HOME"] = old_home

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
