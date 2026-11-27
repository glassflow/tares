"""Linear and GitHub Issues tickets kept in step by their integrations' sync hooks (Tares 2.0, TR-1806).

tares/integrations/sync.py replaces the catalog sources the runtime used to poll: every integration of
the tickets slot with `linked()` and `sync(project)` has `sync` called for each linked project, once per
`sync_interval_s`. Checked here with the real registry and stubbed `sync`:

  * the first pass syncs every linked project of Linear and GitHub, and the Tares backend (no `linked`)
    is left out;
  * a pass inside the interval syncs nothing, one after it syncs again;
  * a project whose sync raises is recorded as not synced and the others still sync;
  * an integration whose project list fails is skipped, the others still run;
  * `run` stops when its stop event is set.

Run: python tests/test_ticket_sync_hooks.py
"""
import asyncio, os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tares.integrations import sync as runner
from tares.integrations.builtin.tickets_github import TicketsGithub
from tares.integrations.builtin.tickets_linear import TicketsLinear

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))


class FakeStore:
    def __init__(self):
        self.linear = [("p1", {"id": "L1"}), ("p2", {"id": "L2"})]
        self.github = [("p3", {"repo": "acme/app"})]
        self.fail_github_list = False

    def linear_projects(self):
        return list(self.linear)

    def github_projects(self):
        if self.fail_github_list:
            raise RuntimeError("the store could not list them")
        return list(self.github)


CALLS = []
BROKEN = set()
SLOW = set()


async def fake_sync(self, project):
    uid = project if isinstance(project, str) else project["id"]
    CALLS.append((self.name, uid))
    if uid in BROKEN:
        raise RuntimeError(f"{uid}: the ticket system did not answer")
    if uid in SLOW:
        await asyncio.sleep(10)
    return 1


async def main():
    TicketsLinear.sync = fake_sync
    TicketsGithub.sync = fake_sync
    store = FakeStore()

    print("== a first pass ==")
    last = {}
    done = await runner.sync_once(store, now=1000.0, last=last)
    ck("every linked project of both integrations is synced",
       sorted(done) == [("tickets:github", "p3"), ("tickets:linear", "p1"), ("tickets:linear", "p2")], done)
    ck("only linear and github sync: the Tares backend has no linked()", {c[0] for c in CALLS} == {"linear", "github"},
       CALLS)

    print("== the interval ==")
    CALLS.clear()
    done = await runner.sync_once(store, now=1000.0 + 30, last=last)
    ck("30 s later nothing is due", done == [] and CALLS == [], CALLS)
    done = await runner.sync_once(store, now=1000.0 + TicketsLinear.sync_interval_s + 1, last=last)
    ck("after the interval both sync again", len(done) == 3, done)

    print("== failures stay with their project ==")
    CALLS.clear()
    BROKEN.add("p1")
    done = await runner.sync_once(store)
    ck("p1 fails, p2 and p3 still sync", sorted(done) == [("tickets:github", "p3"), ("tickets:linear", "p2")]
       and ("linear", "p1") in CALLS, (done, CALLS))
    BROKEN.clear()
    store.fail_github_list = True
    done = await runner.sync_once(store)
    ck("GitHub's project list fails: Linear still syncs", sorted(done) == [("tickets:linear", "p1"),
                                                                          ("tickets:linear", "p2")], done)
    store.fail_github_list = False

    print("== a slow project ==")
    CALLS.clear()
    SLOW.add("p2")
    runner.SYNC_TIMEOUT_S = 0.2
    done = await runner.sync_once(store)
    ck("p2 hangs: it is cut off at the time limit and p1, p3 still sync",
       sorted(done) == [("tickets:github", "p3"), ("tickets:linear", "p1")], done)
    SLOW.clear()
    runner.SYNC_TIMEOUT_S = 120.0

    print("== backoff ==")
    BROKEN.add("p1")
    fails = runner._Failures()
    await runner.sync_once(store, now=0.0, failures=fails)
    CALLS.clear()
    await runner.sync_once(store, now=30.0, failures=fails)
    ck("a project that failed is not tried again before its backoff", ("linear", "p1") not in CALLS, CALLS)
    await runner.sync_once(store, now=61.0, failures=fails)
    ck("it is tried again once the backoff has passed", ("linear", "p1") in CALLS, CALLS)
    ck("a second failure (at 61 s) doubles the wait to 120 s", fails.next_at[("tickets:linear", "p1")] == 61.0 + 120,
       fails.next_at)
    BROKEN.clear()
    await runner.sync_once(store, now=10_000.0, failures=fails)
    ck("a sync that works clears the backoff", ("tickets:linear", "p1") not in fails.count, fails.count)

    print("== an integration that cannot be built ==")
    from tares.integrations import registry

    class Broken:
        slot, name = "tickets", "broken"
        def __init__(self, store):
            raise ValueError("no settings")
        def linked(self): return []
        async def sync(self, p): return 0
    real = registry.available
    registry.available = lambda slot: [*real(slot), Broken]
    try:
        CALLS.clear()
        done = await runner.sync_once(store)
        ck("one integration that raises when built is skipped; the others sync", len(done) == 3, done)
    finally:
        registry.available = real

    print("== the task ==")
    CALLS.clear()
    stop = asyncio.Event()
    task = asyncio.create_task(runner.run(store, stop, tick_s=0.05))
    await asyncio.sleep(0.2)
    stop.set()
    await asyncio.wait_for(task, timeout=2)
    ck("run synced on its first tick and stopped when told", len(CALLS) == 3 and task.done(), CALLS)

    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


if __name__ == "__main__":
    asyncio.run(main())
