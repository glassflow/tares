# Tares enterprise licence, see ee/LICENSE
"""Rule offers, a ticket's challenger findings and a review brief give each reader what comes from
the projects and sessions they can open (TR-1724).

The cell, people and projects are those of test_private_station_reads: Atlas is a team project,
Zephyrvault is private, Lee leads it and Mo is a member of it; Ann, Kim, the key with no person and
Atlas's project key are not on it. Offers are made through the review route.

  1. [count]    an offer lists and counts the tickets the reader can read, is not there for a reader
                who can read none of them, and the rule line written from an offer with a private
                ticket carries no count;
  2. [rename]   an offer's ticket labels follow the same rule after a project is renamed, then deleted,
                and the root caller keeps the labels and offers it had before;
  3. [findings] a ticket's challenger layer gives the finding text of the sessions the reader may open,
                with the same rounds and verdict for everyone;
  4. [brief]    the recurring findings of a review brief come from the projects the reader can read.

Run: .venv/bin/python ee/tests/test_offer_and_brief_reads.py
"""
import asyncio
import itertools
import os
import re
import sys
from datetime import timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_private_station_reads as reads   # its licensed cell, people and projects

import httpx

from tares import factory_github

HEAD = "aaaaaaa1111"
FINDING = "the migration is missing for the new column"
OUTSIDERS = ("ann", "kim", "bot")           # not on Zephyrvault
MEMBERS = ("lee", "mo", "root")
NAMES = ("Zephyrvault", "Quartzvault")      # the private project, before and after the rename
PULLS = itertools.count(1)
TALLY = {p: [0, 0] for p in ("count", "rename", "findings", "brief")}

BOTH, ATLAS_ONLY = "fnd-both", "fnd-atlas"  # BOTH also works for Zephyrvault
VAULT_TEXT = ("zv-open-finding", "zv-fixed-finding", "zv-waived-finding", "zv-waiver-reason")
ATLAS_TEXT = ("atlas-earlier-finding", "atlas-waived-finding", "atlas-waiver-reason")


def fake_github(request: httpx.Request) -> httpx.Response:
    parts = request.url.path.strip("/").split("/")
    if len(parts) == 5 and parts[3] == "pulls":
        return httpx.Response(200, json={
            "number": int(parts[4]), "title": "PR", "state": "open", "merged": False,
            "html_url": f"https://github.com/{parts[1]}/{parts[2]}/pull/{parts[4]}",
            "head": {"sha": HEAD, "ref": "factory/T1"}})
    if parts[-1] == "status":
        return httpx.Response(200, json={"statuses": []})
    return httpx.Response(200, json={"check_runs": []})


factory_github._transport = httpx.MockTransport(fake_github)


def ck(part, label, cond, detail=""):
    TALLY[part][0 if cond else 1] += 1
    print(("  ok   " if cond else "  FAIL ") + f"[{part}] {label}"
          + ("" if cond else f"  {str(detail)[:300]}"))


async def send(app, world, who, method, path, body=None):
    async with reads.client(app, world["who"][who]) as cx:
        return await cx.request(method, path, json=body)


async def make_project(app, world, owner, name, private=False):
    r = await send(app, world, owner, "POST", "/api/projects",
                   {"template": "custom", "name": name, "objects": [], "kind": "software_factory"})
    assert r.status_code in (200, 201), r.text
    uid = r.json()["id"]
    if private:
        r = await send(app, world, owner, "PATCH", f"/api/projects/{uid}", {"scope": "private"})
        assert r.status_code < 300 and app.state.store.get_project(uid)["scope"] == "private", r.text
    return uid


