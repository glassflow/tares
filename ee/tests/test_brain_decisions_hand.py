# Tares enterprise licence, see ee/LICENSE
"""Decisions added by hand, and the list of every decision (TR-1212): POST, DELETE and GET
/api/brain/decisions (ee/tares_ee/brain/decisions_hand.py), against the shared fake lore.

In process, no ports. People and keys are made through the real API (a root token, an admin key, member
keys, a read key), so the guard, the credential's person and the admin check are the daemon's own.

Covered: adding by paste naming another person (listed with that person, the date and the adder: the
definition of done), the default decided_by, a decided_by naming nobody or two people, every refusal of the
body (title, text, file, both, neither, null, blank, too long, unknown field), the 5 MB limit on both
sides, a Markdown upload, a real PDF, a PDF with no text, a corrupt and a password-protected one, no pypdf,
that a refused request writes nothing to lore, who may delete (the adder, an admin, the root token; not
another member), that only `hand:` ids are deleted, the list across a repo's folder, a factory project and
by hand, `where` and `repo` filters, counts with and without a filter, paging by cursor (limit 1 and 2,
with and without a filter, no repeats or gaps), newest first, a private or unknown project's decision
never listed or counted, a missing or unknown `where`, lore failing, the licence and brain settings gates,
no secret in any response, request or log line, and auth off (the single person).

Run: .venv/bin/python ee/tests/test_brain_decisions_hand.py
"""
import asyncio, base64, io, logging, os, re, subprocess, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import licensed   # scrubs the machine's TARES_* settings, puts the repo, ee/ and tests/ on sys.path
licensed.install_test_root()

