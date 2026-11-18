# Area owners and questions to them

Part of the spec-project manual. Read when you know which repos the project changes, in step 1, unless the Tares has no `list_areas` tool.

On an older Tares with no `list_areas` tool (factory-protocol), skip this part:
one person owns every repo. Otherwise, once you know which repos the
project changes, call `list_areas` and note the
owner of each one (`{repo, person: {id, name}}`). If a repo has none, ask the user who owns it
(one question, recommending a person from `list_people`) and call `set_area(repo, person)`. Setting
the owner of a repo that has one, or of a repo for someone else, needs an admin: when you are not
one, tell the user an admin sets the owner. Tickets go to these owners (step 6). Nobody signs off
before the hand-over: an owner is asked only what the spec needs from them, with `ask_area`.
`ask_area`, `list_questions` and `answer_question` come with an enterprise licence, like `list_areas`: when
they are not tools, ask the user what you need to know yourself and skip the rest of this part and
the open-questions step below.

When a question is about a repo whose area owner is not the user (what a module does, whether a
change is acceptable, how it is deployed), do not guess and do not make the user relay it. Once the
project exists (after `create_project`, below), call
`ask_area(repo, text, project)` with the question written so the owner can answer without this
conversation. On a private project, first put the other owners on it with
`set_project_access(project, add_members=[<their emails>])`, because `ask_area` is refused (403) for
an owner who is not on the project. Tell the user "waiting for <name>", and carry on with the other questions. Before
you plan (step 3), call `list_questions(project=<name>)`, fold each answered one into the spec
(name the answerer: "Kiran said ..."), and say which are still open. Plan around an open one only
as an explicit assumption in the spec.
