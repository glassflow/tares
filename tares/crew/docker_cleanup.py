"""Docker upkeep on the crew machine (TR-700): a step of the watch, no model call.

Crew test runs and builds leave Docker data behind (old images, build cache, stopped containers) and nobody
owns it; a full Docker disk fails every builder's tests at once. Once a day the watch removes:

  * stopped containers that ended more than 24 hours ago (never a running or paused one);
  * build cache older than 24 hours (`docker builder prune --filter until=24h`);
  * dangling images no container uses;
  * tagged images no container uses, not on the keep list, and not seen in use for 7 days.

Docker keeps no "last used" time for an image, so the watch keeps one: every round it notes the images that
running containers use, with the time (`docker-seen.json` in the crew home). An image it has never seen in use
counts from the day it started noting (`since`), so nothing tagged goes in the first week, and nothing at all
goes until the watch has been noting for a day (a crew home made for a test never gets there). Volumes are never
removed. Removing an image some container started using meanwhile fails in Docker and is skipped.

It runs only while it holds the factory-heavy lock (taken without waiting), so a running test never loses its
image and no heavy command starts in the middle of a cleanup. Before a heavy command runs, `before_heavy` checks
the free space on the disk that holds the home folder: below 10 GB it cleans up at once, and when that is not
enough it says so and puts one item on the person's desk. (The free space is the host's: OrbStack grows its disk
on the host; a fixed-size Docker Desktop disk is not measured.)

The crew settings on Tares turn it off (`docker_cleanup: false`; on unless false) and name images to keep
(`docker_keep`: names such as `supabase/postgres` for every tag, `myimage:1.2` for one tag, `*` as a wildcard).
Settings that can't be read mean no cleanup this round. `tares factory doctor` shows Docker's disk use and what
the next cleanup would remove (`preview`).

Python only: the bash watch does not do this.
"""
from __future__ import annotations

import fnmatch
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone

from . import heavy, state
from .client import TaresClient
from .errors import CrewError
from .paths import ME, CrewPaths, default_home

DAY = 86400
EVERY = DAY                 # seconds between cleanups
STOPPED_AFTER = DAY         # a stopped container is removed this long after it ended
UNUSED_AFTER = 7 * DAY      # a tagged image is removed when not seen in use for this long
LOW_FREE = 10 * 1024 ** 3   # bytes free below which a heavy command cleans up first
SEEN_FILE = "docker-seen.json"
LAST_FILE = "docker-cleanup.last"
RUN_TIMEOUT = 600
LOW_QUESTION = "The crew machine is low on disk space."
STOPPED_STATES = ("exited", "dead", "created")


# ---------- settings ----------


def settings(raw: dict) -> tuple[bool, list]:
    """(on, keep) from the crew settings on Tares: on unless docker_cleanup is false."""
    keep = raw.get("docker_keep")
    keep = [str(k) for k in keep if str(k).strip()] if isinstance(keep, list) else []
    return raw.get("docker_cleanup") is not False, keep


def kept(tags: list, keep: list) -> bool:
    """True when one of the image's tags matches the keep list. A name with no tag keeps every tag."""
    for tag in tags:
        repo = tag.rsplit(":", 1)[0] if ":" in tag.rsplit("/", 1)[-1] else tag
        for k in keep:
            if fnmatch.fnmatchcase(tag, k) or (":" not in k.rsplit("/", 1)[-1] and fnmatch.fnmatchcase(repo, k)):
                return True
    return False


# ---------- what Docker holds ----------


def docker_bin(env: dict) -> str | None:
    return env.get("FACTORY_DOCKER") or shutil.which("docker", path=env.get("PATH"))


