# Tares enterprise licence, see ee/LICENSE
"""The enterprise team: what a cell does with more than one person.

`Team` is `tares.team.SingleTeam` with the parts that mean something for a team filled in. This
file starts with the people: a new person row is allowed while the licence has a seat for one more.
Seats are counted over every row in `people`, the owner included. An expired licence keeps every
person it already has and refuses only adding one. The team home and the team's project view are
`team_home.py`; the area owners are `areas.py` (routes) and `mcp_areas.py` (MCP tools); ticket
assignees are `ticket_assignee.py`; a project's lead, privacy and members are `access.py` (routes) and
`mcp_access.py` (MCP tool); grants that wait for someone's consent are `grants.py` (routes) and
`mcp_grants.py` (MCP tool); the questions a spec asks a repo's owner are `questions.py` (routes) and
`mcp_questions.py` (MCP tools); the other methods stay single-person until their own tickets
move them here.
"""
from __future__ import annotations

from tares import licence
from tares.team import SingleTeam

from . import access, areas, mcp_access, mcp_areas, mcp_grants, mcp_questions
from . import grants as consent
from . import questions
from . import ticket_assignee as assignees
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

    def assignee_from_linear(self, store, assignee, cache=None):
        return assignees.from_linear(store, assignee, cache)

    def assignee_from_github(self, store, issue):
        return assignees.from_github(store, issue)

    def assignee_warn_nameless(self, project, count):
        assignees.warn_nameless(project, count)

    def assignee_sync_join(self, store, project, person_id, status):
        assignees.sync_join(store, project, person_id, status)

    def assignee_join(self, store, project, person_id, added_by=None):
        assignees.join(store, project, person_id, added_by)

    def assignee_resolve(self, store, ref):
        return assignees.resolve(store, ref)

    def grant_waiting_for(self, c, grant):
        return consent.waiting_for(c, grant)

    def grants_waiting_for_me(self, c, grants, request):
        return consent.waiting_for_me(c, grants, request)

    def settle_grant(self, c, grant, request):
        return consent.settle_on_read(c, grant, request)

    def open_questions_for(self, c, person_id, hidden, want):
        return questions.open_questions_for(c, person_id, hidden, want)

    def team_home(self, c, request, now):
        return home.team_home(c, request, now)

    def team_projects(self, c, request, open_ps, tickets, rows, which, now, waiting):
        return home.team_projects(c, request, open_ps, tickets, rows, which, now, waiting)

    def register_routes(self, app, host):
        areas.register_routes(app, host)
        access.register_routes(app, host)
        consent.register_routes(app, host)
        questions.register_routes(app, host)

    def mcp_tools(self):
        return [*mcp_areas.TOOLS, *mcp_access.TOOLS, *mcp_grants.TOOLS, *mcp_questions.TOOLS]
