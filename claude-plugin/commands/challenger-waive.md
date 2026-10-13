---
description: Waive a disputed finding from the last failed Codex review so it stops blocking the commit.
argument-hint: "[n|all] [why it does not apply]"
allowed-tools: [Bash]
---

The user invoked /tares:challenger-waive with: $ARGUMENTS

Run, from the repository root:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/challenger.py" waive $ARGUMENTS
```

Show the user its output verbatim. If it lists the blocking findings and asks which one, ask the user and run it again with the number. In a tares-factory crew (FACTORY_STATION is set) a waiver needs its reason after the number, for example `2 the endpoint is internal and never sees user input`: the reviewer reads and judges it. Do not edit the waiver file yourself and do not re-run the review.