def _docker(binary: str, args: list, timeout: float = 60) -> tuple[int, str]:
    try:
        p = subprocess.run([binary, *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return p.returncode, p.stdout


def _epoch(text) -> float | None:
    """A Docker time (RFC 3339 with nanoseconds) as epoch seconds; None for the zero time or garbage."""
    if not isinstance(text, str) or not text or text.startswith("0001-"):
        return None
    t = text.rstrip("Z")
    if "." in t:
        head, frac = t.split(".", 1)
        tz = ""
        for sign in ("+", "-"):
            if sign in frac:
                frac, tz = frac.split(sign, 1)
                tz = sign + tz
        t = f"{head}.{frac[:6]}{tz}"
    try:
        d = datetime.fromisoformat(t)
    except ValueError:
        return None
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()


def snapshot(binary: str) -> dict | None:
    """The containers and images Docker has; None when Docker does not answer."""
    rc, out = _docker(binary, ["ps", "-a", "-q", "--no-trunc"])
    if rc != 0:
        return None
    ids = out.split()
    containers = []
    if ids:
        rc, out = _docker(binary, ["container", "inspect", *ids])
        try:
            rows = json.loads(out) if rc == 0 else []
        except ValueError:
            rows = []
        for r in rows:
            st = r.get("State") or {}
            containers.append({"id": r.get("Id", ""), "name": str(r.get("Name") or "").lstrip("/"),
                               "image": r.get("Image", ""), "state": st.get("Status", ""),
                               "ended": _epoch(st.get("FinishedAt")) or _epoch(r.get("Created"))})
    rc, out = _docker(binary, ["image", "ls", "-q", "--no-trunc"])
    if rc != 0:
        return None
    images = []
    ids = sorted(set(out.split()))
    if ids:
        rc, out = _docker(binary, ["image", "inspect", *ids])
        try:
            rows = json.loads(out) if rc == 0 else []
        except ValueError:
            rows = []
        for r in rows:
            tags = [t for t in (r.get("RepoTags") or []) if t and t != "<none>:<none>"]
            images.append({"id": r.get("Id", ""), "tags": tags, "size": int(r.get("Size") or 0)})
    return {"containers": containers, "images": images}


# ---------- when an image was last in use ----------


def _seen_path(paths: CrewPaths):
    return paths.home / SEEN_FILE


def read_seen(paths: CrewPaths, now: float) -> dict:
    doc = state.read_json(_seen_path(paths), None)
    if not isinstance(doc, dict) or not isinstance(doc.get("images"), dict):
        doc = {"since": now, "images": {}}
    if not isinstance(doc.get("since"), (int, float)):
        doc["since"] = now
    return doc


def record_seen(paths: CrewPaths, snap: dict, now: float) -> dict:
    """Note the images running containers use now; drop images Docker no longer has."""
    doc = read_seen(paths, now)
    have = {i["id"] for i in snap["images"]}
    seen = {k: v for k, v in doc["images"].items() if k in have}
    for c in snap["containers"]:
        if c["state"] in ("running", "paused", "restarting") and c["image"]:
            seen[c["image"]] = now
    doc["images"] = seen
    state.write_json(_seen_path(paths), doc)
    return doc


# ---------- what goes ----------


def plan(snap: dict, seen: dict, keep: list, now: float) -> dict:
    """What a cleanup now removes: {"containers": [...], "images": [...]}; build cache is pruned by Docker."""
    gone = [c for c in snap["containers"]
            if c["state"] in STOPPED_STATES and c["ended"] is not None and now - c["ended"] >= STOPPED_AFTER]
    gone_ids = {c["id"] for c in gone}
    used = {c["image"] for c in snap["containers"] if c["id"] not in gone_ids}
    images = []
    for i in snap["images"]:
        if i["id"] in used or kept(i["tags"], keep):
            continue
        last = seen["images"].get(i["id"], seen["since"])
        if not i["tags"] or now - last >= UNUSED_AFTER:
            images.append(i)
    return {"containers": gone, "images": images}


def _gb(n: float) -> str:
    return f"{n / 1024 ** 3:.1f} GB"


def describe(p: dict) -> str:
    """One line: what the plan removes."""
    parts = []
    if p["containers"]:
        parts.append(f"{len(p['containers'])} stopped container(s)")
    if p["images"]:
        names = [i["tags"][0] if i["tags"] else "untagged" for i in p["images"]]
        shown = ", ".join(names[:5]) + (f" and {len(names) - 5} more" if len(names) > 5 else "")
        parts.append(f"{len(p['images'])} unused image(s), {_gb(sum(i['size'] for i in p['images']))} ({shown})")
    parts.append("build cache older than a day")
    return "; ".join(parts)


def remove(binary: str, p: dict) -> dict:
    """Carry out the plan; what was removed. A removal Docker refuses is skipped."""
    out = {"containers": 0, "images": 0, "bytes": 0, "cache": ""}
    for c in p["containers"]:
        if _docker(binary, ["rm", c["id"]], RUN_TIMEOUT)[0] == 0:
            out["containers"] += 1
    for i in p["images"]:
        # every tag of an image goes with its id; without -f Docker refuses an image a container uses
        if _docker(binary, ["image", "rm", i["id"]], RUN_TIMEOUT)[0] == 0:
            out["images"] += 1
            out["bytes"] += i["size"]
    rc, text = _docker(binary, ["builder", "prune", "--force", "--filter", "until=24h"], RUN_TIMEOUT)
    if rc == 0:
        for line in text.splitlines():
            if line.lower().startswith("total"):
                out["cache"] = line.split(":", 1)[-1].strip()
    return out


def summary(done: dict) -> str:
    cache = f", build cache {done['cache']}" if done["cache"] else ""
    return (f"removed {done['containers']} stopped container(s), {done['images']} unused image(s) "
            f"({_gb(done['bytes'])}){cache}")


# ---------- the lock ----------


def _take_lock(env: dict) -> str | None:
    """The heavy lock, taken without waiting; None when a heavy command (or another cleanup) holds it."""
    lock = heavy.lock_dir(env)
    try:
        os.mkdir(lock)
    except FileExistsError:
        return None
    except OSError:
        return None
    heavy._write(os.path.join(lock, "pid"), f"{os.getpid()}\n")
    heavy._write(os.path.join(lock, "what"), "docker cleanup (crew watch)\n")
    return lock


# ---------- the disk ----------


def free_bytes(paths: CrewPaths) -> int | None:
    try:
        return shutil.disk_usage(os.path.expanduser("~")).free
    except OSError:
        return None


def _ask_person(client: TaresClient, free: int) -> None:
    """One open desk item about the disk, however many times it is found low."""
    try:
        if any(d.get("question") == LOW_QUESTION for d in client.desk_json()):
            return
        client.tares_http("POST", "/api/desk", {
            "question": LOW_QUESTION,
            "recommendation": "Free some space on it, or add names to keep to docker_keep only if they must stay.",
            "why": (f"Only {_gb(free)} is free after the crew's Docker cleanup. A full disk fails every "
                    "builder's tests. Run docker system df to see what Docker holds; volumes and your own "
                    "files are never removed by the crew."),
            "asked_by": "the crew watch"})
    except CrewError:
        pass


# ---------- the watch step and the heavy check ----------


def _last(paths: CrewPaths) -> float:
    try:
        return float(state.read_text(paths.home / LAST_FILE).strip() or 0)
    except (OSError, ValueError):
        return 0.0


def clean(paths: CrewPaths, binary: str, keep: list, now: float) -> dict | None:
    """One cleanup, the lock already held: what was removed, or None when Docker does not answer or the watch
    has been noting image use for less than a day."""
    snap = snapshot(binary)
    if snap is None:
        return None
    seen = record_seen(paths, snap, now)
    if now - seen["since"] < EVERY:
        return None
    done = remove(binary, plan(snap, seen, keep, now))
    state.atomic_write_text(paths.home / LAST_FILE, f"{int(now)}\n")
    state.event("docker-cleanup", "", summary(done), paths=paths)
    return done


def step(paths: CrewPaths, env=None, now=None) -> None:
    """The watch's round: note the images in use; once a day, clean up when no heavy command runs."""
    env = dict(os.environ if env is None else env)
    now = time.time() if now is None else now
    binary = docker_bin(env)
    if not binary:
        return
    client = TaresClient(paths, env)
    try:
        if not client.tares_on():
            return
        try:
            on, keep = settings(client.settings_raw())
        except CrewError:
            return
        if not on:
            return
        snap = snapshot(binary)
        if snap is None:
            return   # Docker is not running: nothing to note or clean
        seen = record_seen(paths, snap, now)
        if now - max(_last(paths), seen["since"]) < EVERY:
            return   # once a day, and not before the watch has noted a day of use
        lock = _take_lock(env)
        if lock is None:
            return   # a heavy command runs; the next round tries again
        try:
            done = clean(paths, binary, keep, now)
        finally:
            heavy._release(lock)
        if done is not None:
            print(f"{ME}: docker cleanup: {summary(done)}", file=sys.stderr, flush=True)
        free = free_bytes(paths)
        if free is not None and free < LOW_FREE:
            _ask_person(client, free)
    finally:
        client.close()


def before_heavy(env: dict, now=None) -> None:
    """In factory-heavy, the lock held: with less than 10 GB free, clean up now; still low, say so and ask the
    person. Never stops the heavy command."""
    try:
        paths = CrewPaths(env.get("FACTORY_CREW_HOME") or env.get("FACTORY_HOME") or default_home())
        free = free_bytes(paths)
        if free is None or free >= LOW_FREE:
            return
        binary = docker_bin(env)
        client = TaresClient(paths, env)
        try:
            on, keep = (False, [])
            if client.tares_on():
                try:
                    on, keep = settings(client.settings_raw())
                except CrewError:
                    pass
            if on and binary and paths.home.is_dir():
                done = clean(paths, binary, keep, time.time() if now is None else now)
                if done is not None:
                    print(f"factory-heavy: low on disk, so the crew cleaned up Docker first: {summary(done)}",
                          file=sys.stderr)
                free = free_bytes(paths) or free
            if free < LOW_FREE:
                print(f"factory-heavy: only {_gb(free)} free on this machine; the command may fail for want of "
                      "space", file=sys.stderr)
                _ask_person(client, free)
        finally:
            client.close()
    except Exception as e:   # the check must never stop the command it guards
        print(f"factory-heavy: the disk check failed ({type(e).__name__}); running the command anyway",
              file=sys.stderr)


def onboarding_note(env: dict) -> str | None:
    """What crew up says once about the cleanup: only where the Python watch runs and Docker is installed."""
    from . import dispatch
    if not docker_bin(env) or dispatch.route(["crew", "watch"], env)[0] != "python":
        return None
    return ("  ✓ once a day the watch also removes Docker's leftovers (old stopped containers, build cache, "
            "images unused for 7 days; never volumes); the crew setting docker_cleanup turns it off")


def preview(paths: CrewPaths, env: dict, raw: dict, now=None) -> list:
    """For doctor: (id, status, text, fix) lines about Docker's disk use and the next cleanup. Empty without Docker."""
    binary = docker_bin(env)
    if not binary:
        return []
    now = time.time() if now is None else now
    snap = snapshot(binary)
    if snap is None:
        return []
    on, keep = settings(raw)
    rc, out = _docker(binary, ["system", "df", "--format", "{{.Type}}: {{.Size}} ({{.Reclaimable}} reclaimable)"])
    use = "; ".join(out.split("\n")).strip("; ") if rc == 0 else ""
    lines = [("docker_disk", "ok", f"docker disk use: {use}", "")] if use else []
    if not on:
        lines.append(("docker_cleanup", "warn", "crew setting docker_cleanup is off: nothing cleans up Docker's "
                      "leftovers", "turn docker_cleanup on in the crew settings"))
        return lines
    seen = read_seen(paths, now)   # read only: doctor does not start the clock
    if now - seen["since"] < EVERY:
        lines.append(("docker_cleanup", "ok", "docker cleanup starts once the watch has noted a day of image use",
                      ""))
        return lines
    lines.append(("docker_cleanup", "ok", f"the next docker cleanup removes: {describe(plan(snap, seen, keep, now))}", ""))
    free = free_bytes(paths)
    if free is not None and free < LOW_FREE:
        lines.append(("docker_free", "warn", f"only {_gb(free)} free on this machine; heavy commands clean up "
                      "Docker first", "free some disk space"))
    return lines
