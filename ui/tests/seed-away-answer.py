"""Seed a scratch factory cell for away-answer.e2e.cjs (TR-1462). Invented names only.

Sam, a person with an email and a key of their own, on a cell whose Slack reader has already read
Sam's list once a moment ago: as on a cell that has been up for a while, the next new item is sent.
(A reader's first read of a person, or one after an hour away, only records what waits.)
Prints a JSON map on the last line.
usage: python seed-away-answer.py <data-dir>"""
import hashlib
import json
import os
import sys

sys.path.insert(0, os.getcwd())
from tares import waiting_slack  # noqa: E402
from tares.store import Store, now_utc  # noqa: E402

home = sys.argv[1]
SAM_KEY = "e2e-sam-key-away-answer"
store = Store(home)
now = now_utc()
sam = store.create_person("Sam", email="sam@example.com")
store.insert_api_key("k_sam", "sam", "nvf_sam", hashlib.sha256(SAM_KEY.encode()).hexdigest(),
                     ["admin", "read"], person_id=sam["id"])   # the key the crew runs on
store.set_setting(waiting_slack.LAST_READ, now.isoformat())
store.mark_waiting_told(sam["id"], [waiting_slack.SEEN], False, now)
store.con.close()
print(json.dumps({"sam_key": SAM_KEY, "sam": sam["id"]}))
