import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import social_collection_ingest as ingest


class SocialCollectionStagingTests(unittest.TestCase):
    def test_only_new_permalinks_are_staged_for_review(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            ingest, "STAGED_FILE", Path(directory) / "social_candidates.json"
        ):
            first = ingest.write_staged([
                {"platform": "tiktok", "source_url": "https://www.tiktok.com/@a/video/1", "caption": "One", "author": ""}
            ])
            duplicate = ingest.write_staged([
                {"platform": "tiktok", "source_url": "https://www.tiktok.com/@a/video/1", "caption": "One", "author": ""}
            ])
            self.assertEqual(first, 1)
            self.assertEqual(duplicate, 0)
            item = __import__("json").loads(ingest.STAGED_FILE.read_text())[0]
            self.assertEqual(item["status"], "needs_review")


if __name__ == "__main__":
    unittest.main()
