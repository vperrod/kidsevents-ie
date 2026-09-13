"""Publication-gate tests: fabricated records only, no network and no LLM."""
from datetime import date, timedelta

import factory_worker


def _enriched(**overrides):
    """A minimally complete LLM-shaped event that the gate should accept."""
    base = {
        "title": "Toddler Storytime at Pearse Street Library",
        "description": (
            "A free weekly storytime for toddlers and their grown-ups in the children's "
            "room, with picture books, songs and a simple craft afterwards. No booking "
            "needed, buggies welcome, drop in any Tuesday morning during term."
        ),
        "date": (date.today() + timedelta(days=7)).isoformat(),
        "venue_name": "Pearse Street Library",
        "city": "Dublin",
        "county": "Dublin",
        "country": "IE",
        "family_relevant": True,
        "venue_coords": [53.3441, -6.2527],
        "cost": "free",
        "cost_detail": "Free, no booking needed",
        "age_group": "toddler",
        "category": "workshop",
        "website": "https://www.dublincity.ie/events/storytime",
        "image_url": "https://www.dublincity.ie/img/storytime.jpg",
        "image_alt": "Children sitting on a rug listening to a librarian",
        "suitable_for": "pushchair_accessible",
        "booking_required": "none",
        "booking_url": "",
        "phone": "+353 1 222 8488",
        "contact_email": "libraries@dublincity.ie",
        "duration_hours": 1.0,
        "time": "10:30",
    }
    base.update(overrides)
    return base


def test_past_event_is_rejected():
    today = date.today()
    event = factory_worker.normalize_event(
        _enriched(date=(today - timedelta(days=1)).isoformat())
    )
    reason = factory_worker.date_window_reason(event, today, today + timedelta(days=60))
    assert reason == f"the event is already over (ended {(today - timedelta(days=1)).isoformat()})"


def test_event_beyond_horizon_is_rejected():
    today = date.today()
    event = factory_worker.normalize_event(_enriched(date=(today + timedelta(days=90)).isoformat()))
    assert "beyond the 60-day horizon" in factory_worker.date_window_reason(
        event, today, today + timedelta(days=60)
    )


def test_non_irish_event_is_rejected():
    event = factory_worker.normalize_event(_enriched(country="United Kingdom"))
    assert factory_worker.event_reject_reason(event) == "country is GB, not IE"


def test_non_family_relevant_event_is_rejected():
    event = factory_worker.normalize_event(_enriched(family_relevant=False))
    assert factory_worker.event_reject_reason(event) == "not family-relevant"


def test_caption_as_title_is_rejected():
    caption = (
        "Vintage kilo sale this Saturday at DCU, doors at 11, bring cash, "
        "everything 20 euro a kilo, see you there"
    )
    event = factory_worker.normalize_event(_enriched(title=caption[:120]))
    assert factory_worker.event_reject_reason(event, caption) == (
        "title is the raw caption, not a synthesised title"
    )


def test_overlong_title_is_rejected():
    event = factory_worker.normalize_event(_enriched(title="Storytime " * 20))
    assert factory_worker.event_reject_reason(event) == "title is 199 characters (max 120)"


def test_event_without_start_date_is_rejected():
    event = factory_worker.normalize_event(_enriched(date=""))
    assert factory_worker.event_reject_reason(event) == "no start_date"


def test_complete_irish_event_is_accepted():
    assert factory_worker.event_reject_reason(factory_worker.normalize_event(_enriched())) is None


def test_normalize_event_keeps_the_fields_the_prompt_asks_for():
    event = factory_worker.normalize_event(_enriched())
    kept = (
        "website", "image_url", "image_alt", "suitable_for", "booking_required",
        "booking_url", "phone", "contact_email", "duration_hours", "cost",
        "cost_detail", "age_group", "category",
    )
    assert [k for k in kept if not event.get(k)] == ["booking_url"]


def test_confidence_is_the_completeness_score():
    assert factory_worker.normalize_event(_enriched())["confidence"] == 1.0


def test_confidence_falls_with_missing_fields():
    sparse = factory_worker.normalize_event(
        {"title": "Something", "date": (date.today() + timedelta(days=3)).isoformat()}
    )
    assert sparse["confidence"] == 0.1


def test_non_irish_place_is_rejected():
    place = {"title": "Warner Bros. Studio Tour London", "country": "GB", "family_relevant": True}
    assert factory_worker.place_reject_reason(place) == "country is GB, not IE"


def test_place_without_a_title_is_rejected():
    place = {"title": "", "country": "IE", "family_relevant": True}
    assert factory_worker.place_reject_reason(place) == "no title"


def test_normalize_country_folds_spellings_onto_one_code():
    assert [factory_worker.normalize_country(v) for v in ("Ireland", "ie", "", "Spain")] == [
        "IE", "IE", "IE", "other",
    ]


def test_load_json_store_raises_on_a_torn_file(tmp_path):
    torn = tmp_path / "events_output.json"
    torn.write_text('[{"title": "half a rec')
    try:
        factory_worker.load_json_store(torn, [])
    except ValueError:
        return
    raise AssertionError("a torn store must raise, never report an empty catalogue")


def test_load_json_store_returns_the_default_for_a_missing_file(tmp_path):
    assert factory_worker.load_json_store(tmp_path / "nope.json", []) == []


def test_write_json_atomic_leaves_no_temp_file(tmp_path):
    target = tmp_path / "places_output.json"
    factory_worker.write_json_atomic(target, [{"title": "Airfield Estate"}])
    assert sorted(p.name for p in tmp_path.iterdir()) == ["places_output.json"]
