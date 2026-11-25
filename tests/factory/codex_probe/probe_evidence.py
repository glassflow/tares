"""Can a Codex station's commands back an add_check claim? An offline probe (TR-1576): no model, no network.

    python tests/factory/codex_probe/probe_evidence.py [--wire FILE] [--out DIR]

A builder's check claim is verified against the commands its session really ran: `tares.factory.match_check`
over rows of `session_commands` ({command, at, output, is_error}). Claude Code feeds those rows from its
transcript (`tares/session_ingest.py`: a `Bash` tool_use and its tool_result, where the exit code is only
"failed or not"). Codex's app-server reports each command as a `commandExecution` item with the command,
its working folder, its real exit code and what it printed. `rows_from_wire` turns those items into the same
rows, and the cases below run the real `match_check` on them: a true claim, a claim for a command that never
ran, a "passed" claim for a command that failed, and a command that was refused by the guard (it has no
item at all, so it can never back a claim).

The default input is a transcript recorded on Codex 0.171.0 (fixtures/commands.wire.jsonl, paths replaced).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(HERE))))
from tares.factory import match_check  # noqa: E402

from tares.crew.codex_guard import CLOSE_STDIN  # noqa: E402  (what the guard puts in front of a command it lets through)


def model_command(item: dict) -> str:
    """The command as the model asked for it: Codex wraps it in the login shell (`/bin/zsh -lc '...'`) and
    keeps the inner text in commandActions; the guard adapter's stdin line is taken off."""
    actions = item.get("commandActions") or []
    inner = (actions[0].get("command") if len(actions) == 1 and isinstance(actions[0], dict) else None) or item.get("command") or ""
    return inner[len(CLOSE_STDIN):] if inner.startswith(CLOSE_STDIN) else inner


def rows_from_wire(path: str) -> list[dict]:
    """One row per finished commandExecution item, deduplicated by item id (a replayed event counts once)."""
    rows: dict[str, dict] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            entry = json.loads(line)
            msg = entry.get("msg") or {}
            if entry.get("dir") != "in" or msg.get("method") != "item/completed":
                continue
            params = msg.get("params") or {}
            item = params.get("item") or {}
            if item.get("type") != "commandExecution":
                continue
            done = item.get("completedAtMs") or params.get("completedAtMs")
            rows[item["id"]] = {
                "tool_use_id": item["id"], "command": model_command(item), "cwd": item.get("cwd"),
                "exit_code": item.get("exitCode"), "is_error": item.get("exitCode") not in (0, None) or item.get("status") == "failed",
                "output": item.get("aggregatedOutput") or "", "thread": params.get("threadId"), "turn": params.get("turnId"),
                "at": datetime.fromtimestamp((done or 0) / 1000, timezone.utc)}
    return sorted(rows.values(), key=lambda r: r["at"])


def cases(rows: list[dict]) -> list[dict]:
    later = max(r["at"] for r in rows)
    def check(command, result):
        return {"command": command, "result": result, "at": later}
    rows_twice = rows + rows          # the same events delivered twice
    table = [
        ("a command that ran and passed backs its claim", check("git log --oneline -1", "passed"), rows, "matched"),
        ("a command that never ran is not found", check("pytest -q", "12 passed"), rows, "not_found"),
        ("a 'passed' claim for a command that exited 7 differs", check("sh -c 'exit 7'", "passed"), rows, "differs"),
        ("a command the guard refused has no item, so no claim on it holds",
         check("git push origin main --force", "passed"), rows, "not_found"),
        ("part of a command is not the command", check("git log", "passed"), rows, "not_found"),
    ]
    out = []
    for name, claim, cmds, want in table:
        got, shown = match_check(claim, cmds)
        out.append({"case": name, "pass": got == want, "want": want, "got": got, "shown": shown})
    ids = [r["tool_use_id"] for r in rows_from_rows(rows_twice)]
    out.append({"case": "events delivered twice count once (by item id)", "pass": len(ids) == len(rows),
                "want": len(rows), "got": len(ids), "shown": None})
    exits = sorted({r["exit_code"] for r in rows if r["exit_code"] is not None})
    out.append({"case": "the real exit code is kept (7, not just 'failed')", "pass": 7 in exits, "want": "7 among them", "got": exits, "shown": None})
    out.append({"case": "every row has its working folder", "pass": all(r["cwd"] for r in rows), "want": True,
                "got": all(r["cwd"] for r in rows), "shown": None})
    return out


def rows_from_rows(rows: list[dict]) -> list[dict]:
    return list({r["tool_use_id"]: r for r in rows}.values())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wire", default=os.path.join(HERE, "fixtures", "commands.wire.jsonl"))
    ap.add_argument("--out")
    args = ap.parse_args()
    rows = rows_from_wire(args.wire)
    result = {"wire": os.path.basename(args.wire),
              "rows": [{k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in r.items() if k != "output"} | {"output_chars": len(r["output"])} for r in rows],
              "cases": cases(rows)}
    if args.out:
        os.makedirs(args.out, exist_ok=True)
        with open(os.path.join(args.out, "evidence.json"), "w", encoding="utf-8") as f:
            json.dump(result, f, indent=1, ensure_ascii=False)
    for r in result["rows"]:
        print(f"   row  exit={r['exit_code']}  {r['command']!r}")
    for c in result["cases"]:
        print(("PASS " if c["pass"] else "FAIL ") + c["case"], "" if c["pass"] else f"(got {c['got']})")
    return 0 if all(c["pass"] for c in result["cases"]) else 1


if __name__ == "__main__":
    sys.exit(main())
