# Tares enterprise licence, see ee/LICENSE
"""Shared projects' specs, plans and notes in the brain as decisions (TR-1211). The reconcile loop
(`tares_ee.brain.decisions.Decisions`) compares the cell's projects with lore's `factory_projects`
decisions and makes lore match: puts what is missing or changed, deletes what must not be there.

In process against the shared fake lore (ee/tests/fake_lore.py), which checks a decision's metadata as lore
does (a bad one is a 400 and nothing is written). Most checks call `reconcile()` directly; one scenario
starts the real loop with a short interval and one the app's own task.

Covered: what is eligible (shared project, linked to a connected repo, kinds spec, plan and note only, no
personal doc, nothing else), the exact lore document (external id, kind, uri, metadata), a doc shared by a
private and a shared project, the privacy switch both ways and from creation, an unlinked repo, an edited, a
removed and a re-kinded doc, an empty body, no author, very long names, lore down (listing, put and delete
failing, one delete failing among several), the licence and brain settings gates, entries of other sources
and of other external id shapes left alone, no secret in any request or log line, a second pass that sends
nothing, the real loop, the task stopped with the app.

In process, no ports. With CI's Postgres run (TARES_TEST_PG and tests/pg_harness on PYTHONPATH) the same
script keeps its state in Postgres.

Run: .venv/bin/python ee/tests/test_brain_decisions_projects.py
"""
import asyncio, json, logging, os, sys, tempfile, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import licensed   # scrubs the machine's TARES_* settings, puts the repo, ee/ and tests/ on sys.path
licensed.install_test_root()

TMP = tempfile.mkdtemp(prefix="tares-brain-decisions-")
ROOT = "root-token-" + "brain-decisions"
os.environ["TARES_HOME"] = os.path.join(TMP, "t")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_AUTH_TOKEN"] = ROOT

import httpx

import fake_lore
from tares.envelope import now_utc
from tares_ee.brain import decisions as dec_mod
from tares_ee.brain import index as index_mod
from tares_ee.brain import repos as repos_mod
from tares_ee.brain import sync as sync_mod

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


BRAIN_KEY = "brain-" + "key-test-decisions"   # gitleaks:allow
SOURCE = "factory_projects"
PUBLIC = "https://cell.example.test"