async def make_offer(app, world, slug, *keys):
    """One ticket in each project of `keys`, sent to review with a pull request of example-org/<slug>
    and reviewed with the same finding. Returns the offer the reviews made."""
    repo, made = f"example-org/{slug}", []
    for key in keys:
        uid = world[key]
        base = f"/api/projects/{uid}/tickets"
        r = await send(app, world, "root", "POST", base, {"title": f"{slug} work"})
        assert r.status_code == 201, r.text
        ref = r.json()["label"]
        r = await send(app, world, "root", "POST", f"{base}/{ref}/stage",
                       {"stage": "review", "pr": f"https://github.com/{repo}/pull/{next(PULLS)}"})
        assert r.status_code < 300, r.text
        r = await send(app, world, "root", "POST", f"{base}/{ref}/reviews",
                       {"head": HEAD, "verdict": "changes", "verified": "ran the tests",
                        "findings": [{"severity": "P2", "text": FINDING}]})
        assert r.status_code == 201, r.text
        made.append((key, f"{app.state.store.get_project(uid)['name']} {ref}", ref))
    r = await reads.get(app, world, "root", "/api/rule-proposals", state="")
    found = [p for p in r.json()["proposals"] if p["repo"] == repo]
    assert len(found) == 1 and len(found[0]["tickets"]) == len(keys), r.text
    return {"repo": repo, "id": found[0]["id"], "scope": found[0]["scope"],
            "all": sorted(label for _, label, _ in made),
            "atlas": sorted(label for key, label, _ in made if key == "atlas"),
            "ref":next((ref for key, _, ref in made if key == "atlas"), None)}


# ── 3. a ticket's challenger findings ──────────────────────────────────────────
def finding_lines(atlas, vault):
    def head(sid, s):
        return {"sessionId": sid, "cwd": "/w/crew",
                "timestamp": reads.iso(reads.T0 + timedelta(seconds=s))}

    def station(sid, s, name):
        return {**head(sid, s), "type": "session_station", "station": name, "role": "builder",
                "parent": "crew-orchestrator", "factory_station": name, "factory_crew": "crew",
                "crew": "crew", "factory_machine": "kim-mbp", "machine": "kim-mbp",
                "tares_project": atlas}

    def challenge(sid, s, kind, body):
        return {**head(sid, s), "type": kind, "gitBranch": "factory/T1", "tares_project": atlas,
                "challenge": body}

    def review(sid, s, sha, title):
        return challenge(sid, s, "challenge_commit", {
            "verdict": "FAIL", "sha": sha, "findings": [{"priority": "P1", "title": title}]})

    def waiver(sid, s, title, reason):
        return challenge(sid, s, "challenge_waived", {"findings": [
            {"priority": "P2", "title": title, "waived": True, "reason": reason}]})

    open_, fixed, waived, reason = VAULT_TEXT
    earlier, atlas_waived, atlas_reason = ATLAS_TEXT
    return [
        station(ATLAS_ONLY, 0, "atlas-build-f1"),
        station(BOTH, 1, "atlas-build-f2"),
        # one reply with token counts stamped with Zephyrvault: the session worked there too
        {**head(BOTH, 2), "type": "assistant", "tares_project": vault, "message": {
            "id": f"v-{BOTH}", "role": "assistant", "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": "a reply for the other project"}],
            "usage": {"input_tokens": 2, "output_tokens": 3, "cache_creation_input_tokens": 10,
                      "cache_read_input_tokens": 100}}},
        review(ATLAS_ONLY, 10, "1111111", earlier),
        waiver(ATLAS_ONLY, 11, atlas_waived, atlas_reason),
        review(BOTH, 20, "2222222", fixed),
        waiver(BOTH, 21, waived, reason),
        review(BOTH, 30, "3333333", open_),
    ]


