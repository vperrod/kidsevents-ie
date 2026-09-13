"""QA gate tests: the facet fold and the on-air decision. Fabricated records
only — no network, no LLM.

A truthy `missing_field` means needs-input (a curator's note could fix it);
an empty one means rejected, and these tests assert which of the two each
fixture gets as much as they assert the reason.
"""

from datetime import date, timedelta

import contract
import gate

SOURCES_TEXT = (
    "Toddler Storytime runs every Tuesday from 6 October at Pearse Street Library. "
    "Free, no booking needed, buggies welcome."
)


def _event(**overrides):
    """A minimally complete event the gate should put on air."""
    record = contract.EMPTY_RECORD("event")
    record["title"] = "Toddler Storytime at Pearse Street Library"
    record["summary"] = "Free weekly storytime for under-fives, no booking needed."
    record["description"] = "Picture books, songs and a simple craft afterwards. " * 12
    record["location"].update({"name": "Pearse Street Library", "address": "138-144 Pearse St",
                               "city": "Dublin", "county": "Dublin",
                               "lat": 53.3441, "lon": -6.2527})
    record["links"]["source_url"] = "https://www.dublincity.ie/events/storytime"
    record["provenance"]["sources"] = [
        {"url": "https://www.dublincity.ie/events/storytime", "fetched_at": ""}]
    record["taxonomy"].update({"age_bands": ["0-2"], "price_band": "free",
                               "activity_types": ["library"]})
    record["provenance"]["facts"] = [{"claim": "It is free", "quote": "Free, no booking needed"}]
    record["event"].update({
        "start_date": (date.today() + timedelta(days=7)).isoformat(),
        "date_evidence": "every Tuesday from 6 October",
    })
    record.update(overrides)
    return contract.derive(record)


def _place(**overrides):
    record = contract.EMPTY_RECORD("place")
    record["title"] = "Leisuredome Ashbourne"
    record["summary"] = "Indoor soft play centre in Ashbourne, County Meath."
    record["description"] = "A large indoor soft play centre with a toddler zone. " * 20
    record["location"].update({"name": "Leisuredome", "address": "Ashbourne Business Park",
                               "city": "Ashbourne", "county": "Meath"})
    record["links"]["source_url"] = "https://www.leisuredome.ie/"
    record["provenance"]["sources"] = [{"url": "https://www.leisuredome.ie/", "fetched_at": ""}]
    record["taxonomy"].update({"activity_types": ["soft-play"], "price_band": "under-10"})
    record["provenance"]["facts"] = [{"claim": "It has soft play", "quote": "softplay equipment"}]
    record.update(overrides)
    return contract.derive(record)


def _holiday(domains=2):
    record = contract.EMPTY_RECORD("holiday")
    record["title"] = "Lanzarote with small children"
    record["summary"] = "Short-flight winter sun with calm beaches and a volcano park."
    record["description"] = "Playa Blanca has shallow, sheltered beaches for paddling. " * 20
    record["location"].update({"name": "Playa Blanca", "city": "Playa Blanca", "country": "ES"})
    record["links"]["source_url"] = "https://en.wikivoyage.org/wiki/Lanzarote"
    record["taxonomy"].update({"age_bands": ["0-2"], "price_band": "unknown"})
    record["provenance"]["facts"] = [{"claim": "Beaches are sheltered", "quote": "sheltered beaches"}]
    record["provenance"]["sources"] = [
        {"url": "https://en.wikivoyage.org/wiki/Lanzarote", "fetched_at": ""},
        {"url": "https://www.wikidata.org/wiki/Q81799", "fetched_at": ""},
    ][:domains]
    record["holiday"].update({"holiday_types": ["winter-sun"], "best_seasons": ["winter"],
                              "destination_type": "island"})
    return contract.derive(record)


# --- gate_meta: fold onto the vocabulary, drop the rest -------------------

def test_gate_meta_maps_a_legacy_age_group_onto_bands():
    clean, _dropped = gate.gate_meta({"age_bands": ["toddler"]})
    assert clean["age_bands"] == ["0-2", "3-5"]


def test_gate_meta_maps_a_legacy_category_onto_an_activity_type():
    clean, _dropped = gate.gate_meta({"activity_types": ["Indoor play"]})
    assert clean["activity_types"] == ["soft-play"]


def test_gate_meta_drops_a_value_outside_the_vocabulary():
    _clean, dropped = gate.gate_meta({"activity_types": ["escape room"]})
    assert dropped == ["activity_types=escape room"]


def test_gate_meta_never_substitutes_a_default_for_a_dropped_value():
    clean, _dropped = gate.gate_meta({"setting": "underwater"})
    assert clean["setting"] == ""


