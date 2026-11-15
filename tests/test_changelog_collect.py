"""scripts/changelog_collect.py (TR-1249): the files in changelog.d/ become one CHANGELOG.md section, in order, and a bad one stops everything."""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _env import clean_env

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import changelog_collect as cc

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))


HEAD = "# Changelog\n\nNotable changes.\n\n"
OLD = "## [1.0.0] - 2026-01-01\n\n### Added\n- Old thing.\n"
EMPTY = HEAD + "## [Unreleased]\n\n" + OLD
FRAGS = {"b.md": "Fixed\nFixed b.\n", "a.md": "Added\nAdded a.\n", "c.md": "Changed\nChanged c.\n", "d.md": "Added\nAdded d.\n"}


def run(changelog, frags, *extra):
    """The script as a command, on a temp copy of a repo holding `changelog` and `frags`: (exit code, stderr+stdout, dir)."""
    tmp = Path(tempfile.mkdtemp(prefix="tares-changelog-"))
    (tmp / "CHANGELOG.md").write_text(changelog)
    if frags is not None:
        (tmp / "changelog.d").mkdir()
        (tmp / "changelog.d" / "README.md").write_text("About fragments.\n")
        for n, t in frags.items():
            (tmp / "changelog.d" / n).write_text(t)
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "changelog_collect.py"), "2.0.0", "2026-02-02", *extra,
                        "--root", str(tmp)], capture_output=True, text=True, env=clean_env())
    return r.returncode, r.stderr + r.stdout, tmp


def left(tmp):
    return sorted(p.name for p in (tmp / "changelog.d").glob("*"))


# ---- the pure function
out = cc.collect(EMPTY, FRAGS, "2.0.0", "2026-02-02", "A summary.")
sec = out.split("## [2.0.0] - 2026-02-02\n", 1)[1].split("## [1.0.0]")[0]
ck("one new section, placed under an empty Unreleased and above the older release",
   out.index("## [Unreleased]") < out.index("## [2.0.0]") < out.index("## [1.0.0]") and out.count("## [2.0.0]") == 1, out)
ck("categories come in the order Added, Changed, Fixed", sec.index("### Added") < sec.index("### Changed") < sec.index("### Fixed"), sec)
ck("fragments are ordered by file name inside a category", sec.index("- Added a.") < sec.index("- Added d."), sec)
ck("the summary comes first, before the first category", sec.startswith("\nA summary.\n\n### Added"), sec)
ck("Unreleased is left in place and empty", "## [Unreleased]\n\n## [2.0.0]" in out, out)
ck("the older release is untouched", out.endswith(OLD), out)
ck("no summary: the first category follows the heading", cc.collect(EMPTY, FRAGS, "2.0.0", "2026-02-02").split("2026-02-02\n\n", 1)[1].startswith("### Added"))
ck("a summary of None is no summary", "None" not in cc.collect(EMPTY, FRAGS, "2.0.0", "2026-02-02", None))
ck("a category with no bullets is left out", "### Changed" not in cc.collect(EMPTY, {"a.md": "Added\nOnly this.\n"}, "2.0.0", "2026-02-02"))
multi = cc.collect(EMPTY, {"a.md": "Added\nFirst line.\nSecond line.\n\n\n"}, "2.0.0", "2026-02-02")
ck("a several line fragment is one bullet, later lines indented by two spaces", "- First line.\n  Second line.\n" in multi, multi)

carried = HEAD + "## [Unreleased]\n\n### Added\n- Old unreleased one.\n  Its second line.\n- Old unreleased two.\n\n### Fixed\n- Old fix.\n\n" + OLD
out = cc.collect(carried, FRAGS, "2.0.0", "2026-02-02")
sec = out.split("## [2.0.0] - 2026-02-02\n", 1)[1].split("## [1.0.0]")[0]
ck("bullets already under Unreleased are carried in, before the fragments of their category",
   sec.index("- Old unreleased one.\n  Its second line.\n- Old unreleased two.") < sec.index("- Added a.") and sec.index("- Old fix.") < sec.index("- Fixed b."), sec)
ck("Unreleased is left empty after carrying", "## [Unreleased]\n\n## [2.0.0]" in out and "Old unreleased" not in out.split("## [2.0.0]")[0], out)
ck("carrying Unreleased bullets alone (no fragments) makes the section", "- Old fix." in cc.collect(carried, {}, "2.0.0", "2026-02-02"))

