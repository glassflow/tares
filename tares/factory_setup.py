"""The questions `tares factory crew up` asks the first time, in a box like Claude Code's: tabs across
the top, one question at a time, ↑/↓ through the options, ←/→ between questions, a last tab that
shows every answer before anything is saved. Then the trust of the crew's own folder, in the same
box. Only in a real terminal; scripts and `--yes` get the plain prompts of the factory CLI.

`Form` is the state of the box and draws itself as lines, so it is tested without a terminal;
`run_form` puts it on the screen and feeds it keys."""
from __future__ import annotations

import json
import os
import select
import shutil
import subprocess
import sys
import termios
import textwrap
import tty
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

PROD_PATTERN = "(^|[-_.])(prod|production)([-_.]|$)"


@dataclass
class Option:
    value: str
    label: str
    hint: str = ""
    text: bool = False        # choosing it opens a line to type in (a grant in the person's words)


@dataclass
class Question:
    key: str
    tab: str
    ask: str
    options: list[Option]
    default: int = 0          # the recommended option, preselected
    note: str = ""            # a line of why, under the question


def crew_questions(missing: list[str]) -> list[Question]:
    """The questions for the settings the crew does not have yet, in the order the box shows them."""
    qs = {
        "autonomy": Question("autonomy", "Merging", "Who merges pull requests?", [
            Option("L4-ship", "The crew, once its reviewer approves and tests pass",
                   "Builders merge on their own; you answer only what the crew can't decide."),
            Option("L3-review", "You, each one",
                   "Every merge waits for your yes in the orchestrator's session."),
        ]),
        "releaser": Question("releaser", "Releasing", "Should the crew release and deploy finished work?", [
            Option("no", "No, it stops when work is merged to main",
                   "You release yourself, the way you do today."),
            Option("yes", "Yes, a releaser tags and deploys each finished milestone",
                   "Production always waits for you."),
        ]),
        "challenger": Question("challenger", "Codex check",
                               "Should a second AI (Codex) check every commit before review?", [
            Option("no", "No", "The crew's reviewer checks every pull request either way."),
            Option("yes", "Yes", "Needs the Codex CLI installed and signed in."),
        ]),
        "builders_max": Question("builders_max", "Builders",
                                 "How many builders may work at the same time, across all projects?", [
            Option("2", "2", "Each builder is a Claude Code session working on one repo."),
            Option("1", "1", "One thing at a time."),
            Option("3", "3", ""),
            Option("4", "4", "Uses the most of your plan's limits."),
        ]),
    }
    out = [qs[k] for k in ("autonomy", "releaser", "challenger", "builders_max") if k in missing]
    out.append(Question("grant", "Grants", "Anything you always allow the crew, in your own words?", [
        Option("", "Nothing more", "Your answers above already cover routine work."),
        Option("", "Add one in my own words",
               "For example: builders may bump patch versions of dependencies.", text=True),
    ]))
    return out


SHIPPING = Question("release_profile", "Shipping", "How does the releaser ship?", [
    Option("kind", "To a local kind cluster first", "Rehearses every release on your machine."),
    Option("github-release", "Tags and GitHub releases only", ""),
    Option("custom", "Each project's own steps", "From the project's AGENTS.md."),
])

# key codes the box understands
UP, DOWN, LEFT, RIGHT, ENTER, ESC, BACKSPACE, TAB = ("up", "down", "left", "right", "enter",
                                                     "esc", "backspace", "tab")