TMP = tempfile.mkdtemp(prefix="tares-brain-decisions-hand-")
ROOT = "root-token-" + "decisions-hand"
os.environ["TARES_DB"] = os.path.join(TMP, "t.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
AUTH_OFF = "--auth-off" in sys.argv   # the second run, started by the first: auth is read at import
if not AUTH_OFF:
    os.environ["TARES_AUTH_TOKEN"] = ROOT

import httpx

import fake_lore
from tares_ee.brain import decisions_hand as dh
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


BRAIN_KEY = "brain-" + "key-test-hand"   # gitleaks:allow
MB5 = 5 * 1024 * 1024
MB1 = 1024 * 1024
RFC3339 = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


class Logs(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def file_of(name, data: bytes):
    return {"name": name, "content_base64": b64(data)}


def make_pdf(text: str) -> bytes:
    """A one-page PDF whose text layer is `text`, written byte by byte (so no library makes it)."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return out


def unit():
    print("== the pieces ==")
    ck("the source, kind and prefix", (dh.SOURCE, dh.KIND, dh.PREFIX, dh.MAX_BYTES, dh.TEXT_MAX) == ("decisions", "decision", "hand:", 5 * 1024 * 1024, 1024 * 1024))
    ck("a summary is the first sentence", dh.summary_of("We chose X.  Then more. And more.") == "We chose X.")
    ck("... on one line", dh.summary_of("Line one\nline two. Next") == "Line one line two.")
    ck("... without a full stop it is the whole text, cut at 200 characters",
       dh.summary_of("a" * 500) == "a" * 197 + "..." and dh.summary_of("short") == "short")
    ck("a name is cut at 256 bytes, never inside a character", len(dh._clip("é" * 200).encode()) <= 256 and dh._clip("ab") == "ab")
    ck("a PDF's text is read", "Hello decision" in dh.pdf_text(make_pdf("Hello decision")))


async def main():
    from tares.daemon import make_app
    logs = Logs()
    logging.getLogger().addHandler(logs)
    logging.getLogger().setLevel(logging.DEBUG)
    lore = fake_lore.FakeLore()
    repos_mod.lore_client = lore.client
    os.environ["TARES_BRAIN_URL"] = fake_lore.BASE_URL
    os.environ["TARES_BRAIN_KEY"] = BRAIN_KEY

    async def never(self):
        await asyncio.Event().wait()
    index_mod.Indexer.run = never
    sync_mod.Syncer.run = never

    app = make_app()
    async with app.router.lifespan_context(app):
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=H(ROOT), timeout=60)
        store = app.state.store
        r = await cx.put("/api/licence", json={"key": licensed.test_licence(seats=30)})
        ck("the licence is stored", r.status_code == 200, r.text[:200])
        dec = app.state.brain_decisions
        dec.task.cancel()                 # the projects' reconcile loop is not under test
        await asyncio.gather(dec.task, return_exceptions=True)
        dec.task = None

        async def person(name, email):
            return (await cx.post("/api/people", json={"name": name, "email": email})).json()["id"]

        async def key(scopes, pid):
            r = await cx.post("/api/keys", json={"name": "k-" + "-".join(scopes) + pid, "scopes": scopes, "person_id": pid})
            return H(r.json()["secret"])

        ana = await person("Ana Lopez", "ana@x.dev")
        bo = await person("Bo Chen", "bo@x.dev")
        cy = await person("Cy Park", "cy@x.dev")
        await person("Twin", "t1@x.dev")
        await person("Twin", "t2@x.dev")
        ADMIN, BO, CY, READ = await key(["admin"], ana), await key(["read", "ingest"], bo), await key(["read", "ingest"], cy), await key(["read"], bo)

        def held():
            return {ext: d for (src, ext), d in lore.documents.items() if src == dh.SOURCE}

        def puts():
            return lore.calls("POST", "put_documents")

        def reset():
            lore.documents.clear()
            lore.requests.clear()
            lore._fail.clear()
            lore._raise.clear()
            logs.lines.clear()

        async def add(body, who=BO):
            return await cx.post("/api/brain/decisions", json=body, headers=who)

        async def listing(who=CY, **q):
            return await cx.get("/api/brain/decisions", params=q, headers=who)

        def fixed(ext, at):
            """Pin a stored decision's date, so the order of a test does not depend on the clock."""
            lore.documents[(dh.SOURCE, ext)]["metadata"]["decided_at"] = at

        async def put(source, ext, title, meta, uri=None, kind="decision"):
            async with lore.client() as c:
                await c.put_documents(source, [{"external_id": ext, "title": title, "text": "text of " + title,
                                                "kind": kind, "uri": uri, "metadata": meta}])

        def clean(label):
            blob = repr([(c.path, c.query, c.json) for c in lore.requests]) + "\n".join(logs.lines)
            ck(label, not any(t in blob for t in (BRAIN_KEY, ROOT)), "a key is in a request or a log line")

        print("== add by paste, naming another person (the definition of done) ==")
        reset()
        r = await add({"title": "Use Postgres", "text": "We chose Postgres. It scales better.", "decided_by": "Ana Lopez"})
        j = r.json()
        ck("201 with the new decision", r.status_code == 201 and j["title"] == "Use Postgres", f"{r.status_code} {r.text[:300]}")
        ck("... the row: id, source, who decided, when, who added it, where, summary", j["id"].startswith("hand:") and j["source"] == "decisions"
           and j["decided_by"] == ["Ana Lopez"] and RFC3339.fullmatch(j["decided_at"]) and j["added_by"] == "Bo Chen"
           and j["where"] == "by_hand" and j["summary"] == "We chose Postgres." and j["link"] is None
           and j["repo"] is None and j["project"] is None and j["changed_by"] == [] and j["changed_at"] is None, str(j))
        ck("... no person id in the answer", bo not in r.text and "added_by_id" not in r.text)
        d = lore.documents[("decisions", j["id"])]
        ck("lore holds it: kind decision, the text whole, the external id hand:<uuid>", d["kind"] == "decision"
           and d["text"] == "We chose Postgres. It scales better." and re.fullmatch(r"hand:[0-9a-f-]{36}", j["id"]), str(d))
        ck("... metadata: where, decided_by, decided_at, added_by, the adder's id, summary", d["metadata"] == {
            "where": "by_hand", "decided_by": ["Ana Lopez"], "decided_at": j["decided_at"], "added_by": "Bo Chen",
            "added_by_id": bo, "summary": "We chose Postgres."}, str(d["metadata"]))
        ck("exactly one put, under the source decisions", len(puts()) == 1 and puts()[0].json["source"] == "decisions")
        r = await listing(CY)
        rows = r.json()["decisions"]
        ck("another member lists it with Ana, the date and Bo", r.status_code == 200 and len(rows) == 1 and rows[0]["id"] == j["id"]
           and rows[0]["decided_by"] == ["Ana Lopez"] and rows[0]["decided_at"] == j["decided_at"] and rows[0]["added_by"] == "Bo Chen"
           and rows[0]["where"] == "by_hand" and rows[0]["summary"] == "We chose Postgres.", r.text[:300])
        clean("no brain key or token in any request or log line")
        ck("... nor in a response", BRAIN_KEY not in r.text)

        print("== who decided ==")
        r = await add({"title": "Mine", "text": "Mine."})
        ck("decided_by defaults to the person adding it", r.status_code == 201 and r.json()["decided_by"] == ["Bo Chen"], r.text[:200])
        for ref, want in ((cy, "Cy Park"), ("CY@X.DEV", "Cy Park"), ("cy park", "Cy Park"), ("  Ana Lopez ", "Ana Lopez")):
            r = await add({"title": "By ref", "text": "Ref.", "decided_by": ref})
            ck(f"decided_by {ref!r} finds the person (id, email or name)", r.status_code == 201 and r.json()["decided_by"] == [want], r.text[:200])
        n = len(puts())
        for ref, why in (("Nobody Here", "nobody on this cell"), ("Twin", "more than one person")):
            r = await add({"title": "X", "text": "x", "decided_by": ref})
            ck(f"decided_by {ref!r} is a 400 in plain words", r.status_code == 400 and why in r.text, f"{r.status_code} {r.text[:200]}")
        r = await add({"title": "X", "text": "x", "decided_by": 5})
        ck("decided_by that is not text is a 400", r.status_code == 400, r.text[:200])
        ck("... nothing was written", len(puts()) == n)
        r = await cx.post("/api/brain/decisions", json={"title": "Root", "text": "By root."})
        ck("the root token has no person: decided_by is required", r.status_code == 400 and "decided_by" in r.text, r.text[:200])
        r = await cx.post("/api/brain/decisions", json={"title": "Root", "text": "By root.", "decided_by": "Cy Park"})
        ck("... with one it adds, and the adder is named", r.status_code == 201 and r.json()["added_by"] and "added_by_id" not in
           lore.documents[("decisions", r.json()["id"])]["metadata"], r.text[:200])
        for who, name in ((ADMIN, "Ana Lopez"),):
            r = await add({"title": "Admin", "text": "Admin."}, who)
            ck("an admin key's person is the default decider", r.status_code == 201 and r.json()["decided_by"] == [name], r.text[:200])
        r = await add({"title": "Read only", "text": "x"}, READ)
        ck("a read-only key is refused by the guard", r.status_code == 403, f"{r.status_code}")

        print("== the body is checked before lore is called ==")
        reset()
        good_file = file_of("adr.md", b"# Title\nBody.")
        cases = [
            ("no title", {"text": "t"}), ("a null title", {"title": None, "text": "t"}), ("an empty title", {"title": "", "text": "t"}),
            ("a blank title", {"title": "   ", "text": "t"}), ("a title that is not text", {"title": 5, "text": "t"}),
            ("a title of 201 characters", {"title": "x" * 201, "text": "t"}),
            ("no text and no file", {"title": "T"}), ("a null text and a null file", {"title": "T", "text": None, "file": None}),
            ("an empty text", {"title": "T", "text": ""}), ("a blank text", {"title": "T", "text": " \n\t "}),
            ("a text that is not text", {"title": "T", "text": 5}), ("text and a file", {"title": "T", "text": "t", "file": good_file}),
            ("an unknown field", {"title": "T", "text": "t", "extra": 1}), ("a file that is a list", {"title": "T", "file": []}),
            ("a file with no name", {"title": "T", "file": {"content_base64": b64(b"x")}}),
            ("a file with no content", {"title": "T", "file": {"name": "a.md"}}),
            ("a file with blank content", {"title": "T", "file": {"name": "a.md", "content_base64": "  "}}),
            ("a file of an unknown kind", {"title": "T", "file": file_of("a.docx", b"x")}),
            ("a file with no extension", {"title": "T", "file": file_of("readme", b"x")}),
            ("content that is not base64", {"title": "T", "file": {"name": "a.md", "content_base64": "@@@@"}}),
            ("a text file that is not UTF-8", {"title": "T", "file": file_of("a.txt", b"\xff\xfe\xfa bad")}),
            ("a text file with only blanks", {"title": "T", "file": file_of("a.md", b" \n \n")}),
            ("a file of zero bytes", {"title": "T", "file": {"name": "a.md", "content_base64": "="}}),
        ]
        for label, body in cases:
            r = await add(body)
            ck(f"{label}: 400 with words", r.status_code == 400 and bool(r.json().get("detail")), f"{r.status_code} {r.text[:200]}")
        r = await add({"title": "x" * 200, "text": "t"})
        ck("a title of exactly 200 characters is fine", r.status_code == 201, r.text[:200])
        n_puts = len(puts())
        ck("every refusal wrote nothing: one put, the last one", n_puts == 1 and len(held()) == 1, f"{n_puts} {len(held())}")
        r = await add({"title": "T", "text": "t", "file": None})
        ck("a null file next to a text is fine", r.status_code == 201, r.text[:200])
        r = await add({"title": "T", "text": "", "file": good_file})
        ck("an empty text next to a file is fine (a form sends both)", r.status_code == 201 and r.json()["summary"] == "# Title Body.", r.text[:200])

        print("== the 5 MB file limit and the 1 MB text limit ==")
        reset()
        r = await add({"title": "At the limit", "file": file_of("big.md", b"a" * MB1)})
        ck("a file whose text is exactly 1 MiB is taken", r.status_code == 201 and len(lore.documents[("decisions", r.json()["id"])]["text"]) == MB1, f"{r.status_code} {r.text[:120]}")
        reset()
        r = await add({"title": "Over", "file": file_of("big.md", b"a" * (MB1 + 1))})
        ck("a file whose text is one byte over 1 MiB is 413 in plain words", r.status_code == 413 and "1 MB" in r.text, f"{r.status_code} {r.text[:160]}")
        r = await add({"title": "Over", "file": file_of("big.md", b"a" * MB5)})
        ck("a file of exactly 5 MB is 413 too (its text is over 1 MB)", r.status_code == 413 and "1 MB" in r.text, f"{r.status_code} {r.text[:160]}")
        r = await add({"title": "Over", "file": file_of("big.md", b"a" * (MB5 + 1))})
        ck("a file one byte over 5 MB is 413 in plain words", r.status_code == 413 and "5 MB" in r.text, f"{r.status_code} {r.text[:120]}")
        r = await add({"title": "Over", "file": {"name": "big.md", "content_base64": "QUFB" * (MB5 // 2)}})
        ck("a file far over is 413 without being decoded", r.status_code == 413, f"{r.status_code}")
        r = await add({"title": "Over", "text": "a" * (MB1 + 1)})
        ck("pasted text one byte over 1 MiB is 413 in plain words", r.status_code == 413 and "1 MB" in r.text, f"{r.status_code} {r.text[:160]}")
        r = await add({"title": "Over", "text": "é" * (MB1 // 2 + 1)})
        ck("the limit counts bytes, not characters (512K+1 two-byte characters)", r.status_code == 413, f"{r.status_code}")
        r = await add({"title": "Over", "text": "a" * (MB5 + 1)})
        ck("pasted text over 5 MB is 413", r.status_code == 413, f"{r.status_code}")
        ck("... nothing was written", not puts() and not held())
        r = await add({"title": "At the limit", "text": "a" * MB1})
        ck("pasted text of exactly 1 MiB is taken", r.status_code == 201 and len(lore.documents[("decisions", r.json()["id"])]["text"]) == MB1, f"{r.status_code} {r.text[:120]}")
        reset()
        try:
            async with repos_mod.lore_client() as lc:
                await lc.put_documents("decisions", [{"external_id": "hand:x", "title": "T", "text": "a" * (MB1 + 1), "kind": "decision",
                                                     "metadata": {"decided_by": ["A"], "decided_at": "2026-01-01T00:00:00Z"}}])
            fake_limit = False
        except Exception:   # noqa: BLE001
            fake_limit = True
        ck("the fake lore refuses a text over 1 MiB, like lore", fake_limit and not held())

        print("== a NUL character ==")
        reset()
        r = await add({"title": "Nu\x00l", "text": "before\x00after"})
        doc = lore.documents[("decisions", r.json()["id"])] if r.status_code == 201 else {}
        ck("NUL is taken out of a pasted text and a title (lore would refuse it)", r.status_code == 201 and doc.get("text") == "beforeafter" and doc.get("title") == "Nul", f"{r.status_code} {r.text[:160]}")
        r = await add({"title": "Only NUL text", "text": "\x00\x00 "})
        ck("a text of only NUL is 400", r.status_code == 400, f"{r.status_code} {r.text[:120]}")
        r = await add({"title": "\x00", "text": "t"})
        ck("a title of only NUL is 400", r.status_code == 400, f"{r.status_code} {r.text[:120]}")
        ck("... nothing more was written", len(held()) == 1)

        print("== a Markdown and a text upload ==")
        reset()
        md = "﻿# Adopt queues\n\nWe will use a queue. Reasons follow.\n"
        r = await add({"title": "Queues", "file": file_of("ADR-3.MD", md.encode())})
        ck("a Markdown file (any case, with a BOM) becomes the text", r.status_code == 201
           and lore.documents[("decisions", r.json()["id"])]["text"] == md.lstrip("﻿").strip()
           and r.json()["summary"] == "# Adopt queues We will use a queue.", r.text[:300])
        r = await add({"title": "Plain", "file": file_of("note.txt", "Decided: ship it.".encode())})
        ck("a .txt file is taken", r.status_code == 201 and r.json()["summary"] == "Decided: ship it.", r.text[:200])
        r = await add({"title": "Plain", "file": file_of("note.markdown", "ünï 日本語.".encode())})
        ck("... UTF-8 text keeps its characters", r.status_code == 201 and lore.documents[("decisions", r.json()["id"])]["text"] == "ünï 日本語.", r.text[:200])

        print("== a PDF ==")
        reset()
        r = await add({"title": "Signed off", "file": file_of("sign-off.PDF", make_pdf("We agreed to ship on Friday"))})
        ck("a real PDF becomes its text", r.status_code == 201 and "We agreed to ship on Friday" in lore.documents[("decisions", r.json()["id"])]["text"], f"{r.status_code} {r.text[:300]}")
        from pypdf import PdfWriter
        blank, enc = io.BytesIO(), io.BytesIO()
        w = PdfWriter()
        w.add_blank_page(width=200, height=200)
        w.write(blank)
        w = PdfWriter()
        w.append(io.BytesIO(make_pdf("Secret words")))
        w.encrypt("pw")
        w.write(enc)
        n = len(puts())
        r = await add({"title": "Scan", "file": file_of("scan.pdf", blank.getvalue())})
        ck("a PDF with no text is a 400 that says so", r.status_code == 400 and "no text" in r.text, f"{r.status_code} {r.text[:200]}")
        for label, data in (("not a PDF at all", b"this is not a pdf"), ("cut short", make_pdf("Cut")[:60]), ("only the header", b"%PDF-1.4\n%%EOF")):
            r = await add({"title": "Bad", "file": file_of("bad.pdf", data)})
            ck(f"a corrupt PDF ({label}) is a 400, never a 500", r.status_code == 400 and bool(r.json().get("detail")), f"{r.status_code} {r.text[:200]}")
        r = await add({"title": "Locked", "file": file_of("locked.pdf", enc.getvalue())})
        ck("a password-protected PDF is a 400 that says so", r.status_code == 400 and "password" in r.text, f"{r.status_code} {r.text[:200]}")
        ck("... none of them wrote anything", len(puts()) == n)
        real = sys.modules.get("pypdf")
        sys.modules["pypdf"] = None   # an import now fails, as on a cell without the package
        try:
            r = await add({"title": "No lib", "file": file_of("a.pdf", make_pdf("x"))})
            ck("without pypdf a PDF is 501 in plain words", r.status_code == 501 and "pypdf" in r.text, f"{r.status_code} {r.text[:200]}")
            r = await add({"title": "Still", "text": "Pasting works."})
            ck("... and pasting still works", r.status_code == 201, r.text[:200])
            r = await add({"title": "Still", "file": file_of("a.md", b"Markdown works.")})
            ck("... and so does Markdown", r.status_code == 201, r.text[:200])
        finally:
            sys.modules["pypdf"] = real

        print("== lore failing ==")
        reset()
        lore.fail("put_documents", 502, "lore is down")
        r = await add({"title": "T", "text": "t"})
        ck("a lore error is a 502 in plain words and nothing is stored", r.status_code == 502 and "lore is down" in r.text and not held(), f"{r.status_code} {r.text[:200]}")
        lore.fail("list_documents", 503, "busy")
        r = await listing()
        ck("a listing that fails is a 502", r.status_code == 502, f"{r.status_code} {r.text[:200]}")
        clean("... no key in the log or the requests")

        print("== delete ==")
        reset()
        a = (await add({"title": "Bo's", "text": "Bo's."}, BO)).json()["id"]
        b = (await add({"title": "Bo's too", "text": "Bo's too."}, BO)).json()["id"]
        c = (await add({"title": "Bo's third", "text": "Third."}, BO)).json()["id"]
        r = await cx.delete(f"/api/brain/decisions/{a}", headers=CY)
        ck("another plain member may not delete it (403, in plain words)", r.status_code == 403 and "added this decision" in r.text, f"{r.status_code} {r.text[:200]}")
        ck("... it is still in lore", ("decisions", a) in lore.documents and not lore.calls("DELETE", "delete_document"))
        r = await cx.delete(f"/api/brain/decisions/{a}", headers=READ)
        ck("a read-only key is refused", r.status_code == 403 and ("decisions", a) in lore.documents, f"{r.status_code}")
        r = await cx.delete(f"/api/brain/decisions/{a}", headers=BO)
        ck("the person who added it deletes it", r.status_code == 200 and ("decisions", a) not in lore.documents, f"{r.status_code} {r.text[:200]}")
        r = await cx.delete(f"/api/brain/decisions/{b}", headers=ADMIN)
        ck("an admin deletes another person's", r.status_code == 200 and ("decisions", b) not in lore.documents, f"{r.status_code} {r.text[:200]}")
        r = await cx.delete(f"/api/brain/decisions/{c}")
        ck("so does the root token", r.status_code == 200 and not held(), f"{r.status_code} {r.text[:200]}")
        r = await cx.delete(f"/api/brain/decisions/{a}", headers=BO)
        ck("an id that is gone is 404", r.status_code == 404, f"{r.status_code}")
        r = await cx.delete("/api/brain/decisions/hand:00000000-0000-4000-8000-000000000000", headers=BO)
        ck("an unknown hand id is 404", r.status_code == 404, f"{r.status_code}")
        r = await cx.delete("/api/brain/decisions/hand:nonsense", headers=BO)
        ck("a malformed hand id is 404", r.status_code == 404, f"{r.status_code}")
        rooted = (await cx.post("/api/brain/decisions", json={"title": "Root's", "text": "Root's.", "decided_by": "Bo Chen"})).json()["id"]
        r = await cx.delete(f"/api/brain/decisions/{rooted}", headers=BO)
        ck("a decision the root token added has no person: a member may not delete it", r.status_code == 403 and ("decisions", rooted) in lore.documents, f"{r.status_code}")
        r = await cx.delete(f"/api/brain/decisions/{rooted}", headers=ADMIN)
        ck("... an admin may", r.status_code == 200 and not held(), f"{r.status_code}")
        # what lore may have kept or not: an entry without the adder's id is the admin's to delete
        await put("decisions", "hand:11111111-1111-4111-8111-111111111111", "No adder id", {
            "where": "by_hand", "decided_by": ["Bo Chen"], "decided_at": "2026-01-01T00:00:00Z", "added_by": "Bo Chen"})
        r = await cx.delete("/api/brain/decisions/hand:11111111-1111-4111-8111-111111111111", headers=BO)
        ck("an entry that lost its adder's id is not deletable by a member, even by its name", r.status_code == 403, f"{r.status_code}")
        r = await cx.delete("/api/brain/decisions/hand:11111111-1111-4111-8111-111111111111", headers=ADMIN)
        ck("... an admin deletes it", r.status_code == 200, f"{r.status_code}")
        # ids that are not by hand are never deleted through this route
        reset()
        await put("factory_projects", "project:pa:doc:d1", "A spec", {"where": "factory_project", "decided_by": ["Ann"], "decided_at": "2026-02-01", "project": "pa"})
        await put("acme_web", "docs/adr/0001.md", "A repo ADR", {"where": "repo_folder", "decided_by": ["Ann"], "decided_at": "2026-02-02", "repo": "acme/web"})
        await put("acme_web", "repo-adr-2", "Another", {"where": "repo_folder", "decided_by": ["Ann"], "decided_at": "2026-02-03", "repo": "acme/web"})
        await put("other_source", "hand:22222222-2222-4222-8222-222222222222", "Hand-shaped id elsewhere", {"where": "repo_folder", "decided_by": ["Ann"], "decided_at": "2026-02-04"})
        before = set(lore.documents)
        for did, want in (("project:pa:doc:d1", 400), ("repo-adr-2", 400), ("docs%2Fadr%2F0001.md", 405),
                          ("hand:22222222-2222-4222-8222-222222222222", 404)):
            r = await cx.delete(f"/api/brain/decisions/{did}", headers=ADMIN)
            ck(f"deleting {did} through this route is refused ({want}), even for an admin", r.status_code == want, f"{r.status_code} {r.text[:200]}")
        ck("... and nothing was deleted from lore", set(lore.documents) == before and not lore.calls("DELETE", "delete_document"))
        r = await cx.delete("/api/brain/decisions/project:pa:doc:d1", headers=ADMIN)
        ck("the 400 says why", "added by hand" in r.text, r.text[:200])

        print("== the list: every place, filters, counts ==")
        reset()
        store.create_project("pa", "factory", "Alpha", {})
        store.create_project("pb", "factory", "Beta", {})
        store.create_project("sec", "factory", "Secret", {})
        store.set_project_scope("sec", "private")
        D = lambda by, at, **kw: {"decided_by": [by], "decided_at": at, **kw}
        await put("acme_web", "docs/adr/1.md", "Repo one", D("Ann", "2026-03-10", where="repo_folder", repo="acme/web", changed_by=["Bo"], changed_at="2026-03-12"), uri="https://github.com/acme/web/blob/main/docs/adr/1.md")
        await put("acme_web", "docs/adr/2.md", "Repo two", D("Ann", "2026-03-05", where="repo_folder", repo="acme/web"))
        await put("acme_api", "docs/adr/1.md", "Api one", D("Cy", "2026-03-01", where="repo_folder", repo="acme/api"))
        await put("factory_projects", "project:pa:doc:s1", "Alpha spec", D("Ann", "2026-03-08T10:00:00Z", where="factory_project", project="pa"))
        await put("factory_projects", "project:pb:doc:s1", "Beta plan", D("Bo", "2026-02-20T10:00:00Z", where="factory_project", project="pb"))
        await put("factory_projects", "project:sec:doc:s1", "Secret spec", D("Ann", "2026-04-01T10:00:00Z", where="factory_project", project="sec"))
        await put("factory_projects", "project:gone:doc:s1", "Gone project", D("Ann", "2026-04-02T10:00:00Z", where="factory_project", project="gone"))
        await put("misc", "m1", "No where", {"decided_by": ["Dee"], "decided_at": "2026-02-15"})
        await put("factory_projects", "x1", "A secret one by its project", D("Ann", "2026-04-03", project="sec"))
        await put("acme_web", "note-1", "Not a decision", {}, kind="note")
        hand = []
        for i, (t, at) in enumerate((("Hand A", "2026-03-20T09:00:00Z"), ("Hand B", "2026-03-02T09:00:00Z"))):
            j = (await add({"title": t, "text": f"{t} text. More."}, BO)).json()
            fixed(j["id"], at)
            hand.append(j["id"])
        visible_order = ["Hand A", "Repo one", "Alpha spec", "Repo two", "Hand B", "Api one", "Beta plan", "No where"]
        r = await listing()
        j = r.json()
        titles = [d["title"] for d in j["decisions"]]
        ck("all three places mixed, newest first, hidden ones left out", r.status_code == 200 and titles == visible_order, str(titles))
        ck("counts: all, per place; a decision with no where is only in all", j["counts"] == {"all": 8, "repo_folder": 3, "factory_project": 2, "by_hand": 2}, str(j["counts"]))
        by = {d["title"]: d for d in j["decisions"]}
        ck("a repo row: id, source, uri, who decided and changed, dates, repo, no project, no summary", by["Repo one"] == {
            "id": "docs/adr/1.md", "source": "acme_web", "title": "Repo one", "summary": None, "where": "repo_folder", "decided_by": ["Ann"],
            "changed_by": ["Bo"], "decided_at": "2026-03-10", "changed_at": "2026-03-12", "link": "https://github.com/acme/web/blob/main/docs/adr/1.md",
            "repo": "acme/web", "project": None, "added_by": None}, str(by["Repo one"]))
        ck("a project row names its project", by["Alpha spec"]["project"] == "pa" and by["Alpha spec"]["where"] == "factory_project" and by["Alpha spec"]["id"] == "project:pa:doc:s1")
        ck("a hand row has its summary and adder", by["Hand A"]["summary"] == "Hand A text." and by["Hand A"]["added_by"] == "Bo Chen" and by["Hand A"]["id"] == hand[0])
        ck("a row with no where lists where null", by["No where"]["where"] is None and by["No where"]["decided_by"] == ["Dee"])
        ck("the private project's and the unknown project's decisions are not listed", not {"Secret spec", "Gone project", "A secret one by its project"} & set(titles))
        ck("... and not in any count (private and unknown dropped from all)", j["counts"]["all"] == 8)
        ck("a note is not a decision", "Not a decision" not in titles)
        ck("the page says it is the last", j["next_cursor"] is None)
        ck("the lore listing asked for decisions of every source", all(c.query.get("kind") == "decision" and "source" not in c.query for c in lore.calls("GET", "list_documents")))

        for w, want in (("repo_folder", ["Repo one", "Repo two", "Api one"]), ("factory_project", ["Alpha spec", "Beta plan"]), ("by_hand", ["Hand A", "Hand B"])):
            r = await listing(where=w)
            ck(f"where={w} lists that place only", [d["title"] for d in r.json()["decisions"]] == want, str([d["title"] for d in r.json()["decisions"]]))
            ck(f"... counts do not follow the filter (where={w})", r.json()["counts"] == j["counts"], str(r.json()["counts"]))
        r = await listing(where="all")
        ck("where=all is no filter", len(r.json()["decisions"]) == 8)
        r = await listing(repo="ACME/Web")
        ck("repo=ACME/Web (any case) lists that repo's decisions", [d["title"] for d in r.json()["decisions"]] == ["Repo one", "Repo two"] and r.json()["counts"] == j["counts"], r.text[:300])
        r = await listing(repo="acme/web", where="factory_project")
        ck("repo and where together can match nothing", r.json()["decisions"] == [] and r.json()["next_cursor"] is None and r.json()["counts"]["all"] == 8, r.text[:300])
        r = await listing(repo="nobody/nothing")
        ck("a repo with no decisions lists none", r.status_code == 200 and r.json()["decisions"] == [])
        for q, why in (({"where": "junk"}, "an unknown where"), ({"limit": 0}, "limit 0"), ({"limit": 201}, "limit 201"), ({"cursor": "not-a-cursor"}, "a cursor lore does not know")):
            r = await listing(**q)
            ck(f"{why} is a 400 in plain words", r.status_code == 400 and bool(r.json().get("detail")), f"{r.status_code} {r.text[:200]}")

        print("== paging by cursor ==")
        for lim in (1, 2, 3):
            seen, cursor, pages = [], None, 0
            while True:
                q = {"limit": lim, **({"cursor": cursor} if cursor else {})}
                r = await listing(**q)
                jj = r.json()
                pages += 1
                seen += [d["title"] for d in jj["decisions"]]
                ck(f"limit {lim}: a page holds at most {lim} and counts stay whole", len(jj["decisions"]) <= lim and jj["counts"] == j["counts"], r.text[:200])
                cursor = jj["next_cursor"]
                if not cursor or pages > 20:
                    break
            ck(f"limit {lim}: every decision once, in order, no repeat or gap", seen == visible_order, str(seen))
        r = await listing(limit=1)
        ck("limit 1: the first page is the newest, and a private decision newer than all of them does not leave it empty",
           [d["title"] for d in r.json()["decisions"]] == ["Hand A"] and r.json()["next_cursor"], r.text[:300])
        for w, want in (("repo_folder", ["Repo one", "Repo two", "Api one"]), ("by_hand", ["Hand A", "Hand B"]), ("factory_project", ["Alpha spec", "Beta plan"])):
            seen, cursor, pages = [], None, 0
            while pages < 20:
                r = await listing(where=w, limit=1, **({"cursor": cursor} if cursor else {}))
                jj = r.json()
                pages += 1
                seen += [d["title"] for d in jj["decisions"]]
                ck(f"where={w}, limit 1: a page is one row until the end", len(jj["decisions"]) == 1 or (not jj["next_cursor"] and len(jj["decisions"]) <= 1), r.text[:200])
                cursor = jj["next_cursor"]
                if not cursor:
                    break
            ck(f"where={w}, limit 1: the pages together are the place, once each, in order", seen == want, str(seen))
        seen, cursor = [], None
        for _ in range(10):
            jj = (await listing(where="repo_folder", limit=2, **({"cursor": cursor} if cursor else {}))).json()
            seen += [d["title"] for d in jj["decisions"]]
            cursor = jj["next_cursor"]
            if not cursor:
                break
        ck("where=repo_folder, limit 2: no gap across the pages", seen == ["Repo one", "Repo two", "Api one"], str(seen))
        clean("no key in any request or log line of the list")

        print("== counts when nothing is there, and many more than a page ==")
        reset()
        r = await listing()
        ck("an empty brain: nothing, zero counts", r.json() == {"decisions": [], "next_cursor": None, "counts": {"all": 0, "repo_folder": 0, "factory_project": 0, "by_hand": 0}}, r.text[:200])
        async with lore.client() as c:
            for start in range(0, 450, 150):
                await c.put_documents("bulk", [{"external_id": f"b{i}", "title": f"B{i}", "text": "t", "kind": "decision", "metadata": D("Ann", "2026-01-01", where="repo_folder", repo="acme/web")}
                                               for i in range(start, start + 150)])
        r = await listing(limit=5)
        ck("counts walk every lore page: 450 decisions are 450 (lore pages are 200)", r.json()["counts"]["all"] == 450 and r.json()["counts"]["repo_folder"] == 450 and len(r.json()["decisions"]) == 5, str(r.json()["counts"]))

        print("== the gates ==")
        reset()
        os.environ["TARES_BRAIN_KEY"] = ""
        for method, path in (("GET", "/api/brain/decisions"), ("POST", "/api/brain/decisions"), ("DELETE", "/api/brain/decisions/hand:00000000-0000-4000-8000-000000000000")):
            r = await cx.request(method, path, json={"title": "T", "text": "t"} if method == "POST" else None, headers=BO)
            ck(f"no brain settings: {method} is 404 as if not mounted", r.status_code == 404 and r.json() == {"detail": "Not Found"}, f"{r.status_code} {r.text[:100]}")
        os.environ["TARES_BRAIN_KEY"] = BRAIN_KEY
        ck("... and nothing reached lore", not lore.requests)
        r = await cx.delete("/api/licence")
        ck("the licence is removed", r.status_code == 200, r.text[:200])
        for method, path in (("GET", "/api/brain/decisions"), ("POST", "/api/brain/decisions"), ("DELETE", "/api/brain/decisions/hand:00000000-0000-4000-8000-000000000000")):
            r = await cx.request(method, path, json={"title": "T", "text": "t"} if method == "POST" else None, headers=BO)
            ck(f"no licence: {method} is 404 as if not mounted", r.status_code == 404 and r.json() == {"detail": "Not Found"}, f"{r.status_code} {r.text[:100]}")
        ck("... and nothing reached lore", not lore.requests)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as bare:
            r = await bare.get("/api/brain/decisions")
        ck("no credential at all is refused by the daemon", r.status_code == 401, f"{r.status_code}")
        await cx.aclose()


async def auth_off():
    """No TARES_AUTH_TOKEN: the one person is the adder and the admin."""
    os.environ["TARES_DB"] = os.path.join(TMP, "off.duckdb")
    os.environ["TARES_LICENSE"] = licensed.test_licence()
    from tares.daemon import make_app
    lore = fake_lore.FakeLore()
    repos_mod.lore_client = lore.client
    os.environ["TARES_BRAIN_URL"] = fake_lore.BASE_URL
    os.environ["TARES_BRAIN_KEY"] = BRAIN_KEY

    async def never(self):
        await asyncio.Event().wait()
    index_mod.Indexer.run = never
    sync_mod.Syncer.run = never
    app = make_app()
    async with app.router.lifespan_context(app):
        local = app.state.store.get_person("local")
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
        print("== auth off ==")
        r = await cx.post("/api/brain/decisions", json={"title": "Mine", "text": "Mine."})
        j = r.json()
        ck("it adds with no key: the local person decided and added it", r.status_code == 201 and j["decided_by"] == [local["name"]] and j["added_by"] == local["name"], f"{r.status_code} {r.text[:200]}")
        r = await cx.get("/api/brain/decisions")
        ck("... and lists it", r.status_code == 200 and [d["id"] for d in r.json()["decisions"]] == [j["id"]] and r.json()["counts"]["by_hand"] == 1, r.text[:200])
        r = await cx.delete(f"/api/brain/decisions/{j['id']}")
        ck("... and deletes it (the adder and the admin)", r.status_code == 200 and not lore.documents, f"{r.status_code} {r.text[:200]}")
        r = await cx.post("/api/brain/decisions", json={"title": "Mine", "text": "Mine.", "decided_by": local["name"]})
        ck("naming the local person works", r.status_code == 201, r.text[:200])
        await cx.aclose()


if AUTH_OFF:
    asyncio.run(auth_off())
else:
    unit()
    os.environ["TARES_LICENSE"] = ""
    asyncio.run(main())
    env = {k: v for k, v in os.environ.items() if k not in ("TARES_AUTH_TOKEN", "TARES_LICENSE")}
    off = subprocess.run([sys.executable, os.path.abspath(__file__), "--auth-off"], env=env)
    F += 1 if off.returncode else 0
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
