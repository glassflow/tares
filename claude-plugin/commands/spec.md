---
description: Spec a new project with Claude, and keep everything the build needs (spec, plan, AGENTS.md, starting prompt, tickets with working docs) in a Tares project. Tickets can live in Linear.
argument-hint: "[what you want to build]"
---

The user invoked /tares:spec with: $ARGUMENTS

Load the `spec-project` skill of the tares plugin and follow it from step 1. If the user said what
they want to build above, start the brainstorm from that; otherwise ask what they want to build.
