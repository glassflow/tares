# Tares enterprise licence, see ee/LICENSE
"""The files of a repo's decision folders, in the brain as decisions (TR-1210).

An admin chooses decision folders per repo (`folders.py`, TR-1209). Every markdown file (.md or .mdx, at
any depth) in a chosen folder becomes one lore document of kind `decision` under the source
`repo_decisions`, with who wrote it first, who changed it last, when, and in which pull request, all read
from GitHub:

  * external id `<repo>:<path>` (the repo as the cell stores it, lowercase `owner/name`); uri the file on
    GitHub at HEAD; title the file's first `# ` heading, else its name without the extension; text the file;
  * metadata `where: repo_folder`, `repo`, `decided_by` (the first commit's author), `decided_at` (that
    commit's author date), `pr` (the pull request of the first commit, left out when it has none) and,
    when the last commit is another commit, `changed_by` and `changed_at` (its date, as YYYY-MM-DD).
    A key with nothing to say is left out: lore refuses a whole batch for one bad value;
  * an author is named by the Tares person who has the commit's GitHub login, else the git name, else the
    login, else "Unknown".

When it runs: after an admin adds a folder (a hook on `folders.HOOKS`) and once when the app starts, for
the folders already chosen. Both run in the background, one import per repo at a time (a request that
comes while one runs is merged into one more run), and one GitHub lookup at a time across the cell. The
import reads the chosen folders again from the cell before it starts and before each batch, so a folder
dropped in the meantime is not imported.

It then follows the repo (TR-1219). A push to the default branch (`on_push`, called by the syncer) names the
files it touched; the tree read decides for each: still there means import it again (the cache below spares a
file whose blob did not change), gone means delete its lore document. A push that touches no markdown costs
nothing, and one with no usable file list (GitHub cuts a push's list at 20 commits; a token repo has no push
events and is looked at when its head moves) runs a full pass over the chosen folders, which also deletes
every document whose file is gone. A force push has no usable list either (it can drop files its commits never name).
An edit that makes a file stop counting (over the size limit, its blob gone, no usable commit date) deletes the
file's document too. A folder an admin drops (a hook on `folders.HOOKS`) deletes every document
no longer under a chosen folder; start-up does the same once, so a delete that failed is tried again.

How many documents the brain holds for a repo (`count`, shown on each row of `GET /api/brain`, TR-1442) is kept
here, not asked of lore: the paths of the files put per repo, filled once at start-up from lore's list of the
source (one walk, never on a request) and added to by each put. `count` is the paths under the folders still
chosen, so a dropped folder stops counting at once. A request never calls lore or GitHub.

GitHub's rate limit (the token's hourly budget) is the thing to spare. The tree is one call. Per file the
import needs the blob, the latest commit, the first commit (the last page of a one-per-page list) and that
commit's pull request. What was learned for a file is kept in memory under (repo, path, blob sha), so a
file that did not change costs no call on a later pass. A rate limit answer stops the pass for that repo
(what was found so far is put) and the next pass carries on from the cache.

Nothing secret is sent or logged: a GitHub error is logged by its status or type, a lore error through the
client's scrubbed words, any other error by its type.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import os
import re
from collections import OrderedDict
from datetime import datetime
from urllib.parse import parse_qs, quote, urlparse

import httpx

from tares import github_credentials, licence

from . import folders as folders_mod
from .lore import LoreClient, LoreError, scrub

__all__ = ["BATCH", "KIND", "MAX_BYTES", "MAX_CACHE", "SOURCE", "TRANSPORT", "RepoDecisions"]

log = logging.getLogger("tares_ee.brain.decisions_repo")

SOURCE = "repo_decisions"       # the lore source these documents live under
KIND = "decision"               # the lore kind of each one
BATCH = 50                      # documents in one request
MAX_BYTES = 1_000_000           # a file over this is not imported
MAX_CACHE = 5000                # files whose lookups are kept in memory
MAX_CACHE_BYTES = 64_000_000    # ... and the bytes of their text
SEED_PAGES = 500                # pages of 200 read from lore at start-up to fill the counts
NAME_BYTES = 256                # lore's limit for a name
TITLE_CHARS = 300
UNKNOWN = "Unknown"             # the author of a commit with no name at all
TIMEOUT = 30.0                  # seconds for one GitHub call
TRANSPORT = None                # a test sets an httpx transport here: no real GitHub
_RFC3339 = re.compile(r"\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})")
_LAST = re.compile(r'<([^>]*)>\s*;\s*rel="last"')
_HEADING = re.compile(r"^#[ \t]+(.+?)[ \t]*#*[ \t]*$")


class _Stop(Exception):
    """The pass for this repo ends here (a rate limit, or GitHub is not answering)."""

    def __init__(self, why: str):
        super().__init__(why)
        self.why = why


class _Gone(Exception):
    """GitHub says the file is not there (it changed between the tree and the read)."""


def _say(e) -> str:
    """An error in words that are safe to log: lore's own text with the brain's key taken out."""
    return scrub(e, [os.environ.get("TARES_BRAIN_KEY", "")])


def _clip(text: str, limit: int = NAME_BYTES) -> str:
    """`text` cut to at most `limit` bytes, never in the middle of a character."""
    raw = text.encode("utf-8")
    return text if len(raw) <= limit else raw[:limit].decode("utf-8", "ignore").rstrip()


def _is_markdown(path: str) -> bool:
    return path.lower().endswith(folders_mod._MARKDOWN)


def external_id(repo: str, path: str) -> str:
    return f"{repo}:{path}"


def file_uri(repo: str, path: str) -> str:
    """The file on GitHub at the repo's default branch (the tree is read at HEAD)."""
    return f"https://github.com/{repo}/blob/HEAD/" + "/".join(quote(p, safe="") for p in path.split("/"))


