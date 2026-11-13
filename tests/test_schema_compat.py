"""The crew's JSON schemas must not break a reader of the last release unless schema_version was raised (TR-1280).

The newest release tag reachable from HEAD is the baseline. A change is breaking when a schema file is gone, a name is
gone from an object's properties, a property's type changed, a name was added to required, or a value left an enum.
Anything else (a new optional property, a new file, a new enum value, a new description) keeps old readers working.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMAS = "tares/crew/schemas"
VERSION_FILE = "schema_version.json"


def _types(t):
    return set(t) if isinstance(t, list) else {t}


def breaking(old, new, path="$"):
    """Findings (path, what changed) in `new` that break a reader written for `old`. Both are schema dicts."""
    found = []
    if not isinstance(old, dict) or not isinstance(new, dict):
        return found
    if "type" in old and "type" in new and _types(old["type"]) != _types(new["type"]):
        found.append((path, f"type changed from {old['type']} to {new['type']}"))
    elif "type" in old and "type" not in new:
        found.append((path, f"type {old['type']} removed"))
    if isinstance(old.get("enum"), list):
        gone = [v for v in old["enum"] if v not in (new.get("enum") or [])]
        if gone and "enum" in new:
            found.append((path, f"enum value removed: {gone}"))
        elif "enum" not in new:
            pass  # an enum dropped entirely accepts more values, so old readers still work
    added = [n for n in new.get("required", []) if n not in old.get("required", [])]
    if added:
        found.append((path, f"added to required: {added}"))
    op, np_ = old.get("properties", {}), new.get("properties", {})
    for name, sub in op.items():
        if name not in np_:
            found.append((f"{path}.{name}", "property removed"))
        else:
            found += breaking(sub, np_[name], f"{path}.{name}")
    if "items" in old and "items" in new:
        found += breaking(old["items"], new["items"], f"{path}[]")
    elif "items" in old:
        found.append((f"{path}[]", "items removed"))
    for key in ("$defs", "definitions"):
        for name, sub in old.get(key, {}).items():
            if name in new.get(key, {}):
                found += breaking(sub, new[key][name], f"{path}/{key}/{name}")
            else:
                found.append((f"{path}/{key}/{name}", "definition removed"))
    for key in ("anyOf", "oneOf", "allOf"):
        o, n = old.get(key), new.get(key)
        if isinstance(o, list) and isinstance(n, list):
            for i, sub in enumerate(o):
                if i < len(n):
                    found += breaking(sub, n[i], f"{path}/{key}/{i}")
                else:
                    found.append((f"{path}/{key}/{i}", "branch removed"))
    return found


# the comparison itself, on small cases
_base = {"type": "object", "required": ["a"], "properties": {"a": {"type": "string"}, "b": {"type": "integer"}}}
assert breaking(_base, {**_base, "properties": {"a": {"type": "string"}}}) == [("$.b", "property removed")]
assert breaking(_base, {**_base, "properties": {**_base["properties"], "c": {"type": "string"}}}) == []
assert breaking(_base, {**_base, "required": ["a", "b"]}) == [("$", "added to required: ['b']")]
assert breaking(_base, {**_base, "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}}})[0][0] == "$.a"
assert breaking({"enum": ["x", "y"]}, {"enum": ["x"]}) == [("$", "enum value removed: ['y']")]
assert breaking({"enum": ["x"]}, {"enum": ["x", "y"]}) == []
assert breaking({"items": {"properties": {"k": {}}}}, {"items": {"properties": {}}}) == [("$[].k", "property removed")]


def git(*args):
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, encoding="utf-8")


def newest_tag():
    r = git("describe", "--tags", "--abbrev=0", "--match", "v*")
    return r.stdout.strip() if r.returncode == 0 else ""


def main():
    tag = newest_tag()
    if not tag:
        git("fetch", "--tags", "--depth=200", "origin")
        tag = newest_tag()
        if not tag:
            # a shallow checkout of a branch keeps its own depth: fetch more of its history
            git("fetch", "--deepen=200", "origin")
            tag = newest_tag()
    if not tag:
        print("no release tag reachable")
        return 1 if os.environ.get("CI") else 0
    names = git("ls-tree", "-r", "--name-only", tag, SCHEMAS + "/").stdout.split()
    names = [n.rsplit("/", 1)[1] for n in names if n.endswith(".json")]
    findings, ok = [], 0
    old_version = new_version = None
    for name in sorted(names):
        shown = git("show", f"{tag}:{SCHEMAS}/{name}")
        old = json.loads(shown.stdout)
        cur = ROOT / SCHEMAS / name
        if name == VERSION_FILE:
            old_version = old.get("schema_version")
            new_version = json.loads(cur.read_text(encoding="utf-8")).get("schema_version") if cur.exists() else None
        if not cur.exists():
            findings.append((name, "$", "schema file removed"))
            continue
        new = json.loads(cur.read_text(encoding="utf-8"))
        for p, what in breaking(old, new):
            findings.append((name, p, what))
        ok += 1
    print(f"baseline {tag}: {ok} schema files compared, schema_version {old_version} -> {new_version}")
    if not findings:
        print("no breaking change")
        return 0
    if new_version is not None and old_version is not None and new_version != old_version:
        for f, p, what in findings:
            print(f"  accepted (schema_version raised): {f} {p}: {what}")
        return 0
    for f, p, what in findings:
        print(f"FAIL {f} {p}: {what}: raise schema_version or keep the old shape")
    return 1


if __name__ == "__main__":
    sys.exit(main())
