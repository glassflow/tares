# Tares enterprise licence, see ee/LICENSE
"""The files of a repo's decision folders become decisions in the brain (TR-1210).

`tares_ee.brain.decisions_repo.RepoDecisions` puts every markdown file of a chosen folder into lore as a
decision under the source `repo_decisions`, with who wrote it first, who changed it last, when and in which
pull request, read from GitHub. Here it runs against the shared fake lore (ee/tests/fake_lore.py, which checks
a decision's metadata as lore does: one bad value is a 400 and nothing of the batch is written), the fake
control plane's licence helpers and a fake GitHub (an httpx mock transport that counts its calls; never the
real one).

Covered: the document of each file (external id, uri, title, text, metadata), who is named (a Tares person,
else the git name, else the login, else Unknown, null author), the pull request, a file changed since it
began, a file with one commit, a file with no date, a file GitHub no longer has, a file over 1 MB; the cache
(a second pass with an unchanged tree makes no commit, pull or blob call and puts nothing; a changed blob sha
refreshes only that file; a person linked later renames the author without a call); the rate limit (the pass
stops, keeps what it put, the next pass finishes); batches of 50; a token that cannot be found; the PUT
folders route as the trigger; a folder no longer chosen is not imported, also when it is dropped while the
import runs; a request while an import runs is queued once; the start-up pass; no licence and no lore
settings do nothing; a lore 400 or 500 does not crash and is logged without the key; the token and the key
never reach a log line or lore.

In process, no ports. With CI's Postgres run (TARES_TEST_PG and tests/pg_harness on PYTHONPATH) the same
script keeps its state in Postgres.

Run: .venv/bin/python ee/tests/test_brain_decisions_repo.py
"""
import asyncio, base64, logging, os, sys, tempfile
from collections import Counter
from urllib.parse import unquote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import licensed   # scrubs the machine's TARES_* settings, puts the repo, ee/ and tests/ on sys.path
licensed.install_test_root()

TMP = tempfile.mkdtemp(prefix="tares-brain-decisions-repo-")
ROOT = "root-token-" + "brain-decisions-repo"
os.environ["TARES_DB"] = os.path.join(TMP, "t.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_AUTH_TOKEN"] = ROOT

import httpx

import fake_lore
from tares import github_credentials
from tares_ee.brain import decisions_repo as dr
from tares_ee.brain import folders as folders_mod
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


BRAIN_KEY = fake_lore.FakeLore().key   # the key the fake lore accepts and the client sends
GH_TOKEN = "gh-token-" + "for-the-tests-9f3a"       # gitleaks:allow
SOURCE = "repo_decisions"
NOW = "2026-01-01T00:00:00Z"


class Logs(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())

    def mine(self):
        return [l for l in self.lines if l.startswith("brain repo decisions")]


# -- a fake GitHub ----------------------------------------------------------------------------

def cm(sha, login, name, date):
    """A commit as the fake GitHub keeps it: `login` None is a deleted account, `date` None is a commit with
    no usable date."""
    return {"sha": sha, "login": login, "name": name, "date": date}


def pull(n, merged=None):
    return {"html_url": f"https://github.com/acme/web/pull/{n}", "merged_at": merged}


def fil(text, commits, prs=None, sha=None, size=None, missing=False, pulls_status=200):
    """A file: `commits` newest first, `prs` {commit sha: [pull requests]}."""
    return {"text": text, "commits": commits, "prs": prs or {}, "sha": sha or ("blob-" + commits[0]["sha"]),
            "size": size, "missing": missing, "pulls_status": pulls_status}


