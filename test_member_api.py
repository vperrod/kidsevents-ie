import os
import unittest
from unittest.mock import patch

from server import app


class MemberApiTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_reports_when_optional_authentication_is_not_configured(self):
        with patch.dict(os.environ, {}, clear=True):
            config = self.client.get("/api/auth/config")
            saves = self.client.get("/api/member/saves")

        self.assertEqual(config.status_code, 200)
        self.assertEqual(config.get_json(), {"configured": False, "firebase": None})
        self.assertEqual(saves.status_code, 503)
        self.assertEqual(saves.get_json()["error"], "Authentication is not configured")


if __name__ == "__main__":
    unittest.main()
