"""The words list for the factory and sre editions (ui/src/editions/words.json), for API tests and
the MCP tool names.

`offenders(text)` is the whole-word rule of the console's check (ui/tests/words.cjs): a forbidden
word or its plural, any case, with the list's allowed phrases taken out first, plus an em dash.
`offenders_in(obj)` walks a JSON body and checks every string value (never a key).

As a command it checks JSON on stdin, so an acceptance check pipes a screen's JSON into it:

    curl -s $A/api/home | python -m tares.words --skip name,goal,title,detail,said,text

It prints `<json path>: <word>` for each offender, or `0 problems`, and exits 1 on any offender.
`editions_words.json` here is a copy of ui/src/editions/words.json (the wheel bundles ui/dist, not
ui/src); tests/test_words_sync.py keeps the two identical.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_WORD_CHAR = "A-Za-z0-9_"
_data: dict | None = None
_rules: list | None = None


def load() -> dict:
    """The words list: {"forbidden": [{word, plural, factory, sre, allow}], "punctuation": [...]}."""
    global _data
    if _data is None:
        _data = json.loads((Path(__file__).with_name("editions_words.json")).read_text(encoding="utf-8"))
    return _data


def _compiled() -> list:
    global _rules
    if _rules is None:
        _rules = []
        for e in load()["forbidden"]:
            allow = [
                re.compile(rf"(?<![{_WORD_CHAR}]){re.escape(' '.join(a.split()))}(?![{_WORD_CHAR}])", re.I)
                for a in e["allow"]
            ]
            find = re.compile(
                rf"(?<![{_WORD_CHAR}])(?:{re.escape(e['plural'])}|{re.escape(e['word'])})(?![{_WORD_CHAR}])", re.I
            )
            _rules.append((allow, find))
    return _rules


def offenders(text: str) -> list[str]:
    """The forbidden words in a text, in order of appearance, each once; "em dash" for an em dash."""
    t = " ".join(str(text).split())
    found: list[tuple[int, str]] = []
    dash = load()["punctuation"][0]
    if dash in t:
        found.append((t.index(dash), "em dash"))
    for allow, find in _compiled():
        clean = t
        for a in allow:
            clean = a.sub(lambda m: " " * len(m.group(0)), clean)
        found += [(m.start(), m.group(0).lower()) for m in find.finditer(clean)]
    found.sort(key=lambda f: f[0])
    seen: list[str] = []
    for _, name in found:
        if name not in seen:
            seen.append(name)
    return seen


def _path(parent: str, key) -> str:
    if isinstance(key, int):
        return f"{parent}[{key}]"
    return f"{parent}.{key}" if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) else f"{parent}[{json.dumps(key)}]"


def offenders_in(obj, skip_keys=()) -> list[str]:
    """`<json path>: <word>` for each offender in the string values of a JSON body. Keys are not
    checked; whatever sits under a key in `skip_keys` (text a person or an agent wrote) is skipped."""
    skip = set(skip_keys)
    out: list[str] = []

    def walk(v, path: str) -> None:
        if isinstance(v, str):
            out.extend(f"{path}: {w}" for w in offenders(v))
        elif isinstance(v, dict):
            for k, x in v.items():
                if k not in skip:
                    walk(x, _path(path, str(k)))
        elif isinstance(v, (list, tuple)):
            for i, x in enumerate(v):
                walk(x, _path(path, i))

    walk(obj, "$")
    return out


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    skip: list[str] = []
    while args:
        a = args.pop(0)
        if a == "--skip" and args:
            skip += [k for k in args.pop(0).split(",") if k]
        elif a.startswith("--skip="):
            skip += [k for k in a.split("=", 1)[1].split(",") if k]
        else:
            print("usage: python -m tares.words [--skip key1,key2]  (JSON on stdin)", file=sys.stderr)
            return 2
    try:
        body = json.load(sys.stdin)
    except ValueError as e:
        print(f"not JSON: {e}", file=sys.stderr)
        return 2
    found = offenders_in(body, skip)
    print("\n".join(found) if found else "0 problems")
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