class Gh:
    """GitHub's tree, blob, commit-history and commit-pulls routes for a few repos, counting every call."""

    def __init__(self):
        self.repos = {}                  # repo -> {path: file}
        self.truncated = set()           # repos whose tree answers truncated
        self.counts = Counter()          # tree, blob, commits, pulls
        self.requests = []               # (path, query, authorization)
        self.pages = []                  # the page of every commits call that had one
        self.budget = None               # calls left before a 403 rate limit; None is unlimited
        self.gate = None                 # (route, asyncio.Event): that route waits for the event
        self.reached = asyncio.Event()   # set when a gated call arrives
        self.limit_headers = {"x-ratelimit-remaining": "0"}

    def reset(self):
        self.counts.clear()
        self.requests.clear()
        self.pages.clear()

    async def handle(self, request):
        path = request.url.raw_path.decode().split("?")[0]
        q = dict(request.url.params)
        self.requests.append((path, q, request.headers.get("authorization")))
        parts = path.strip("/").split("/")       # repos, owner, name, ...
        repo = f"{parts[1]}/{parts[2]}"
        rest = "/".join(parts[3:])
        route = ("tree" if rest.startswith("git/trees/") else "blob" if rest.startswith("git/blobs/")
                 else "pulls" if rest.endswith("/pulls") else "commits")
        self.counts[route] += 1
        if self.gate and self.gate[0] == route:
            self.reached.set()
            await self.gate[1].wait()
        if self.budget is not None:
            if self.budget <= 0:
                return httpx.Response(403, headers=self.limit_headers, json={"message": "API rate limit exceeded"})
            self.budget -= 1
        files = self.repos.get(repo)
        if files is None:
            return httpx.Response(404, json={"message": "Not Found"})
        if route == "tree":
            tree = [{"path": p, "type": "blob", "sha": f["sha"], **({"size": f["size"]} if f["size"] else {})}
                    for p, f in files.items()]
            tree += [{"path": "docs", "type": "tree", "sha": "t1"}]
            return httpx.Response(200, json={"tree": tree, "truncated": repo in self.truncated})
        if route == "blob":
            sha = unquote(parts[-1])
            f = next((f for f in files.values() if f["sha"] == sha), None)
            if f is None or f["missing"]:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"encoding": "base64", "size": len(f["text"]),
                                             "content": base64.encodebytes(f["text"].encode()).decode()})
        if route == "pulls":
            sha = parts[-2]
            f = next((f for f in files.values() if any(c["sha"] == sha for c in f["commits"])), None)
            if f and f["pulls_status"] != 200:
                return httpx.Response(f["pulls_status"], json={"message": "No commit found"})
            return httpx.Response(200, json=(f or {"prs": {}})["prs"].get(sha, []))
        f = files.get(unquote(q.get("path", "")))
        if f is None:
            return httpx.Response(200, json=[])
        page = int(q.get("page", "1"))
        if "page" in q:
            self.pages.append(page)
        n = len(f["commits"])
        headers = {}
        if n > 1 and page == 1:
            base = f"https://api.github.com/repositories/9/commits?path={q['path']}&per_page=1"
            headers["link"] = f'<{base}&page=2>; rel="next", <{base}&page={n}>; rel="last"'
        if page > n:
            return httpx.Response(200, json=[])
        c = f["commits"][page - 1]
        git = {"name": c["name"], **({"date": c["date"]} if c["date"] else {})}
        body = {"sha": c["sha"], "commit": {"author": git, "committer": dict(git)},
                "author": {"login": c["login"]} if c["login"] else None}
        return httpx.Response(200, json=[body], headers=headers)


def web_files():
    """acme/web: six decisions in docs/adr (one nested, one with a space in its name), and the files that
    are not."""
    a1, a2, a3 = cm("a1", "alice", "Alice Git", "2024-01-02T10:00:00Z"), cm("a2", "alice", "Alice A.", "2024-03-01T12:00:00Z"), \
        cm("a3", "bob", "Bob B", "2024-06-05T09:30:00Z")
    return {
        "docs/adr/0001-use-postgres.md": fil("# Use Postgres\n\nWe use Postgres.\n", [a3, a2, a1],
                                             {"a1": [pull(3), pull(12, "2024-01-03T00:00:00Z")]}),
        "docs/adr/0002-queue.md": fil("## Context\nA queue.\n", [cm("b1", "carol", "Carol C", "2024-02-02T08:00:00+02:00")],
                                      {"b1": [pull(20)]}),
        "docs/adr/archive/0003-old.md": fil("```\n# not a title\n```\n# Old thing #\nBody\n", [cm("c1", None, "Dan D", "2023-05-05T05:05:05Z")]),
        "docs/adr/README.md": fil("Index of decisions\n", [cm("d1", "erin", "", "2023-01-01T00:00:00Z")]),
        "docs/adr/ghost.md": fil("# Ghost\n", [cm("e1", None, "  ", "2023-02-02T00:00:00Z")], pulls_status=404),
        "docs/adr/0004 spaced name.md": fil("# Spaced\n", [cm("f1", "frank", "Frank F", "2023-03-03T00:00:00Z")]),
        "docs/adr/nodate.md": fil("# No date\n", [cm("g1", "gus", "Gus G", None)]),
        "docs/adr/missing.md": fil("# Gone\n", [cm("h1", "hal", "Hal H", "2023-04-04T00:00:00Z")], missing=True),
        "docs/adr/big.md": fil("# Big\n", [cm("i1", "ivy", "Ivy I", "2023-04-04T00:00:00Z")], size=2_000_000),
        "docs/adr/template.txt": fil("not markdown\n", [cm("j1", "jo", "Jo J", "2023-04-04T00:00:00Z")]),
        "src/main.py": fil("print(1)\n", [cm("k1", "kim", "Kim K", "2023-04-04T00:00:00Z")]),
        "other/notes.md": fil("# Elsewhere\n", [cm("l1", "lee", "Lee L", "2023-04-04T00:00:00Z")]),
    }


