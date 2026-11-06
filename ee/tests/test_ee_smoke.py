# Tares enterprise licence, see ee/LICENSE
"""The ee test run is wired up (TR-913): tares_ee imports, the test licence is valid only while
the test root is installed, and the ee test list is not empty.

Run: python ee/tests/test_ee_smoke.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import licensed

import tares.licence as L

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))


try:
    import tares_ee
    ck("import tares_ee works", True)
except Exception as e:
    ck("import tares_ee works", False, f"{type(e).__name__}: {e}")

text = licensed.test_licence()
ck("the test licence is not trusted by the shipped root keys", L.state(text, 0)["ee_allowed"] is False)
licensed.install_test_root()
try:
    st = L.state(text, 0)
    ck("with the test root installed the test licence allows ee", st["ee_allowed"] is True and st["valid"] is True, st)
    ck("the licence holds 5 seats", st["seats"] == 5, st)
    ck("a licence with no enterprise feature does not allow ee",
       L.state(licensed.test_licence(features=("other",)), 0)["ee_allowed"] is False)
    old = L.state(licensed.test_licence(expires="2020-01-01"), 0)
    ck("an expired licence says it is expired", old["expired"] is True, old)
    ck("no licence does not allow ee", L.state(None, 0)["ee_allowed"] is False)
finally:
    L._set_root_keys_for_tests(None)
ck("after the reset the test licence is not trusted again", L.state(text, 0)["ee_allowed"] is False)

lst = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ci_list.txt")
names = [ln.strip() for ln in open(lst) if ln.strip() and not ln.strip().startswith("#")]
ck("ee/tests/ci_list.txt is not empty", len(names) > 0)
ck("ee/tests/ci_list.txt lists test_ee_smoke", "test_ee_smoke" in names, names)

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
