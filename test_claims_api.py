"""The public facet feed and the organiser claim endpoint (phase 7).

Both write or read real files, so every test points the server's module-level
paths at a temp directory and clears the in-memory rate-limit table — a leaked
entry from one test silently 429s the next.
"""

import json
import os
import tempfile
import unittest

import server


CLAIM = {
    "record_id": "place-lullymore-heritage-park-kildare",
    "record_title": "Lullymore Heritage Park",
    "source_url": "https://example.ie/lullymore",
    "name": "Aoife Byrne",
    "email": "aoife@lullymore.ie",
    "relationship": "owner",
    "message": "We open at 10, not 9, and under-3s are free.",
    "has_photos": True,
}


class ClaimApiTests(unittest.TestCase):
    def setUp(self):
        self.client = server.app.test_client()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.claims_file = os.path.join(self.tmp.name, "claims.json")
        self._restore("CLAIMS_FILE", self.claims_file)
        server._claim_seen.clear()
        self.addCleanup(server._claim_seen.clear)

    def _restore(self, attribute, value):
        original = getattr(server, attribute)
        setattr(server, attribute, value)
        self.addCleanup(setattr, server, attribute, original)

    def _stored(self):
        with open(self.claims_file, encoding="utf-8") as handle:
            return json.load(handle)

    def test_stores_a_valid_claim_as_its_own_record(self):
        response = self.client.post("/api/claim", json=CLAIM)

        self.assertEqual(response.status_code, 200)
        stored = self._stored()
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["email"], "aoife@lullymore.ie")

    def test_a_stored_claim_starts_as_new(self):
        self.client.post("/api/claim", json=CLAIM)

        self.assertEqual(self._stored()[0]["status"], "new")

    def test_rejects_an_address_that_is_not_an_email(self):
        response = self.client.post("/api/claim", json={**CLAIM, "email": "aoife.at.home"})

        self.assertEqual(response.status_code, 400)

    def test_rejects_an_empty_message(self):
        response = self.client.post("/api/claim", json={**CLAIM, "message": "   "})

        self.assertEqual(response.status_code, 400)

    def test_rejects_a_relationship_outside_the_vocabulary(self):
        response = self.client.post("/api/claim", json={**CLAIM, "relationship": "admin"})

        self.assertEqual(response.status_code, 400)

    def test_a_rejected_claim_is_not_stored(self):
        self.client.post("/api/claim", json={**CLAIM, "name": ""})

        self.assertFalse(os.path.exists(self.claims_file))

    def test_a_second_claim_on_the_same_listing_from_one_address_is_refused(self):
        self.client.post("/api/claim", json=CLAIM)

        response = self.client.post("/api/claim", json=CLAIM)

        self.assertEqual(response.status_code, 429)

    def test_the_same_address_may_claim_a_different_listing(self):
        self.client.post("/api/claim", json=CLAIM)

        response = self.client.post(
            "/api/claim", json={**CLAIM, "source_url": "https://example.ie/other"}
        )

        self.assertEqual(response.status_code, 200)

    def test_a_failed_claim_does_not_use_up_the_hourly_slot(self):
        self.client.post("/api/claim", json={**CLAIM, "email": "nope"})

        response = self.client.post("/api/claim", json=CLAIM)

        self.assertEqual(response.status_code, 200)

    def test_the_admin_route_lists_stored_claims_newest_first(self):
        self.client.post("/api/claim", json={**CLAIM, "source_url": "https://example.ie/a"})
        stored = self._stored()
        stored[0]["received_at"] = "2020-01-01T00:00:00+00:00"
        with open(self.claims_file, "w", encoding="utf-8") as handle:
            json.dump(stored, handle)
        self.client.post("/api/claim", json={**CLAIM, "source_url": "https://example.ie/b"})

        listed = self.client.get("/admin/api/claims").get_json()["claims"]

        self.assertEqual(listed[0]["source_url"], "https://example.ie/b")

    def test_the_admin_route_reports_no_claims_before_any_arrive(self):
        self.assertEqual(self.client.get("/admin/api/claims").get_json()["total"], 0)


class FacetFeedTests(unittest.TestCase):
    """/api/v2/* is /api/v1/* trimmed for the browser: the rendering keys plus
    the facets the filter panel reads, and nothing else."""

    def setUp(self):
        self.client = server.app.test_client()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.places_file = os.path.join(self.tmp.name, "places.json")
        original = server.PLACES_FILE
        server.PLACES_FILE = self.places_file
        self.addCleanup(setattr, server, "PLACES_FILE", original)

    def _publish(self, *records):
        with open(self.places_file, "w", encoding="utf-8") as handle:
            json.dump(list(records), handle)

    def _record(self, **overrides):
        import contract

        record = contract.EMPTY_RECORD("place")
        record.update({"title": "Ardgillan Castle", "status": "on-air"})
        record["location"].update({"name": "Ardgillan", "county": "Dublin", "lat": 53.6, "lon": -6.2})
        record["taxonomy"].update(
            {
                "age_bands": ["3-5", "6-9"],
                "price_band": "free",
                "setting": "outdoor",
                "activity_types": ["park"],
                "accessibility": ["buggy"],
            }
        )
        record.update(overrides)
        return contract.derive(record)

    def test_carries_the_taxonomy_the_filters_read(self):
        self._publish(self._record())

        facets = self.client.get("/api/v2/places").get_json()[0]["facets"]

        self.assertEqual(facets["price_band"], "free")

    def test_carries_the_coordinates_near_me_sorts_on(self):
        self._publish(self._record())

        facets = self.client.get("/api/v2/places").get_json()[0]["facets"]

        self.assertEqual((facets["lat"], facets["lon"]), (53.6, -6.2))

    def test_keeps_the_rendering_keys_the_cards_already_use(self):
        self._publish(self._record())

        self.assertEqual(self.client.get("/api/v2/places").get_json()[0]["title"], "Ardgillan Castle")

    def test_leaves_out_records_that_are_not_on_air(self):
        self._publish(self._record(status="needs-input"))

        self.assertEqual(self.client.get("/api/v2/places").get_json(), [])

    def test_a_record_with_no_taxonomy_still_answers_with_empty_facets(self):
        self._publish({"title": "Older record", "status": "on-air"})

        facets = self.client.get("/api/v2/places").get_json()[0]["facets"]

        self.assertEqual(facets["activity_types"], [])

    def test_the_legacy_feed_is_untouched_by_the_new_one(self):
        self._publish(self._record())

        self.assertNotIn("facets", self.client.get("/api/places").get_json()[0])


if __name__ == "__main__":
    unittest.main()
