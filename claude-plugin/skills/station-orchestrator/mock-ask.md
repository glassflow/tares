# A builder's mock ASK

Part of the station-orchestrator manual. Read when a builder's `ASK` asks "Approve this mock for <screen>?" with a file path (a screen ticket), and again when the person answers it. It is always the person's to answer. Before you put it on the desk:

1. Read the mock file from the path in the `ASK`.
2. Keep it as the project doc "Mock: <screen>" with `write_doc` (kind `note`, the project as the doc tools take it), so the reviewer can read it later with `list_docs` and `get_doc`. The mock is never committed to a repo. If `write_doc` refuses it for size, do not put it on the desk: ask the builder to shrink the mock. If the builder redraws, `list_docs` for the existing "Mock: <screen>" and update that doc by its id; `write_doc` with no id makes a second doc.
3. Put the question on the desk with `desk_add(..., project="<the project>")`. Without `project` the approval does not show in `list_decisions(project)`, which is where the reviewer's design check reads it.
4. As soon as the item exists, send the builder an `ANSWER` with its `D<n>`, so it can write `Waiting for mock approval (desk D<n>)`.

When the person answers, send the builder a `RELAY` quoting the person verbatim and naming the `D<n>` (the builder acts only on a `RELAY`): the builder writes it in its PR body as `Mock: "Mock: <screen>" (desk D<n>)`.