def title_of(path: str, text: str) -> str:
    """The first `# ` heading of the file (outside a fenced code block), else the file's name without
    its extension."""
    fenced = False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if fenced:
            continue
        m = _HEADING.match(line.rstrip())
        if m and m.group(1).strip():
            return " ".join(m.group(1).split())[:TITLE_CHARS]
    name = path.rsplit("/", 1)[-1]
    stem = name.rsplit(".", 1)[0] if "." in name else name
    return stem or name


def _rfc3339(value) -> str | None:
    """`value` when it is an RFC 3339 time lore accepts, else None."""
    if not isinstance(value, str) or not _RFC3339.fullmatch(value):
        return None
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        return None
    return value


def _commit_view(c) -> dict | None:
    """What the import keeps of one GitHub commit: {sha, login, name, date}, or None for a junk entry."""
    if not isinstance(c, dict) or not isinstance(c.get("sha"), str):
        return None
    inner = c.get("commit") if isinstance(c.get("commit"), dict) else {}
    git_author = inner.get("author") if isinstance(inner.get("author"), dict) else {}
    committer = inner.get("committer") if isinstance(inner.get("committer"), dict) else {}
    account = c.get("author") if isinstance(c.get("author"), dict) else {}   # null for a deleted account
    login = account.get("login")
    name = git_author.get("name")
    return {"sha": c["sha"],
            "login": login if isinstance(login, str) else None,
            "name": name if isinstance(name, str) else None,
            "date": _rfc3339(git_author.get("date")) or _rfc3339(committer.get("date"))}


def author_name(commit: dict, person_name: str | None) -> str:
    """Who a commit is by: the Tares person's name, else the git name, else the GitHub login, else Unknown."""
    for candidate in (person_name, commit.get("name"), commit.get("login")):
        words = " ".join(str(candidate or "").split())
        if words:
            return _clip(words) or UNKNOWN
    return UNKNOWN


def build_document(repo: str, path: str, facts: dict, names: dict) -> dict | None:
    """The lore document for one file, from what was learned about it (`facts`: text, first, last, pr) and
    `names` ({login: a Tares person's name}). None when the file has no usable date: a decision without
    one is refused by lore."""
    first, last = facts.get("first"), facts.get("last") or facts.get("first")
    if not first or not first.get("date"):
        return None
    meta: dict = {"where": "repo_folder", "repo": repo,
                  "decided_by": [author_name(first, names.get(first.get("login")))],
                  "decided_at": first["date"]}
    pr = facts.get("pr")
    if isinstance(pr, str) and pr and len(pr.encode("utf-8")) <= 2048:
        meta["pr"] = pr
    if last and last["sha"] != first["sha"] and last.get("date"):
        meta["changed_by"] = [author_name(last, names.get(last.get("login")))]
        meta["changed_at"] = last["date"][:10]
    text = (facts.get("text") or "").replace("\x00", "")   # lore refuses a NUL in a text or a title (the title comes from it), and the whole batch with it
    return {"external_id": external_id(repo, path), "title": title_of(path, text), "text": text,
            "kind": KIND, "uri": file_uri(repo, path), "metadata": meta}