async def check_findings(app, world):
    atlas, vault = world["atlas"], world["vault"]
    r = await send(app, world, "root", "POST", f"/api/projects/{atlas}/tickets",
                   {"title": "Findings ticket"})
    assert r.status_code == 201 and r.json()["label"] == "T1", r.text
    async with reads.client(app, {**world["who"]["kim"],
                                  "Content-Type": "application/x-ndjson"}) as cx:
        r = await cx.post("/ingest/claude_code", content=reads.nd(finding_lines(atlas, vault)))
        assert r.status_code < 300, r.text
    await asyncio.sleep(0.5)

    one = f"/api/projects/{atlas}/tickets/T1"
    routes = (
        ("the ticket", "GET", one, None),
        ("the answer to a PUT", "PUT", one, {"title": "Findings ticket"}),
        ("the hand-over", "GET", f"/api/projects/{atlas}/pickup", None),
        ("the review brief", "GET", f"{one}/brief", None),
    )

    def said(name, r):
        """(rounds, verdict) as the answer states them."""
        if name == "the review brief":
            m = re.search(r"(\d+) rounds, last: (\w+)", r.json()["brief"])
            return (int(m.group(1)), m.group(2)) if m else None
        body = r.json()["ticket"] if name == "the hand-over" else r.json()
        layer = (body or {}).get("challenger") or {}
        return (layer.get("rounds"), layer.get("verdict"))

    of_root = {}
    for who in ("root", "kim", "lee", "mo", "ann", "bot", "atlaskey"):
        for name, method, path, body in routes:
            r = await send(app, world, who, method, path, body)
            at = f"{who}, {name}"
            got = [t for t in VAULT_TEXT if t in r.text]
            if r.status_code != 200:
                ck("findings", f"{at}: refuses this key and gives no finding of that session",
                   who == "atlaskey" and r.status_code in (401, 403, 404) and not got,
                   (r.status_code, r.text))
                continue
            seen = said(name, r)
            if who == "root":
                of_root[name] = seen
                ck("findings", f"{at}: three rounds and their verdict",
                   seen == (3, "findings"), seen)
            if who in ("ann", "bot", "atlaskey"):
                ck("findings", f"{at}: no finding text of a session they cannot open",
                   not got, f"got {got}")
            else:
                ck("findings", f"{at}: the finding text of both sessions",
                   got == list(VAULT_TEXT), f"got {got}")
            kept = [t for t in ATLAS_TEXT if t in r.text]
            ck("findings", f"{at}: the findings of the Atlas session",
               kept == list(ATLAS_TEXT), f"got {kept}")
            ck("findings", f"{at}: the rounds and verdict root sees",
               seen == of_root.get(name), (seen, of_root.get(name)))


# ── 4. the review brief's recurring findings ───────────────────────────────────
def recurring_part(brief):
    return brief.partition("## What keeps coming back")[2].partition("\n## ")[0].strip()


async def check_brief(app, world, offers):
    atlas = world["atlas"]
    for who in OUTSIDERS + ("atlaskey",) + MEMBERS:
        for key in ("rules", "brief"):
            offer = offers[key]
            r = await reads.get(app, world, who,
                                f"/api/projects/{atlas}/tickets/{offer['ref']}/brief")
            at = f"{who}, the brief of a ticket of {offer['repo']}"
            if r.status_code != 200:
                ck("brief", f"{at}: refuses this key and names no other project",
                   who == "atlaskey" and r.status_code in (401, 403, 404)
                   and "Zephyrvault" not in r.text, (r.status_code, r.text))
                continue
            brief = r.json()["brief"]
            part = recurring_part(brief)
            if who in MEMBERS:
                ck("brief", f"{at}: the recurring findings name every ticket",
                   all(label in part for label in offer["all"])
                   and f"migration: {len(offer['all'])} tickets" in part, part)
                continue
            ck("brief", f"{at}: names only projects they can read",
               "Zephyrvault" not in brief, part)
            if key == "brief":   # enough of the tickets are in Atlas for the finding to recur there
                ck("brief", f"{at}: the finding recurs over the tickets they can read",
                   "migration: 3 tickets" in part and all(x in part for x in offer["atlas"]), part)


# ── 1. what an offer lists, counts and writes ──────────────────────────────────
LISTS = (("the open offers", "/api/rule-proposals", {}, "proposals"),
         ("the offers of every state", "/api/rule-proposals", {"state": ""}, "proposals"),
         ("the Crew page", "/api/factory/crew", {}, "rule_proposals"))


def counts(text, n):
    return bool(re.search(rf"\b{n} ticket{'' if n == 1 else 's'}\b", text))


def view(offer):
    return None if offer is None else (offer.get("tickets"), offer.get("text"))


async def accept(app, world, who, offer, body, marker=None):
    """Accepts the offer. Returns the answer and the lines of the written doc that contain `marker`
    (the offer's repo when none is given)."""
    r = await send(app, world, who, "POST", f"/api/rule-proposals/{offer['id']}/accept", body)
    if r.status_code != 200:
        return r, []
    doc = app.state.store.get_doc(None, r.json()["doc"])["body"]
    marker = marker or offer["repo"]
    return r, [line for line in doc.splitlines() if marker in line]


