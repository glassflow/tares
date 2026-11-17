# Contributor mode

Part of the spec-project manual. Read when a session opened by `tares factory spec <project>` has the first message "You are contributing to <project> led by <lead>", before anything else.

For a session opened by `tares factory spec <project>` by someone who is not the project's lead,
with the first message "You are contributing to <project> led by <lead>". The project's spec and
tickets exist; you ask nothing about the idea. Your job is to answer the questions for this person
as an area owner and to check the tickets in their repos. Use `<project>` as `project`
in every call, and the person's own words in anything you write.

1. Call `read_global_docs`, then `get_doc` for the `spec` and the `plan`, and read them.
2. Call `my_work(project=<project>)` and read `person` (who this is) and `spec_questions` (the
   open questions addressed to this person in this project only). An error 404 ("no project 'x' you
   can see: use a project id or its exact name") means the name or id is wrong or the project is
   not shared with this person: stop
   and tell the user, do not retry with another spelling. Question numbers (Q1, Q2)
   restart in each project, so never answer a question from a list that was not asked for this
   project. Do not match people by display name. Ask the user each one, one at a time, with your
   recommended answer and why, so they can say yes.
3. On their answer, call `answer_question(id, answer, project)` with their words. Say which
   questions are left.
4. Call `list_tickets(project=<project>)` (read on with `offset` while it ends with `more`) and keep the tickets whose `repo` is one that
   `list_areas` gives to the `person` from `my_work` (match by `person.id`, not by name). Show them
   (title, milestone, depends on, a line from the working doc: `get_ticket` has it) and ask what to change. Apply each
   edit where the ticket lives: `write_ticket` (and `write_doc` for its working doc) for Tares
   tickets, Linear's `save_issue` for Linear ones, `gh issue edit` for GitHub ones. A ticket that
   changes a repo this person does not own is not theirs to edit: tell the lead instead. Editing a
   Tares ticket or its working doc (`write_ticket`, `write_doc`) needs a key with the `admin`
   scope, and an ordinary key (read and ingest) is refused with 403 "this credential lacks the
   'admin' scope". Linear and GitHub edits do not go through the Tares key, so they are not refused
   this way. On that 403, do not retry and do not go around it: write the change the person wants, ticket by ticket, in your reply and
   tell the user to pass it to <lead>, who makes it. Answering still works.
5. When the questions are answered and the tickets checked, say so, and that <lead> can hand the
   project over: nothing else waits for this person.
