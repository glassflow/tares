"""Core with one person and no licence: the places that make a new person row must refuse
(answer licence_required) when the licence does not allow it. The audit is the Tares note
"Audit: multi-person paths" (TR-916). Each check below is skipped until TR-925 builds the
licence check it names; TR-925 replaces the skip with the real test."""


def skip(where):
    print(f"  skip {where}")


def test_body_person():
    skip("tares/daemon.py:_body_person")


def test_create_key():
    skip("tares/daemon.py:POST /api/keys (create_key)")


def test_set_key_person():
    skip("tares/daemon.py:PUT /api/keys/{kid}/person (set_key_person)")


def test_create_person():
    skip("tares/daemon.py:POST /api/people (create_person)")


def test_factory_people():
    skip("tares/screens/factory_people.py:POST /api/factory/people")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