async def check_counts(app, world, offers):
    mixed, inside = offers["rules"], offers["inside"]
    for who in OUTSIDERS + ("atlaskey",) + MEMBERS:
        for name, path, params, key in LISTS:
            r = await reads.get(app, world, who, path, **params)
            at = f"{who}, {name}"
            if r.status_code != 200:
                ck("count", f"{at}: refuses this key and names no other project",
                   who == "atlaskey" and r.status_code in (401, 403, 404)
                   and "Zephyrvault" not in r.text, (r.status_code, r.text))
                continue
            got = {p["id"]: p for p in r.json()[key]}
            m, i = got.get(mixed["id"]), got.get(inside["id"])
            if who in MEMBERS:
                ck("count", f"{at}: the mixed offer lists and counts all three tickets",
                   m is not None and sorted(m["tickets"]) == mixed["all"] and counts(m["text"], 3),
                   view(m))
                ck("count", f"{at}: the offer from private tickets lists and counts all three",
                   i is not None and sorted(i["tickets"]) == inside["all"] and counts(i["text"], 3),
                   view(i))
                continue
            ck("count", f"{at}: the mixed offer lists the Atlas ticket only",
               m is not None and m["tickets"] == mixed["atlas"], view(m))
            ck("count", f"{at}: the mixed offer's text counts 1 ticket",
               m is not None and counts(m["text"], 1) and not counts(m["text"], 3), view(m))
            ck("count", f"{at}: an offer with no ticket they can read is not listed",
               i is None, view(i))

    # an offer with no ticket of a private project keeps its count
    for key in ("team", "own"):
        offer = offers[key]
        for name, path, params, field in LISTS:
            r = await reads.get(app, world, "ann", path, **params)
            got = {p["id"]: p for p in r.json()[field]} if r.status_code == 200 else {}
            o = got.get(offer["id"])
            ck("count", f"ann, {name}: the offer of {offer['repo']} keeps its count",
               o is not None and sorted(o["tickets"]) == offer["all"] and counts(o["text"], 3),
               view(o))
        r, lines = await accept(app, world, "ann", offer, {})
        ck("count", f"ann accepts the offer of {offer['repo']}: the line keeps its count",
           len(lines) == 1 and counts(lines[0], 3), (r.status_code, lines or r.text))

    # a decision on an offer with no ticket the caller can read
    for decision in ("reject", "accept"):
        offer = offers[f"inside-{decision}"]
        r = await send(app, world, "ann", "POST",
                       f"/api/rule-proposals/{offer['id']}/{decision}", {})
        ck("count", f"ann, {decision} on an offer with no ticket they can read: 404",
           r.status_code == 404, (r.status_code, r.text))
        state = app.state.store.get_rule_proposal(offer["id"])["state"]
        ck("count", f"after ann's {decision}: that offer is still open", state == "open", state)
    for who in MEMBERS:
        offer = offers[f"inside-{who}"]
        r = await send(app, world, who, "POST", f"/api/rule-proposals/{offer['id']}/reject", {})
        ck("count", f"{who} rejects an offer made from private tickets",
           r.status_code == 200 and sorted(r.json()["tickets"]) == offer["all"],
           (r.status_code, r.text))

    # the line written from an offer with a private ticket, with no text typed
    for who in ("ann",) + MEMBERS:
        offer = offers[f"accept-{who}"]
        r, lines = await accept(app, world, who, offer, {})
        ck("count", f"{who} accepts a mixed offer: the written line carries no ticket count",
           len(lines) == 1 and not re.search(r"\d", lines[0]),
           (r.status_code, lines or r.text))
        if who == "ann":
            ck("count", "ann accepts a mixed offer: the answer does not count 3 tickets",
               r.status_code == 200 and not counts(r.json().get("text") or "", 3),
               (r.status_code, r.text))

    # a typed text is written as typed
    for who in ("ann", "lee", "root"):
        offer = offers[f"typed-{who}"]
        text = f"{who} says: check the schema change in all 3 tickets before review."
        r, lines = await accept(app, world, who, offer, {"text": text}, marker=text)
        ck("count", f"{who} accepts with a typed text: written exactly as typed",
           lines == [f"- {text}"], (r.status_code, lines or r.text))

    # the offer's own sentence sent back, as read or as stored, is not a typed text
    for who, kind, counted in (("ann", "read", 1), ("lee", "stored", 3)):
        offer = offers[f"echo-{who}"]
        if kind == "read":
            r = await reads.get(app, world, who, "/api/rule-proposals", state="")
            text = next((p["text"] for p in r.json()["proposals"] if p["id"] == offer["id"]), "")
        else:
            text = app.state.store.get_rule_proposal(offer["id"])["text"]
        ck("count", f"{who}, the {kind} sentence of an offer counts {counted}",
           counts(text, counted), text)
        r, lines = await accept(app, world, who, offer, {"text": text})
        ck("count", f"{who} accepts sending the {kind} sentence back: no ticket count is written",
           len(lines) == 1 and not re.search(r"\d", lines[0]),
           (r.status_code, lines or r.text))


