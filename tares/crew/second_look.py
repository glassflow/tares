"""`tares factory second-look`: a separate, fresh Codex session looks at a review or a plan (TR-1599).

    tares factory second-look --file <what to look at> [--dir <folder>] [--model <model>]

A Claude Code reviewer hands its diff and draft findings to a subagent on another Claude model; a spec session
does the same with its plan. On Codex there is no subagent: this command runs one `codex exec` session of its
own instead, and prints what it answered. That session is fresh (nothing of the caller's conversation, kept on
no disk: `--ephemeral`), read-only (its sandbox writes nothing), and loads none of the person's own Codex setup
(`--ignore-user-config`, connectors and plugins off). Its model: `--model`, else the one the crew names for
second looks (crew.json, `stations.<role>.second_look_model`), else the model the calling station was started on
(its row in stations.json), named on the command line so it is the same. A caller whose model is not known (one
started on its account's default, or a planning session) gets the account's default, and the answer says that
whether it is the caller's own is not known. The answer always says which model it was: a review by the same
model in another conversation is what it is, and is never told as a second model's.

It never runs inside itself: the session it starts has TARES_SECOND_LOOK set, and this command refuses to run
under it, so a second look cannot start a third. It needs the network (the model is remote): a Codex station
runs it asking for approval, as any command that reaches outside its sandbox.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile

from . import state
from .errors import CrewError
from .paths import ME, CrewPaths

USAGE = f"usage: {ME} second-look --file <what to look at> [--dir <folder>] [--model <model>]"
NESTED = "TARES_SECOND_LOOK"
TIMEOUT = 900   # seconds one look may take
LEAN = ("--disable", "apps", "--disable", "plugins", "--disable", "remote_plugin")
HEAD = ("You are a second look, in a Codex session of your own: nobody else's reasoning is here, only what "
        "follows. Read it, read the files it names in this folder if you need them (you cannot change anything), "
        "and answer plainly: what is missing or wrong, and, for each finding it already holds, whether it stands. "
        "Do not start another session or agent.\n\n")


def model_for(paths: CrewPaths | None, role: str) -> str:
    """The model a second look runs on: the crew's `stations.<role>.second_look_model`, else "" (the caller's own,
    which Codex picks from the session's home)."""
    if not (paths and role):
        return ""
    try:
        return str(state.ccfg("stations", role, "second_look_model", paths=paths) or "")
    except CrewError:
        return ""


def command(codex: str, folder: str, model: str, out_file: str) -> list:
    """The `codex exec` command line of one look; the prompt comes on stdin."""
    args = [codex, "exec", "--ephemeral", "--sandbox", "read-only", "--skip-git-repo-check", "--ignore-user-config",
            *LEAN, "-C", folder, "--output-last-message", out_file]
    if model:
        args += ["-m", model]
    return [*args, "-"]


def _args(argv: list) -> dict:
    args = list(argv[1:] if argv[:1] == ["second-look"] else argv)
    out = {"file": "", "dir": "", "model": ""}
    while args:
        a = args.pop(0)
        if a in ("--file", "--dir", "--model") and args:
            out[a[2:]] = args.pop(0)
        else:
            raise CrewError(USAGE)
    if not out["file"]:
        raise CrewError(USAGE)
    return out


def caller_model(paths: CrewPaths | None, station: str) -> str:
    """The model the calling station was started on (stations.json), "" when it is not known."""
    if not (paths and station):
        return ""
    try:
        return str(state.stations_get(station, "model", paths=paths) or "")
    except CrewError:
        return ""


def said_model(model: str, how: str) -> str:
    """How the answer names the model it ran on (see the module's note)."""
    if how == "same":
        return f"{model}, the same model as yours"
    if how == "named":
        return f"{model}, the model named for this look"
    return "the Codex account's default model (whether it is the one you run on is not known)"


def look(env: dict, file: str, folder: str = "", model: str = "") -> tuple:
    """(exit code, the answer, the model, how it was chosen: named, same or default) of one look at `file`, run
    in `folder`."""
    if env.get(NESTED):
        raise CrewError("a second look does not start another one")
    try:
        with open(file, encoding="utf-8") as f:
            body = f.read()
    except OSError as e:
        raise CrewError(f"cannot read {file}: {e.strerror or e}") from None
    if not body.strip():
        raise CrewError(f"{file} is empty: write what to look at in it")
    from . import codex as cx
    codex = cx.codex_bin(env)
    folder = os.path.abspath(folder or os.getcwd())
    if not os.path.isdir(folder):
        raise CrewError(f"{folder} is not a folder")
    home = env.get("FACTORY_CREW_HOME") or env.get("FACTORY_HOME")
    paths = CrewPaths.at(home) if home else None
    how = "named" if model else ""
    if not model:
        model = model_for(paths, env.get("FACTORY_ROLE", ""))
        how = "named" if model else ""
    if not model:
        model = caller_model(paths, env.get("FACTORY_STATION", ""))
        how = "same" if model else "default"
    with tempfile.TemporaryDirectory(prefix="tares-look-") as tmp:
        out = os.path.join(tmp, "answer.md")
        try:
            done = subprocess.run(command(codex, folder, model, out), input=HEAD + body, text=True, capture_output=True,
                                  env={**env, NESTED: "1"}, timeout=TIMEOUT)
        except FileNotFoundError:
            raise CrewError(f"{codex}: command not found") from None
        except subprocess.TimeoutExpired:
            raise CrewError(f"the second look took longer than {TIMEOUT // 60} minutes and was stopped") from None
        answer = open(out, encoding="utf-8").read().strip() if os.path.exists(out) else ""
    if done.returncode != 0 and not answer:
        why = " ".join((done.stderr or done.stdout or "").split())[-300:]
        raise CrewError(f"the second look failed (codex exited {done.returncode}): {why or 'no output'}")
    return done.returncode, answer, model, how


def cmd_second_look(argv: list, env: dict) -> int:
    try:
        a = _args(argv)
        code, answer, model, how = look(env, a["file"], a["dir"], a["model"])
    except CrewError as e:
        print(e.line, file=sys.stderr)
        return 1
    print(f"Second look: a separate Codex session, read-only, on {said_model(model, how)}. "
          "Not a second model unless that names one.\n")
    print(answer or "(it gave no answer)")
    return code
