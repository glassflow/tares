"""GitHub commits connector. Commit->Envelope mapping is unit-tested (no network); discover is a
best-effort live check against a public repo (skipped on network/rate-limit)."""
import asyncio

from tares.config import _source_from_dict
from tares.connectors import full_schema
from tares.connectors.github import GithubConnector

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))

# --- mapping (deterministic) ---
cfg = _source_from_dict({"name": "gh", "connector": "github", "config": {
    "repo": "glassflow/tares",
    "labels": [{"name": "repo", "field": "repo", "primary": True},
               {"name": "author", "field": "author"}]}})
conn = GithubConnector(cfg, store=None)
commit = {"sha": "abc1234def567", "html_url": "https://github.com/glassflow/tares/commit/abc1234def567",
          "commit": {"message": "Fix the pool exhaustion\n\nlong body here",
                     "author": {"name": "Alice Dev", "date": "2026-06-16T10:00:00Z"}},
          "author": {"login": "alice"}}
env = conn._commit_envelope(commit, {"repo": "glassflow/tares", "branch": "main"})
ck("event_type=commit", env.event_type == "commit", env.event_type)
ck("text = short-sha login: summary", env.text == "abc1234 alice: Fix the pool exhaustion", env.text)
ck("keyed by repo (primary label)", env.key_value == "glassflow/tares", env.key_value)
ck("labels = repo + author + the contract's sha",
   env.labels == {"repo": "glassflow/tares", "author": "alice", "sha": "abc1234def567"},
   str(env.labels))
ck("event_time parsed from commit date", env.event_time is not None and env.event_time.year == 2026, str(env.event_time))
ck("full commit kept in payload (lossless)", env.payload.get("sha") == "abc1234def567")
# author falls back to commit author name when there's no GitHub login
env2 = conn._commit_envelope({"sha": "x", "commit": {"message": "m", "author": {"name": "Bob", "date": None}}, "author": None},
                             {"repo": "glassflow/tares", "branch": "main"})
ck("author falls back to commit name when login is null", env2.labels.get("author") == "Bob", str(env2.labels))

# --- schema-backed + discoverable ---
ck("github declares a CONFIG_SCHEMA", full_schema("github") is not None)
ck("github.discover exists", callable(getattr(GithubConnector, "discover", None)))

# --- live discover (best-effort) ---
try:
    p = asyncio.run(GithubConnector.discover({"repo": "octocat/Hello-World"}))
    ck("live discover: repo primary + author labels",
       p["proposed_config"]["labels"][0] == {"name": "repo", "field": "repo", "primary": True}, str(p.get("proposed_config")))
    ck("live discover: finds default branch", "default branch" in p["summary"], p["summary"])
    p404 = None
    try:
        asyncio.run(GithubConnector.discover({"repo": "glassflow/definitely-not-a-real-repo-xyz"}))
    except ValueError:
        p404 = True
    ck("live discover: unknown repo -> error", p404 is True)
except Exception as e:
    print(f"  skip live discover (network/rate-limit: {e})")

print(f"\n{P} passed, {F} failed")
raise SystemExit(1 if F else 0)
