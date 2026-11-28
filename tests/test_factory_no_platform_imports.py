"""Factory code imports no platform module (Tares 2.0, TR-1808).

Tares 2.0 deletes the platform: the AI SRE edition, Tares agents and Ask, triggers, schedules and agent
dispatch, agent tracing, the source catalog and its connectors, the OTLP receiver, the platform reads,
project templates, the goal page and setup flow, the edition switch and the DuckDB move code. Before any
of it is deleted, nothing that stays may import it, so the deletions are only deletions.

This scans every module under tares/ and ee/tares_ee/ with the AST, lazy imports inside functions too,
and fails on an import of a GOING module from a module that is neither GOING nor MIXED. MIXED modules
stay but lose their platform parts in the deletion tickets, so they are not scanned here.

Run: python tests/test_factory_no_platform_imports.py
"""
import ast, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# modules that go, as dotted names (a package name covers everything under it)
GOING = {
    "tares.edition", "tares.sre", "tares.sre_home", "tares.sre_store", "tares.incidents", "tares.watch",
    "tares.timeline", "tares.feedback", "tares.slack_actions",
    "tares.builtin_agents", "tares.agent", "tares.mcp_client", "tares.github_tools", "tares.skills",
    "tares.triggers", "tares.schedule", "tares.dispatch", "tares.tracing",
    "tares.runtime", "tares.connectors", "tares.otlp_grpc", "tares.discovery", "tares.reads", "tares.stats",
    "tares.resources", "tares.results", "tares.saved_counts", "tares.status",
    "tares.goal", "tares.setup_flow", "tares.projects",
    "tares.state_db", "tares.session_digest", "tares.session_events", "tares.session_move",
    "tares.session_drain", "tares.session_store.copy", "tares.session_store.takeover",
}
GOING_PREFIXES = ("tares.sre_", "tares.agent_", "tares.session_move_", "tares.screens.sre_")

# modules that stay but shrink: their platform parts go in the deletion tickets
# (tares.routes.projects holds the project timeline route that moved out of tares.daemon, TR-1814)
MIXED = {"tares.store", "tares.config", "tares.daemon", "tares.cli", "tares.mcp_server",
         "tares.routes.projects", "tares.screens", "tares.mcp_outcomes", "tares.start_steps",
         "tares.slack"}


# Imports of a going module that stay until the ticket that removes their feature: (file, module) ->
# that ticket. The ticket deletes the line from here; an entry that no longer matches fails the test,
# so the list only shrinks.
KNOWN: dict = {}


def going(mod: str) -> bool:
    if mod in GOING or mod.startswith(GOING_PREFIXES):
        return True
    return any(mod.startswith(g + ".") for g in GOING)


def module_name(path: str) -> str:
    rel = os.path.relpath(path, ROOT)
    if rel.startswith("ee" + os.sep):
        rel = rel[len("ee" + os.sep):]
    mod = rel[:-3].replace(os.sep, ".")
    return mod[:-len(".__init__")] if mod.endswith(".__init__") else mod


def resolve(mod: str, node: ast.ImportFrom, is_pkg: bool) -> list[str]:
    """The absolute module names an ImportFrom brings in (the module, and each name as a submodule)."""
    if node.level:
        parts = mod.split(".")
        base = parts if is_pkg else parts[:-1]
        base = base[:len(base) - (node.level - 1)] if node.level > 1 else base
        head = ".".join(base + ([node.module] if node.module else []))
    else:
        head = node.module or ""
    return [head] + [f"{head}.{a.name}" for a in node.names]


def scan() -> list[str]:
    hits = []
    for top in (os.path.join(ROOT, "tares"), os.path.join(ROOT, "ee", "tares_ee")):
        for dirpath, _dirs, files in os.walk(top):
            if "migrations" in dirpath.split(os.sep):
                continue
            for f in files:
                if not f.endswith(".py"):
                    continue
                path = os.path.join(dirpath, f)
                mod = module_name(path)
                if going(mod) or mod in MIXED:
                    continue
                tree = ast.parse(open(path, encoding="utf-8").read(), path)
                is_pkg = f == "__init__.py"
                for node in ast.walk(tree):
                    names = []
                    if isinstance(node, ast.Import):
                        names = [a.name for a in node.names]
                    elif isinstance(node, ast.ImportFrom):
                        names = resolve(mod, node, is_pkg)
                    bad = sorted({n for n in names if n.startswith("tares") and going(n)})
                    for n in bad:
                        hits.append((os.path.relpath(path, ROOT).replace(os.sep, "/"), node.lineno, n))
    return hits


def main():
    hits = scan()
    seen = {(f, m) for f, _l, m in hits}
    bad = [h for h in hits if (h[0], h[2]) not in KNOWN]
    stale = sorted(k for k in KNOWN if k not in seen)
    for f, line, m in bad:
        print(f"  FAIL {f}:{line} imports {m}, which Tares 2.0 removes")
    for f, m in stale:
        print(f"  FAIL KNOWN still names {f} -> {m} ({KNOWN[(f, m)]}), which no longer imports it: drop the line")
    for (f, m), t in sorted(KNOWN.items()):
        if (f, m) in seen:
            print(f"  ok   {f} -> {m}, allowed until {t}")
    n_bad = len(bad) + len(stale)
    print(f"\n{len(hits) - len(bad)} allowed, {n_bad} failed")
    sys.exit(1 if n_bad else 0)


if __name__ == "__main__":
    main()
