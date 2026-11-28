# Moving a cell from Tares 1.61.0 to 2.0

Tares 2.0 does not start on a 1.x database: it says the database was written by Tares 1.x and stops
there. `scripts/migrate_to_2.py` moves one 1.61.0 cell's data into a new 2.0 store, once. This page
is the runbook: a local cell first, then a cloud cell.

What the script does, in short:

- It reads the cell and never writes it: the SQLite file is copied and the copy is read, DuckDB is
  opened read-only, and its Postgres sessions are read-only.
- It writes a new store: a folder that does not exist yet, or a Postgres schema with no table in
  it. It builds the 2.0 tables from the 2.0 revision, copies the rows, checks the result against
  the cell, and only then stamps the store `0001_baseline`. A store that failed its check is not
  stamped, and 2.0 does not start on it.
- It refuses a cell that 1.61.0 has not finished upgrading (a session move still pending, a
  revision other than `0024_merge_ledger`) and says what to do.
- `--check` prints what it would copy, rename, rebuild and leave behind, table by table, and writes
  nothing. Read it before every `--apply`.

What you need:

- The cell on Tares 1.61.0 (not an rc), stopped.
- [uv](https://docs.astral.sh/uv/) and a checkout of the 2.0 release the cell will run
  (`git clone --branch v2.0.0 https://github.com/glassflow/tares`). The script builds the 2.0
  tables from that checkout and uses its code to rebuild what 2.0 writes as lines arrive. It is not
  in the wheel: run it from the checkout with `uv run scripts/migrate_to_2.py`, which installs what
  it needs (duckdb, sqlalchemy, psycopg, alembic) in its own environment.

Every command below runs from the root of that checkout.

## A local cell

The cell is a data folder, `~/.tares` unless you started it with `--data-dir`. It holds
`tares.sqlite`, `tares.duckdb` and the `archive` folder.

1. Bring the cell to 1.61.0 if it is older, and let it finish its upgrade:

   ```bash
   uv tool install tares==1.61.0
   tares up
   ```

   Wait until the log says the session move is done, then stop it.

2. Stop `tares up` (Ctrl-C in its terminal).

3. Check the cell:

   ```bash
   uv run scripts/migrate_to_2.py --check ~/.tares
   ```

   It ends with "The check passed". A "Refused" line says what to do first.

4. Write the 2.0 store into a new folder:

   ```bash
   uv run scripts/migrate_to_2.py --apply ~/.tares --to ~/.tares-2
   ```

   It ends with "Done: the output is stamped 0001_baseline". The archive files are linked (or
   copied) into `~/.tares-2/archive`, unchanged.

5. Start 2.0 on the new folder:

   ```bash
   uv tool install tares==2.0.0
   tares up --data-dir ~/.tares-2
   ```

6. Check: the console lists your projects, a ticket opens, a session shows its lines, and the crew
   posts lines again (`tares factory doctor`).

Keep `~/.tares` until you are sure: nothing was written to it. To go back, stop 2.0, run
`uv tool install tares==1.61.0` and `tares up` on `~/.tares`. What 2.0 wrote in `~/.tares-2` stays
there and is not carried back.

## A cloud cell

A cloud cell keeps its session store in its state Postgres (`TARES_STATE_DSN`) and `tares.duckdb` on
its volume, and usually its archive in a bucket (`TARES_ARCHIVE_URL`). Move one cell at a time, the
factory cell first, and the rest only once it runs well on 2.0.

For each cell:

1. Write down the cell's settings: `TARES_STATE_DSN`, `TARES_ARCHIVE_URL` and the
   `TARES_ARCHIVE_*` keys, and where its volume is mounted (`TARES_HOME`). Use the database's
   direct address for the script, not PgBouncer.

2. Make sure the cell runs 1.61.0 and its session move is done (its log says so, or `--check` in
   step 5 shows `0 pending`).

3. Scale the cell down to 0. The script needs it stopped: DuckDB cannot be read while the cell
   holds it.

4. Back up both halves:

   ```bash
   pg_dump -Fc "$TARES_STATE_DSN" -f cell-<name>-1.61.0.dump
   ```

   and a snapshot of the volume (or a copy of `tares.duckdb`). The archive does not change and
   needs no backup of its own.

5. Run the script from a pod that mounts the volume (read-only is enough) and reaches the
   database, with the 2.0 checkout and uv in it. First the check:

   ```bash
   uv run --with boto3 scripts/migrate_to_2.py --check /data \
     --state-dsn "$TARES_STATE_DSN" --archive-url "$TARES_ARCHIVE_URL"
   ```

   (`--with boto3` and the `TARES_ARCHIVE_*` variables are for an s3:// archive.) Give the pod about
   4 GB of memory for a factory-sized cell: DuckDB takes up to 2 GB of it while its events are read
   (`MIGRATE_DUCKDB_MEMORY=1GB` lowers that, and it spills to the temp folder). Read the plan:
   the row counts, the projects that are not factory projects (kept as plain projects unless you
   pass `--drop-non-factory`), the GitHub App keys it carries over.

6. Make an empty database (or schema) for 2.0, and apply into it:

   ```bash
   createdb <cell>_v2          # or CREATE SCHEMA in the cell's database
   uv run --with boto3 scripts/migrate_to_2.py --apply /data \
     --state-dsn "$TARES_STATE_DSN" --archive-url "$TARES_ARCHIVE_URL" \
     --to "postgresql://<user>:<password>@<host>:5432/<cell>_v2"
   ```

   For a schema, put it in the DSN: `...?options=-csearch_path%3D<schema>`. The script makes a
   missing schema and refuses one that holds a table. It ends with "Done: the output is stamped
   0001_baseline".

7. Point the cell at the new store (`TARES_STATE_DSN` to the 2.0 database), set the image to 2.0,
   and scale it up. Keep `TARES_ARCHIVE_URL` as it was.

8. Check the cell:
   - `/health` answers 200;
   - the console lists the projects, a ticket opens, a session shows its lines (an older one too,
     which reads from the archive);
   - a GitHub App webhook delivery lands: in the App's settings, Advanced, redeliver a recent
     delivery and see 202;
   - the crew's stations post lines again (`tares factory doctor`, or the crew page).

9. Keep the 1.61.0 database, the volume snapshot and the dump for a while. 2.0 never opens
   `tares.duckdb`; delete it only once you will not go back.

`--verify` runs the check of step 6 again on an applied store, at any time before the cell starts
on it.

### Going back

1. Scale the cell down to 0.
2. Point `TARES_STATE_DSN` back at the 1.61.0 database. The script did not write it; if anything
   else did, restore it from the dump first:

   ```bash
   pg_restore --clean --if-exists -d "$OLD_STATE_DSN" cell-<name>-1.61.0.dump
   ```

   and the volume from its snapshot.
3. Set the image to 1.61.0 and scale it up.

What the cell did on 2.0 in between (new tickets, sessions, reviews) is not carried back.

## What is not carried over

The `--check` plan names each of these for the cell it reads:

- The platform tables (catalog, triggers, agents and their runs, dispatch, SRE, the query log,
  subscriptions, Ask, MCP servers) and the platform objects of a project (kind source, trigger,
  agent, mcp_server).
- The DuckDB events, once read: the claude_code lines a session is missing, the challenger lines and
  the model replies the session store lacks, and the pull request events of the last 30 days.
- The GitHub token sources (polling). A GitHub App source becomes the App's webhook key; its
  credential and webhook secret are copied as they are.
- The ledgers of the 1.x moves (`session_moves`, `session_ingest_marks`, `session_store_copies`,
  `tares_state_meta`) and the old `skills` table (its skills are in `shared_skills`).
- Render flags: every line in the table is stored under `claude_code` with redact on and thinking
  off. An archived part keeps the flags it was written with.
- A session whose lines were counted but never stored anywhere (not in the table, the archive or
  DuckDB) keeps its count and is marked incomplete; the script names it.
