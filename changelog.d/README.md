# Changelog fragments

Each change adds one small file here instead of editing `CHANGELOG.md`, so two pull requests never
conflict on it.

- The file is `changelog.d/<ticket>.md`, for example `changelog.d/TR-123.md`. A change with no ticket
  uses a short slug, for example `changelog.d/docker-cleanup.md`.
- The first line is the category, exactly `Added`, `Changed` or `Fixed` (capital first letter, nothing else
  on the line).
- The rest is the entry. It becomes one `- ` bullet, and any further lines are indented by two spaces.
  Write it in plain language and name what the person sees, then the cause.

```
Fixed
The Reviews page showed a pull request twice after a force push (TR-123). It now shows one row per pull request.
```

CI asks for a fragment on a pull request that changes `tares/` or `ui/src/`. The label `no changelog` skips
that check, for a change nobody needs to read about.

`scripts/release.sh` collects the fragments: it writes them, with anything under `## [Unreleased]`, into a
new release section of `CHANGELOG.md`, deletes the files and commits both with the version bump. A fragment
with another first line, or no text, stops the release before any commit. `CHANGELOG_SUMMARY="..."` gives the
release's summary paragraph.
