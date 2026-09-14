"""Migration tests: legacy record in, contract record out, and the three-way
split. Fabricated records against a temporary store — no network, no LLM, and
the real catalogues are never touched."""

import json
from datetime import date, timedelta

import contract
import migrate_contract


def _legacy_event(**overrides):
    """A published event in the pre-contract shape, with a description long
    enough to clear the gate's word bar."""
    base = {
        "title": "Toddler Storytime at Pearse Street Library",
        "description": "Picture books, songs and a simple craft afterwards. " * 12,
        "start_date": (date.today() + timedelta(days=7)).isoformat(),
        "end_date": (date.today() + timedelta(days=7)).isoformat(),
        "time": "10:30",
        "venue_name": "Pearse Street Library",
        "venue_address": "138-144 Pearse St",
        "city": "Dublin",
        "county": "DN",
        "country": "Ireland",
        "latitude": "53.3441",
        "longitude": "-6.2527",
        "url": "https://www.dublincity.ie/events/storytime",
        "cost": "free",
        "age_group": "toddler",
        "category": "workshop",
        "suitable_for": "pushchair_accessible",
    }
    base.update(overrides)
    return base


def _legacy_place(**overrides):
    base = {
        "title": "Leisuredome Ashbourne",
        "description": "A large indoor soft play centre with a toddler zone. " * 20,
        "region": "Leinster",
        "county": "Meath",
        "location": "Ashbourne",
        "category": "Indoor play",
        "price_range": "check",
        "source_url": "https://www.tiktok.com/@leisuredomeashbourne/video/7677187258478349590",
        "source_name": "tiktok",
    }
    base.update(overrides)
    return base


def _migrated(old, kind="event"):
    record, _dropped = migrate_contract.legacy_to_contract(old, kind)
    return record


def test_a_migrated_event_matches_the_contract():
    assert contract.validate(_migrated(_legacy_event())) == []


def test_migration_resolves_the_county_code():
    assert _migrated(_legacy_event())["location"]["county"] == "Dublin"


def test_migration_folds_the_country_spelling_onto_a_code():
    assert _migrated(_legacy_event())["location"]["country"] == "IE"


def test_migration_maps_the_legacy_age_group_onto_bands():
    assert _migrated(_legacy_event())["taxonomy"]["age_bands"] == ["0-2", "3-5"]


def test_migration_maps_the_legacy_category_onto_an_activity_type():
    assert _migrated(_legacy_event())["taxonomy"]["activity_types"] == ["workshop-class"]


def test_migration_maps_suitable_for_onto_accessibility():
    assert _migrated(_legacy_event())["taxonomy"]["accessibility"] == ["buggy"]


def test_migration_maps_a_legacy_place_category():
    assert _migrated(_legacy_place(), "place")["taxonomy"]["activity_types"] == ["soft-play"]


def test_migration_keeps_the_source_url_as_provenance():
    assert _migrated(_legacy_event())["provenance"]["sources"] == [
        {"url": "https://www.dublincity.ie/events/storytime", "fetched_at": ""}]


def test_a_migrated_record_has_no_quoted_facts_so_it_needs_research():
    # Nothing in a legacy record is a verified quote, and the gate will not put
    # an ungrounded record on air -- this is why the migration cannot publish.
    assert _migrated(_legacy_event())["provenance"]["facts"] == []


def test_a_good_legacy_event_is_held_for_input_on_its_missing_facts():
    _record, verdict, _reason, missing_field, _dropped = migrate_contract.classify_record(
        _legacy_event(), "event", [])
    assert (verdict, missing_field) == ("needs-input", "facts")


def test_a_past_legacy_event_is_rejected():
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    _record, verdict, _reason, _missing, _dropped = migrate_contract.classify_record(
        _legacy_event(start_date=yesterday, end_date=yesterday), "event", [])
    assert verdict == "rejected"


def test_an_adult_legacy_event_is_rejected():
    _record, _verdict, reason, _missing, _dropped = migrate_contract.classify_record(
        _legacy_event(family_relevant=False), "event", [])
    assert reason == "not family-relevant"


def test_a_british_legacy_event_is_rejected():
    """Not a Northern Irish county, so still off the island of Ireland — the
    gate accepts GB only via a recognised NI county now (test_gate.py)."""
    _record, _verdict, reason, _missing, _dropped = migrate_contract.classify_record(
        _legacy_event(country="United Kingdom"), "event", [])
    assert reason == "country is GB, not on the island of Ireland"


def test_a_short_description_is_held_for_input_not_rejected():
    _record, verdict, _reason, missing_field, _dropped = migrate_contract.classify_record(
        _legacy_event(description="Storytime for toddlers."), "event", [])
    assert (verdict, missing_field) == ("needs-input", "description")


def test_a_record_already_on_the_contract_is_left_alone():
    already = contract.EMPTY_RECORD("event")
    already["status"] = "on-air"
    record, verdict, _reason, _missing, _dropped = migrate_contract.classify_record(
        already, "event", [])
    assert (record is already, verdict) == (True, "on-air")


def test_migrate_store_moves_needs_input_records_out_of_the_public_file(tmp_path, monkeypatch):
    monkeypatch.setattr(migrate_contract, "BASE", tmp_path)
    (tmp_path / "events_output.json").write_text(json.dumps([_legacy_event()]))
    kept, needs_input, _counts, _reasons, _dropped = migrate_contract.migrate_store(
        "events_output.json", "event", apply_changes=True)
    assert (len(kept), len(needs_input)) == (0, 1)


def test_migrate_store_writes_only_the_kept_records_back(tmp_path, monkeypatch):
    monkeypatch.setattr(migrate_contract, "BASE", tmp_path)
    store = tmp_path / "events_output.json"
    store.write_text(json.dumps([_legacy_event()]))
    migrate_contract.migrate_store("events_output.json", "event", apply_changes=True)
    assert json.loads(store.read_text()) == []


def test_a_dry_run_leaves_the_store_untouched(tmp_path, monkeypatch):
    monkeypatch.setattr(migrate_contract, "BASE", tmp_path)
    store = tmp_path / "events_output.json"
    store.write_text(json.dumps([_legacy_event()]))
    migrate_contract.migrate_store("events_output.json", "event", apply_changes=False)
    assert json.loads(store.read_text()) == [_legacy_event()]
