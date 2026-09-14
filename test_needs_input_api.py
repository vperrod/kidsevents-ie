"""The needs-input catalogue admin API: `/admin/api/needs_input` and its
resubmit/reject actions.

These are full contract records the gate could not publish (a different file
and a different shape from the social candidates in `/admin/api/social/
staged`), so every test points `server`'s and `factory_worker`'s module-level
file constants at a temp directory -- a resubmit that succeeds writes into
the real events/places/holidays store, and the next test must not see it.
"""

import os
import tempfile
import unittest

import contract
import factory_worker
import server

REAL_DESCRIPTION = (
    "A multi-level soft play frame with slides and ball pits for children, in a "
    "space designed to give kids room to burn off energy away from the weather "
    "outside. The frame has separate zones aimed at different age groups, so "
    "toddlers are not competing for space with older children running around "
    "the same area. Parents can sit at tables around the edge of the frame "
    "while children play, and there is a small cafe area for snacks and "
    "drinks during a visit. Leisuredome is a popular choice locally for "
    "birthday parties, with party packages typically available on request "
    "from the venue. As an indoor venue it works well as a rainy day option "
    "for families in the area looking for something to do on a wet afternoon."
)


def _place(missing_field="description", **overrides):
    record = contract.EMPTY_RECORD("place")
    record["title"] = "Leisuredome Ashbourne"
    record["summary"] = "Indoor soft play centre in Ashbourne, County Meath."
    record["description"] = ("Indoor soft play for kids in Ashbourne."
                             if missing_field == "description" else REAL_DESCRIPTION)
    record["location"].update({
        "name": "Ashbourne", "city": "Ashbourne",
        "county": "" if missing_field == "county" else "Meath",
        "address": "" if missing_field == "address" else "Ashbourne, Co. Meath",
    })
    record["taxonomy"]["activity_types"] = [] if missing_field == "activity_types" else ["soft-play"]
    record["links"]["source_url"] = "https://example.ie/leisuredome"
    record["provenance"]["sources"] = [{"url": "https://example.ie/leisuredome", "fetched_at": ""}]
    record["provenance"]["facts"] = [] if missing_field == "facts" else [
        {"claim": "x", "quote": "soft play centre"}]
    record["status"] = "needs-input"
    record["missing_field"] = missing_field
    record["reason"] = f"no {missing_field} could be identified"
    for key, value in overrides.items():
        record[key] = value
    record = contract.derive(record)
    record["id"] = "place-leisuredome-ashbourne-meath"
    return record