# ── 2. a rename, then a delete ─────────────────────────────────────────────────
async def check_labels(app, world, offer, when, member_tickets):
    for who in OUTSIDERS + MEMBERS:
        r = await reads.get(app, world, who, "/api/rule-proposals", state="")
        rows = r.json().get("proposals") if r.status_code == 200 else None
        got = {p["id"]: p["tickets"] for p in rows or []}
        at = f"{who}, {when}"
        if who in MEMBERS:
            want = offer["all"] if who == "root" else member_tickets   # root keeps every label
            ck("rename", f"{at}: the offer lists the tickets expected for them",
               sorted(got.get(offer["id"]) or []) == want, got.get(offer["id"]))
            continue
        named = sorted({t for ts in got.values() for t in ts if t.startswith(NAMES)})
        ck("rename", f"{at}: no ticket label carries the private project's old or new name",
           rows is not None and not named, f"{r.status_code} got {named}")
        ck("rename", f"{at}: the offer still lists the Atlas ticket",
           offer["atlas"][0] in (got.get(offer["id"]) or []), got.get(offer["id"]))


async def check_rename(app, world, offers):
    store, vault, offer = app.state.store, world["vault"], offers["renamed"]
    r = await send(app, world, "root", "POST", f"/api/rule-proposals/{offer['id']}/reject", {})
    assert r.status_code == 200, r.text
    r = await send(app, world, "lee", "PUT", f"/api/projects/{vault}", {"name": "Quartzvault"})
    assert r.status_code == 200 and store.get_project(vault)["name"] == "Quartzvault", r.text
    await check_labels(app, world, offer, "after the rename", offer["all"])
    r = await send(app, world, "lee", "DELETE", f"/api/projects/{vault}")
    assert r.status_code < 300 and store.get_project(vault) is None, r.text
    # the deleted private project stays in every reader's hidden set: Lee and Mo get current names
    await check_labels(app, world, offer, "after the delete", sorted(offer["atlas"]))


async def check_gone(app, world):
    """An offer whose tickets all carry the names of deleted private projects."""
    store, uids = app.state.store, []
    for key, name in (("third", "Emberhold"), ("fourth", "Duskmere")):
        world[key] = await make_project(app, world, "lee", name, private=True)
        uids.append(world[key])
    offer = await make_offer(app, world, "gone-rules", "third", "third", "fourth")
    assert offer["scope"] == "all", offer
    text = store.get_rule_proposal(offer["id"])["text"]
    for uid in uids:
        r = await send(app, world, "lee", "DELETE", f"/api/projects/{uid}")
        assert r.status_code < 300 and store.get_project(uid) is None, r.text

    async def listed(who):
        found = []
        for name, path, params, field in LISTS:
            r = await reads.get(app, world, who, path, **params)
            rows = r.json().get(field) if r.status_code == 200 else None
            row = next((p for p in rows or [] if p["id"] == offer["id"]), None)
            found.append((name, r.status_code, row))
        return found

    for name, status, row in await listed("ann"):
        ck("rename", f"ann, {name}: an offer from deleted private projects only is not listed",
           status == 200 and row is None, (status, row))
    for decision in ("reject", "accept"):
        r = await send(app, world, "ann", "POST",
                       f"/api/rule-proposals/{offer['id']}/{decision}", {})
        ck("rename", f"ann, {decision} on that offer: 404", r.status_code == 404,
           (r.status_code, r.text))
    state = store.get_rule_proposal(offer["id"])["state"]
    ck("rename", "after ann's calls: that offer is still open", state == "open", state)

    for name, status, row in await listed("root"):
        ck("rename", f"root, {name}: that offer is listed, no label, its text as stored",
           view(row) == ([], text), (status, view(row)))
    r, lines = await accept(app, world, "root", offer, {})
    ck("rename", "root accepts that offer: the written line carries no ticket count",
       r.status_code == 200 and len(lines) == 1 and not re.search(r"\d", lines[0]),
       (r.status_code, lines or r.text))