@dataclass
class Form:
    questions: list[Question]
    done_label: str = "Save and start the crew"
    single: bool = False                          # one question: answering it finishes the box
    at: int = 0                                   # the tab shown; len(questions) is the review tab
    cursor: dict = field(default_factory=dict)    # question key → highlighted option
    answers: dict = field(default_factory=dict)   # question key → index chosen
    typing: str | None = None                     # the line being typed for a text option
    texts: dict = field(default_factory=dict)     # question key → words typed
    result: str | None = None                     # "save" or "cancel" once finished

    def __post_init__(self):
        for q in self.questions:
            self.cursor.setdefault(q.key, q.default)

    # ── state ────────────────────────────────────────────────────────────────
    @property
    def on_review(self) -> bool:
        return self.at >= len(self.questions)

    def value(self, key: str) -> str | None:
        q = next(x for x in self.questions if x.key == key)
        i = self.answers.get(key, q.default)
        o = q.options[i]
        return self.texts.get(key, "") if o.text else o.value

    def values(self) -> dict:
        return {q.key: self.value(q.key) for q in self.questions}

    def _add_shipping(self):
        """A releaser asked for brings the question of how it ships, right after it."""
        has = any(q.key == "release_profile" for q in self.questions)
        want = self.value("releaser") == "yes" if any(q.key == "releaser" for q in self.questions) else False
        if want and not has:
            i = next(n for n, q in enumerate(self.questions) if q.key == "releaser")
            self.questions.insert(i + 1, SHIPPING)
            self.cursor.setdefault(SHIPPING.key, SHIPPING.default)
        elif not want and has:
            self.questions = [q for q in self.questions if q.key != "release_profile"]
            self.answers.pop("release_profile", None)

    def key(self, k: str) -> None:
        """One key press."""
        if self.result:
            return
        if self.typing is not None:
            q = self.questions[self.at]
            if k == ENTER:
                words = " ".join(self.typing.split())
                self.typing = None
                if words:
                    self.texts[q.key] = words
                    self.answers[q.key] = self.cursor[q.key]
                    self._next()
                else:   # nothing typed: back to the list, nothing chosen
                    self.cursor[q.key] = q.default
            elif k == ESC:
                self.typing = None
            elif k == BACKSPACE:
                self.typing = self.typing[:-1]
            elif len(k) == 1 and k.isprintable():
                self.typing = (self.typing + k)[:300]
            return
        if k == ESC:
            self.result = "cancel"
            return
        if k in (LEFT,):
            self.at = max(0, self.at - 1)
            return
        if k in (RIGHT, TAB):
            self.at = min(len(self.questions), self.at + 1)
            return
        if self.on_review:
            c = self.cursor.get("__done", 0)
            if k in (UP, DOWN):
                self.cursor["__done"] = 1 - c
            elif k in ("1", "2"):
                self.cursor["__done"] = int(k) - 1
            elif k == ENTER:
                if c == 0:
                    self.result = "save"
                else:
                    self.result = "cancel"
            return
        q = self.questions[self.at]
        c = self.cursor[q.key]
        if k == UP:
            self.cursor[q.key] = (c - 1) % len(q.options)
        elif k == DOWN:
            self.cursor[q.key] = (c + 1) % len(q.options)
        elif k.isdigit() and 1 <= int(k) <= len(q.options):
            self.cursor[q.key] = int(k) - 1
            self.key(ENTER)
        elif k == ENTER:
            o = q.options[self.cursor[q.key]]
            if o.text:
                self.typing = self.texts.get(q.key, "")
                return
            self.answers[q.key] = self.cursor[q.key]
            self.texts.pop(q.key, None)
            if q.key == "releaser":
                self._add_shipping()
            self._next()

    def _next(self) -> None:
        if self.single:
            self.result = "save"
        else:
            self.at = min(len(self.questions), self.at + 1)

    def shown(self, q: Question) -> str:
        """The answer to one question, as the review and the summary say it."""
        o = q.options[self.answers.get(q.key, q.default)]
        if o.text:
            return f'"{self.texts[q.key]}"' if q.key in self.texts else "Nothing more"
        return o.label

    # ── drawing ──────────────────────────────────────────────────────────────
    def lines(self, width: int = 80, color: bool = True) -> list[str]:
        B, D, C, R, INV = (("\x1b[1m", "\x1b[2m", "\x1b[36m", "\x1b[0m", "\x1b[7m") if color
                           else ("", "", "", "", ""))
        width = max(40, min(width, 100))
        tabs = []
        for i, q in enumerate(self.questions):
            mark = "✔" if q.key in self.answers else "☐"
            t = f" {mark} {q.tab} "
            tabs.append(f"{INV}{t}{R}" if i == self.at else t)
        done = " ✔ Done " if self.on_review else " Done "
        if not self.single:
            tabs.append(f"{INV}{done}{R}" if self.on_review else done)
        out = ([" ←" + "".join(tabs) + "→", ""] if not self.single else [])
        if self.on_review:
            out.append(f" {B}Your answers{R}")
            out.append("")
            for q in self.questions:
                out.append(f"   {q.tab:<12} {self.shown(q)}")
            out.append("")
            c = self.cursor.get("__done", 0)
            for i, label in enumerate((self.done_label, "Cancel")):
                pointer = f"{C}❯{R}" if i == c else " "
                out.append(f" {pointer} {i + 1}. {(C + label + R) if i == c else label}")
            out += ["", f" {D}Enter to choose · ←/→ to change an answer · Esc to cancel{R}"]
            return out
        q = self.questions[self.at]
        out += [f" {B}{line}{R}" for line in textwrap.wrap(q.ask, width - 2)]
        out += [f" {D}{line}{R}" for line in textwrap.wrap(q.note, width - 2)] if q.note else []
        out.append("")
        for i, o in enumerate(q.options):
            sel = i == self.cursor[q.key]
            pointer = f"{C}❯{R}" if sel else " "
            label = o.label + ("   (recommended)" if i == q.default and not o.text else "")
            out.append(f" {pointer} {i + 1}. {(C + label + R) if sel else label}")
            for h in textwrap.wrap(o.hint, width - 7) if o.hint else []:
                out.append(f"      {D}{h}{R}")
        if self.typing is not None:
            out += ["", f"   > {self.typing}{C}▏{R}", f" {D}Enter to keep it · Esc to go back{R}"]
        else:
            moves = "" if self.single else " · ←/→ between questions"
            out += ["", f" {D}Enter to select · ↑/↓ to move{moves} · Esc to cancel{R}"]
        return out


