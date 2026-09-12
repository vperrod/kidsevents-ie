import tempfile
import unittest
from pathlib import Path

from member_store import MemberStore


class MemberStoreTests(unittest.TestCase):
    def test_saves_belong_to_the_verified_member_subject(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MemberStore(Path(directory) / "members.sqlite3")
            store.initialise()
            store.upsert_member("google:parent-1", "parent@example.ie")
            store.save_event("google:parent-1", "event-123")

            self.assertEqual(store.saved_event_keys("google:parent-1"), ["event-123"])
            store.remove_saved_event("google:parent-1", "event-123")
            self.assertEqual(store.saved_event_keys("google:parent-1"), [])
            self.assertEqual(store.saved_event_keys("google:parent-2"), [])

    def test_unknown_member_cannot_save_an_event(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MemberStore(Path(directory) / "members.sqlite3")
            store.initialise()

            with self.assertRaises(LookupError):
                store.save_event("unverified-subject", "event-123")


if __name__ == "__main__":
    unittest.main()
