"""A project's goal: what the project is for, in one line.

The goal-first project page (its outline, results and health) went with the source catalog in
Tares 2.0 (TR-1811); the goal itself stays, on every project.
"""
from __future__ import annotations

GOAL_MAX = 200


def normalize_goal(goal) -> str | None:
    """A goal is one line: whitespace (new lines included) folds to single spaces and the ends
    are trimmed. Empty is None. Raises ValueError past GOAL_MAX characters."""
    if goal is None:
        return None
    text = " ".join(str(goal).split())
    if not text:
        return None
    if len(text) > GOAL_MAX:
        raise ValueError(f"a goal is at most {GOAL_MAX} characters (this one has {len(text)})")
    return text
