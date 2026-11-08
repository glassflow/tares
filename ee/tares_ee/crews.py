# Tares enterprise licence, see ee/LICENSE
"""Crews kept apart by machine (TR-628).

With a licence a crew is one person's crew on one machine: the same crew name on two machines is
two crews. Without a licence core drops the machine (`SingleTeam.resolve_crew`), so every line with
one crew name joins one crew. The person is never changed here: each person's lines stay theirs.
"""
from __future__ import annotations


def resolve_crew(name, person_id, machine):
    """The (name, machine) to give to `store.resolve_crew`: the line's own, trimmed, the crew
    named `crew` when the line has none."""
    return (name or "").strip() or "crew", (machine or "").strip()
