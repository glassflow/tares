"""Area owners (TR-627): which person owns each GitHub repo on the cell. An area is one repo,
`owner/name`, matched in lowercase. The list lives in the cell setting `areas` as JSON,
[{repo, person_id, note}], one entry per repo.
"""
from __future__ import annotations

import json
import re

AREAS_SETTING = "areas"
_REPO = re.compile(r"^[a-z0-9][a-z0-9._-]*/[a-z0-9._-]+$")


def norm_repo(repo) -> str:
    """The repo as stored and matched: trimmed and lowercase. ValueError unless it is
    `owner/name`."""
    r = str(repo or "").strip().lower()
    if not _REPO.match(r):
        raise ValueError(f"{repo!r} is not a repo: write it as owner/name, like glassflow/tares")
    return r


def load(store) -> list[dict]:
    """The saved entries, [{repo, person_id, note}]; empty when none or the setting is unreadable."""
    try:
        raw = json.loads(store.get_setting(AREAS_SETTING) or "[]")
    except ValueError:
        return []
    return [{"repo": e["repo"], "person_id": e["person_id"], "note": e.get("note") or ""}
            for e in raw if isinstance(e, dict) and e.get("repo") and e.get("person_id")]


def save(store, entries: list[dict]) -> list[dict]:
    """Validate and replace the whole list. The people must exist (ValueError says who); a repo
    given twice keeps its last entry. Returns what was saved."""
    by_repo: dict[str, dict] = {}
    for e in entries:
        if not isinstance(e, dict):
            raise ValueError("each area is {repo, person_id, note?}")
        repo = norm_repo(e.get("repo"))
        pid = str(e.get("person_id") or "").strip()
        if not pid or store.get_person(pid) is None:
            raise ValueError(f"no person {pid!r}; GET /api/people lists them")
        note = e.get("note")
        if note is not None and not isinstance(note, str):
            raise ValueError("note is text")
        by_repo.pop(repo, None)
        by_repo[repo] = {"repo": repo, "person_id": pid, "note": (note or "").strip()}
    out = list(by_repo.values())
    store.set_setting(AREAS_SETTING, json.dumps(out))
    return out


def owner(store, repo) -> dict | None:
    """The person (a store person dict) who owns this repo, or None: no area for it, a repo
    that is not `owner/name`, or an owner who is no longer on the cell. Ignores case."""
    try:
        want = norm_repo(repo)
    except ValueError:
        return None
    for e in load(store):
        if e["repo"] == want:
            return store.get_person(e["person_id"])
    return None


def view(store) -> list[dict]:
    """What GET /api/areas returns: [{repo, person: {id, name}, note}], skipping an area whose
    person is gone."""
    out = []
    for e in load(store):
        p = store.get_person(e["person_id"])
        if p:
            out.append({"repo": e["repo"], "person": {"id": p["id"], "name": p["name"]},
                        "note": e["note"]})
    return out
