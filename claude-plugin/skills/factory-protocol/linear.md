# Linear's own tools

Part of the factory-protocol skill. Read when your tool list has Linear's tools (`list_issues`, `get_issue`, `save_issue`) and you are about to call one.

Linear's tools are not Tares's and answer big: by default every issue carries its whole description. Call them in their small form.

- **A ticket of a Tares project is read with the Tares tools** (`get_ticket`, `list_tickets`), never with Linear's read tools. Tares already syncs its status. `save_issue` still changes a Linear ticket, and Linear's read tools serve an issue Tares does not hold.
- **`list_issues` always with `fields=["id","title","status","assignee","updatedAt"]` and `limit` of 25 or less.** Never the default, which carries every description. Narrow it with `assignee`, `team`, `project` or `query` first.
- **`get_issue` with `fields=["status","updatedAt"]`**, unless you need to read the description; then ask for `description` as well and nothing else you do not need.
- **One look per tick at most.** To learn whether the issues you wait on moved, make one `list_issues(updatedAt="<your last look, ISO-8601>", fields=["id","status","updatedAt"], limit=25)` (with `assignee`, `team` or `project` to narrow it), note the time in your report, and read an issue with `get_issue` only when it is listed. Never read each waited-on issue in turn.
- **One `save_issue` carrying every field at once** (`patch` for part of a long description): each call answers with the whole issue, so two calls cost twice.

If Linear refuses `fields` or `limit` (an older Linear connector), call it without them and say so in your report; narrow it with `team`, `project` and `assignee` instead.