def read_key(fd: int) -> str:
    """One key press from a terminal in cbreak mode."""
    b = os.read(fd, 1)
    if b == b"\x1b":
        r, _, _ = select.select([fd], [], [], 0.05)
        if not r:
            return ESC
        seq = os.read(fd, 2)
        return {b"[A": UP, b"[B": DOWN, b"[C": RIGHT, b"[D": LEFT}.get(seq, "")
    if b in (b"\r", b"\n"):
        return ENTER
    if b in (b"\x7f", b"\x08"):
        return BACKSPACE
    if b == b"\t":
        return TAB
    if b == b"\x03":
        raise KeyboardInterrupt
    if b and b[0] >= 0xC0:          # the rest of a UTF-8 character
        b += os.read(fd, 1 if b[0] < 0xE0 else 2 if b[0] < 0xF0 else 3)
    return b.decode("utf-8", "replace")


def run_form(form: Form) -> Form:
    """Shows the box until the person saves or cancels; redraws it in place."""
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    color = not os.environ.get("NO_COLOR")
    drawn = 0
    out = sys.stdout
    try:
        tty.setcbreak(fd)
        out.write("\x1b[?25l")
        while not form.result:
            width = shutil.get_terminal_size((80, 24)).columns
            lines = form.lines(width, color)
            if drawn:
                out.write(f"\x1b[{drawn}F\x1b[J")
            out.write("\n".join(lines) + "\n")
            out.flush()
            drawn = len(lines)
            form.key(read_key(fd))
    except KeyboardInterrupt:
        form.result = "cancel"
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        if drawn:
            out.write(f"\x1b[{drawn}F\x1b[J")
        out.write("\x1b[?25h")
        out.flush()
    return form


# ── talking to Tares and to Claude Code ────────────────────────────────────

def _req(url: str, token: str, method: str = "GET", body: dict | None = None):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": f"Bearer {token}"} if token else {})})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read() or b"{}")


