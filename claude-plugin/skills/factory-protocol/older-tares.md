# An older Tares (before 1.56)

Part of the factory-protocol skill. Read when `my_work`, `list_areas`, `list_people`, `ask_area` or `answer_question` is not listed or a call says the tool is unknown, or when `list_crew` or `desk_list` refuses `mine`.

An older Tares has no people. One person runs the factory: every ticket of every project in `crew_projects` is that person's, the desk has no addressee, and there are no area owners or questions. Work as the skills say for one person, skip the steps about other people, and do not tell the person to upgrade unless they ask why.

For the orchestrator, `my_work` is replaced by:

- `ready`: for each project in `crew_projects`, `list_tickets` with `ready: true` and no holder;
- `in_progress`: the tickets a station holds;
- `desk`: `desk_list`.

Everything else in the Boot and Tick steps stays the same.
