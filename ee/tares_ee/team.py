# Tares enterprise licence, see ee/LICENSE
"""The enterprise team: what a cell does with more than one person.

`Team` is `tares.team.SingleTeam` with the parts that mean something for a team filled in. This
file starts with the people: a new person row is allowed while the licence has a seat for one more.
Seats are counted over every row in `people`, the owner included. An expired licence keeps every
person it already has and refuses only adding one. The team home and the team's project view are
`team_home.py`; the area owners are `areas.py` (routes) and `mcp_areas.py` (MCP tools); the other
methods stay single-person until their own tickets move them here.
"""
from __future__ import annotations

from tares import licence
from tares.team import SingleTeam

from . import areas, mcp_areas
from . import team_home as home

EXPIRED_REASON = "The enterprise licence has expired. Renew it to add people."


class Team(SingleTeam):
    def can_add_person(self, store):
        st = licence.current(store)
        if not licence.allows(st):
            return False, st.get("reason") or "Adding people needs an enterprise licence."
        if st["expired"]:
            return False, EXPIRED_REASON
        if st["used"] >= (st["seats"] or 0):
            return False, (f"Every seat of the enterprise licence is in use ({st['used']} of "
                           f"{st['seats']}). Ask for more seats to add people.")
        return True, None

    def areas(self, store):
        return areas.view(store)

    def area_owner(self, store, repo):
        return areas.owner(store, repo)

    def team_home(self, c, request, now):
        return home.team_home(c, request, now)

    def team_projects(self, c, request, open_ps, tickets, rows, which, now, waiting):
        return home.team_projects(c, request, open_ps, tickets, rows, which, now, waiting)

    def register_routes(self, app, host):
        areas.register_routes(app, host)

    def mcp_tools(self):
        return [*mcp_areas.TOOLS]
