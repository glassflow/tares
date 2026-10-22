Patches carried over from TR-635 for TR-638. Delete this folder before the TR-638 PR.

- end_guard_code.patch / end_guard_test.patch: the end_station person guard in tares/factory_api.py and its section in tests/test_crews.py (#258 F3, server half).
- factory_api_both.patch: tares/factory_api.py with the end_station guard AND the desk_target private-project check (F5); f5_test.patch is the F5 section of tests/test_desk_person.py.
- Apply with `git apply`. Both patches to factory_api.py were taken against teams at d48229e; check the hunks still apply.
