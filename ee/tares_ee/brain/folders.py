# Tares enterprise licence, see ee/LICENSE
"""The decision folders of a connected repo (TR-1209).

A decision folder is a folder whose files are decisions (architecture decision records and the like).
An admin chooses them per repo, when connecting it or later; `store.list_brain_folders` keeps each with
who chose it and when. Tares helps by looking at the repo's git tree and suggesting likely folders, each
with its file count:

  * the known names: docs/adr, docs/adrs, docs/decisions, adr, decisions, doc/architecture/decisions;
  * any other folder where most of the files directly in it are numbered like `0007-use-postgres.md`.

A file counts when it is markdown (.md or .mdx), anywhere under the folder: those are the files that
become decisions. The scan is one call to GitHub's git trees API with `recursive=1`. When GitHub says the
tree was `truncated` (a huge repo), only the known names are looked at, from what GitHub did send, and the
answer says `partial: true`; the tree is never paged through.

This module holds the pure parts (reading a tree, checking a folder path) and the hooks that run when a
repo's chosen folders change: TR-1210 imports the files of an added folder, TR-1219 removes the documents
of a dropped one. Until those are merged the hooks list is empty and a change is only stored.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger("tares.brain")

__all__ = ["HOOKS", "KNOWN", "MAX_FOLDERS", "check_folder", "check_folders", "count_files", "notify", "suggest"]

KNOWN = ("docs/adr", "docs/adrs", "docs/decisions", "adr", "decisions", "doc/architecture/decisions")
MAX_FOLDERS = 50     # chosen folders per repo
MAX_SUGGESTED = 30   # suggestions in one answer (a monorepo can hold an adr folder per service)
MAX_PATH = 200       # characters in a folder path
_NUMBERED = re.compile(r"^\d{3,}-.+\.mdx?$", re.IGNORECASE)
_MARKDOWN = (".md", ".mdx")
# Called as `await hook(repo, added, removed)` after a repo's chosen folders changed (TR-1210 and TR-1219 add theirs).
HOOKS: list = []


def check_folder(value) -> str:
    """A folder path as stored (no slash at either end), or ValueError in plain words. The repo's root,
    `.` and `..` parts, backslashes, empty parts and control characters are refused."""
    if not isinstance(value, str):
        raise ValueError(f"{value!r} is not a folder: write it like docs/adr")
    path = value.strip().strip("/")
    parts = path.split("/")
    if (not path or len(path) > MAX_PATH or "\\" in path or any(ord(c) < 32 or ord(c) == 127 for c in path)
            or any(p in ("", ".", "..") for p in parts)):
        raise ValueError(f"{value!r} is not a folder: write it like docs/adr, a path inside the repo")
    return path


def check_folders(value) -> list[str]:
    """A list of folder paths, checked and in the order given; ValueError in plain words for a bad
    one, one named twice, or more than MAX_FOLDERS. An empty list is fine: no decision folders."""
    if not isinstance(value, list):
        raise ValueError('folders: a list of folder paths, like ["docs/adr"]')
    if len(value) > MAX_FOLDERS:
        raise ValueError(f"choose at most {MAX_FOLDERS} folders for a repo")
    out: list[str] = []
    for item in value:
        path = check_folder(item)
        if path in out:
            raise ValueError(f"{path} is in the list twice")
        out.append(path)
    return out


def _is_markdown(path: str) -> bool:
    return path.lower().endswith(_MARKDOWN)


def count_files(entries: list[dict], folder: str) -> int:
    """Markdown files in `entries` (a git tree) at any depth under `folder`."""
    prefix = folder + "/"
    return sum(1 for e in entries if e.get("type") == "blob" and e["path"].startswith(prefix)
               and _is_markdown(e["path"]))


def suggest(entries: list[dict], truncated: bool = False) -> list[dict]:
    """Likely decision folders in a git tree (`entries`: {path, type} with type blob or tree), each as
    {path, files, why}: `why` is "known" (one of KNOWN) or "numbered" (most of its files are numbered
    like 0007-title.md, at least two of them). Folders with no markdown file are left out, and a numbered
    folder inside a suggested one is not suggested again. Known names come first (in KNOWN's order), then
    the numbered ones by path. With `truncated` only the known names are looked at, and what GitHub sent
    of them is all there is to count."""
    out: list[dict] = []
    have: dict[str, str] = {}   # lowercase known name -> the path as the repo spells it
    for e in entries:
        if e.get("type") == "tree" and e["path"].lower() in KNOWN:
            have[e["path"].lower()] = e["path"]
    for name in KNOWN:
        path = have.get(name)
        if path is None:
            continue
        files = count_files(entries, path)
        if files:
            out.append({"path": path, "files": files, "why": "known"})
    if truncated:
        return out
    taken = [s["path"] for s in out]
    direct: dict[str, list[str]] = {}   # folder -> the names of the files directly in it
    for e in entries:
        if e.get("type") == "blob" and "/" in e["path"]:
            folder, _, name = e["path"].rpartition("/")
            direct.setdefault(folder, []).append(name)
    numbered = []
    for folder, names in sorted(direct.items()):
        hits = sum(1 for n in names if _NUMBERED.match(n))
        if hits < 2 or hits * 2 <= len(names):
            continue
        if any(folder == t or folder.startswith(t + "/") for t in taken):
            continue
        files = count_files(entries, folder)
        numbered.append({"path": folder, "files": files, "why": "numbered"})
    return (out + numbered)[:MAX_SUGGESTED]


async def notify(repo: str, added: list[str], removed: list[str]) -> None:
    """Tell every hook that `repo`'s chosen folders changed. The change is already stored, so a hook
    that fails is logged (its type only) and the rest still run."""
    if not added and not removed:
        return
    for hook in list(HOOKS):
        try:
            await hook(repo, list(added), list(removed))
        except Exception as e:   # noqa: BLE001
            log.warning("decision folder hook for %s failed: %s", repo, type(e).__name__)