ck("no fragments and an empty Unreleased: no change", cc.collect(EMPTY, {}, "2.0.0", "2026-02-02", "S") == EMPTY)
only_headings = HEAD + "## [Unreleased]\n\n### Added\n\n### Fixed\n\n" + OLD
ck("Unreleased with only category headings and no bullets: no change", cc.collect(only_headings, {}, "2.0.0", "2026-02-02") == only_headings)
nohead = HEAD + OLD
out = cc.collect(nohead, FRAGS, "2.0.0", "2026-02-02")
ck("no Unreleased heading: one is added before the first release, with the new section under it",
   out.index("## [Unreleased]") < out.index("## [2.0.0]") < out.index("## [1.0.0]") and out.count("## [Unreleased]") == 1, out)
ck("no Unreleased heading and nothing to collect: no change", cc.collect(nohead, {}, "2.0.0", "2026-02-02") == nohead)
ck("a changelog with no release headings at all still works", "## [2.0.0] - 2026-02-02" in cc.collect("# Changelog\n", FRAGS, "2.0.0", "2026-02-02"))
ck("the text ends with one newline", out.endswith("\n") and not out.endswith("\n\n"), repr(out[-20:]))
for bad, why in (("Removed\nGone.\n", "unknown category"), ("added\nGone.\n", "lower case category"), ("Added\n", "no text"),
                 ("Added\n   \n\n", "only white space"), ("", "empty file"), ("Gone.\n", "no category line")):
    try:
        cc.collect(EMPTY, {"ok.md": "Added\nFine.\n", "bad.md": bad}, "2.0.0", "2026-02-02")
        ck(f"collect raises for {why}", False, "no error")
    except ValueError as e:
        ck(f"collect raises for {why}, naming the file", "bad.md" in str(e) and "ok.md" not in str(e), str(e))

# ---- the command, on temp copies
code, msg, tmp = run(EMPTY, FRAGS, "A summary.")
text = (tmp / "CHANGELOG.md").read_text()
ck("the command writes the section and exits 0", code == 0 and "## [2.0.0] - 2026-02-02\n\nA summary.\n\n### Added" in text, f"{code} {msg}")
ck("the fragments are deleted and README.md is kept", left(tmp) == ["README.md"], str(left(tmp)))
shutil.rmtree(tmp)

for bad, why in (("Removed\nGone.\n", "an unknown category"), ("Fixed\n  \n", "no text")):
    frags = {"a.md": "Added\nFine.\n", "bad.md": bad}
    code, msg, tmp = run(EMPTY, frags)
    ck(f"{why}: exit 1 and bad.md named on stderr", code == 1 and "bad.md" in msg, f"{code} {msg}")
    ck(f"{why}: CHANGELOG.md is not changed", (tmp / "CHANGELOG.md").read_text() == EMPTY)
    ck(f"{why}: every fragment is still there", left(tmp) == ["README.md", "a.md", "bad.md"], str(left(tmp)))
    ck(f"{why}: the good fragment is untouched", (tmp / "changelog.d" / "a.md").is_file() and (tmp / "changelog.d" / "a.md").read_text() == "Added\nFine.\n")
    shutil.rmtree(tmp)

code, msg, tmp = run(EMPTY, {})
ck("an empty changelog.d (README.md only): no change, says so, exit 0",
   code == 0 and (tmp / "CHANGELOG.md").read_text() == EMPTY and "unchanged" in msg and left(tmp) == ["README.md"], f"{code} {msg}")
shutil.rmtree(tmp)
code, msg, tmp = run(EMPTY, None)
ck("a missing changelog.d: no change, exit 0", code == 0 and (tmp / "CHANGELOG.md").read_text() == EMPTY and not (tmp / "changelog.d").exists(), f"{code} {msg}")
shutil.rmtree(tmp)
code, msg, tmp = run(carried, {})
ck("Unreleased bullets and no fragments: collected, Unreleased left empty", code == 0 and "## [Unreleased]\n\n## [2.0.0]" in (tmp / "CHANGELOG.md").read_text(), f"{code} {msg}")
shutil.rmtree(tmp)

print(f"\n{P} passed, {F} failed")
raise SystemExit(1 if F else 0)