def _digest(d: dict) -> str:
    return hashlib.sha256(json.dumps(d, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


class RepoDecisions:
    """One per app. `lore_client` is a callable that gives a `LoreClient` (looked up per call, so a test
    can swap it); `github_token` is `async (repo) -> token` (raises ValueError in plain words when no
    GitHub connection reads the repo). `transport` is for a test: an httpx transport for the GitHub calls."""

    def __init__(self, store, lore_client, github_token, transport=None):
        self.store = store
        self._lore = lore_client
        self._token = github_token
        self.transport = transport
        self._lock = asyncio.Lock()                   # one GitHub lookup at a time across the cell
        self._pending: dict[str, set[str]] = {}       # repo -> folders asked for while its import runs
        self._tasks: dict[str, asyncio.Task] = {}     # repo -> its running import (keeps the task referenced)
        self._sent: dict[str, str] = {}               # external id -> digest of what this process last put
        self._cache: OrderedDict[tuple, dict] = OrderedDict()   # (repo, path, blob sha) -> what GitHub said
        self._cache_bytes = 0
        self._held: dict[str, set[str]] = {}          # repo -> paths of the files whose documents lore holds
        self._touched: dict[str, set[str] | None] = {}   # repo -> files pushes touched (None: look at every chosen folder)
        self._prune: set[str] = set()                 # repos whose documents outside the chosen folders are to be deleted
        self._gone: set[tuple[str, str]] | None = None   # (repo, path) deleted while the seed walks lore's list
        self._seed_failed = False                     # the last read of lore's list failed: `_held` may miss documents
        self.startup_task: asyncio.Task | None = None
        self.calls = 0                                # GitHub calls made, for tests

    def active(self) -> bool:
        return licence.ee_allowed() and LoreClient.configured()

    # -- how many the brain holds -----------------------------------------------------------

    def count(self, repo: str, folders) -> int:
        """Documents the brain holds from the files of `folders` (the repo's chosen folders) of `repo`.
        Answers from memory: 0 for no folders or nothing put yet. Safe to call from a thread."""
        if not folders:
            return 0
        held = list(self._held.get(repo) or ())   # a copy: the event loop may add to the set meanwhile
        return sum(1 for p in held if self._under(p, folders))

    async def _seed(self) -> None:
        """Fill `_held` from what lore already holds under the source, so the count is right after a restart
        before the first import has run. One walk of lore's list, at start-up only."""
        found: dict[str, set[str]] = {}
        cursor, failed = None, False
        self._gone = set()   # a delete that lands during the walk must not be put back by the merge below
        try:
            async with self._lore() as lore:
                for _ in range(SEED_PAGES):
                    page = await lore.list_documents(source=SOURCE, kind=KIND, limit=200, cursor=cursor)
                    for d in page.get("documents") or []:
                        if not isinstance(d, dict):
                            continue
                        ext = d.get("external_id")
                        meta = d.get("metadata") if isinstance(d.get("metadata"), dict) else {}
                        repo = meta.get("repo")
                        if isinstance(ext, str) and isinstance(repo, str) and ext.startswith(repo + ":"):
                            found.setdefault(repo, set()).add(ext[len(repo) + 1:])
                    cursor = page.get("next_cursor")
                    if not cursor:
                        break
                else:
                    log.warning("brain repo decisions: lore holds more than %d decisions; the counts may be low until the next import",
                                SEED_PAGES * 200)
        except asyncio.CancelledError:
            raise
        except LoreError as e:
            failed = True
            log.warning("brain repo decisions: could not read what lore holds: %s", _say(e))
        except Exception as e:   # noqa: BLE001
            failed = True
            log.warning("brain repo decisions: could not read what lore holds: %s", type(e).__name__)
        gone, self._gone = self._gone or set(), None
        self._seed_failed = failed
        for repo, paths in found.items():
            self._held.setdefault(repo, set()).update(p for p in paths if (repo, p) not in gone)

    async def _known(self) -> None:
        """A pass deletes by what `_held` says lore holds. When start-up could not read lore's list, `_held`
        misses the older documents, so read the list again (under the lock, so no delete runs meanwhile)."""
        if self._seed_failed:
            await self._seed()

    # -- asking for an import ---------------------------------------------------------------

    def request(self, repo: str, folders) -> None:
        """Ask for `folders` of `repo` to be imported in the background. Never waits and never raises. While
        an import of the repo runs, the folders are merged and run once more after it."""
        folders = {f for f in folders or [] if isinstance(f, str) and f}
        if not folders:
            return
        self._pending.setdefault(repo, set()).update(folders)
        self._kick(repo)

    def _kick(self, repo: str) -> None:
        task = self._tasks.get(repo)
        if task is None or task.done():
            self._tasks[repo] = asyncio.create_task(self._drain(repo))

    def on_push(self, repo: str, paths) -> None:
        """A push to the repo's default branch touched `paths` (None: the files are not known). Never waits
        and never raises. Only markdown files count; a push with none of them does nothing."""
        try:
            if not self.active() or not repo:
                return
            if paths is None:
                self._touched[repo] = None
            else:
                paths = {p for p in paths if isinstance(p, str) and _is_markdown(p)}
                if not paths:
                    return
                if self._touched.get(repo, set()) is not None:
                    self._touched.setdefault(repo, set()).update(paths)
            self._kick(repo)
        except Exception as e:   # noqa: BLE001
            log.warning("brain repo decisions: could not queue a push of %s: %s", repo, type(e).__name__)

    async def _drain(self, repo: str) -> None:
        try:
            seed = self.startup_task   # what lore holds must be known before anything is deleted
            if seed is not None and not seed.done() and seed is not asyncio.current_task():
                await asyncio.wait({seed})
            while self._pending.get(repo) or repo in self._touched or repo in self._prune:
                if repo in self._prune:
                    self._prune.discard(repo)
                    try:
                        await self.prune(repo)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:   # noqa: BLE001
                        log.warning("brain repo decisions: dropping documents of %s failed: %s", repo, type(e).__name__)
                wanted = self._pending.pop(repo, None)
                if wanted:
                    try:
                        await self.import_folders(repo, wanted)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:   # noqa: BLE001  (one bad import must not end the loop)
                        log.warning("brain repo decisions: the import of %s failed: %s", repo, type(e).__name__)
                if repo in self._touched:
                    paths = self._touched.pop(repo)
                    try:
                        await self.import_changed(repo, paths)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:   # noqa: BLE001
                        log.warning("brain repo decisions: following a push to %s failed: %s", repo, type(e).__name__)
        finally:
            if self._tasks.get(repo) is asyncio.current_task():
                self._tasks.pop(repo, None)

    async def hook(self, repo: str, added: list[str], removed: list[str]) -> None:
        """The `folders.HOOKS` entry: import what was added, delete what was dropped. Answers at once."""
        try:
            if not self.active():
                return
            if removed:
                self._prune.add(repo)
                self._kick(repo)
            if added:
                self.request(repo, added)
        except Exception as e:   # noqa: BLE001
            log.warning("brain repo decisions: could not queue %s: %s", repo, type(e).__name__)

    async def startup(self) -> None:
        """One pass over every connected repo's chosen folders, for folders chosen before this ran."""
        try:
            if not self.active():
                return
            await self._seed()
            repos = await asyncio.to_thread(self.store.list_brain_repos)
            for r in repos:
                paths = [f["path"] for f in await asyncio.to_thread(self.store.list_brain_folders, r["repo"])]
                if self._held.get(r["repo"]):   # documents of a folder dropped while the cell was down
                    self._prune.add(r["repo"])
                    self._kick(r["repo"])
                self.request(r["repo"], paths)
        except asyncio.CancelledError:
            raise
        except Exception as e:   # noqa: BLE001
            log.warning("brain repo decisions: the start-up pass failed: %s", type(e).__name__)

    async def wait(self) -> None:
        """Wait until the start-up pass and every import have finished (for tests)."""
        if self.startup_task is not None:
            await asyncio.gather(self.startup_task, return_exceptions=True)
        while self._tasks:
            await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)
            await asyncio.sleep(0)

    # -- the import -------------------------------------------------------------------------

    async def _chosen(self, repo: str) -> list[str]:
        return [f["path"] for f in await asyncio.to_thread(self.store.list_brain_folders, repo)]

    async def import_folders(self, repo: str, wanted) -> int:
        """Import the markdown files of `wanted` folders of `repo` that are still chosen. Returns the
        number of documents put. Never raises for GitHub or lore trouble: it logs and stops."""
        async with self._lock:
            return await self._import(repo, set(wanted))

    async def import_changed(self, repo: str, paths) -> int:
        """Follow a push: the files `paths` names (None: every chosen folder) are imported again if the tree
        still has them and deleted from lore if it does not. Returns the number of documents put."""
        async with self._lock:
            return await self._import(repo, None, None if paths is None else set(paths))

    async def prune(self, repo: str) -> int:
        """Delete the documents of `repo` that are no longer under a chosen folder. Returns how many."""
        async with self._lock:
            if not self.active():
                return 0
            await self._known()
            chosen = await self._chosen(repo)
            stale = [p for p in self._held.get(repo, ()) if not self._under(p, chosen)]
            return await self._delete(repo, stale)

    async def _delete(self, repo: str, paths) -> int:
        """Delete the lore documents of `paths` (a document lore no longer has counts as deleted). A lore
        error is logged and ends the pass; what is left is tried again by the next pass. Returns how many."""
        paths = sorted(paths)
        if not paths or not self.active():
            return 0
        held, done = self._held.setdefault(repo, set()), 0
        try:
            async with self._lore() as lore:
                for p in paths:
                    ext = external_id(repo, p)
                    try:
                        await lore.delete_document(SOURCE, ext)
                    except LoreError as e:
                        if e.status != 404:
                            raise
                    held.discard(p)
                    if self._gone is not None:
                        self._gone.add((repo, p))
                    self._sent.pop(ext, None)
                    done += 1
        except asyncio.CancelledError:
            raise
        except LoreError as e:
            log.warning("brain repo decisions: could not delete a document of %s: %s", repo, _say(e))
        except Exception as e:   # noqa: BLE001
            log.warning("brain repo decisions: could not delete a document of %s: %s", repo, type(e).__name__)
        if done:
            log.info("brain repo decisions: %s: deleted %d documents", repo, done)
        return done

    async def _leave(self, repo: str, path: str) -> None:
        """The file no longer qualifies (too big, its blob gone, no usable commit date): the document lore holds
        for its earlier text goes, so the brain never answers from a version the repo no longer has."""
        if path in self._held.get(repo, ()):
            await self._delete(repo, [path])

    def _under(self, path: str, folders) -> bool:
        return any(path.startswith(f + "/") for f in folders)

    async def _import(self, repo: str, wanted: set[str] | None, only: set[str] | None = None) -> int:
        """Import the markdown files of the chosen folders in `wanted` (None: all of them); with `only`, just
        those paths. Documents whose file the tree no longer has are deleted (every one under the folders, or
        the ones in `only`), unless GitHub cut the tree short."""
        if not self.active():
            return 0
        folders = [f for f in await self._chosen(repo) if wanted is None or f in wanted]   # not trust the request: re-read
        if only is not None:   # a push that touched nothing in a chosen folder costs no GitHub call
            only = {p for p in only if self._under(p, folders)}
        if not folders or only == set():
            return 0
        await self._known()
        try:
            token = await self._token(repo)
        except asyncio.CancelledError:
            raise
        except ValueError as e:
            log.warning("brain repo decisions: cannot read %s: %s", repo, _say(e))
            return 0
        except Exception as e:   # noqa: BLE001
            log.warning("brain repo decisions: cannot read %s: %s", repo, type(e).__name__)
            return 0
        put = 0
        counts = {"large": 0, "undated": 0, "gone": 0}
        async with httpx.AsyncClient(transport=self.transport or TRANSPORT, timeout=TIMEOUT,
                                     headers=github_credentials._headers(token)) as gh:
            try:
                entries, truncated = await self._tree(gh, repo)
            except _Stop as e:
                log.warning("brain repo decisions: %s: %s", repo, e.why)
                return 0
            files = {}
            for e in entries:
                p = e["path"]
                if _is_markdown(p) and self._under(p, folders) and (only is None or p in only):
                    files[p] = e
            if not truncated:   # a file the tree no longer has is a deleted (or moved) file
                here = {e["path"] for e in entries}
                await self._delete(repo, [p for p in self._held.get(repo, ())
                                          if self._under(p, folders) and (only is None or p in only) and p not in here])
            names: dict = {}
            batch: list[tuple[dict, str]] = []
            stop: _Stop | None = None
            for path in sorted(files):
                entry = files[path]
                if entry["size"] is not None and entry["size"] > MAX_BYTES:
                    counts["large"] += 1
                    await self._leave(repo, path)
                    continue
                try:
                    facts = await self._facts(gh, repo, path, entry["sha"])
                except _Gone:
                    counts["gone"] += 1
                    await self._leave(repo, path)
                    continue
                except _Stop as e:
                    stop = e
                    break
                if facts["size_over"]:
                    counts["large"] += 1
                    await self._leave(repo, path)
                    continue
                for c in (facts.get("first"), facts.get("last")):
                    if c and c.get("login") and c["login"] not in names:
                        person = await asyncio.to_thread(self.store.find_person_by_github, c["login"])
                        names[c["login"]] = (person or {}).get("name") or None
                doc = build_document(repo, path, facts, {k: v for k, v in names.items() if v})
                if doc is None:
                    counts["undated"] += 1
                    await self._leave(repo, path)
                    continue
                digest = _digest(doc)
                if self._sent.get(doc["external_id"]) == digest:
                    continue   # put before and unchanged since
                batch.append((doc, digest))
                if len(batch) >= BATCH:
                    ok, n = await self._flush(repo, batch)
                    put += n
                    batch = []
                    if not ok:
                        return put
            if batch:
                ok, n = await self._flush(repo, batch)
                put += n
                if not ok:
                    return put
            if stop is not None:
                log.warning("brain repo decisions: %s: %s", repo, stop.why)
        skipped = ", ".join(f"{n} {k}" for k, n in counts.items() if n)
        log.info("brain repo decisions: %s: put %d documents%s", repo, put, f" (skipped: {skipped})" if skipped else "")
        if counts["undated"]:
            log.warning("brain repo decisions: %s: %d files have no usable commit date and were left out",
                        repo, counts["undated"])
        if counts["large"]:
            log.warning("brain repo decisions: %s: %d files are over %d bytes and were left out",
                        repo, counts["large"], MAX_BYTES)
        return put

    async def _flush(self, repo: str, batch: list[tuple[dict, str]]) -> tuple[bool, int]:
        """Put one batch of (document, digest). A document whose folder is no longer chosen is dropped
        first. Returns (carry on, documents put); a lore error is logged and ends the pass."""
        if not self.active():
            return False, 0
        chosen = await self._chosen(repo)
        batch = [b for b in batch if self._under(b[0]["external_id"][len(repo) + 1:], chosen)]
        if not batch:
            return True, 0
        try:
            async with self._lore() as lore:
                try:
                    await lore.put_documents(SOURCE, [b[0] for b in batch])
                except LoreError as e:
                    if e.status is None or e.status not in (400, 413, 422):
                        raise
                    if len(batch) == 1:
                        # the one document lore dislikes stays out; the pass carries on, as it does inside a bigger batch
                        log.warning("brain repo decisions: lore refused %s: %s", batch[0][0]["external_id"], _say(e))
                        return True, 0
                    # lore refused the batch for one document it dislikes: put them one at a time, so only that one stays out
                    log.warning("brain repo decisions: lore refused a batch of %d documents of %s (%s); putting them one at a time",
                                len(batch), repo, _say(e))
                    kept = []
                    for item in batch:
                        try:
                            await lore.put_documents(SOURCE, [item[0]])
                        except LoreError as one:
                            if one.status not in (400, 413, 422):
                                raise
                            log.warning("brain repo decisions: lore refused %s: %s", item[0]["external_id"], _say(one))
                        else:
                            kept.append(item)
                    batch = kept
        except LoreError as e:
            log.warning("brain repo decisions: could not put %d documents of %s: %s", len(batch), repo, _say(e))
            return False, 0
        except asyncio.CancelledError:
            raise
        except Exception as e:   # noqa: BLE001
            log.warning("brain repo decisions: could not put %d documents of %s: %s", len(batch), repo, type(e).__name__)
            return False, 0
        held = self._held.setdefault(repo, set())
        for doc, digest in batch:
            self._sent[doc["external_id"]] = digest
            held.add(doc["external_id"][len(repo) + 1:])
        return True, len(batch)

    # -- GitHub -----------------------------------------------------------------------------

    @staticmethod
    def _limited(r: httpx.Response) -> bool:
        if r.status_code == 429:
            return True
        if r.status_code != 403:
            return False
        if r.headers.get("x-ratelimit-remaining") == "0" or r.headers.get("retry-after"):
            return True
        return "rate limit" in r.text[:500].lower()

    async def _get(self, gh: httpx.AsyncClient, repo: str, route: str, params: dict | None = None) -> httpx.Response:
        """One GitHub call. A rate limit or a network or server failure raises _Stop; the caller decides what
        else a status means."""
        self.calls += 1
        try:
            r = await gh.get(f"{github_credentials.API_DEFAULT}/repos/{repo}/{route}", params=params)
        except asyncio.CancelledError:
            raise
        except Exception as e:   # noqa: BLE001
            raise _Stop(f"GitHub could not be reached ({type(e).__name__})")
        if self._limited(r):
            raise _Stop("GitHub's rate limit is used up, the rest waits for the next pass")
        if r.status_code in (401, 403):
            raise _Stop(f"GitHub refused the token for this repo ({r.status_code})")
        if r.status_code >= 500:
            raise _Stop(f"GitHub answered {r.status_code}")
        return r

    async def _tree(self, gh, repo: str) -> tuple[list[dict], bool]:
        """`(blobs, truncated)`: the files of the repo's default branch, and whether GitHub cut the list short."""
        r = await self._get(gh, repo, "git/trees/HEAD", {"recursive": "1"})
        if r.status_code == 409:   # an empty repo
            return [], False
        if r.status_code != 200:
            raise _Stop(f"GitHub answered {r.status_code} for the tree")
        try:
            body = r.json()
        except ValueError:
            raise _Stop("GitHub sent a tree that is not JSON")
        if not isinstance(body, dict):
            raise _Stop("GitHub sent a tree that is not JSON")
        truncated = bool(body.get("truncated"))
        if truncated:
            log.warning("brain repo decisions: GitHub cut the tree of %s short; only the files it sent are imported and no document is deleted", repo)
        out = []
        for e in body.get("tree") or []:
            if isinstance(e, dict) and e.get("type") == "blob" and isinstance(e.get("path"), str) \
                    and isinstance(e.get("sha"), str):
                size = e.get("size")
                out.append({"path": e["path"], "sha": e["sha"], "size": size if isinstance(size, int) else None})
        return out, truncated

    def _entry(self, key: tuple) -> dict:
        got = self._cache.get(key)
        if got is None:
            got = self._cache[key] = {}
        else:
            self._cache.move_to_end(key)
        return got

    def _evict(self) -> None:
        while self._cache and (len(self._cache) > MAX_CACHE or self._cache_bytes > MAX_CACHE_BYTES):
            _k, old = self._cache.popitem(last=False)
            self._cache_bytes -= len(old.get("text") or "")

    async def _facts(self, gh, repo: str, path: str, sha: str) -> dict:
        """What GitHub knows of the file: text, the first and last commit and the first commit's pull
        request. Kept per (repo, path, blob sha); each part is asked for only when it is not kept yet, so a
        pass that stopped half way carries on where it was."""
        key = (repo, path, sha)
        e = self._entry(key)
        if e.get("gone"):
            raise _Gone()   # GitHub did not have it for this blob before; a new blob sha asks again
        try:
            if "size_over" not in e:
                text = await self._blob(gh, repo, sha)
                e["size_over"] = text is None
                e["text"] = text or ""
                self._cache_bytes += len(e["text"])
            if e["size_over"]:
                return e
            if "last" not in e:
                e["last"], e["first"] = await self._ends(gh, repo, path)
            if "pr" not in e:
                e["pr"] = await self._pr(gh, repo, e["first"]["sha"]) if e["first"] else None
        except _Gone:
            self._cache_bytes -= len(e.get("text") or "")
            e.clear()
            e["gone"] = True
            raise
        finally:
            self._evict()
        return e

    async def _blob(self, gh, repo: str, sha: str) -> str | None:
        """The text of a blob, or None when it is over MAX_BYTES."""
        r = await self._get(gh, repo, f"git/blobs/{sha}")
        if r.status_code in (404, 409):
            raise _Gone()
        if r.status_code != 200:
            raise _Stop(f"GitHub answered {r.status_code} for a file")
        try:
            b = r.json()
            if b.get("encoding") != "base64":
                raise _Gone()
            if isinstance(b.get("size"), int) and b["size"] > MAX_BYTES:
                return None
            raw = base64.b64decode(b.get("content") or "")
        except _Gone:
            raise
        except (ValueError, AttributeError):
            raise _Gone()
        if len(raw) > MAX_BYTES:
            return None
        return raw.decode("utf-8", errors="replace")

    async def _ends(self, gh, repo: str, path: str) -> tuple[dict | None, dict | None]:
        """(last commit, first commit) of the file, each as `_commit_view`. The first is the one on the last
        page of a one-per-page list (the Link header says which page); no Link header means one commit."""
        r = await self._get(gh, repo, "commits", {"path": path, "per_page": "1"})
        if r.status_code in (404, 409, 422):
            raise _Gone()
        if r.status_code != 200:
            raise _Stop(f"GitHub answered {r.status_code} for the history of a file")
        try:
            rows = r.json()
        except ValueError:
            raise _Stop("GitHub sent a history that is not JSON")
        last = _commit_view(rows[0]) if isinstance(rows, list) and rows else None
        if last is None:
            raise _Gone()   # no commit touches it: nothing to say about who wrote it
        first = last
        m = _LAST.search(r.headers.get("link") or "")
        page = (parse_qs(urlparse(m.group(1)).query).get("page") or [""])[0] if m else ""
        if page.isdigit() and int(page) > 1:
            r2 = await self._get(gh, repo, "commits", {"path": path, "per_page": "1", "page": page})
            if r2.status_code == 200:
                try:
                    rows2 = r2.json()
                except ValueError:
                    rows2 = None
                got = _commit_view(rows2[0]) if isinstance(rows2, list) and rows2 else None
                first = got or last
            elif r2.status_code not in (404, 409, 422):
                raise _Stop(f"GitHub answered {r2.status_code} for the history of a file")
        return last, first

    async def _pr(self, gh, repo: str, sha: str) -> str | None:
        """The URL of the pull request of a commit: the first merged one, else the first, else None."""
        r = await self._get(gh, repo, f"commits/{sha}/pulls")
        if r.status_code in (404, 409, 422):
            return None
        if r.status_code != 200:
            raise _Stop(f"GitHub answered {r.status_code} for a pull request")
        try:
            rows = [p for p in r.json() if isinstance(p, dict) and isinstance(p.get("html_url"), str)]
        except (ValueError, TypeError):
            return None
        pick = next((p for p in rows if p.get("merged_at")), rows[0] if rows else None)
        return pick["html_url"] if pick else None

    # -- wiring -----------------------------------------------------------------------------

    def attach(self, app) -> None:
        """Hook into the folder changes and run the start-up pass with the app; both end with it."""
        if getattr(app.state, "brain_repo_decisions", None) is not None:
            return
        app.state.brain_repo_decisions = self
        outer = app.router.lifespan_context

        @contextlib.asynccontextmanager
        async def lifespan(a):
            async with outer(a) as state:
                folders_mod.HOOKS.append(self.hook)
                self.startup_task = asyncio.create_task(self.startup())
                try:
                    yield state
                finally:
                    with contextlib.suppress(ValueError):
                        folders_mod.HOOKS.remove(self.hook)
                    tasks = [self.startup_task, *self._tasks.values()]
                    self.startup_task = None
                    for t in tasks:
                        if t is not None:
                            t.cancel()
                    await asyncio.gather(*[t for t in tasks if t is not None], return_exceptions=True)
                    self._tasks.clear()
                    self._pending.clear()
                    self._touched.clear()
                    self._prune.clear()

        app.router.lifespan_context = lifespan
