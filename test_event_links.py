import unittest

from event_links import google_maps_url, public_source_url


class EventLinkTests(unittest.TestCase):
    def test_uses_coordinates_when_present(self):
        url = google_maps_url({"latitude": 53.3498, "longitude": -6.2603})
        self.assertIn("query=53.3498%2C-6.2603", url)

    def test_builds_a_search_from_the_known_address(self):
        url = google_maps_url({"venue_name": "Farmleigh", "city": "Dublin", "country": "IE"})
        self.assertIn("Farmleigh%2C+Dublin%2C+IE", url)

    def test_rejects_unsafe_source_urls(self):
        self.assertEqual(public_source_url({"url": "javascript:alert(1)"}), "")
        self.assertEqual(public_source_url({"url": "https://example.ie/listing"}), "https://example.ie/listing")


if __name__ == "__main__":
    unittest.main()
