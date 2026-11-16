# Assumptions, once per milestone

Part of the station-orchestrator manual. Read when a milestone's open assumptions are due for the person, and again when they answer.

1. `list_assumptions(state="open")`, then one desk item listing them with your recommendation to keep each, `assumptions=[...]`. Its block has one line per assumption, `**A<n>** <the choice>`, and ends "Reply "keep all", or name the ones to change".
2. The answer must settle every assumption on the item exactly once: each in `kept=[...]` or in `overturned={"A<n>": "<the person's words about it>"}`, the words theirs (a phrase from their answer is fine). If their answer leaves one out, ask about it before recording.
3. `decide_assumption` exists for a single one outside a batch; normally the batch does it.
4. For each overturned one, write a follow-up ticket (`write_ticket(title, milestone=..., repo=...)`: it creates the ticket in Linear or GitHub when the project's tickets live there) and tell the builder.
