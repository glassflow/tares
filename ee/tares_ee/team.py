# Tares enterprise licence, see ee/LICENSE
"""The enterprise team: what a cell does with more than one person.

`Team` is `tares.team.SingleTeam` with the parts that mean something for a team filled in. This
file starts with the people: a new person row is allowed while the licence has a seat for one more.
Seats are counted over every row in `people`, the owner included. An expired licence keeps every
person it already has and refuses only adding one. The other methods stay single-person until their
own tickets move them here.
"""
from __future__ import annotations

from tares import licence
from tares.team import SingleTeam

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
