"""The 1.61.0 to 2.0 migration script's own test, from the CI list (TR-1821).

scripts/migrate_to_2.py is not part of the package: it runs through uv in an environment of its own,
as on a cell, and so does its test, scripts/tests/test_migrate_to_2.py. This runs that test with
`uv run --with-editable .` (the 2.0 daemon it starts is this checkout's), on SQLite, and on Postgres
too when TARES_TEST_PG is set. Without uv on PATH it says so and passes: CI installs uv in the test job.

    .venv/bin/python tests/test_migrate_to_2_script.py
"""
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    uv = shutil.which("uv")
    if uv is None:
        print("  skip  uv is not on PATH: scripts/tests/test_migrate_to_2.py was not run")
        return 0
    env = dict(os.environ)
    # the postgres suite's harness (tests/pg_harness on PYTHONPATH) must not reach the script's own
    # environment or the daemons its test starts
    env.pop("PYTHONPATH", None)
    p = subprocess.run([uv, "run", "-q", "--with-editable", ".", "scripts/tests/test_migrate_to_2.py"],
                       cwd=ROOT, env=env)
    return p.returncode


if __name__ == "__main__":
    sys.exit(main())
