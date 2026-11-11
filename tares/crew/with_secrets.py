"""`tares factory with-secrets [--project <p>] [--only NAME,...] -- <command...>` and `tares factory secrets
[--project <p>]` (TR-775).

with-secrets fetches the project's secrets from Tares at that moment, with the crew's key (the person's own),
and runs the command with them set as environment variables. Nothing is written to disk and nothing stays in
the session's environment: the next command that needs them asks again, so a secret changed in the console
applies at once. It prints only the names it set, never a value. Tares refuses anyone who is not a member of
the project. When Tares can't give them, the command does not run: a command that needs a key would only
fail later, less clearly.

secrets lists the names a project has, so a builder knows what it can ask for.

The project is --project (a name or an id), else TARES_PROJECT, which a builder's session has.
Python only: bash has no such commands.
"""
from __future__ import annotations

import subprocess
import sys
from urllib.parse import quote

from . import state
from .client import TaresClient, TaresHTTPError
from .errors import CrewError
from .paths import ME, CrewPaths

USAGE = f"usage: {ME} with-secrets [--project <name or id>] [--only NAME,...] -- <command...>"
LIST_USAGE = f"usage: {ME} secrets [--project <name or id>]"


def _why(e: TaresHTTPError, project: str) -> str:
    return {403: f"Tares refused: only members of {project} may use its secrets",
            404: f"Tares has no project {project}",
            409: "secrets are off on this Tares (it has no TARES_SECRETS_KEY); ask the person who runs it"
            }.get(getattr(e, "status", None) or getattr(e, "code", None), f"Tares could not give the secrets: {e}")


def _project(args: list, env: dict, usage: str) -> tuple:
    project, only, rest = "", [], list(args)
    while rest and rest[0] != "--":
        a = rest.pop(0)
        if a == "--project" and rest:
            project = rest.pop(0)
        elif a == "--only" and rest:
            only = [n.strip() for n in rest.pop(0).split(",") if n.strip()]
        else:
            raise CrewError(usage)
    project = project or env.get("TARES_PROJECT", "")
    if not project:
        raise CrewError(f"no project: pass --project, or run it in a builder's session (TARES_PROJECT)\n{usage}")
    return project, only, rest[1:] if rest else []


def cmd_with_secrets(argv: list, env: dict) -> int:
    """argv starts with the word with-secrets."""
    try:
        project, only, command = _project(argv[1:], env, USAGE)
    except CrewError as e:
        print(e.line if hasattr(e, "line") else str(e), file=sys.stderr)
        return 2
    if not command:
        print(USAGE, file=sys.stderr)
        return 2
    with TaresClient(CrewPaths.from_env(), env, say=lambda _m: None) as client:
        try:
            doc = state.loads_json(client.tares_http(
                "POST", f"/api/projects/{quote(project, safe='')}/secrets/use",
                {"names": only} if only else {}))
        except TaresHTTPError as e:
            print(f"{ME} with-secrets: {_why(e, project)}; {command[0]} did not run", file=sys.stderr)
            return 1
        except CrewError as e:
            print(f"{ME} with-secrets: {getattr(e, 'line', e)}; {command[0]} did not run", file=sys.stderr)
            return 1
    values = doc.get("values") if isinstance(doc, dict) else None
    values = {str(k): str(v) for k, v in (values or {}).items()}
    missing = [n for n in only if n not in values]
    if missing:
        print(f"{ME} with-secrets: {project} has no readable secret {', '.join(missing)}; "
              f"{command[0]} did not run", file=sys.stderr)
        return 1
    unreadable = [str(n) for n in (doc.get("unreadable") or [])] if isinstance(doc, dict) else []
    if unreadable:
        print(f"{ME} with-secrets: {', '.join(unreadable)} can't be read on Tares (set again in the console)",
              file=sys.stderr)
    print(f"{ME} with-secrets: {', '.join(sorted(values)) or 'no secrets'} set for {command[0]}", file=sys.stderr)
    try:
        return subprocess.run(command, env={**env, **values}).returncode
    except FileNotFoundError:
        print(f"{ME} with-secrets: {command[0]}: command not found", file=sys.stderr)
        return 127
    except KeyboardInterrupt:
        return 130


def cmd_secrets(argv: list, env: dict) -> int:
    """argv starts with the word secrets: the names, who set each and when."""
    try:
        project, only, rest = _project(argv[1:], env, LIST_USAGE)
    except CrewError as e:
        print(e.line if hasattr(e, "line") else str(e), file=sys.stderr)
        return 2
    if only or rest:
        print(LIST_USAGE, file=sys.stderr)
        return 2
    with TaresClient(CrewPaths.from_env(), env, say=lambda _m: None) as client:
        try:
            doc = state.loads_json(client.tares_http("GET", f"/api/projects/{quote(project, safe='')}/secrets"))
        except TaresHTTPError as e:
            print(f"{ME} secrets: {_why(e, project)}", file=sys.stderr)
            return 1
        except CrewError as e:
            print(f"{ME} secrets: {getattr(e, 'line', e)}", file=sys.stderr)
            return 1
    if not doc.get("enabled"):
        print(doc.get("off") or "secrets are off on this Tares")
        return 0
    rows = doc.get("secrets") or []
    if not rows:
        print(f"{project} has no secrets; a member adds them on the project's page in the console")
        return 0
    for r in rows:
        when = str(r.get("set_at") or "")[:10]
        print(f"{r['name']:<32} set by {r.get('set_by') or 'someone'} {when}"
              + ("" if r.get("readable", True) else "  (can't be read: set it again)"))
    print(f"use them with: {ME} with-secrets -- <command>")
    return 0
