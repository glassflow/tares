"""A GitHub repository named the way people write it (Tares 2.0, TR-1806; moved out of the GitHub
connector so the ticket integrations need no connector)."""
from __future__ import annotations


def normalize_repo(repo) -> str:
    """Accept 'owner/name' but also pasted URLs (https://github.com/owner/name[.git][/…])."""
    r = str(repo or "").strip().rstrip("/")
    if "://" in r:
        r = r.split("://", 1)[1]
    parts = r.split("/")
    if parts and "." in parts[0]:   # a hostname (github.com, GHE) — owner names can't contain dots
        parts = parts[1:]
    if len(parts) < 2 or not parts[0] or not parts[1]:
        raise ValueError(f"repo must be owner/name (got {repo!r})")
    name = parts[1][:-4] if parts[1].endswith(".git") else parts[1]
    return f"{parts[0]}/{name}"