async def build_offers(app, world):
    mixed = ("atlas", "vault", "vault")
    inside = ("vault", "vault", "second")   # two private projects: the offer is not one project's
    plan = {"rules": ("rules", *mixed),
            "brief": ("brief-rules", "atlas", "atlas", "atlas", "vault"),
            "team": ("team-rules", "atlas", "atlas", "orchard"),
            "own": ("own-rules", "atlas", "atlas", "atlas"),
            "inside": ("inside-rules", *inside),
            "renamed": ("renamed-rules", *mixed)}
    for who in ("reject", "accept") + MEMBERS:
        plan[f"inside-{who}"] = (f"inside-by-{who}", *inside)
    for who in ("ann",) + MEMBERS:
        plan[f"accept-{who}"] = (f"accept-by-{who}", *mixed)
    for who in ("ann", "lee", "root"):
        plan[f"typed-{who}"] = (f"typed-by-{who}", *mixed)
    for who in ("ann", "lee"):
        plan[f"echo-{who}"] = (f"echo-by-{who}", *mixed)
    offers = {key: await make_offer(app, world, *spec) for key, spec in plan.items()}
    assert offers["own"]["scope"] == "project", offers["own"]
    assert all(o["scope"] == "all" for k, o in offers.items() if k != "own"), offers
    return offers


def check_no_session():
    """A review or waiver that names no session goes to everyone who can open the ticket. The
    ingest route stores no such line, so this hands the filter events made here."""
    from tares import factory, factory_api
    if not hasattr(factory_api, "readable_layer"):
        ck("findings", "the challenger layer has a read filter", False)
        return

    def event(kind, session, title):
        return {"type": kind, "session": session, "challenge": {
            "verdict": "FAIL", "findings": [{"priority": "P1", "title": title}]}}

    events = [event("challenge_commit", "", "earlier, no session"),
              event("challenge_waived", "", "waived, no session"),
              event("challenge_commit", "closed", "latest, closed session")]
    kept = factory_api.unread_sessions
    factory_api.unread_sessions = lambda h, sids: {"closed"}
    try:
        host = SimpleNamespace(hidden_ids=lambda: {"a project"})
        got = factory_api.readable_layer(host, factory.challenger_layer(events), events)
    finally:
        factory_api.unread_sessions = kept
    shown = {k: [f["title"] for f in got[k]] for k in ("open", "fixed", "waived")}
    ck("findings", "a finding and a waiver with no session are read; a closed session's are not",
       shown == {"open": [], "fixed": ["earlier, no session"],
                 "waived": ["waived, no session"]}, shown)
    ck("findings", "rounds and verdict count every review",
       (got["rounds"], got["verdict"]) == (2, "findings"), got)


async def main():
    from tares.daemon import make_app
    app = make_app()
    async with app.router.lifespan_context(app):
        world = await reads.seed(app)
        world["second"] = await make_project(app, world, "lee", "Quillmarsh", private=True)
        # Mo is on both private projects: a second key of theirs names the same person
        r = await send(app, world, "root", "POST", "/api/keys", {
            "name": "mo-desktop", "scopes": ["read"],
            "person": {"email": "mo-psr@example.com", "name": "Mo"}})
        assert r.status_code < 300, r.text
        app.state.store.add_project_member(world["second"], r.json()["person"]["id"])
        world["orchard"] = await make_project(app, world, "root", "Orchard")

        await check_findings(app, world)
        offers = await build_offers(app, world)
        await check_brief(app, world, offers)
        await check_counts(app, world, offers)
        await check_rename(app, world, offers)
        await check_gone(app, world)
    check_no_session()

    for part, (passed, failed) in TALLY.items():
        print(f"[{part}] {passed} passed, {failed} failed")
    passed, failed = (sum(t[i] for t in TALLY.values()) for i in (0, 1))
    print(f"{passed} passed, {failed} failed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(main()) else 0)
