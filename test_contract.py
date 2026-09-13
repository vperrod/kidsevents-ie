"""Contract tests: the shape, the derivations and the legacy view. No network,
no LLM — every record here is fabricated in the test."""

import contract


def _event():
    record = contract.EMPTY_RECORD("event")
    record["title"] = "Toddler Storytime at Pearse Street Library"
    record["summary"] = "Free weekly storytime for under-fives, no booking needed."
    record["description"] = "Picture books, songs and a simple craft. " * 12
    record["location"].update({
        "name": "Pearse Street Library", "address": "138-144 Pearse St",
        "city": "Dublin", "county": "DN", "lat": 53.3441, "lon": -6.2527,
    })
    record["links"]["source_url"] = "https://www.dublincity.ie/events/storytime"
    record["taxonomy"].update({
        "age_bands": ["0-2", "3-5"], "price_band": "free", "price_detail": "Free",
        "activity_types": ["library"],
    })
    record["provenance"]["facts"] = [{"claim": "It is free", "quote": "Free, no booking needed"}]
    record["provenance"]["sources"] = [
        {"url": "https://www.dublincity.ie/events/storytime", "fetched_at": ""}]
    record["event"].update({"start_date": "2026-10-06", "end_date": "2026-10-06",
                            "date_evidence": "every Tuesday from 6 October"})
    return contract.derive(record)


def test_empty_record_carries_only_its_own_kind_block():
    assert [k for k in contract.KINDS if k in contract.EMPTY_RECORD("place")] == ["place"]


def test_empty_record_rejects_an_unknown_kind():
    try:
        contract.EMPTY_RECORD("attraction")
    except ValueError:
        return
    raise AssertionError("EMPTY_RECORD must refuse a kind outside KINDS")


def test_a_complete_event_has_no_contract_violations():
    assert contract.validate(_event()) == []


def test_a_record_carrying_two_kind_blocks_is_a_violation():
    record = _event()
    record["place"] = {}
    assert "carries a place block but kind is event" in contract.validate(record)


def test_an_overlong_title_is_a_violation():
    record = _event()
    record["title"] = "Storytime " * 12
    assert "title is 120 characters (max 80)" in contract.validate(record)


def test_a_missing_kind_block_is_a_violation():
    record = _event()
    del record["event"]
    assert "missing the event block" in contract.validate(record)


def test_derive_resolves_a_county_code_to_its_name():
    assert _event()["location"]["county"] == "Dublin"


def test_derive_fills_the_region_from_the_county():
    assert _event()["location"]["region"] == "Leinster"


def test_region_for_knows_the_six_northern_counties():
    assert contract.region_for("Fermanagh") == "Ulster"


def test_region_for_returns_empty_for_a_non_county():
    assert contract.region_for("Yorkshire") == ""


def test_maps_url_prefers_coordinates():
    assert contract.maps_url({"lat": 53.3441, "lon": -6.2527}) == (
        "https://www.google.com/maps/search/?api=1&query=53.3441%2C-6.2527"
    )


def test_maps_url_falls_back_to_the_written_address():
    assert contract.maps_url({"name": "Airfield Estate", "county": "Dublin"}) == (
        "https://www.google.com/maps/search/?api=1&query=Airfield%20Estate%2C%20Dublin%2C%20Ireland"
    )


def test_maps_url_is_empty_when_there_is_no_location_at_all():
    assert contract.maps_url({"country": "IE"}) == ""


def test_slugify_includes_the_county():
    assert contract.slugify("Toddler Storytime!", "Dublin") == "toddler-storytime-dublin"


def test_derive_ids_the_record_by_kind_and_slug():
    assert _event()["id"] == "event-toddler-storytime-at-pearse-street-library-dublin"


def test_ireland_is_derived_from_the_country_code():
    record = _event()
    record["location"]["country"] = "ES"
    assert contract.derive(record)["location"]["ireland"] is False


def test_confidence_is_the_completeness_score():
    assert _event()["provenance"]["confidence"] == contract.completeness(_event())


def test_completeness_falls_when_fields_are_missing():
    assert contract.completeness(contract.EMPTY_RECORD("event")) == 0.0


def test_legacy_view_exposes_the_keys_the_frontend_reads():
    expected = {
        "title", "description", "start_date", "end_date", "venue_name", "venue_address",
        "city", "county", "country", "latitude", "longitude", "url", "source", "cost",
        "age_group", "category", "confidence", "region", "location", "price_range",
        "source_url", "source_name", "booking_url",
    }
    assert set(contract.legacy_view(_event())) == expected


def test_legacy_view_flattens_the_nested_start_date():
    assert contract.legacy_view(_event())["start_date"] == "2026-10-06"


def test_legacy_view_names_the_source_host():
    assert contract.legacy_view(_event())["source_name"] == "dublincity.ie"


def test_legacy_view_passes_a_pre_contract_record_through_unchanged():
    old = {"title": "Kidspace Rathcoole", "start_date": "2026-12-01"}
    assert contract.legacy_view(old) is old
