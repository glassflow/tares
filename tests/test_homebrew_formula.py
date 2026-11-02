"""scripts/update_homebrew_formula.py rewrites the formula's url and sha256, and nothing else. No network."""
import contextlib, io, os, sys, tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import update_homebrew_formula as u

REAL_FETCH = u.fetch_sdist

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))


SAMPLE = '''class Tares < Formula
  desc "Always-on agents"
  homepage "https://example.com"
  url "https://files.pythonhosted.org/packages/aa/bb/tares-1.0.0.tar.gz"
  sha256 "0000000000000000000000000000000000000000000000000000000000000000"
  license "MIT"

  depends_on "python@3.14"

  resource_note = '  url "inner"'
end
'''
URL = "https://files.pythonhosted.org/packages/cc/dd/tares-1.2.3.tar.gz"
SHA = "a" * 64


def run_main(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = u.main(argv)
    return rc, out.getvalue().strip(), err.getvalue().strip()


def test_rewrite():
    new = u.rewrite(SAMPLE, URL, SHA)
    ck("rewrite sets the url", f'  url "{URL}"\n' in new)
    ck("rewrite sets the sha256", f'  sha256 "{SHA}"\n' in new)
    old, nw = SAMPLE.splitlines(), new.splitlines()
    diff = [i for i, (a, b) in enumerate(zip(old, nw)) if a != b]
    ck("rewrite changes only the two lines", len(old) == len(nw) and len(diff) == 2, diff)
    ck("rewrite is idempotent", u.rewrite(new, URL, SHA) == new)


def test_rewrite_rejects():
    no_url = SAMPLE.replace('  url "https://files.pythonhosted.org/packages/aa/bb/tares-1.0.0.tar.gz"\n', "")
    two_url = SAMPLE.replace("  license", '  url "https://x/tares-2.tar.gz"\n  license')
    no_sha = "\n".join(l for l in SAMPLE.splitlines() if not l.startswith("  sha256")) + "\n"
    for label, text in (("zero url lines", no_url), ("two url lines", two_url), ("zero sha256 lines", no_sha)):
        try:
            u.rewrite(text, URL, SHA)
            ck(f"rewrite raises on {label}", False, "no error")
        except ValueError:
            ck(f"rewrite raises on {label}", True)


# The tap's Formula/tares.rb as of tares 1.45.0, verbatim.
REAL = '''class Tares < Formula
  desc "Data plane for always-on AI agents"
  homepage "https://tares-glassflow.com"
  url "https://files.pythonhosted.org/packages/68/a7/d2a5a654d97f978197a0b996f83a360cab72126a345a98a17b8fe57be38f/tares-1.45.0.tar.gz"
  sha256 "70ecf9284c91fa7c9eb8f81f4198301f095af264b15d835c6b0e7e7c5e47a851"
  license "MIT"

  depends_on "python@3.14"

  # The prebuilt arm64 wheels (jiter, ...) have no Mach-O header room for
  # Homebrew to rewrite their @rpath dylib IDs, which fails the install.
  preserve_rpath

  def install
    system formula_opt_bin("python@3.14")/"python3.14", "-m", "venv", libexec
    system libexec/"bin/pip", "install", "--no-cache-dir", "--only-binary=:all:", "tares==#{version}"
    %w[tares taresd tares-mcp].each { |c| bin.install_symlink libexec/"bin"/c }
  end

  test do
    assert_match version.to_s, shell_output("#{bin}/tares --version")
    system bin/"tares", "--help"
  end
end
'''


def test_preserve_rpath():
    new = u.rewrite(REAL, URL, SHA)
    ck("the tap formula keeps preserve_rpath", "\n  preserve_rpath\n" in new)
    ck("the tap formula keeps the preserve_rpath comment", "Homebrew to rewrite their @rpath dylib IDs" in new)
    old, nw = REAL.splitlines(), new.splitlines()
    diff = [(a.split() or [""])[0] for a, b in zip(old, nw) if a != b]
    ck("only url and sha256 change in the tap formula", len(old) == len(nw) and diff == ["url", "sha256"], (len(old), len(nw), diff))


def test_main(tmp):
    calls = []
    u.fetch_sdist = lambda v, *a, **k: (calls.append(v), (URL, SHA))[1]
    path = os.path.join(tmp, "tares.rb")
    with open(path, "w") as f:
        f.write(SAMPLE)

    rc, out, err = run_main(["1.2.3-rc.1", path])
    ck("main rejects a pre-release version", rc == 1 and "1.2.3-rc.1" in err, (rc, err))
    ck("a rejected version never reaches PyPI", calls == [], calls)
    with open(path) as f:
        ck("a rejected version leaves the file alone", f.read() == SAMPLE)

    rc, out, err = run_main(["1.2.3", path])
    ck("first run prints changed", rc == 0 and out == "changed", (rc, out, err))
    with open(path) as f:
        text = f.read()
    ck("first run writes the new url and sha256", f'  url "{URL}"' in text and f'  sha256 "{SHA}"' in text)
    rc, out, err = run_main(["1.2.3", path])
    ck("second run prints unchanged", rc == 0 and out == "unchanged", (rc, out, err))
    with open(path) as f:
        ck("second run leaves the file alone", f.read() == text)

    bad = os.path.join(tmp, "bad.rb")
    with open(bad, "w") as f:
        f.write("class Tares < Formula\nend\n")
    before = len(calls)
    rc, out, err = run_main(["1.2.3", bad])
    after_bad = len(calls)
    ck("main exits 1 with a message when the formula has no url", rc == 1 and err.startswith("error:"), (rc, err))
    ck("a formula of the wrong shape fails before any PyPI call", after_bad == before, calls)
    rc, out, err = run_main(["1.2.3\n", path])
    ck("main rejects a version with a trailing newline", rc == 1 and "release version" in err, (rc, err))
    rc, out, err = run_main(["1.2.3", os.path.join(tmp, "missing.rb")])
    ck("main exits 1 when the file is missing", rc == 1 and err.startswith("error:"), (rc, err))


def test_fetch_retries():
    import urllib.error
    u.fetch_sdist = REAL_FETCH
    slept = []
    u.time.sleep = lambda s: slept.append(s)
    n = {"i": 0}
    class Resp:
        def __init__(s, body): s.body = body
        def __enter__(s): return s
        def __exit__(s, *a): return False
        def read(s, *a): return s.body
    good = b'{"urls": [{"packagetype": "bdist_wheel", "url": "w", "digests": {"sha256": "w"}}, {"packagetype": "sdist", "url": "https://x/tares-1.2.3.tar.gz", "digests": {"sha256": "abc"}}]}'
    headers = []
    def fake(req, timeout=None):
        n["i"] += 1
        headers.append(req.get_header("Cache-control"))
        if n["i"] == 1:
            raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, None)
        if n["i"] == 2:
            raise urllib.error.URLError("down")
        if n["i"] == 3:
            return Resp(b'{"urls": []}')
        return Resp(good)
    u.urllib.request.urlopen = fake
    with contextlib.redirect_stderr(io.StringIO()):
        got = u.fetch_sdist("1.2.3", attempts=5, wait=7)
    ck("fetch_sdist retries a 404, a network error and a missing sdist", got == ("https://x/tares-1.2.3.tar.gz", "abc"), got)
    ck("fetch_sdist waits between attempts", slept == [7, 7, 7], slept)
    ck("fetch_sdist asks PyPI for a fresh answer", set(headers) == {"no-cache"}, headers)

    slept.clear()
    import http.client
    seq = iter([http.client.IncompleteRead(b""), Resp(b'{"urls": [{"packagetype": "sdist", "url": "u", "digests": {}}]}'), Resp(good)])
    def fake2(req, timeout=None):
        r = next(seq)
        if isinstance(r, Exception): raise r
        return r
    u.urllib.request.urlopen = fake2
    with contextlib.redirect_stderr(io.StringIO()):
        got = u.fetch_sdist("1.2.3", attempts=5, wait=1)
    ck("fetch_sdist retries a cut-off response and an sdist without sha256", got == ("https://x/tares-1.2.3.tar.gz", "abc") and slept == [1, 1], (got, slept))

    slept.clear()
    u.urllib.request.urlopen = lambda req, timeout=None: (_ for _ in ()).throw(urllib.error.HTTPError(req.full_url, 404, "nf", {}, None))
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            u.fetch_sdist("1.2.3", attempts=2, wait=1)
        ck("fetch_sdist raises after the last attempt", False, "no error")
    except RuntimeError as e:
        ck("fetch_sdist raises after the last attempt", "404" in str(e), str(e))
    ck("fetch_sdist does not sleep after the last attempt", slept == [1], slept)


test_rewrite()
test_rewrite_rejects()
test_preserve_rpath()
with tempfile.TemporaryDirectory() as d:
    test_main(d)
test_fetch_retries()

print(f"\n{P} passed, {F} failed")
raise SystemExit(1 if F else 0)