class NeedsInputApiTests(unittest.TestCase):
    def setUp(self):
        self.client = server.app.test_client()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.needs_input_file = os.path.join(self.tmp.name, "needs_input.json")
        self.places_file = os.path.join(self.tmp.name, "places_output.json")
        self.events_file = os.path.join(self.tmp.name, "events_output.json")
        self.holidays_file = os.path.join(self.tmp.name, "holidays_output.json")
        self._restore("NEEDS_INPUT_FILE", self.needs_input_file)
        self._restore_fw("OUTPUT_FILE", self.events_file)
        self._restore_fw("PLACES_FILE", self.places_file)
        self._restore_fw("HOLIDAYS_FILE", self.holidays_file)
        self._restore_fw("STORE_FOR_KIND", {
            "event": self.events_file, "place": self.places_file, "holiday": self.holidays_file,
        })

    def _restore(self, attribute, value):
        original = getattr(server, attribute)
        setattr(server, attribute, value)
        self.addCleanup(setattr, server, attribute, original)

    def _restore_fw(self, attribute, value):
        original = getattr(factory_worker, attribute)
        setattr(factory_worker, attribute, value)
        self.addCleanup(setattr, factory_worker, attribute, original)

    def _seed(self, *records):
        factory_worker.write_json_atomic(self.needs_input_file, list(records))

    def _stored(self):
        return factory_worker.load_json_store(self.needs_input_file, [])

    def _on_air(self, kind_file):
        return factory_worker.load_json_store(kind_file, [])

    def _resubmit(self, record_id="place-leisuredome-ashbourne-meath", **patch):
        return self.client.post("/admin/api/needs_input/resubmit",
                                json={"id": record_id, "patch": patch})

    def test_lists_only_needs_input_records(self):
        self._seed(_place(), _place(status="rejected", id="place-other"))

        response = self.client.get("/admin/api/needs_input")

        self.assertEqual(response.get_json()["total"], 1)

    def test_counts_by_missing_field(self):
        self._seed(_place(missing_field="county"), _place(missing_field="description", id="place-b"))

        counts = self.client.get("/admin/api/needs_input").get_json()["by_missing_field"]

        self.assertEqual(counts, {"county": 1, "description": 1})

    def test_a_patch_that_clears_the_gate_publishes_the_record(self):
        self._seed(_place(missing_field="description"))

        response = self._resubmit(description=REAL_DESCRIPTION)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "on-air")

    def test_a_published_record_leaves_the_needs_input_queue(self):
        self._seed(_place(missing_field="description"))

        self._resubmit(description=REAL_DESCRIPTION)

        self.assertEqual(self._stored(), [])

    def test_a_published_record_appears_in_its_output_file(self):
        self._seed(_place(missing_field="description"))

        self._resubmit(description=REAL_DESCRIPTION)

        places = self._on_air(self.places_file)
        self.assertEqual(places[0]["status"], "on-air")

    def test_a_patch_that_does_not_clear_the_gate_stays_in_the_queue(self):
        self._seed(_place(missing_field="county"))

        response = self._resubmit(county="Not A Real County")

        self.assertEqual(response.status_code, 422)
        self.assertEqual(self._stored()[0]["status"], "needs-input")

    def test_a_still_failing_record_gets_its_reason_updated(self):
        self._seed(_place(missing_field="activity_types"))

        self._resubmit(county="Cork")  # unrelated field -- activity_types is still missing

        self.assertEqual(self._stored()[0]["missing_field"], "activity_types")

    def test_a_curator_supplied_fact_is_stamped_curator_confirmed_not_a_quote(self):
        self._seed(_place(missing_field="facts"))

        self._resubmit(facts="Leisuredome Ashbourne is a soft play centre.",
                       description=REAL_DESCRIPTION)

        places = self._on_air(self.places_file)
        self.assertEqual(places[0]["provenance"]["facts"][-1]["claim"], "curator-confirmed")

    def test_resubmit_requires_an_id(self):
        response = self.client.post("/admin/api/needs_input/resubmit",
                                    json={"patch": {"county": "Cork"}})

        self.assertEqual(response.status_code, 400)

    def test_resubmit_on_an_unknown_id_is_a_404(self):
        response = self._resubmit(record_id="place-does-not-exist", county="Cork")

        self.assertEqual(response.status_code, 404)

    def test_only_patched_fields_the_curator_actually_sent_are_touched(self):
        self._seed(_place(missing_field="description"))
        before = self._stored()[0]["title"]

        self._resubmit(description=REAL_DESCRIPTION)

        after = self._on_air(self.places_file)[0]["title"]
        self.assertEqual(after, before)

    def test_reject_marks_the_record_rejected_without_deleting_it(self):
        self._seed(_place())

        response = self.client.post("/admin/api/needs_input/reject",
                                    json={"id": "place-leisuredome-ashbourne-meath"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._stored()[0]["status"], "rejected")

    def test_a_rejected_record_no_longer_appears_in_the_default_list(self):
        self._seed(_place())
        self.client.post("/admin/api/needs_input/reject",
                         json={"id": "place-leisuredome-ashbourne-meath"})

        response = self.client.get("/admin/api/needs_input")

        self.assertEqual(response.get_json()["total"], 0)

    def test_reject_requires_an_id(self):
        response = self.client.post("/admin/api/needs_input/reject", json={})

        self.assertEqual(response.status_code, 400)

    def test_reject_on_an_unknown_id_is_a_404(self):
        response = self.client.post("/admin/api/needs_input/reject",
                                    json={"id": "place-does-not-exist"})

        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
