"""The release hold: a final release is refused while RELEASE_BLOCK exists, a release candidate is not, and the three places that cut a release call the check."""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _env import clean_env

ROOT = Path(__file__).resolve().parent.parent

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))


def run(root, tag):
    """The script as copied into <root>/scripts, so it finds <root> as its own root."""
    env = clean_env()
    env.pop("RELEASE_BLOCK_ROOT", None)
    r = subprocess.run(["bash", str(root / "scripts" / "check_release_block.sh"), tag],
                       capture_output=True, text=True, env=env)
    return r.returncode, r.stdout + r.stderr


tmp = Path(tempfile.mkdtemp(prefix="tares-release-block-"))
try:
    (tmp / "scripts").mkdir()
    shutil.copy(ROOT / "scripts" / "check_release_block.sh", tmp / "scripts")
    (tmp / "RELEASE_BLOCK").write_text("Held for testing.\n")

    code, out = run(tmp, "v1.2.3")
    ck("a final tag is refused while RELEASE_BLOCK exists", code == 1, f"exit {code}: {out}")
    ck("the refusal is a workflow error naming RELEASE_BLOCK and its first line",
       "::error::" in out and "RELEASE_BLOCK" in out and "Held for testing." in out, out)
    code, out = run(tmp, "v1.2.3-rc.1")
    ck("a release candidate passes while RELEASE_BLOCK exists", code == 0, f"exit {code}: {out}")
    (tmp / "RELEASE_BLOCK").unlink()
    code, out = run(tmp, "v1.2.3")
    ck("a final tag passes once RELEASE_BLOCK is gone", code == 0, f"exit {code}: {out}")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

ck("the repo has a RELEASE_BLOCK", (ROOT / "RELEASE_BLOCK").is_file())
for rel in (".github/workflows/release.yml", ".github/workflows/docker-publish.yml", "scripts/release.sh"):
    ck(f"{rel} calls the check", "check_release_block" in (ROOT / rel).read_text())

# docker-publish also runs from workflow_dispatch on main, which pushes :latest. The step must
# therefore run on every trigger: no `if:` on it, and it comes before the image is pushed.
dp = (ROOT / ".github/workflows/docker-publish.yml").read_text().splitlines()
hit = next((i for i, ln in enumerate(dp) if "check_release_block" in ln), None)
start = hit
while start is not None and start > 0 and not dp[start].startswith("      - "):
    start -= 1
step = dp[start:hit + 1] if hit is not None else []
push = next((i for i, ln in enumerate(dp) if "build-push-action" in ln), len(dp))
ck("docker-publish runs the check on every trigger, with no if:",
   hit is not None and not any(ln.strip().startswith("if:") for ln in step), "\n".join(step))
ck("docker-publish runs the check before it pushes the image", hit is not None and hit < push)

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
