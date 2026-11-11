"""tares.crew.docker_cleanup: the watch's daily Docker cleanup and the disk check before a heavy command (TR-700).

Docker is a fake here (docker_cleanup._docker is replaced), so the tests never touch the machine's real Docker.

Run: python tests/test_crew_docker_cleanup.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tares import factory  # noqa: E402
from tares.crew import docker_cleanup as dc, heavy, watch  # noqa: E402
from tares.crew.paths import CrewPaths  # noqa: E402
from tares.docs import DocError  # noqa: E402

P = F = 0
DAY = 86400
NOW = 1_800_000_000.0


def check(label, ok, detail=""):
    global P, F
    if ok:
        P += 1
        print(f"  ok   {label}")
    else:
        F += 1
        print(f"  FAIL {label}  {detail}")


def iso(t):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.123456789Z")


class FakeDocker:
    """Containers and images as `docker inspect` gives them; every command it was asked is kept."""

    def __init__(self, containers, images):
        self.containers = containers
        self.images = images
        self.calls = []

    def __call__(self, binary, args, timeout=60):
        self.calls.append(args)
        if args[:1] == ["ps"]:
            return 0, "\n".join(c["Id"] for c in self.containers)
        if args[:2] == ["container", "inspect"]:
            return 0, json.dumps([c for c in self.containers if c["Id"] in args[2:]])
        if args[:2] == ["image", "ls"]:
            return 0, "\n".join(i["Id"] for i in self.images)
        if args[:2] == ["image", "inspect"]:
            return 0, json.dumps([i for i in self.images if i["Id"] in args[2:]])
        if args[:1] == ["rm"]:
            self.containers = [c for c in self.containers if c["Id"] != args[1]]
            return 0, ""
        if args[:2] == ["image", "rm"]:
            if any(c["Image"] == args[2] for c in self.containers):
                return 1, ""
            self.images = [i for i in self.images if i["Id"] != args[2]]
            return 0, ""
        if args[:2] == ["builder", "prune"]:
            return 0, "Total:\t5.7GB\n"
        if args[:2] == ["system", "df"]:
            return 0, "Images: 23GB (20GB reclaimable)\nBuild Cache: 5.7GB (5.7GB reclaimable)"
        return 1, ""

    def removed(self):
        return ([a[1] for a in self.calls if a[:1] == ["rm"]], [a[2] for a in self.calls if a[:2] == ["image", "rm"]],
                any(a[:2] == ["builder", "prune"] for a in self.calls))


def container(cid, image, status, ended):
    return {"Id": cid, "Name": "/" + cid, "Image": image, "Created": iso(ended - 60),
            "State": {"Status": status, "FinishedAt": iso(ended) if status != "running" else "0001-01-01T00:00:00Z"}}


def image(iid, tags, size=1024 ** 3):
    return {"Id": iid, "RepoTags": tags, "Size": size}


def machine():
    return FakeDocker(
        [container("run1", "img-running", "running", NOW),
         container("old1", "img-old-stopped", "exited", NOW - 3 * DAY),
         container("new1", "img-new-stopped", "exited", NOW - 3600)],
        [image("img-running", ["app:1"]), image("img-old-stopped", ["demo:old"]),
         image("img-new-stopped", ["review:1"]), image("img-unused", ["supabase/postgres:15.1"]),
         image("img-dangling", []), image("img-kept", ["electron-builder:24"]),
         image("img-recent", ["fresh:1"])])


class Client:
    def __init__(self, raw=None, on=True):
        self.raw = {} if raw is None else raw
        self.on = on
        self.desk = []
        self.posted = []

    def tares_on(self):
        return self.on

    def settings_raw(self):
        return self.raw

    def desk_json(self):
        return self.desk

    def tares_http(self, method, path, body=None):
        self.posted.append((method, path, body))
        self.desk.append({"question": body["question"]})
        return "{}"

    def close(self):
        pass


def setup(raw=None, seen_since=NOW - 10 * DAY, recent_seen=True):
    tmp = Path(tempfile.mkdtemp())
    home = tmp / "factory"
    home.mkdir()
    paths = CrewPaths.at(home)
    seen = {"since": seen_since, "images": {}}
    if recent_seen:
        seen["images"]["img-recent"] = NOW - 2 * DAY
    (home / dc.SEEN_FILE).write_text(json.dumps(seen), encoding="utf-8")
    fake = machine()
    client = Client(raw)
    env = {"TMPDIR": str(tmp), "FACTORY_DOCKER": "docker", "FACTORY_CREW_HOME": str(home)}
    return paths, fake, client, env


def run_step(paths, fake, client, env, now=NOW, free=100 * 1024 ** 3):
    saved = dc._docker, dc.TaresClient, dc.free_bytes
    try:
        dc._docker = fake
        dc.TaresClient = lambda p, e: client
        dc.free_bytes = lambda p: free
        dc.step(paths, env, now=now)
    finally:
        dc._docker, dc.TaresClient, dc.free_bytes = saved


print("== settings ==")
check("on unless docker_cleanup is false", dc.settings({})[0] and dc.settings({"docker_cleanup": None})[0]
      and not dc.settings({"docker_cleanup": False})[0])
check("the keep list matches a name for every tag, a name with a tag, and wildcards",
      dc.kept(["supabase/postgres:15.1"], ["supabase/postgres"]) and dc.kept(["myimage:1.2"], ["myimage:1.2"])
      and not dc.kept(["myimage:1.3"], ["myimage:1.2"]) and dc.kept(["ghcr.io/x/y:2"], ["ghcr.io/x/*"])
      and not dc.kept(["supabase/postgres-meta:1"], ["supabase/postgres"]))
check("a registry port is not a tag", dc.kept(["localhost:5000/app:1"], ["localhost:5000/app"]))
try:
    factory.docker_keep_ok(["ok/name", "bad name"])
    check("a name with a space is refused", False)
except DocError:
    check("a name with a space is refused", True)
check("docker_keep is saved trimmed and without repeats",
      factory.docker_keep_ok([" a/b ", "a/b", "", "c:1"]) == ["a/b", "c:1"])
s = factory.crew_settings_ok({"docker_cleanup": False, "docker_keep": ["x"]}, {})
check("crew settings take docker_cleanup and docker_keep", s["docker_cleanup"] is False and s["docker_keep"] == ["x"])
check("both start unset", factory.CREW_DEFAULTS["docker_cleanup"] is None and factory.CREW_DEFAULTS["docker_keep"] is None)
check("it is a step of the watch", "docker" in watch.STEP_NAMES and watch._STEPS.get("docker") is dc.step)

print("== the daily cleanup ==")
paths, fake, client, env = setup({"docker_keep": ["electron-builder"]})
run_step(paths, fake, client, env)
rm_c, rm_i, pruned = fake.removed()
check("a container stopped 3 days ago goes", rm_c == ["old1"], rm_c)
check("the unused image, the dangling one and the stopped container's image go",
      sorted(rm_i) == ["img-dangling", "img-old-stopped", "img-unused"], rm_i)
check("build cache older than a day is pruned", pruned)
check("an image a running container uses stays", "img-running" not in rm_i)
check("an image a container stopped an hour ago uses stays, and so does that container",
      "img-new-stopped" not in rm_i and "new1" not in rm_c)
check("an image on the keep list stays", "img-kept" not in rm_i)
check("an image seen in use 2 days ago stays", "img-recent" not in rm_i)
check("no volume is touched", not any("volume" in a for a in fake.calls))
ev = [json.loads(line) for line in paths.events.read_text(encoding="utf-8").splitlines()]
check("the event log says what went", ev[-1]["event"] == "docker-cleanup" and "3 unused image(s)" in ev[-1]["detail"],
      ev[-1])
seen = json.loads((paths.home / dc.SEEN_FILE).read_text(encoding="utf-8"))
check("the running container's image is noted in use", seen["images"].get("img-running") == NOW)
check("the next round, the same day, does nothing", (fake.calls.clear(), run_step(paths, fake, client, env, NOW + 3600),
                                                      fake.removed())[2] == ([], [], False))
_, _, again = None, None, None
run_step(paths, fake, client, env, NOW + DAY + 1)
check("a day later it runs again", fake.removed()[2])

print("== when it does nothing ==")
paths, fake, client, env = setup({"docker_cleanup": False})
run_step(paths, fake, client, env)
check("with the setting off nothing is removed", fake.removed() == ([], [], False))
paths, fake, client, env = setup()
os.mkdir(heavy.lock_dir(env))
(Path(heavy.lock_dir(env)) / "pid").write_text(f"{os.getpid()}\n")
run_step(paths, fake, client, env)
check("nothing runs while a heavy command holds the lock", fake.removed() == ([], [], False))
check("and the lock is still the heavy command's", Path(heavy.lock_dir(env)).is_dir())
paths, fake, client, env = setup()
run_step(paths, fake, client, env)
check("the cleanup gives the heavy lock back", not Path(heavy.lock_dir(env)).exists())
paths, fake, client, env = setup(seen_since=NOW - 3600)
run_step(paths, fake, client, env)
check("nothing goes before the watch has noted a day of use", fake.removed() == ([], [], False))
paths, fake, client, env = setup()
(paths.home / dc.SEEN_FILE).unlink()
run_step(paths, fake, client, env)
check("a crew home with no record yet starts the record and removes nothing",
      fake.removed() == ([], [], False) and (paths.home / dc.SEEN_FILE).is_file())
paths, fake, client, env = setup(seen_since=NOW - 3 * DAY, recent_seen=False)
run_step(paths, fake, client, env)
check("a tagged image is kept for 7 days from the start of the record",
      "img-unused" not in fake.removed()[1] and "img-dangling" in fake.removed()[1])
paths, fake, client, env = setup()
client.on = False
run_step(paths, fake, client, env)
check("without Tares (no settings) nothing is removed", fake.removed() == ([], [], False))
paths, fake, client, env = setup()
run_step(paths, fake, client, {**env, "FACTORY_DOCKER": "", "PATH": "/nonexistent"})
check("without Docker it does nothing", fake.calls == [])

print("== low disk ==")
paths, fake, client, env = setup()
run_step(paths, fake, client, env, free=2 * 1024 ** 3)
check("still low after the cleanup: one item on the person's desk",
      len(client.posted) == 1 and client.posted[0][2]["question"] == dc.LOW_QUESTION)
run_step(paths, fake, client, env, NOW + DAY + 1, free=2 * 1024 ** 3)
check("only one, however often it is found low", len(client.posted) == 1)


def run_heavy(paths, fake, client, env, free):
    saved = dc._docker, dc.TaresClient, dc.free_bytes
    frees = list(free)
    try:
        dc._docker = fake
        dc.TaresClient = lambda p, e: client
        dc.free_bytes = lambda p: frees.pop(0) if len(frees) > 1 else frees[0]
        dc.before_heavy(env, now=NOW)
    finally:
        dc._docker, dc.TaresClient, dc.free_bytes = saved


paths, fake, client, env = setup()
run_heavy(paths, fake, client, env, [50 * 1024 ** 3])
check("before a heavy command with enough disk: nothing", fake.calls == [] and client.posted == [])
paths, fake, client, env = setup()
run_heavy(paths, fake, client, env, [2 * 1024 ** 3, 30 * 1024 ** 3])
check("low before a heavy command: it cleans up first", "img-unused" in fake.removed()[1] and client.posted == [])
paths, fake, client, env = setup({"docker_cleanup": False})
run_heavy(paths, fake, client, env, [2 * 1024 ** 3])
check("low with the setting off: nothing removed, the person is asked",
      fake.removed() == ([], [], False) and len(client.posted) == 1)
paths, fake, client, env = setup()


def boom(*a, **k):
    raise RuntimeError("x")


saved = dc.free_bytes
dc.free_bytes = boom
try:
    dc.before_heavy(env)
    check("a check that fails never stops the heavy command", True)
except Exception as e:  # noqa: BLE001
    check("a check that fails never stops the heavy command", False, repr(e))
finally:
    dc.free_bytes = saved

print("== doctor ==")
paths, fake, client, env = setup({"docker_keep": ["electron-builder"]})
saved = dc._docker, dc.free_bytes
dc._docker, dc.free_bytes = fake, (lambda p: 100 * 1024 ** 3)
try:
    lines = dc.preview(paths, env, {"docker_keep": ["electron-builder"]}, now=NOW)
    off = dc.preview(paths, env, {"docker_cleanup": False}, now=NOW)
    early = dc.preview(setup(seen_since=NOW - 60)[0], env, {}, now=NOW)
finally:
    dc._docker, dc.free_bytes = saved
text = " | ".join(t for _, _, t, _ in lines)
check("doctor shows Docker's disk use", "docker disk use: Images: 23GB" in text, text)
check("doctor lists what the next cleanup removes, before it goes",
      "1 stopped container(s)" in text and "3 unused image(s)" in text and "supabase/postgres:15.1" in text
      and fake.removed() == ([], [], False), text)
check("doctor warns when the setting is off", any(s == "warn" and "off" in t for _, s, t, _ in off))
check("doctor says when cleanup has not started yet", any("a day of image use" in t for _, _, t, _ in early))

print("== onboarding ==")
note = dc.onboarding_note({"FACTORY_DOCKER": "docker", "TARES_FACTORY_ENGINE": "python"})
check("crew up says once that the watch cleans up Docker", note and "docker_cleanup turns it off" in note, note)
check("not with the bash watch, which does not", dc.onboarding_note({"FACTORY_DOCKER": "docker",
                                                                      "TARES_FACTORY_ENGINE": "bash"}) is None)
check("not without Docker", dc.onboarding_note({"FACTORY_DOCKER": "", "PATH": "/nonexistent"}) is None)

print(f"{P} passed, {F} failed")
sys.exit(1 if F else 0)