def simple_files(n, prefix="f", folder="docs/adr"):
    return {f"{folder}/{prefix}{i:03d}.md": fil(f"# {prefix}{i}\n", [cm(f"{prefix}c{i}", "zed", "Zed Z", "2024-01-01T00:00:00Z")])
            for i in range(n)}


# -- pure parts -------------------------------------------------------------------------------

def unit():
    print("== the pure parts ==")
    ck("the title is the first # heading", dr.title_of("a/b.md", "intro\n# Use X #\n# Second\n") == "Use X")
    ck("a heading inside a code fence is not the title", dr.title_of("a/b.md", "```\n# no\n```\n# Yes\n") == "Yes")
    ck("a ## heading is not a title; the file name is, without its extension", dr.title_of("a/0007-x.md", "## sub\n") == "0007-x"
       and dr.title_of("a/notes.mdx", "") == "notes")
    ck("the uri is the file at HEAD, path segments quoted", dr.file_uri("acme/web", "docs/adr/0004 a#b.md")
       == "https://github.com/acme/web/blob/HEAD/docs/adr/0004%20a%23b.md")
    ck("an id is the repo, a colon and the path", dr.external_id("acme/web", "docs/a.md") == "acme/web:docs/a.md")
    ck("a name: the person, else the git name, else the login, else Unknown",
       dr.author_name({"login": "a", "name": "Git A"}, "Person A") == "Person A"
       and dr.author_name({"login": "a", "name": "Git A"}, None) == "Git A"
       and dr.author_name({"login": "a", "name": " "}, None) == "a"
       and dr.author_name({"login": None, "name": None}, None) == dr.UNKNOWN)
    ck("a name is cut to 256 bytes, not in a character", len(dr.author_name({"name": "é" * 300}, None).encode()) <= 256)
    ck("only RFC 3339 times pass", dr._rfc3339("2024-01-02T10:00:00Z") and dr._rfc3339("2024-01-02T10:00:00+02:00")
       and not dr._rfc3339("2024-01-02") and not dr._rfc3339(None) and not dr._rfc3339("2024-13-02T10:00:00Z"))
    first = {"sha": "1", "login": "x", "name": "X", "date": "2024-01-02T10:00:00Z"}
    last = {"sha": "2", "login": "y", "name": "Y", "date": "2024-02-03T10:00:00Z"}
    d = dr.build_document("a/b", "docs/adr/x.md", {"text": "# T\n", "first": first, "last": last, "pr": None}, {})
    ck("no pr key without a pull request", "pr" not in d["metadata"] and d["metadata"]["changed_at"] == "2024-02-03", str(d))
    same = dr.build_document("a/b", "x.md", {"text": "", "first": first, "last": first, "pr": "https://x/pull/1"}, {})
    ck("one commit: no changed_*, an empty file still builds", "changed_by" not in same["metadata"] and same["text"] == ""
       and same["metadata"]["pr"] == "https://x/pull/1", str(same))
    ck("no date: no document", dr.build_document("a/b", "x.md", {"text": "", "first": {**first, "date": None}}, {}) is None)
    nul = dr.build_document("a/b", "docs/adr/x.md", {"text": "# T\x00itle\nbo\x00dy\n", "first": first, "last": first, "pr": None}, {})
    ck("a NUL is taken out of the text and the title (lore refuses the batch for one)",
       "\x00" not in nul["text"] and "\x00" not in nul["title"] and nul["title"] == "Title" and nul["text"] == "# Title\nbody\n", str(nul))


