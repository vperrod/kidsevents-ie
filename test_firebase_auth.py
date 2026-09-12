import os
import unittest
from unittest.mock import patch

from firebase_auth import public_config


class FirebaseConfigurationTests(unittest.TestCase):
    def test_requires_every_public_web_setting(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(public_config())

    def test_returns_only_the_public_web_settings(self):
        settings = {
            "FIREBASE_API_KEY": "public-key",
            "FIREBASE_AUTH_DOMAIN": "project.firebaseapp.com",
            "FIREBASE_PROJECT_ID": "project",
            "FIREBASE_APP_ID": "app-id",
        }
        with patch.dict(os.environ, settings, clear=True):
            self.assertEqual(public_config(), {
                "apiKey": "public-key",
                "authDomain": "project.firebaseapp.com",
                "projectId": "project",
                "appId": "app-id",
            })


if __name__ == "__main__":
    unittest.main()
