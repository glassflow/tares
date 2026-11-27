"""Which tickets take a model reply's cost (TR-1671). A reply names no ticket, so this is an
approximation from who held which ticket in which stage at the time of the reply.

- A builder's reply goes to the tickets its station held in stage doing or changes at that time,
  split evenly; with none, to the ones it held in review. A station is matched by name and project.
- The reply of a builder's helper goes as the builder's own would.
- A reviewer's reply is split over the tickets in review at that time (`review_shares`).
- A reply that counts for a project with limited access goes only to that project's tickets.
- A reply no ticket takes, and a reply of any other role, is tied to no ticket.

A ticket is in a stage, and held, from the change that says so until the next change; what the
last change says holds until now.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import Counter, defaultdict
from datetime import datetime

WORKING = ("doing", "changes")   # the stages in which a builder's reply goes to a held ticket first


class _Over:
    """Which tickets a set of spans holds at a time. A span is (start, end, ticket), `end` None for
    one still open; it holds from `start` up to, not including, `end`."""

    def __init__(self, spans=()):
        steps: dict = defaultdict(list)
        for start, end, ticket in spans:
            steps[start].append((ticket, 1))
            if end is not None:
                steps[end].append((ticket, -1))
        self.times, self.held = [], []
        count: Counter = Counter()
        for at in sorted(steps):
            for ticket, step in steps[at]:
                count[ticket] += step
                if not count[ticket]:
                    del count[ticket]
            self.times.append(at)
            self.held.append(tuple(count))

    def at(self, when: datetime) -> tuple:
        i = bisect_right(self.times, when) - 1
        return self.held[i] if i >= 0 else ()


_NONE = _Over()


def review_shares(waiting: tuple, project_of: dict, limited: set[str]) -> dict:
    """{ticket: the share it takes of a reviewer's reply} for the tickets in `waiting`, all in
    review at the time of the reply. A ticket's share is one part in the number of those tickets
    that every reader of the ticket can see: the ones in projects open to everyone and, for a
    ticket of a project in `limited`, the ones of its own project. So a ticket has one figure for
    every reader."""
    open_to_all = sum(1 for t in waiting if project_of[t] not in limited)
    own = Counter(project_of[t] for t in waiting if project_of[t] in limited)
    return {t: 1 / (open_to_all + own[project_of[t]]) for t in waiting}


class Ties:
    """The module's rule over one pass's records. `project_of` {ticket id: project} holds the
    tickets a reply can go to, `limited` the projects with limited access, `builders` the
    (station, project) pairs that are builders, `changes` and `holders` the stage and holder
    changes of tickets, each {ticket, at (with a time zone), value}."""

    def __init__(self, project_of: dict, limited: set[str], builders: set[tuple],
                 changes: list[dict], holders: list[dict]):
        self.project_of, self.limited, self.builders = project_of, limited, builders
        moves: dict = defaultdict(list)
        for field, rows in (("stage", changes), ("holder", holders)):
            for row in rows:
                if row["ticket"] in project_of:
                    moves[row["ticket"]].append((row["at"], field, row["value"]))
        working: dict = defaultdict(list)
        reviewing: dict = defaultdict(list)
        review = []
        for ticket, events in moves.items():
            events.sort(key=lambda e: e[0])
            now = {"stage": None, "holder": None}
            ends = [e[0] for e in events[1:]] + [None]
            for (at, field, value), end in zip(events, ends):
                now[field] = value
                if end == at:       # another change at the same time: that one closes the span
                    continue
                span = (at, end, ticket)
                station = (now["holder"], project_of[ticket])
                if now["stage"] == "review":
                    review.append(span)
                    if now["holder"]:
                        reviewing[station].append(span)
                elif now["stage"] in WORKING and now["holder"]:
                    working[station].append(span)
        self._review = _Over(review)
        self._working = {key: _Over(spans) for key, spans in working.items()}
        self._reviewing = {key: _Over(spans) for key, spans in reviewing.items()}

    def shares(self, counted: str, role: str, station: str | None, parent: str | None,
               home: str | None, at: datetime) -> dict:
        """{ticket: its share} of one reply of a builder, helper or reviewer station, empty when
        the reply is tied to no ticket. `counted` is the project the reply counts for ("" for
        none), `parent` the station a helper works for, `home` the station's project."""
        if role == "reviewer":
            takers = review_shares(self._review.at(at), self.project_of, self.limited)
        else:
            if role == "helper":
                station = parent if (parent, home) in self.builders else None
            key = (station, home)
            held = (self._working.get(key, _NONE).at(at)
                    or self._reviewing.get(key, _NONE).at(at))
            takers = {t: 1 / len(held) for t in held}
        if counted in self.limited:
            takers = {t: s for t, s in takers.items() if self.project_of[t] == counted}
        return takers

    def tied(self, counted: str, takers: dict) -> bool:
        """Whether a reply with these `takers` counts as tied to a ticket in the figures of
        `counted`: one of them is in a project open to everyone, or in `counted` itself."""
        return any(self.project_of[t] not in self.limited or self.project_of[t] == counted
                   for t in takers)