# -- the scenarios ----------------------------------------------------------------------------

async def main():
    from tares.daemon import make_app
    logs = Logs()
    logging.getLogger().addHandler(logs)
    logging.getLogger().setLevel(logging.DEBUG)
    lore = fake_lore.FakeLore()
    repos_mod.lore_client = lore.client
    gh = Gh()
    gh.repos["acme/web"] = web_files()
    dr.TRANSPORT = httpx.MockTransport(gh.handle)   # whatever an importer does, it never reaches a real GitHub

    async def never(self):                # the indexer's and syncer's own tasks are not under test here
        await asyncio.Event().wait()
    index_mod.Indexer.run = never
    sync_mod.Syncer.run = never
    known = ["acme/web", "acme/api", "acme/big", "acme/q", "acme/stale", "acme/cleared", "acme/cut", "acme/drop", "acme/boot",
             "acme/lorefail", "acme/nul"]

    async def readable_repos(cred, query=""):      # acme/lost is not readable by any connection
        return [{"full_name": r} for r in known]
    github_credentials.list_repos_for = readable_repos

    def docs():
        return {ext: d for (src, ext), d in lore.documents.items() if src == SOURCE}

    def puts():
        return lore.calls("POST", "put_documents")

    def choose(store, repo, folders):
        if store.get_brain_repo(repo) is None:
            store.add_brain_repo(repo, "src-" + repo, None, NOW)
        return store.set_brain_folders(repo, folders, None, NOW)

    app = make_app()
    async with app.router.lifespan_context(app):
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=H(ROOT))
        store = app.state.store
        rd = app.state.brain_repo_decisions
        store.upsert_github_credential("setup", GH_TOKEN)
        ck("the cell has one importer, its hook is on the folder changes", rd is not None and rd.hook in folders_mod.HOOKS)
        FOLDERS = "/api/brain/repos/acme/web/folders"
        choose(store, "acme/web", [])

        print("== no licence, no brain settings: nothing runs ==")
        os.environ["TARES_BRAIN_URL"] = fake_lore.BASE_URL
        os.environ["TARES_BRAIN_KEY"] = BRAIN_KEY
        choose(store, "acme/web", ["docs/adr"])
        await rd.hook("acme/web", ["docs/adr"], [])
        await rd.wait()
        ck("no licence: no GitHub call, no lore call", not gh.requests and not lore.requests, f"{len(gh.requests)} {len(lore.requests)}")
        r = await cx.put("/api/licence", json={"key": licensed.test_licence()})
        ck("the licence is stored", r.status_code == 200, r.text[:200])
        del os.environ["TARES_BRAIN_URL"], os.environ["TARES_BRAIN_KEY"]
        await rd.hook("acme/web", ["docs/adr"], [])
        await rd.wait()
        ck("no brain settings: no GitHub call, no lore call", not gh.requests and not lore.requests, f"{len(gh.requests)} {len(lore.requests)}")
        os.environ["TARES_BRAIN_URL"] = fake_lore.BASE_URL
        os.environ["TARES_BRAIN_KEY"] = BRAIN_KEY
        choose(store, "acme/web", [])

        print("== the PUT route imports the files of a chosen folder ==")
        bob = store.create_person("Bob Builder", "bob@x.dev", github="Bob")
        r = await cx.put(FOLDERS, json={"folders": ["docs/adr"]})
        ck("the PUT answers at once with what was added", r.status_code == 200 and r.json()["added"] == ["docs/adr"], r.text[:200])
        await rd.wait()
        got = docs()
        want = {"acme/web:docs/adr/0001-use-postgres.md", "acme/web:docs/adr/0002-queue.md", "acme/web:docs/adr/archive/0003-old.md",
                "acme/web:docs/adr/README.md", "acme/web:docs/adr/ghost.md", "acme/web:docs/adr/0004 spaced name.md"}
        ck("six decisions: markdown only, nested too, README too; not .txt, not other folders, not the no-date, gone or big file",
           set(got) == want, str(sorted(got)))
        ck("all are of kind decision, with a uri and where repo_folder", all(d["kind"] == "decision" and d["metadata"]["where"] == "repo_folder"
           and d["metadata"]["repo"] == "acme/web" for d in got.values()))
        a = got["acme/web:docs/adr/0001-use-postgres.md"]
        ck("the document: title from the heading, the text, the uri", a["title"] == "Use Postgres" and a["text"] == "# Use Postgres\n\nWe use Postgres.\n"
           and a["uri"] == "https://github.com/acme/web/blob/HEAD/docs/adr/0001-use-postgres.md", str(a))
        ck("decided by the first commit's author (git name: alice is no person yet), at that commit's date",
           a["metadata"]["decided_by"] == ["Alice Git"] and a["metadata"]["decided_at"] == "2024-01-02T10:00:00Z", str(a["metadata"]))
        ck("the pr is the merged pull request of the FIRST commit", a["metadata"]["pr"] == "https://github.com/acme/web/pull/12", str(a["metadata"]))
        ck("changed by the last commit's author (a Tares person: the person's name) on that date",
           a["metadata"]["changed_by"] == ["Bob Builder"] and a["metadata"]["changed_at"] == "2024-06-05", str(a["metadata"]))
        ck("the first commit came from the last page of the history", gh.pages.count(3) == 1 and gh.pages.count(2) == 0, str(gh.pages))
        b = got["acme/web:docs/adr/0002-queue.md"]
        ck("one commit: no changed_*; a title from the file name; an unmerged pull request when it is the only one",
           b["title"] == "0002-queue" and b["metadata"]["decided_by"] == ["Carol C"] and "changed_by" not in b["metadata"]
           and "changed_at" not in b["metadata"] and b["metadata"]["pr"] == "https://github.com/acme/web/pull/20"
           and b["metadata"]["decided_at"] == "2024-02-02T08:00:00+02:00", str(b))
        c = got["acme/web:docs/adr/archive/0003-old.md"]
        ck("a deleted GitHub account (null author): the git name; no pr key when there is no pull request; a title past a fence",
           c["metadata"]["decided_by"] == ["Dan D"] and "pr" not in c["metadata"] and c["title"] == "Old thing", str(c))
        d = got["acme/web:docs/adr/README.md"]
        ck("an empty git name falls back to the login", d["metadata"]["decided_by"] == ["erin"] and d["title"] == "README", str(d))
        g = got["acme/web:docs/adr/ghost.md"]
        ck("no name and no login is Unknown; a pulls 404 is no pr", g["metadata"]["decided_by"] == ["Unknown"] and "pr" not in g["metadata"], str(g))
        ck("a file name with a space is quoted in the uri", got["acme/web:docs/adr/0004 spaced name.md"]["uri"]
           == "https://github.com/acme/web/blob/HEAD/docs/adr/0004%20spaced%20name.md")
        ck("every GitHub call carried the token", gh.requests and all(x[2] == f"Bearer {GH_TOKEN}" for x in gh.requests))
        ck("the too-large file was never read; the missing one was tried", not any("blob-i1" in x[0] for x in gh.requests)
           and any("blob-h1" in x[0] for x in gh.requests))
        ck("the log says what was left out, by count", any("1 files have no usable commit date" in l for l in logs.mine())
           and any("1 files are over" in l for l in logs.mine()), str(logs.mine()))
        ck("one put, in the source repo_decisions", len(puts()) == 1 and puts()[0].json["source"] == SOURCE, str(len(puts())))

        print("== a second pass with an unchanged tree costs only the tree ==")
        gh.reset()
        n = len(puts())
        await rd.import_folders("acme/web", {"docs/adr"})
        ck("one tree call; no blob, commits or pulls call", dict(gh.counts) == {"tree": 1}, str(gh.counts))
        ck("... and no lore put", len(puts()) == n)

        print("== a changed blob refreshes only that file ==")
        files = gh.repos["acme/web"]
        files["docs/adr/0002-queue.md"]["sha"] = "blob-b2"
        files["docs/adr/0002-queue.md"]["text"] = "# Queue\nNow with Kafka.\n"
        files["docs/adr/0002-queue.md"]["commits"].insert(0, cm("b2", "carol", "Carol C", "2024-07-07T07:07:07Z"))
        gh.reset()
        await rd.import_folders("acme/web", {"docs/adr"})
        ck("that file's blob, history (two pages) and pull request were read again, nothing else",
           dict(gh.counts) == {"tree": 1, "blob": 1, "commits": 2, "pulls": 1}, str(gh.counts))
        ck("one put with one document", len(puts()) == n + 1 and len(puts()[-1].json["documents"]) == 1
           and puts()[-1].json["documents"][0]["external_id"] == "acme/web:docs/adr/0002-queue.md")
        b = docs()["acme/web:docs/adr/0002-queue.md"]
        ck("its title, text and changed_* follow", b["title"] == "Queue" and b["metadata"]["changed_by"] == ["Carol C"]
           and b["metadata"]["changed_at"] == "2024-07-07" and b["metadata"]["decided_by"] == ["Carol C"], str(b))

        print("== a person linked later renames the author with no GitHub call ==")
        store.create_person("Alice Archer", "alice@x.dev", github="alice")
        gh.reset()
        n = len(puts())
        await rd.import_folders("acme/web", {"docs/adr"})
        ck("the author is now the person's name", docs()["acme/web:docs/adr/0001-use-postgres.md"]["metadata"]["decided_by"] == ["Alice Archer"])
        ck("... from the cache: one tree call, one put of one document", dict(gh.counts) == {"tree": 1} and len(puts()) == n + 1
           and len(puts()[-1].json["documents"]) == 1, f"{gh.counts} {len(puts())}")

        print("== a rate limit stops the pass; the next one finishes ==")
        gh.repos["acme/api"] = simple_files(6)
        choose(store, "acme/api", ["docs/adr"])
        gh.reset()
        gh.budget = 1 + 3 * 2 + 1          # the tree, two whole files, the blob of the third
        n = len(puts())
        await rd.import_folders("acme/api", {"docs/adr"})
        mine = {e for e in docs() if e.startswith("acme/api:")}
        ck("the two finished files were put, the rest waits", mine == {"acme/api:docs/adr/f000.md", "acme/api:docs/adr/f001.md"}, str(sorted(mine)))
        ck("the log says so, by type, with no token", any("rate limit" in l for l in logs.mine()) and not any(GH_TOKEN in l for l in logs.lines))
        gh.budget = None
        gh.reset()
        await rd.import_folders("acme/api", {"docs/adr"})
        ck("the next pass has all six", len({e for e in docs() if e.startswith("acme/api:")}) == 6)
        ck("... and asked only for what it lacked: the third file's history, nothing twice",
           dict(gh.counts) == {"tree": 1, "blob": 3, "commits": 4, "pulls": 4}, str(gh.counts))

        print("== more than 50 files go in batches of 50 ==")
        gh.repos["acme/big"] = simple_files(120)
        choose(store, "acme/big", ["docs/adr"])
        n = len(puts())
        await rd.import_folders("acme/big", {"docs/adr"})
        sizes = [len(p.json["documents"]) for p in puts()[n:]]
        ck("50, 50 and 20", sizes == [50, 50, 20], str(sizes))

        print("== a folder that is no longer chosen is not imported ==")
        gh.repos["acme/stale"] = {**simple_files(2, "s", "docs/adr"), **simple_files(2, "o", "docs/old")}
        choose(store, "acme/stale", ["docs/adr"])            # docs/old was added, then dropped, before the hook ran
        await rd.hook("acme/stale", ["docs/adr", "docs/old"], [])
        await rd.wait()
        got = {e for e in docs() if e.startswith("acme/stale:")}
        ck("only the folder that is still chosen came in", got == {"acme/stale:docs/adr/s000.md", "acme/stale:docs/adr/s001.md"}, str(sorted(got)))
        gh.repos["acme/cleared"] = simple_files(2)
        choose(store, "acme/cleared", [])
        gh.reset()
        await rd.hook("acme/cleared", ["docs/adr"], [])
        await rd.wait()
        ck("a repo with nothing chosen any more makes no GitHub call", not gh.requests and not any(e.startswith("acme/cleared:") for e in docs()))
        gh.repos["acme/drop"] = simple_files(3)
        choose(store, "acme/drop", ["docs/adr"])
        gh.gate = ("commits", asyncio.Event())
        gh.reached.clear()
        n = len(puts())
        rd.request("acme/drop", ["docs/adr"])
        await asyncio.wait_for(gh.reached.wait(), 5)
        choose(store, "acme/drop", [])                          # dropped while the import runs
        gh.gate[1].set()
        gh.gate = None
        await rd.wait()
        ck("a folder dropped while the import runs is not put", len(puts()) == n and not any(e.startswith("acme/drop:") for e in docs()), str(len(puts()) - n))

        print("== a request while an import runs is queued once ==")
        gh.repos["acme/q"] = simple_files(2)
        choose(store, "acme/q", ["docs/adr"])
        gh.gate = ("tree", asyncio.Event())
        gh.reached.clear()
        gh.reset()
        rd.request("acme/q", ["docs/adr"])
        await asyncio.wait_for(gh.reached.wait(), 5)
        for _ in range(3):
            rd.request("acme/q", ["docs/adr"])
        ck("a second import is not started beside the first", len(rd._tasks) == 1 and gh.counts["tree"] == 1, str(gh.counts))
        gh.gate[1].set()
        gh.gate = None
        await rd.wait()
        ck("the three requests ran as one more import", gh.counts["tree"] == 2, str(gh.counts))

        print("== a repo no connection can read does nothing and does not raise ==")
        choose(store, "acme/lost", ["docs/adr"])
        gh.reset()
        before = len(lore.requests)
        await rd.hook("acme/lost", ["docs/adr"], [])
        await rd.wait()
        ck("no GitHub call, no lore call, a plain-words line in the log", not gh.requests and len(lore.requests) == before
           and any("cannot read acme/lost" in l for l in logs.mine()), str(logs.mine()[-2:]))
        ck("the folder change is still stored (the hook never raises into the PUT)", store.list_brain_folders("acme/lost") != [])

        print("== a truncated tree is imported as far as GitHub sent it ==")
        gh.repos["acme/cut"] = simple_files(2)
        gh.truncated.add("acme/cut")
        choose(store, "acme/cut", ["docs/adr"])
        await rd.import_folders("acme/cut", {"docs/adr"})
        ck("both files came in and the log says the tree was cut", len({e for e in docs() if e.startswith("acme/cut:")}) == 2
           and any("cut the tree of acme/cut short" in l for l in logs.mine()), str(logs.mine()[-2:]))

        print("== GitHub failing does not crash it ==")
        gh.repos["acme/boot"] = simple_files(2)
        choose(store, "acme/boot", ["docs/adr"])
        saved = rd.transport

        def boom(request):
            raise httpx.ConnectError("no route " + GH_TOKEN)
        rd.transport = httpx.MockTransport(boom)
        await rd.import_folders("acme/boot", {"docs/adr"})
        ck("a network failure is logged by its type, no token", any("ConnectError" in l for l in logs.mine()) and not any(GH_TOKEN in l for l in logs.lines))
        rd.transport = httpx.MockTransport(lambda request: httpx.Response(500, json={}))
        await rd.import_folders("acme/boot", {"docs/adr"})
        ck("a 500 ends the pass quietly", not any(e.startswith("acme/boot:") for e in docs()))
        rd.transport = saved

        print("== lore refusing does not crash it, and the key stays out of the log ==")
        gh.repos["acme/lorefail"] = simple_files(2)
        choose(store, "acme/lorefail", ["docs/adr"])
        for status in (500, 502):
            lore.fail("put_documents", status, "no thanks " + BRAIN_KEY)
            await rd.import_folders("acme/lorefail", {"docs/adr"})
            ck(f"a lore {status} is logged and nothing is stored", not any(e.startswith("acme/lorefail:") for e in docs())
               and any("could not put 2 documents of acme/lorefail" in l for l in logs.mine()), str(logs.mine()[-1:]))
        ck("the key is in no log line", not any(BRAIN_KEY in l for l in logs.lines), str([l[:200] for l in logs.lines if BRAIN_KEY in l]))
        await rd.import_folders("acme/lorefail", {"docs/adr"})
        ck("the next pass puts them (a failed put was not remembered as sent)", len({e for e in docs() if e.startswith("acme/lorefail:")}) == 2)

        print("== a file with a NUL in it, and a batch lore refuses ==")
        gh.repos["acme/nul"] = {**simple_files(2, "n"),
                                "docs/adr/z-nul.md": fil("# Bad\x00 title\nbody\x00\n", [cm("zc", "zed", "Zed Z", "2024-01-01T00:00:00Z")])}
        choose(store, "acme/nul", ["docs/adr"])
        await rd.import_folders("acme/nul", {"docs/adr"})
        got = {e: d for e, d in docs().items() if e.startswith("acme/nul:")}
        ck("a file with a NUL goes in, the NUL taken out, and the other files are not held back",
           len(got) == 3 and got["acme/nul:docs/adr/z-nul.md"]["title"] == "Bad title"
           and got["acme/nul:docs/adr/z-nul.md"]["text"] == "# Bad title\nbody\n", str(sorted(got)))
        for e in [e for e in docs() if e.startswith("acme/nul:")]:
            lore.documents.pop((SOURCE, e))
        rd._sent.clear()
        before = len(puts())
        lore.fail("put_documents", 400, "documents[1] is not acceptable " + BRAIN_KEY)   # the batch is refused once
        await rd.import_folders("acme/nul", {"docs/adr"})
        ck("a refused batch is put again one document at a time, so nothing stays out", len([e for e in docs() if e.startswith("acme/nul:")]) == 3
           and len(puts()) - before == 4, f"{len(puts()) - before} puts")
        ck("... and the refusal is logged without the key", any("putting them one at a time" in l for l in logs.mine())
           and not any(BRAIN_KEY in l for l in logs.lines))

        print("== no secret anywhere ==")
        ck("the GitHub token is in no log line and no lore request", not any(GH_TOKEN in l for l in logs.lines)
           and not any(GH_TOKEN in repr(r.json) or GH_TOKEN in r.path for r in lore.requests))
        ck("a put carried only the document's own fields", all(set(d) <= {"external_id", "title", "text", "kind", "uri", "metadata"}
           for p in puts() for d in p.json["documents"]))

        print("== the start-up pass imports folders chosen before it ran ==")
        gh.repos["acme/boot"] = simple_files(3, "b")
        started = choose(store, "acme/boot", ["docs/adr"])
        for path in [p for p in list(docs()) if p.startswith("acme/boot:")]:
            lore.documents.pop((SOURCE, path))
        rd._sent.clear()
        gh.reset()
        await rd.startup()
        await rd.wait()
        ck("every connected repo's chosen folders were imported", {e for e in docs() if e.startswith("acme/boot:")}
           == {f"acme/boot:docs/adr/b{i:03d}.md" for i in range(3)} and gh.counts["tree"] >= 8, f"{gh.counts}")
        ck("the cache served the repos it already knew (acme/api: no blob call again)", not any("blob-f" in x[0] and "api" in x[0] for x in gh.requests))
        await cx.aclose()
    ck("the hook is gone with the app", rd.hook not in folders_mod.HOOKS and rd.startup_task is None and not rd._tasks)

    print("== the app's own start-up pass ==")
    app2 = make_app()
    gh2 = Gh()
    gh2.repos["acme/web"] = web_files()
    dr.TRANSPORT = httpx.MockTransport(gh2.handle)
    lore.documents.clear()
    lore.requests.clear()
    logs.lines.clear()
    async with app2.router.lifespan_context(app2):
        rd2 = app2.state.brain_repo_decisions
        ck("a new app has a new importer with its hook on", rd2 is not rd and rd2.hook in folders_mod.HOOKS)
        await rd2.wait()
        ck("the folder chosen in the earlier run is imported by the new app's own start-up pass, nothing asked for",
           {e for e in docs() if e.startswith("acme/web:")} == want, str(sorted(docs())))
        ck("... and only the repos with chosen folders were read", {x[0].split("/")[3] for x in gh2.requests if x[0].startswith("/repos/")} <= {"web", "api", "big", "q", "stale", "drop", "cut", "boot", "lorefail", "lost", "nul"})
    ck("and its hook is removed when it stops", rd2.hook not in folders_mod.HOOKS)
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


if __name__ == "__main__":
    unit()
    os.environ["TARES_LICENSE"] = ""
    asyncio.run(main())
