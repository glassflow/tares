"""No method name is defined twice among Store and the classes it is built from.

Store is FactoryStore + ClaudeUsageStore (SreStore went with the AI SRE edition, TR-1809). Two of them defining the same name do not fail
anywhere: the first in the order wins and the other's callers get the wrong method. This happened once:
the Slack reader's `mark_told(person, items, sent, at)` in FactoryStore replaced the watch's
`mark_told(services, at, where)` in SreStore, and the watch's Slack notice raised a TypeError.

Run: python tests/test_store_methods.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tares.store import Store  # noqa: E402

P = F = 0


def ck(label, cond, detail: object = ""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


classes = [c for c in Store.__mro__ if c is not object]
ck("Store is built from more than one class", len(classes) > 1, classes)
where: dict[str, list[str]] = {}
for c in classes:
    for name, v in vars(c).items():
        if name.startswith("__") or not (callable(v) or isinstance(v, (staticmethod, classmethod, property))):
            continue
        where.setdefault(name, []).append(c.__name__)
twice = {n: cs for n, cs in where.items() if len(cs) > 1}
ck("no method name is defined in two of them", not twice, twice)

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