def test_gate_meta_maps_pushchair_accessible_onto_buggy():
    clean, _dropped = gate.gate_meta({"accessibility": ["pushchair_accessible"]})
    assert clean["accessibility"] == ["buggy"]


def test_gate_meta_derives_a_price_band_from_a_euro_amount():
    clean, _dropped = gate.gate_meta({"price_band": "paid", "price_detail": "€5 per child"})
    assert clean["price_band"] == "under-10"


def test_gate_meta_leaves_a_paid_band_unknown_without_an_amount():
    clean, _dropped = gate.gate_meta({"price_band": "membership", "price_detail": "members only"})
    assert clean["price_band"] == "unknown"


def test_gate_holiday_drops_an_invented_holiday_type():
    _clean, dropped = gate.gate_holiday({"holiday_types": ["beach", "spa-retreat"]})
    assert dropped == ["holiday_types=spa-retreat"]


def test_facet_gloss_lists_the_holiday_fields_only_for_holidays():
    assert "budget_band" in gate.facet_gloss("holiday") and "budget_band" not in gate.facet_gloss("event")


# --- qa: the on-air decision ---------------------------------------------

def test_a_complete_event_goes_on_air():
    assert gate.qa(_event(), SOURCES_TEXT) == (True, "", "")


def test_a_past_event_is_rejected():
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    record = _event()
    record["event"].update({"start_date": yesterday, "end_date": yesterday})
    assert gate.qa(record, SOURCES_TEXT) == (False, f"the event is already over (ended {yesterday})", "")


def test_an_adult_event_is_rejected_not_held_for_input():
    record = _event()
    record["family_relevant"] = False
    assert gate.qa(record, SOURCES_TEXT) == (False, "not family-relevant", "")


def test_a_caption_titled_candidate_is_rejected():
    caption = ("Vintage kilo sale this Saturday at DCU, doors at 11, bring cash, "
               "everything 20 euro a kilo, see you there")
    record = _event()
    record["title"] = caption[:70]
    assert gate.qa(record, caption)[1] == "title is the raw caption, not a synthesised title"


def test_an_event_outside_ireland_is_rejected():
    record = _event()
    record["location"]["country"] = "GB"
    assert gate.qa(contract.derive(record), SOURCES_TEXT) == (False, "country is GB, not IE", "")


def test_irish_coordinates_that_are_not_in_ireland_are_rejected():
    record = _event()
    record["location"].update({"lat": 40.4, "lon": -3.7})
    assert gate.qa(record, SOURCES_TEXT)[1].startswith("says Ireland but the coordinates")


def test_a_duplicate_of_an_on_air_title_is_rejected():
    record = _event()
    assert gate.qa(record, SOURCES_TEXT, ["Toddler storytime at Pearse Street Library!"]) == (
        False, "duplicate of an item already on air", "")


def test_a_short_description_needs_input_not_rejection():
    record = _event()
    record["description"] = "Storytime for toddlers."
    assert gate.qa(record, SOURCES_TEXT)[2] == "description"


def test_date_evidence_that_is_not_in_the_source_needs_input():
    record = _event()
    record["event"]["date_evidence"] = "every Thursday from 9 November"
    assert gate.qa(record, SOURCES_TEXT) == (
        False, "the quoted date evidence is not in the source text", "date_evidence")


def test_an_event_with_no_quoted_date_needs_input():
    record = _event()
    record["event"]["date_evidence"] = ""
    assert gate.qa(record, SOURCES_TEXT)[2] == "date_evidence"


def test_an_event_without_an_age_band_needs_input():
    record = _event()
    record["taxonomy"]["age_bands"] = []
    assert gate.qa(record, SOURCES_TEXT)[2] == "age_bands"


def test_a_record_with_no_source_at_all_is_rejected():
    record = _event()
    record["links"]["source_url"] = ""
    record["provenance"]["sources"] = []
    assert gate.qa(record, SOURCES_TEXT)[1].endswith("(no-content)")


def test_a_record_with_no_quoted_facts_needs_input():
    record = _event()
    record["provenance"]["facts"] = []
    assert gate.qa(record, SOURCES_TEXT)[2] == "facts"


def test_a_complete_place_goes_on_air():
    assert gate.qa(_place()) == (True, "", "")


def test_a_place_without_an_activity_type_needs_input():
    record = _place()
    record["taxonomy"]["activity_types"] = []
    assert gate.qa(record)[2] == "activity_types"


def test_a_holiday_with_two_source_domains_goes_on_air():
    assert gate.qa(_holiday(domains=2)) == (True, "", "")


def test_a_holiday_with_one_source_domain_needs_input():
    assert gate.qa(_holiday(domains=1)) == (
        False, "a holiday needs two independent sources", "sources")
