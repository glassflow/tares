"""The crew's JSON schemas must not break a reader of the last release unless schema_version was raised (TR-1280).

The newest release tag reachable from HEAD is the baseline. A change is breaking when a schema file is gone, a name is
gone from an object's properties, a property's type changed, a name was added to required, a name was removed from required, a const changed, or a value left an enum.
anyOf, oneOf and allOf branches are paired by position, so a branch inserted in the middle reads as a change:
append a new branch at the end. Anything else (a new optional property, a new file, a new enum value, a new description) keeps old readers working.
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
    dropped = [n for n in old.get("required", []) if n not in new.get("required", [])]
    if dropped:
        found.append((path, f"removed from required: {dropped}"))
    if "const" in old and "const" in new:
        o_c, n_c = json.dumps(old["const"], sort_keys=True), json.dumps(new["const"], sort_keys=True)
        if o_c != n_c:
            found.append((path, f"const changed from {o_c} to {n_c}"))
    op, np_ = old.get("properties", {}), new.get("properties", {})
    for name, sub in op.items():
        if name not in np_:
            found.append((f"{path}.{name}", "property removed"))
        else:
            found += breaking(sub, np_[name], f"{path}.{name}")
    oa, na = old.get("additionalProperties"), new.get("additionalProperties")
    if isinstance(oa, dict) and isinstance(na, dict):
        found += breaking(oa, na, f"{path}.*")
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


def raised(old, new):
    """True only when schema_version went up between two integers; a lowered or odd value is not a raise."""
    ints = all(isinstance(v, int) and not isinstance(v, bool) for v in (old, new))
    return ints and new > old


# the comparison itself, on small cases
_base = {"type": "object", "required": ["a"], "properties": {"a": {"type": "string"}, "b": {"type": "integer"}}}
assert breaking(_base, {**_base, "properties": {"a": {"type": "string"}}}) == [("$.b", "property removed")]
assert breaking(_base, {**_base, "properties": {**_base["properties"], "c": {"type": "string"}}}) == []
assert breaking(_base, {**_base, "required": ["a", "b"]}) == [("$", "added to required: ['b']")]
assert breaking(_base, {**_base, "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}}})[0][0] == "$.a"
assert breaking({"enum": ["x", "y"]}, {"enum": ["x"]}) == [("$", "enum value removed: ['y']")]
assert breaking({"enum": ["x"]}, {"enum": ["x", "y"]}) == []
assert breaking({"additionalProperties": {"type": "boolean"}}, {"additionalProperties": {"type": "string"}})[0][0] == "$.*"
assert breaking({"additionalProperties": {"type": "boolean"}}, {"additionalProperties": {"type": "boolean"}}) == []
assert breaking({"$defs": {"d": {"type": "string"}}}, {"$defs": {"d": {"type": "integer"}}})[0][0] == "$/$defs/d"
assert breaking({"$defs": {"d": {}}}, {}) == [("$/$defs/d", "definition removed")]
assert breaking({"anyOf": [{"type": "string"}, {"type": "null"}]}, {"anyOf": [{"type": "string"}]}) == [("$/anyOf/1", "branch removed")]
assert raised(1, 2) and not raised(1, 1) and not raised(1, 0) and not raised(1, "2") and not raised(1, None) and not raised(True, 2)
assert breaking({"items": {"properties": {"k": {}}}}, {"items": {"properties": {}}}) == [("$[].k", "property removed")]
assert breaking(_base, {**_base, "required": []}) == [("$", "removed from required: ['a']")]
assert breaking(_base, {**_base}) == []
assert breaking({"const": 1}, {"const": 2}) == [("$", "const changed from 1 to 2")]
assert breaking({"const": 1}, {"const": 1.0}) == [("$", "const changed from 1 to 1.0")]
assert breaking({"const": 1}, {"const": 1}) == []
assert breaking({"const": 1}, {}) == []


def git(*args):
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, encoding="utf-8", errors="replace")


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
    tree = git("ls-tree", "-r", "-z", "--name-only", tag, SCHEMAS + "/")
    if tree.returncode != 0:
        print(f"FAIL could not list the schema files of {tag}")
        return 1
    listed = tree.stdout.split("\0")
    prefix = SCHEMAS + "/"
    names = [n[len(prefix):] for n in listed if n.startswith(prefix) and n.endswith(".json")]
    findings, ok, unreadable = [], 0, False
    old_version = new_version = None
    for name in sorted(names):
        shown = git("show", f"{tag}:{SCHEMAS}/{name}")
        try:
            if shown.returncode != 0:
                raise ValueError("git show failed")
            old = json.loads(shown.stdout)
        except ValueError:
            print(f"FAIL {name}: could not read it from {tag}")
            unreadable = True
            continue
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
    if ok == 0 and not unreadable:
        print(f"FAIL no schema files compared: {tag} lists none under {SCHEMAS}/ (was the folder moved?)")
        return 1
    if not findings:
        if unreadable:
            return 1
        print("no breaking change")
        return 0
    if raised(old_version, new_version) and not unreadable:
        for f, p, what in findings:
            print(f"  accepted (schema_version raised): {f} {p}: {what}")
        return 0
    for f, p, what in findings:
        print(f"FAIL {f} {p}: {what}: raise schema_version or keep the old shape")
    if unreadable and raised(old_version, new_version):
        print("schema_version was raised, but a file could not be read, so nothing was compared for it")
    return 1


if __name__ == "__main__":
    sys.exit(main())