def missing_settings(s: dict) -> list[str]:
    """The settings the person has to choose (the release ones only with a releaser)."""
    out = [k for k in ("autonomy", "releaser", "challenger", "builders_max") if s.get(k) is None]
    if s.get("autonomy") not in (None, "L3-review", "L4-ship") and "autonomy" not in out:
        out.insert(0, "autonomy")   # a level the crew no longer has
    if s.get("releaser") and s.get("release_profile") is None:
        out.append("release_profile")
    return out


def home_trusted(home: Path) -> bool:
    try:
        cfg = json.loads((Path.home() / ".claude.json").read_text())
    except (OSError, ValueError):
        return False
    projects = cfg.get("projects") or {}
    paths = {str(home), str(home.resolve())}
    return any((projects.get(p) or {}).get("hasTrustDialogAccepted") is True for p in paths)


def short(p: Path) -> str:
    """A path the way a person reads it: the home folder as ~."""
    s, h = str(p), str(Path.home())
    return "~" + s[len(h):] if s == h or s.startswith(h + os.sep) else s


def say(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def ask_trust(home: Path, claude: str) -> bool:
    """The crew's own folder must be trusted by Claude Code: offer to open Claude Code there."""
    if home_trusted(home):
        return True
    form = Form(single=True, questions=[Question("trust", "Crew folder",
                          f"Open Claude Code in {short(home)} to trust it?", note=(
                              "The crew works from this folder of its own, whichever folder you run "
                              "this from, and Claude Code runs sessions only in folders you trust."),
                          options=[
                              Option("yes", "Yes, open it",
                                     'Pick "Yes, I trust this folder", then type /exit.'),
                              Option("no", "Not now", "The crew can't start until it is trusted."),
                          ])])
    run_form(form)
    if form.result != "save" or form.value("trust") != "yes":
        say(f"Not starting the crew: Claude Code doesn't trust {short(home)} yet.")
        return False
    home.mkdir(parents=True, exist_ok=True)
    subprocess.run([claude], cwd=str(home))
    if not home_trusted(home):
        say(f"Claude Code still doesn't trust {short(home)}. Run `claude` there, pick "
            '"Yes, I trust this folder", then try again.')
        return False
    say(f" ✓ Claude Code trusts {short(home)}")
    return True


def setup(tares: str, token: str, home: Path, claude: str) -> bool:
    """Before `crew up`: trust the crew's folder and ask the settings it lacks. False when the person
    cancelled; True to go on (also when Tares can't be reached: the factory CLI then says so)."""
    try:
        settings = _req(f"{tares}/api/crew/settings", token)
    except (urllib.error.URLError, OSError, ValueError):
        return True
    if not ask_trust(home, claude):
        return False
    missing = missing_settings(settings)
    if not missing:
        return True
    print("\n Setting up your crew. Asked once, saved on Tares.\n")
    form = run_form(Form(crew_questions(missing)))
    if form.result != "save":
        say("Cancelled: nothing was saved and the crew did not start.")
        return False
    v = form.values()
    body: dict = {}
    if "autonomy" in v:
        body["autonomy"] = v["autonomy"]
    if "releaser" in v:
        body["releaser"] = v["releaser"] == "yes"
    if "challenger" in v:
        body["challenger"] = v["challenger"] == "yes"
    if "builders_max" in v:
        body["builders_max"] = int(v["builders_max"])
    if v.get("release_profile"):
        body["release_profile"] = v["release_profile"]
        body["prod_pattern"] = settings.get("prod_pattern") or PROD_PATTERN
    try:
        _req(f"{tares}/api/crew/settings", token, "PUT", body)
        if v.get("grant"):
            _req(f"{tares}/api/grants", token, "POST", {"words": v["grant"], "scope": "all"})
    except (urllib.error.URLError, OSError, ValueError) as e:
        say(f"Could not save the answers on Tares at {tares}: {e}")
        return False
    print(" Saved on Tares\n" + "\n".join(f"  ✓ {q.tab}: {form.shown(q)}" for q in form.questions))
    return True