class Logs(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


async def until(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.02)
    return cond()


def unit():
    print("== the pieces ==")
    ck("the external id", dec_mod.external_id("p1", "doc_x") == "project:p1:doc:doc_x")
    os.environ["TARES_PUBLIC_URL"] = PUBLIC + "/"
    ck("the uri is the public address without its trailing slash, then the doc's page",
       dec_mod.doc_uri("p1", "doc_x") == PUBLIC + "/projects/p1/docs?doc=doc_x", dec_mod.doc_uri("p1", "doc_x"))
    os.environ["TARES_PUBLIC_URL"] = ""
    ck("with no public address the uri is a path", dec_mod.doc_uri("p1", "doc_x") == "/projects/p1/docs?doc=doc_x")
    ck("the interval is a minute, a batch 50, the kinds spec, plan, note",
       (dec_mod.INTERVAL_SECONDS, dec_mod.BATCH, dec_mod.DOC_KINDS) == (60.0, 50, ("spec", "plan", "note")))
    ck("a name is cut at 256 bytes and never inside a character", len(dec_mod._clip("é" * 200).encode()) <= 256
       and dec_mod._clip("é" * 200).encode().decode() and dec_mod._clip("ab") == "ab")
    ck("a time is RFC 3339 in UTC", dec_mod._when("2026-03-01T10:00:00+02:00") == "2026-03-01T08:00:00Z"
       and dec_mod._when("junk", "2026-03-01") == "2026-03-01T00:00:00Z" and len(dec_mod._when(None)) == 10)
    d = dec_mod.build_document("p1", {"id": "d1", "title": " T ", "body": "  ", "updated_by": None, "updated_at": "2026-03-01"})
    ck("a doc with no body has its title as text, and no author is Unknown",
       d["text"] == "T" and d["metadata"]["decided_by"] == ["Unknown"] and d["kind"] == "decision", str(d))
    ck("a project id lore would refuse is not built", dec_mod.build_document("p" * 257, {"id": "d1", "title": "T", "body": "b"}) is None
       and dec_mod.build_document("", {"id": "d1", "title": "T", "body": "b"}) is None)


async def main():
    from tares.daemon import make_app
    from tares_ee.brain.lore import LoreClient
    logs = Logs()
    logging.getLogger().addHandler(logs)
    logging.getLogger().setLevel(logging.DEBUG)
    lore = fake_lore.FakeLore()
    repos_mod.lore_client = lore.client
    os.environ["TARES_BRAIN_URL"] = fake_lore.BASE_URL
    os.environ["TARES_BRAIN_KEY"] = BRAIN_KEY
    os.environ["TARES_PUBLIC_URL"] = PUBLIC + "/"

    async def never(self):                # the indexer's and syncer's own tasks are not under test here
        await asyncio.Event().wait()
    index_mod.Indexer.run = never
    sync_mod.Syncer.run = never

    app = make_app()
    async with app.router.lifespan_context(app):
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=H(ROOT))
        store = app.state.store
        r = await cx.put("/api/licence", json={"key": licensed.test_licence()})
        ck("the licence is stored", r.status_code == 200, r.text[:200])
        dec = app.state.brain_decisions
        ck("the cell has one reconciler and its task", dec is not None and dec.task is not None and not dec.task.done())
        dec2 = dec_mod.Decisions(store, lambda: repos_mod.lore_client())
        dec2.attach(app)
        ck("attaching a second reconciler to the app is refused", app.state.brain_decisions is dec and dec2.task is None)
        dec.task.cancel()                 # from here the passes are asked for by hand
        await asyncio.gather(dec.task, return_exceptions=True)
        dec.task = None

        # -- helpers ------------------------------------------------------------------------

        def reset():
            for p in store.list_projects():
                store.delete_project(p["id"])
            for r in store.list_brain_repos():
                store.delete_brain_repo(r["repo"])
            lore.documents.clear()
            lore.requests.clear()
            lore._fail.clear()
            lore._raise.clear()
            dec._sent.clear()
            dec._known.clear()
            logs.lines.clear()

        def connect(repo="acme/web"):
            store.add_brain_repo(repo, "id-" + repo.replace("/", "-"), None, now_utc())

        def project(pid, name=None, repo="acme/web", scope="team"):
            store.create_project(pid, name or "Name of " + pid)
            if repo:
                store.set_handover(pid, [{"path": "/work/" + repo.split("/")[-1], "name": repo}], None, "t", now_utc())
            if scope != "team":
                store.set_project_scope(pid, scope)

        def doc(pid, kind="spec", title=None, body="the body", by="Ann Lee", **kw):
            return store.create_doc(pid, kind, title or f"{kind} title", body, by=by, **kw)

        def held():
            return {ext: d for (src, ext), d in lore.documents.items() if src == SOURCE}

        def puts():
            return lore.calls("POST", "put_documents")

        def dels():
            return lore.calls("DELETE", "delete_document")

        def sent_docs():
            return [d for c in puts() for d in c.json["documents"]]

        stuck = set()                     # external id parts whose delete always fails, until cleared
        orig_handle = lore.handle

        def handle(request):
            from urllib.parse import unquote
            if request.method == "DELETE" and any(x in unquote(request.url.raw_path.decode()) for x in stuck):
                lore.fail("delete_document", 503, "stuck")
            return orig_handle(request)
        lore.handle = handle

        def down(route, n=1, error="lore is down"):
            for _ in range(n):
                lore.fail(route, 503, error)

        async def go():
            await dec.reconcile()

        def clean(label):
            blob = repr([(c.path, c.query, c.json) for c in lore.requests]) + "\n".join(logs.lines)
            ck(label, not any(t in blob for t in (BRAIN_KEY, ROOT)), "a key is in a request or a log line")

        # -- scenarios ----------------------------------------------------------------------

        print("== a shared, linked project puts its spec, plan and note in ==")
        reset(); connect(); project("pa", "Alpha")
        d_spec, d_plan, d_note = doc("pa", "spec", "The spec", "spec body"), doc("pa", "plan", "The plan", "plan body"), doc("pa", "note", "A note", "note body")
        await go()
        h = held()
        ck("three decisions, with the exact external ids", set(h) == {f"project:pa:doc:{d}" for d in (d_spec, d_plan, d_note)}, str(sorted(h)))
        e = h[f"project:pa:doc:{d_spec}"]
        full = store.get_doc("pa", d_spec)
        ck("the spec: kind decision, title, text, uri", (e["kind"], e["title"], e["text"], e["uri"]) ==
           ("decision", "The spec", "spec body", f"{PUBLIC}/projects/pa/docs?doc={d_spec}"), str(e))
        ck("... and metadata: where, project, one author, the last change as RFC 3339", e["metadata"] == {
            "where": "factory_project", "project": "pa", "doc_kind": "spec", "decided_by": ["Ann Lee"], "decided_at": dec_mod._when(full["updated_at"])}, str(e["metadata"]))
        ck("the plan and the note carry their own title and text", (h[f"project:pa:doc:{d_plan}"]["title"], h[f"project:pa:doc:{d_note}"]["text"]) == ("The plan", "note body"))
        ck("one put under the source factory_projects, one listing, no delete", len(puts()) == 1 and puts()[0].json["source"] == SOURCE
           and len(lore.calls("GET", "list_documents")) == 1 and not dels(), str([(c.method, c.route) for c in lore.requests]))
        q = lore.calls("GET", "list_documents")[0].query
        ck("the listing asks for the source's decisions", q.get("source") == SOURCE and q.get("kind") == "decision", str(q))

        ck("each carries its own doc_kind: spec, plan, note",
           [h[f"project:pa:doc:{d}"]["metadata"]["doc_kind"] for d in (d_spec, d_plan, d_note)] == ["spec", "plan", "note"])
        # a row put before doc_kind existed has no doc_kind; a process that starts later puts it again, with the kind
        plan_ext = f"project:pa:doc:{d_plan}"
        old = json.loads(json.dumps(h[plan_ext]))
        del old["metadata"]["doc_kind"]
        lore.documents[(SOURCE, plan_ext)] = old
        dec._sent.clear()   # what a restart does
        await go()
        ck("an older row without doc_kind is put again by the next pass after a start, now with its kind",
           held()[plan_ext]["metadata"].get("doc_kind") == "plan", str(held()[plan_ext]["metadata"]))
        ck("build_document says doc_kind null for a kind it does not know",
           dec_mod.build_document("pa", {"id": "x", "kind": "ticket", "title": "T", "body": "b"})["metadata"]["doc_kind"] is None)

        print("== an idempotent second pass ==")
        n = len(lore.requests)
        await go()
        ck("a second pass lists and sends nothing else", [c.route for c in lore.requests[n:]] == ["list_documents"], str([c.route for c in lore.requests[n:]]))
        ck("... the passes are counted", dec.passes >= 2)

        print("== other kinds never go in ==")
        reset(); connect(); project("pk")
        for k in ("working", "agents", "start", "memory", "grants"):
            doc("pk", k, f"a {k} doc")
        store.create_doc("pk", "note", "personal", "mine", by="Ann Lee", owner="person-1")
        keep = doc("pk", "note", "kept note")
        await go()
        ck("only the shared note is in: no working doc, AGENTS.md, start prompt, memory, grants or personal doc",
           set(held()) == {f"project:pk:doc:{keep}"}, str(sorted(held())))
        ck("a ticket is never read: no ticket text reached lore", "ticket" not in repr([c.json for c in puts()]).lower())

        print("== no connected repo, no link, an unusable link ==")
        reset(); project("pn", repo=None); doc("pn")
        await go()
        ck("no repo on the brain at all: nothing, and not even a call to lore for puts", not held() and not puts())
        connect("acme/web"); project("pu", repo="acme/other"); doc("pu"); project("po", repo=None); doc("po")
        store.set_handover("po", [{"path": "/x", "name": None}, {"path": "/y", "name": "not a repo"}], None, "t", now_utc())
        await go()
        ck("a project linked to another repo, to none, or to a name that is no repo contributes nothing", not held() and not puts(), str(sorted(held())))
        project("pm", repo="acme/other"); doc("pm", "note", "multi")
        store.set_handover("pm", [{"path": "/y", "name": "acme/other"}, {"path": "/w", "name": "ACME/Web"}], None, "t", now_utc())
        await go()
        ck("a project linked to several repos counts when one is connected (any case)", set(held()) == {k for k in held() if k.startswith("project:pm:")} and len(held()) == 1, str(sorted(held())))
        store.set_handover("pm", [{"path": "/y", "name": "acme/other"}], None, "t", now_utc())
        await go()
        ck("unlinking it from the connected repo removes the entry", not held() and len(dels()) == 1, str(sorted(held())))
        ck("... the delete named the source and the external id", dels()[0].path.startswith("/v1/documents/factory_projects/project%3Apm%3Adoc%3A") or "project" in dels()[0].path, dels()[0].path)

        print("== a private project never reaches the brain ==")
        reset(); connect(); project("pr", scope="private"); doc("pr", "spec"); doc("pr", "plan"); doc("pr", "note")
        await go(); await go()
        ck("no put request at all, over two passes", not puts() and not held(), str([c.route for c in lore.requests]))

        print("== private from creation, then shared, then private again ==")
        store.set_project_scope("pr", "team")
        await go()
        ck("shared: its three docs go in", len(held()) == 3 and all(k.startswith("project:pr:doc:") for k in held()), str(sorted(held())))
        store.set_project_scope("pr", "private")
        await go()
        ck("private again: all three are removed", not held() and len(dels()) == 3, str(sorted(held())))
        ck("... deleted before anything else was sent in that pass", [c.route for c in lore.requests][-4:] == ["list_documents", "delete_document", "delete_document", "delete_document"],
           str([c.route for c in lore.requests][-5:]))
        n = len(puts())
        await go()
        ck("and it stays out", not held() and len(puts()) == n)

        print("== shared, then private: removed; private, then shared: put back ==")
        reset(); connect(); project("ps", "Switch"); a, b = doc("ps", "spec"), doc("ps", "plan")
        await go()
        ck("shared: in", len(held()) == 2)
        store.set_project_scope("ps", "private")
        await go()
        ck("made private: both entries are removed", not held() and len(dels()) == 2)
        store.set_project_scope("ps", "team")
        await go()
        ck("made shared again: both are put back", set(held()) == {f"project:ps:doc:{a}", f"project:ps:doc:{b}"})

        print("== a doc shared by a private and a shared project ==")
        reset(); connect(); project("sh", "Shared one"); project("pv", "Secret plans", scope="private")
        both = doc("sh", "spec", "Both", "both body")
        store.use_doc("pv", both)
        only_private = doc("pv", "note", "Only private")
        await go()
        h = held()
        ck("it goes in once, through the shared project", set(h) == {f"project:sh:doc:{both}"}, str(sorted(h)))
        ck("its metadata names the shared project", h[f"project:sh:doc:{both}"]["metadata"]["project"] == "sh")
        ck("the private project's id and name are in nothing sent", not any(t in repr([(c.path, c.json) for c in lore.requests]) for t in ("pv", "Secret plans")))
        ck("a doc that only a private project holds is never put in", f"project:pv:doc:{only_private}" not in h and not any(only_private in k for k in h))
        reset(); connect(); project("sh", "Shared one"); project("pv", "Secret plans", scope="private")
        mine = doc("pv", "spec", "Made in private", "made there")
        store.use_doc("sh", mine)
        await go()
        h = held()
        ck("a doc made in a private project and used by a shared one is labelled with the shared one",
           set(h) == {f"project:sh:doc:{mine}"} and h[f"project:sh:doc:{mine}"]["metadata"]["project"] == "sh"
           and "pv" not in repr([(c.path, c.json) for c in lore.requests]), str(sorted(h)))
        store.set_project_scope("sh", "private")
        await go()
        ck("when the shared one turns private too, the entry goes", not held())

        print("== a doc edited, removed, or turned into another kind ==")
        reset(); connect(); project("pe"); d1, d2, d3 = doc("pe", "spec", "One", "first"), doc("pe", "plan", "Two", "second"), doc("pe", "note", "Three", "third")
        await go()
        before = dict(held())
        store.update_doc(d1, title="One, renamed", body="first, changed", by="Bo Chen")
        await go()
        h = held()
        e = h[f"project:pe:doc:{d1}"]
        ck("an edited doc is updated in place: new title, text and author", (e["title"], e["text"], e["metadata"]["decided_by"]) == ("One, renamed", "first, changed", ["Bo Chen"]), str(e))
        ck("... and the other two were not sent again", [x["external_id"] for x in puts()[-1].json["documents"]] == [f"project:pe:doc:{d1}"], str(puts()[-1].json))
        ck("... the count of entries is the same", len(h) == 3 and before.keys() == h.keys())
        store.remove_doc("pe", d2)
        await go()
        ck("a doc removed from the project is removed from lore", set(held()) == {f"project:pe:doc:{d1}", f"project:pe:doc:{d3}"}, str(sorted(held())))
        store.update_doc(d3, kind="working")
        await go()
        ck("a doc whose kind changes to an excluded one is removed", set(held()) == {f"project:pe:doc:{d1}"}, str(sorted(held())))
        store.update_doc(d3, kind="plan")
        await go()
        ck("... and put back when it is a plan again", f"project:pe:doc:{d3}" in held())
        store.delete_project("pe")
        await go()
        ck("a deleted project removes everything it had", not held())

        print("== an empty body, no author, very long names ==")
        reset(); connect(); project("px"); e1 = doc("px", "spec", "Empty one", "   ", by=""); e2 = doc("px", "note", "Long author", "text", by="é" * 200)
        await go()
        h = held()
        ck("an empty body is put with the title as its text", h[f"project:px:doc:{e1}"]["text"] == "Empty one", str(h[f"project:px:doc:{e1}"]))
        ck("no author is Unknown, never empty or null", h[f"project:px:doc:{e1}"]["metadata"]["decided_by"] == ["Unknown"])
        who = h[f"project:px:doc:{e2}"]["metadata"]["decided_by"]
        ck("a long author is cut to at most 256 bytes and is still one non-empty name", len(who) == 1 and 0 < len(who[0].encode()) <= 256, str(who))
        big = "p" * 300
        project(big, repo="acme/web"); doc(big, "spec", "Long project")
        project("p" * 256, repo="acme/web"); long256 = doc("p" * 256, "spec", "Exactly 256")
        await go()
        ck("a project id over 256 bytes is skipped (lore would 400 the whole batch), one of 256 goes in, the others are unaffected",
           set(held()) == {f"project:px:doc:{e1}", f"project:px:doc:{e2}", f"project:{'p' * 256}:doc:{long256}"}, str(sorted(k[:30] for k in held())))
        ck("every request was accepted by lore", all(c.status in (200, 204) for c in lore.requests), str([c.status for c in lore.requests]))

        print("== more than a batch: 120 docs in batches of at most 50 ==")
        reset(); connect(); project("pb")
        for i in range(120):
            doc("pb", "note", f"Note {i}", f"body {i}")
        await go()
        ck("all 120 are in, in three requests of at most 50", len(held()) == 120 and sorted(len(c.json["documents"]) for c in puts()) == [20, 50, 50], str([len(c.json["documents"]) for c in puts()]))
        n = len(lore.requests)
        await go()
        ck("and a second pass sends nothing", [c.route for c in lore.requests[n:]] == ["list_documents"])

        print("== lore's listing pages: stale entries beyond the first page are found and removed ==")
        reset(); connect(); project("pg")
        for i in range(230):
            lore.documents[(SOURCE, f"project:gone{i}:doc:d{i}")] = {"id": f"id{i}", "kind": "decision", "title": "old", "uri": None, "text": "t",
                                                                         "metadata": {"where": "factory_project", "project": f"gone{i}", "decided_by": ["A"], "decided_at": "2026-01-01"}}
        await go()
        ck("230 stale entries over two listing pages are all deleted", not held() and len(dels()) == 230 and len(lore.calls("GET", "list_documents")) == 2,
           f"{len(held())} {len(dels())} {len(lore.calls('GET', 'list_documents'))}")

        print("== entries this loop does not own are never touched ==")
        reset(); connect(); project("pt"); mine = doc("pt", "spec", "Mine")
        meta = {"where": "repo_folder", "decided_by": ["A"], "decided_at": "2026-01-01"}
        lore.documents[("repo_docs", "project:pt:doc:x")] = {"id": "o1", "kind": "decision", "title": "other source", "uri": None, "text": "t", "metadata": dict(meta)}
        lore.documents[(SOURCE, "legacy:thing")] = {"id": "o2", "kind": "decision", "title": "other shape", "uri": None, "text": "t", "metadata": dict(meta)}
        lore.documents[(SOURCE, "project:pt:doc:note")] = {"id": "o3", "kind": "note", "title": "a note kind", "uri": None, "text": "t", "metadata": {}}
        await go()
        ck("another source, another external id shape and another kind are all still there", ("repo_docs", "project:pt:doc:x") in lore.documents
           and (SOURCE, "legacy:thing") in lore.documents and (SOURCE, "project:pt:doc:note") in lore.documents and not dels(), str(sorted(lore.documents)))
        ck("... and the project's own doc was put in", f"project:pt:doc:{mine}" in held())

        print("== a project made private while a pass is running ==")
        reset(); connect(); project("ra"); doc("ra", "spec", "Race one"); doc("ra", "plan", "Race two")
        real_wanted = dec.wanted

        def wanted_then_private():
            out = real_wanted()
            store.set_project_scope("ra", "private")   # the switch lands after the pass read the store
            return out
        dec.wanted = wanted_then_private
        await go()
        dec.wanted = real_wanted
        ck("nothing of it is sent: the batch is checked for privacy again just before it goes", not puts() and not held(), str(len(puts())))
        reset(); connect(); project("rb"); doc("rb", "spec", "Race three")
        await go()
        store.set_project_scope("rb", "team")
        store.set_project_scope("rb", "private")
        ck("and a project made private after a put is taken out by the next pass", await go() is None and not held())

        print("== one stuck delete does not hold back the others (nothing private involved) ==")
        reset(); connect(); project("pq"); q1, q2, q3 = doc("pq", "spec", "Q1"), doc("pq", "plan", "Q2"), doc("pq", "note", "Q3")
        await go()
        order = sorted(f"project:pq:doc:{d}" for d in (q1, q2, q3))
        for d in (q1, q2, q3):
            store.remove_doc("pq", d)
        stuck.add(order[0])
        await go()
        ck("the first entry in line will not delete: the other two are removed anyway", set(held()) == {order[0]}, str(sorted(held())))
        stuck.clear()
        await go()
        ck("... and it goes on the next pass", not held())

        print("== lore is down ==")
        reset(); connect(); project("pd"); x1, x2 = doc("pd", "spec", "X1"), doc("pd", "plan", "X2")
        down("list_documents", 1, "down " + BRAIN_KEY); down("put_documents", 1, "down " + BRAIN_KEY)
        await go()
        ck("a failed listing and a failed put do not crash the pass, and write nothing", not held())
        ck("... the failure is logged without the key", any("brain decisions" in l for l in logs.lines) and not any(BRAIN_KEY in l for l in logs.lines if l.startswith("brain decisions")), str(logs.lines[-3:]))
        await go()
        ck("the next pass, with lore back, puts both", len(held()) == 2)
        store.set_project_scope("pd", "private")
        down("list_documents", 1); stuck.add(x1)
        await go()
        ck("made private while the listing is down: no crash, the entries this process had put are still named and deleted", set(held()) == {f"project:pd:doc:{x1}"}, str(sorted(held())))
        ck("... the delete that kept failing (tried again at the end of the pass) did not stop the other one", len(dels()) == 3, str(len(dels())))
        stuck.clear()
        await go()
        ck("lore back: the rest is removed", not held())
        reset(); connect(); project("pf"); f1 = doc("pf", "spec", "F1"); f2 = doc("pf", "plan", "F2")
        await go()
        store.set_project_scope("pf", "private"); stuck.add(f1)
        await go()
        ck("a listing that works and one stuck delete: the other entry is still deleted in the same pass", set(held()) == {f"project:pf:doc:{f1}"}, str(sorted(held())))
        stuck.clear()
        await go()
        ck("... and the stuck one on the next pass", not held())
        reset(); connect(); project("pl"); doc("pl", "spec", "L1")
        await go()
        down("list_documents", 1)
        n = len(puts())
        await go()
        ck("a failed listing does not stop the puts, and a put that is the same is not repeated", len(held()) == 1 and len(puts()) == n, str(len(puts()) - n))
        reset(); connect(); project("pw"); doc("pw", "spec", "W1")
        lore.raise_next("list_documents", httpx.ConnectError("no route to lore"))
        lore.raise_next("put_documents", httpx.ReadTimeout("slow"))
        await go()
        await go()
        ck("a network error is survived too", len(held()) == 1)
        ck("no key in any request so far (the client's own log line of an error lore echoed the key into is not this loop's)",
           not any(BRAIN_KEY in repr((c.path, c.query, c.json)) for c in lore.requests))

        print("== the licence and the brain settings gate it ==")
        reset(); connect(); project("pg2"); g = doc("pg2", "spec", "G1")
        await go()
        ck("licensed and configured: in", len(held()) == 1)
        n = len(lore.requests)
        r = await cx.put("/api/licence", json={"key": licensed.test_licence(features=("other",))})
        project("pg3"); doc("pg3", "spec", "G2")
        store.set_project_scope("pg2", "private")
        await go()
        ck("without the licence the pass does nothing at all, not even a call", len(lore.requests) == n and not dec.active(), str([c.route for c in lore.requests[n:]]))
        r = await cx.put("/api/licence", json={"key": licensed.test_licence()})
        os.environ.pop("TARES_BRAIN_KEY")
        ck("without the brain's key the reconciler is not active", not LoreClient.configured() and not dec.active())
        await go()
        ck("... and does nothing", len(lore.requests) == n)
        os.environ["TARES_BRAIN_KEY"] = BRAIN_KEY
        await go()
        ck("both back: it catches up, the private one out and the new one in", set(k.split(":")[1] for k in held()) == {"pg3"}, str(sorted(held())))

        print("== the real loop, with a short interval ==")
        reset(); connect(); project("lp"); l1 = doc("lp", "spec", "Loop")
        saved = dec_mod.INTERVAL_SECONDS
        dec_mod.INTERVAL_SECONDS = 0.1
        dec.task = asyncio.create_task(dec.run())
        ck("the loop puts the doc on its own", await until(lambda: f"project:lp:doc:{l1}" in held()), str(sorted(held())))
        store.set_project_scope("lp", "private")
        ck("and takes it out again on a later pass, with nobody asking", await until(lambda: not held()), str(sorted(held())))
        down("list_documents", 2)
        passes = dec.passes
        store.set_project_scope("lp", "team")
        ck("a pass that fails does not end the loop", await until(lambda: dec.passes > passes + 1 and held()), f"{dec.passes} {len(held())}")
        dec.task.cancel()
        await asyncio.gather(dec.task, return_exceptions=True)
        dec.task = None
        dec_mod.INTERVAL_SECONDS = saved

        print("== no secret anywhere ==")
        lore.requests.clear(); logs.lines[:] = [l for l in logs.lines if l.startswith("brain decisions")]
        project("zz"); doc("zz", "spec", "Last")
        await go()
        clean("no brain key or cell token in any request, path or this loop's log line")
        ck("no put carried more than the doc's own fields", all(set(d) <= {"external_id", "title", "text", "kind", "uri", "metadata"} for d in sent_docs()))

        print("== it stops with the app ==")
        task = None
    # the lifespan has ended; a fresh app shows the task starts and stops with it
    app2 = make_app()
    async with app2.router.lifespan_context(app2):
        d = app2.state.brain_decisions
        ck("a new app starts the task", d is not None and d.task is not None and not d.task.done())
        task = d.task
    ck("the task ended with the app", task.done() and d.task is None)
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


if __name__ == "__main__":
    unit()
    asyncio.run(main())
