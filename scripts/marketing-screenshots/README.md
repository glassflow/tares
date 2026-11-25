# Marketing screenshot library

Builds the folder of factory console screenshots the content team uses for articles: PNGs, a
`manifest.json` saying what each one shows and that its figures are made up, and a README for the
reader (`library-README.md`). Same shape as the Rius library in argus-ui. First built 2026-11-24.

    cd ui && npm ci && npm run build && cd .. && uv venv && uv pip install -e .
    scripts/marketing-screenshots/run.sh ~/Downloads/tares-screens-<date>

Two sources of data, both fake:

- **Console samples** (`?sample=<name>` with the `taresSamples` browser flag), the curated states the
  e2e suites use. Most scenes.
- **A seeded project**, "Bakery orders" (`scripts/seed_factory_demo.py`), written through the real API
  into a scratch cell, for the `live` scenes.

Nothing in `src/` changes. A few samples carry real names or repos; `capture.cjs` rewrites them in the
page before the shot (`MASK`) and flags any text from `FLAGS` it still finds. Review the printed `FLAG`
lines, then look at every shot (`sheet.cjs` builds contact sheets). A new sample with a real name
gets a line in `MASK`.

Pass a slug regex as a second argument to redo some shots; that skips the manifest.
