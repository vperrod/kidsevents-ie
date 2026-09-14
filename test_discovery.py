#!/usr/bin/env python3
"""Phase 3 -- the mechanics of the discovery lanes, on fixtures.

Everything here is the half of a lane that has no network and no model in it:
the ledger's recheck arithmetic, the two feed formats, the search template
expansion, the Overpass and sitemap parsers, and the computed holiday facts.
The lanes' live halves are verified by running a real cycle, not by a mock.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

import discovery
import factory_worker
from discovery import ledger
from discovery.lanes import (feeds, holidays_seed, listings, opendata_places, search,
                             sitemaps, wikidata)

NOW = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------

def test_a_url_never_seen_is_due():
    assert ledger.is_due(None, "event", now=NOW) is True


def test_an_event_rechecked_yesterday_is_not_due():
    entry = {"last_seen": (NOW - timedelta(days=1)).isoformat(), "kind": "event"}
    assert ledger.is_due(entry, now=NOW) is False


def test_an_event_rechecked_four_days_ago_is_due():
    entry = {"last_seen": (NOW - timedelta(days=4)).isoformat(), "kind": "event"}
    assert ledger.is_due(entry, now=NOW) is True


def test_a_place_rechecked_four_days_ago_is_not_due():
    entry = {"last_seen": (NOW - timedelta(days=4)).isoformat(), "kind": "place"}
    assert ledger.is_due(entry, now=NOW) is False


def test_a_holiday_rechecked_forty_days_ago_is_not_due():
    entry = {"last_seen": (NOW - timedelta(days=40)).isoformat(), "kind": "holiday"}
    assert ledger.is_due(entry, now=NOW) is False


def test_an_unparseable_timestamp_is_treated_as_due():
    assert ledger.is_due({"last_seen": "whenever", "kind": "place"}, now=NOW) is True


def test_filter_due_drops_a_url_twice_in_one_batch():
    batch = [{"source_url": "https://a.ie/x"}, {"source_url": "https://a.ie/x"}]
    assert len(ledger.filter_due(batch, data={}, now=NOW)) == 1


def test_filter_due_drops_a_url_the_ledger_saw_today():
    data = {"https://a.ie/x": {"last_seen": NOW.isoformat(), "kind": "event"}}
    batch = [{"source_url": "https://a.ie/x"}, {"source_url": "https://a.ie/y"}]
    assert [one["source_url"] for one in ledger.filter_due(batch, data=data, now=NOW)] \
        == ["https://a.ie/y"]


@pytest.fixture
def temp_ledger(monkeypatch, tmp_path):
    monkeypatch.setattr(ledger, "LEDGER_FILE", tmp_path / "ledger.json")
    return ledger


def test_a_lane_outage_on_a_new_url_leaves_the_ledger_untouched(temp_ledger):
    temp_ledger.record("https://a.ie/x", "event", "lane-failed")
    assert temp_ledger.load() == {}


def test_a_lane_outage_does_not_move_an_existing_urls_recheck_clock(temp_ledger):
    temp_ledger.record("https://a.ie/x", "event", "rejected")
    stamped = temp_ledger.load()["https://a.ie/x"]["last_seen"]
    temp_ledger.record("https://a.ie/x", "event", "lane-failed")
    assert temp_ledger.load()["https://a.ie/x"]["last_seen"] == stamped


def test_a_researched_url_records_its_kind_and_verdict(temp_ledger):
    temp_ledger.record("https://a.ie/x", "place", "on-air")
    entry = temp_ledger.load()["https://a.ie/x"]
    assert (entry["kind"], entry["status"]) == ("place", "on-air")


# ---------------------------------------------------------------------------
# Lane 1 -- the listing link index
# ---------------------------------------------------------------------------

PAGE = "https://www.purecork.ie/whats-on"
LISTING_MD = """
[Sign in](https://www.purecork.ie/auth/login)
[Cookie policy](https://www.purecork.ie/privacy)
![logo](https://www.purecork.ie/images/logo.png)
[Circus Vegas](https://www.purecork.ie/whats-on/11200726/circus-vegas)
[Circus Vegas](https://www.purecork.ie/whats-on/11200726/circus-vegas)
[Playspaces](https://www.purecork.ie/whats-on/11198372/playspaces)
[Our Instagram](https://www.instagram.com/purecork.ie/)
[Cork Tourism](https://failteireland.ie/cork)
[What's On](https://www.purecork.ie/whats-on)
[x](https://www.purecork.ie/whats-on/11111/short-anchor-ok)
"""


def test_the_link_index_keeps_the_pages_own_detail_links():
    assert [one["title"] for one in listings.link_index(LISTING_MD, PAGE)] \
        == ["Circus Vegas", "Playspaces"]


def test_the_link_index_drops_sign_in_and_policy_chrome():
    urls = [one["url"] for one in listings.link_index(LISTING_MD, PAGE)]
    assert not any("login" in url or "privacy" in url for url in urls)


def test_the_link_index_drops_images():
    assert not any(".png" in one["url"] for one in listings.link_index(LISTING_MD, PAGE))


def test_the_link_index_drops_another_sites_link_that_merely_contains_the_host():
    assert not any("instagram" in one["url"] for one in listings.link_index(LISTING_MD, PAGE))


def test_the_link_index_drops_the_listing_page_pointing_at_itself():
    assert PAGE not in [one["url"] for one in listings.link_index(LISTING_MD, PAGE)]


def test_the_link_index_lists_a_repeated_link_once():
    urls = [one["url"] for one in listings.link_index(LISTING_MD, PAGE)]
    assert len(urls) == len(set(urls))


def test_the_split_turns_the_models_picks_back_into_links(monkeypatch):
    links = [{"title": "A", "url": "https://a.ie/1"}, {"title": "B", "url": "https://a.ie/2"}]
    monkeypatch.setattr(listings.factory_worker, "hermes",
                        lambda *a, **k: '{"picks": [2]}')
    assert listings._split_listing(links, PAGE, 10) == [links[1]]


def test_the_split_ignores_a_pick_that_is_not_a_link_number(monkeypatch):
    links = [{"title": "A", "url": "https://a.ie/1"}]
    monkeypatch.setattr(listings.factory_worker, "hermes",
                        lambda *a, **k: '{"picks": [99, "banana", 1]}')
    assert listings._split_listing(links, PAGE, 10) == [links[0]]


def test_the_split_makes_no_model_call_for_a_page_with_no_links(monkeypatch):
    monkeypatch.setattr(listings.factory_worker, "hermes",
                        lambda *a, **k: pytest.fail("should not call a model"))
    assert listings._split_listing([], PAGE, 10) == []


def test_a_lane_outage_on_the_split_yields_no_items(monkeypatch):
    monkeypatch.setattr(listings.factory_worker, "hermes", lambda *a, **k: "")
    assert listings._split_listing([{"title": "A", "url": "https://a.ie/1"}], PAGE, 10) == []


# ---------------------------------------------------------------------------
# Lane 2 -- ICS and RSS parsing
# ---------------------------------------------------------------------------

ICS = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
DTSTART;TZID=Europe/Dublin:20261026T100000
DTEND;TZID=Europe/Dublin:20261026T160000
SUMMARY:Halloween Family Day
DESCRIPTION:Pumpkin carving\\, storytelling and a costume parade for
  all ages.
LOCATION:Monaghan Library\\, Monaghan
URL:https://monaghan.ie/event/halloween-family-day/
END:VEVENT
BEGIN:VEVENT
DTSTART;VALUE=DATE:20261101
SUMMARY:Toddler Storytime
END:VEVENT
END:VCALENDAR
"""

RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>Family Fun</title>
  <item>
    <title>Ten free things to do in Cork this weekend</title>
    <link>https://www.familyfun.ie/free-cork/</link>
    <description>&lt;p&gt;Our pick of the &lt;b&gt;free&lt;/b&gt; days out.&lt;/p&gt;</description>
  </item>
</channel></rss>
"""


def test_ics_parsing_finds_every_vevent():
    assert len(feeds.parse_ics(ICS)) == 2


def test_ics_parsing_unfolds_a_wrapped_description():
    assert feeds.parse_ics(ICS)[0]["description"].endswith("costume parade for all ages.")


def test_ics_parsing_unescapes_a_comma():
    assert feeds.parse_ics(ICS)[0]["location"] == "Monaghan Library, Monaghan"


def test_ics_parsing_strips_the_parameters_off_a_property():
    assert feeds.parse_ics(ICS)[0]["dtstart"] == "20261026T100000"


def test_ics_date_reads_a_datetime_stamp():
    assert feeds.ics_date("20261026T100000") == "2026-10-26"


def test_ics_date_reads_a_date_only_stamp():
    assert feeds.ics_date("20261101") == "2026-11-01"


def test_ics_date_is_empty_for_a_stamp_it_cannot_read():
    assert feeds.ics_date("next Tuesday") == ""


def test_an_ics_event_becomes_a_candidate_quoting_its_own_dtstart():
    candidates = feeds._from_ics(ICS, {"url": "f", "county": "Monaghan"}, "monaghan", 10)
    assert candidates[0]["prefill"]["date_evidence"] == "DTSTART:20261026T100000"


def test_an_ics_candidates_caption_contains_the_evidence_it_quotes():
    candidate = feeds._from_ics(ICS, {"url": "f", "county": "Monaghan"}, "monaghan", 10)[0]
    assert candidate["prefill"]["date_evidence"] in candidate["caption"]


def test_an_ics_candidate_uses_the_events_own_url():
    candidate = feeds._from_ics(ICS, {"url": "f"}, "monaghan", 10)[0]
    assert candidate["source_url"] == "https://monaghan.ie/event/halloween-family-day/"


def test_rss_parsing_reads_the_item_link():
    assert feeds.parse_rss(RSS)[0]["link"] == "https://www.familyfun.ie/free-cork/"


def test_rss_parsing_strips_html_out_of_the_description():
    assert feeds.parse_rss(RSS)[0]["description"] == "Our pick of the free days out."


def test_rss_parsing_ignores_an_item_with_no_http_link():
    broken = RSS.replace("https://www.familyfun.ie/free-cork/", "javascript:void(0)")
    assert feeds.parse_rss(broken) == []


# ---------------------------------------------------------------------------
# Lane 3 -- sitemaps
# ---------------------------------------------------------------------------

SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://a.ie/events/halloween-farm</loc></url>
  <url><loc>https://a.ie/about-us</loc></url>
  <url><loc>https://a.ie/things-to-do/kayaking</loc></url>
  <url><loc>https://a.ie/privacy</loc></url>
</urlset>
"""

SITEMAP_INDEX = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://a.ie/event-sitemap.xml</loc></sitemap>
  <sitemap><loc>https://a.ie/post-sitemap.xml</loc></sitemap>
</sitemapindex>
"""


def test_a_sitemap_yields_its_page_urls():
    pages, _children = sitemaps.sitemap_urls(SITEMAP)
    assert len(pages) == 4


def test_a_sitemap_index_yields_child_sitemaps_and_no_pages():
    pages, children = sitemaps.sitemap_urls(SITEMAP_INDEX)
    assert (pages, children) == ([], ["https://a.ie/event-sitemap.xml",
                                      "https://a.ie/post-sitemap.xml"])


def test_the_sitemap_lane_skips_urls_already_researched_before_it_caps(monkeypatch,
                                                                      temp_ledger):
    temp_ledger.record("https://a.ie/events/halloween-farm", "event", "rejected")
    monkeypatch.setattr(sitemaps, "_read", lambda url: SITEMAP)
    state = {"config": {"domains": ["https://a.ie"], "domains_per_cycle": 1,
                        "urls_per_domain": 1},
             "budget": 10, "errors": [], "factory_state": {}, "cursor": None, "calls": None}
    assert [one["source_url"] for one in sitemaps.run(state)] \
        == ["https://a.ie/things-to-do/kayaking"]


def test_the_url_filter_keeps_only_family_and_event_paths():
    pages, _children = sitemaps.sitemap_urls(SITEMAP)
    assert sitemaps.wanted(pages) == ["https://a.ie/events/halloween-farm",
                                      "https://a.ie/things-to-do/kayaking"]


# ---------------------------------------------------------------------------
# Lane 4 -- search template expansion
# ---------------------------------------------------------------------------

TEMPLATES = {
    "always": ["family events {county} this weekend", "soft play {county}"],
    "seasonal": {
        "santa": {"months": [10, 11, 12], "templates": ["santa experience {county} {year}"]},
        "summer_camps": {"months": [3, 4, 5, 6], "templates": ["summer camps {county} {year}"]},
    },
}


def test_october_gets_the_santa_templates():
    assert "santa experience {county} {year}" in search.templates_for(10, TEMPLATES)


def test_may_does_not_get_the_santa_templates():
    assert "santa experience {county} {year}" not in search.templates_for(5, TEMPLATES)


def test_may_gets_the_summer_camp_templates():
    assert "summer camps {county} {year}" in search.templates_for(5, TEMPLATES)


def test_january_gets_only_the_always_on_templates():
    assert search.templates_for(1, TEMPLATES) == TEMPLATES["always"]


def test_expansion_produces_one_query_per_county_and_template():
    assert len(search.expand(search.templates_for(10, TEMPLATES), ["Cork", "Mayo"], 2026)) == 6


def test_expansion_renders_the_county_and_the_year():
    queries = search.expand(["santa experience {county} {year}"], ["Mayo"], 2026)
    assert queries == [("Mayo", "santa experience Mayo 2026")]


def test_expansion_is_county_major_so_a_truncated_run_covers_whole_counties():
    queries = search.expand(["a {county}", "b {county}"], ["Cork", "Mayo"], 2026)
    assert [county for county, _query in queries] == ["Cork", "Cork", "Mayo", "Mayo"]


# ---------------------------------------------------------------------------
# Lane 5 -- Overpass and CSV parsing
# ---------------------------------------------------------------------------

OVERPASS = {"elements": [
    {"type": "node", "id": 1, "lat": 53.34, "lon": -6.26,
     "tags": {"leisure": "playground", "name": "St Anne's Playground",
              "addr:county": "County Dublin"}},
    {"type": "way", "id": 2, "center": {"lat": 51.89, "lon": -8.47},
     "tags": {"tourism": "museum", "name": "Cork Public Museum",
              "website": "https://www.corkcity.ie/museum"}},
    {"type": "node", "id": 3, "lat": 53.0, "lon": -7.0, "tags": {"leisure": "playground"}},
    {"type": "node", "id": 4, "tags": {"tourism": "zoo", "name": "No Position Zoo"}},
]}


def test_overpass_parsing_keeps_only_named_rows_with_a_position():
    assert [row["name"] for row in opendata_places.parse_overpass(OVERPASS)] \
        == ["St Anne's Playground", "Cork Public Museum"]


def test_overpass_parsing_reads_a_ways_position_from_its_center():
    rows = opendata_places.parse_overpass(OVERPASS)
    assert (rows[1]["lat"], rows[1]["lon"]) == (51.89, -8.47)


def test_overpass_parsing_normalises_the_county_out_of_an_address_tag():
    assert opendata_places.parse_overpass(OVERPASS)[0]["county"] == "Dublin"


def test_overpass_parsing_does_not_repeat_a_tag_key_once_per_filter():
    assert opendata_places.parse_overpass(OVERPASS)[0]["kinds"] == ["leisure=playground"]


def test_overpass_parsing_keeps_the_osm_object_page_as_a_citable_url():
    assert opendata_places.parse_overpass(OVERPASS)[0]["osm_url"] \
        == "https://www.openstreetmap.org/node/1"


def test_the_overpass_query_covers_every_wanted_tag():
    query = opendata_places.overpass_query((51.0, -10.0, 55.0, -6.0))
    assert all(f'"{key}"="{value}"' in query
               for key, value in opendata_places.OSM_FILTERS)


def test_a_row_with_a_website_becomes_a_candidate_on_that_website():
    rows = opendata_places.parse_overpass(OVERPASS)
    made = opendata_places._candidates_from_rows(rows, "osm-test", 10)
    assert made[0]["source_url"] == "https://www.corkcity.ie/museum"


def test_a_row_without_a_website_is_grounded_in_the_dataset_row_itself():
    rows = opendata_places.parse_overpass(OVERPASS)
    made = opendata_places._candidates_from_rows(rows, "osm-test", 10)
    assert "St Anne's Playground is recorded in the osm-test open dataset" in made[1]["text"]


def test_every_open_data_candidate_carries_its_coordinates():
    rows = opendata_places.parse_overpass(OVERPASS)
    made = opendata_places._candidates_from_rows(rows, "osm-test", 10)
    assert all(one["location"]["lat"] is not None for one in made)


CSV_TEXT = ("Name,Latitude,Longitude,County,Website\n"
            "Bishopstown Playground,51.88,-8.53,Cork,\n"
            "Broken Row,,,Cork,\n")

# What Roscommon County Council actually publishes: WGS84 degrees and the
# Irish grid side by side, plus a Street View link in a "street"-ish column.
CSV_MIXED_GRID = ("OBJECTID,Name,Streetview_Link,WGS84Longitude,WGS84Latitude,x,y\n"
                  "1,Ballaghaderreen Playground,https://maps.example/1,"
                  "-8.59124852,53.89927626,561142.09,794593.20\n")

# South Dublin publishes Irish Transverse Mercator only.
CSV_ITM_ONLY = ("X,Y,Name,Layout\n701939,735535,Lucan Demesne,Natural Playspace\n")


def test_place_csv_parsing_keeps_only_rows_with_a_name_and_a_position():
    assert [row["name"] for row in opendata_places.parse_place_csv(CSV_TEXT)] \
        == ["Bishopstown Playground"]


def test_place_csv_parsing_prefers_the_degree_column_over_the_irish_grid():
    row = opendata_places.parse_place_csv(CSV_MIXED_GRID)[0]
    assert (row["lat"], row["lon"]) == (53.89927626, -8.59124852)


def test_a_grid_only_dataset_yields_no_rows_rather_than_a_point_in_the_atlantic():
    assert opendata_places.parse_place_csv(CSV_ITM_ONLY) == []


def test_a_street_view_link_is_not_read_as_an_address():
    assert opendata_places.parse_place_csv(CSV_MIXED_GRID)[0]["address"] == ""


def test_place_csv_parsing_survives_a_byte_order_mark():
    assert len(opendata_places.parse_place_csv("﻿" + CSV_TEXT)) == 1


def test_a_csv_row_with_no_website_is_cited_against_the_dataset_resource():
    rows = opendata_places.parse_place_csv(CSV_TEXT)
    made = opendata_places._candidates_from_rows(rows, "datagov-x", 10,
                                                 fallback_url="https://data.gov.ie/x.csv")
    assert made[0]["source_url"] == "https://data.gov.ie/x.csv#bishopstown-playground"


# ---------------------------------------------------------------------------
# Lane 6 -- Wikidata
# ---------------------------------------------------------------------------

def _binding(qid, name, site, county="", point=""):
    row = {"item": {"value": f"http://www.wikidata.org/entity/{qid}"},
           "itemLabel": {"value": name}, "site": {"value": site}}
    if county:
        row["countyLabel"] = {"value": county}
    if point:
        row["coord"] = {"value": point}
    return row


SPARQL = {"results": {"bindings": [
    _binding("Q1", "Dublin Zoo", "http://www.dublinzoo.ie", "Dublin", "Point(-6.30 53.35)"),
    _binding("Q1", "Dublin Zoo", "http://www.dublinzoo.ie", "Dublin"),
    _binding("Q2", "Q99999", "https://nolabel.ie"),
    _binding("Q3", "Cork Public Museum", ""),
]}}


def test_the_same_venue_arriving_on_several_rows_is_read_once():
    assert [row["name"] for row in wikidata.parse_bindings(SPARQL)] == ["Dublin Zoo"]


def test_a_row_whose_label_never_resolved_is_dropped():
    assert all(not row["name"].startswith("Q9") for row in wikidata.parse_bindings(SPARQL))


def test_a_wikidata_point_is_read_as_latitude_then_longitude():
    assert wikidata.parse_point("Point(-6.30 53.35)") == (53.35, -6.30)


def test_a_missing_point_yields_no_coordinates():
    assert wikidata.parse_point(None) == (None, None)


def test_a_venue_already_on_air_under_the_same_name_is_not_proposed_again():
    row = wikidata.parse_bindings(SPARQL)[0]
    assert wikidata.already_known(row, [{"title": "dublin  zoo!"}]) is True


def test_a_venue_within_a_kilometre_of_one_on_air_is_not_proposed_again():
    row = wikidata.parse_bindings(SPARQL)[0]
    assert wikidata.already_known(
        row, [{"title": "Something Else", "latitude": "53.3505", "longitude": "-6.3005"}]) is True


def test_a_venue_ten_kilometres_away_is_a_new_proposal():
    row = wikidata.parse_bindings(SPARQL)[0]
    assert wikidata.already_known(
        row, [{"title": "Something Else", "latitude": "53.45", "longitude": "-6.30"}]) is False


# ---------------------------------------------------------------------------
# Lane 8 -- the computed holiday facts
# ---------------------------------------------------------------------------

def _archive(temps_by_month, rain=1.0):
    time, temperature, precipitation = [], [], []
    for month, temp in temps_by_month.items():
        for day in (1, 2, 3):
            time.append(f"2024-{month:02d}-{day:02d}")
            temperature.append(temp)
            precipitation.append(rain)
    return {"daily": {"time": time, "temperature_2m_max": temperature,
                      "precipitation_sum": precipitation}}


def test_the_seed_list_lists_a_destination_named_twice_only_once():
    seeds = [{"name": "Paphos", "country": "CY"}, {"name": "paphos!", "country": "CY"},
             {"name": "Crete", "country": "GR"}]
    assert [one["name"] for one in holidays_seed._deduplicate(seeds)] == ["Paphos", "Crete"]


def test_best_months_keeps_the_months_in_the_comfortable_band():
    payload = _archive({1: 9.0, 6: 24.0, 7: 26.0, 8: 34.0})
    assert holidays_seed.months_from_archive(payload) == ["June", "July"]


def test_best_months_is_empty_when_no_month_is_comfortable():
    assert holidays_seed.months_from_archive(_archive({1: 4.0, 7: 41.0})) == []


def test_best_months_is_empty_for_an_archive_with_no_readings():
    assert holidays_seed.months_from_archive({"daily": {}}) == []


def test_seasons_are_derived_from_the_months_not_authored():
    assert holidays_seed.seasons_for(["June", "July", "October"]) == ["summer", "autumn"]


def test_a_two_hour_flight_lands_in_the_two_to_four_band():
    assert holidays_seed.flight_band(2.5) == "2-4h"


def test_a_ninety_minute_flight_lands_in_the_under_two_band():
    assert holidays_seed.flight_band(1.5) == "under-2h"


def test_a_ten_hour_flight_lands_in_the_long_haul_band():
    assert holidays_seed.flight_band(10.0) == "8h-plus"


def test_an_unknown_flight_time_has_no_band():
    assert holidays_seed.flight_band(None) == ""


AIRPORTS = {"DUB": (53.42, -6.27), "FAO": (37.01, -7.97), "NRT": (35.76, 140.39)}


def test_a_destination_on_a_dublin_route_is_a_direct_flight():
    direct, band = holidays_seed.flight_facts(37.09, -8.24, AIRPORTS, {"FAO"})
    assert direct is True


def test_a_destination_with_no_dublin_route_is_not_a_direct_flight():
    direct, _band = holidays_seed.flight_facts(35.68, 139.69, AIRPORTS, {"FAO"})
    assert direct is False


def test_the_flight_time_is_measured_from_dublin_not_guessed():
    _direct, band = holidays_seed.flight_facts(37.09, -8.24, AIRPORTS, {"FAO"})
    assert band == "2-4h"


def test_a_destination_with_no_airport_within_range_has_no_flight_facts():
    assert holidays_seed.flight_facts(0.0, 0.0, AIRPORTS, {"FAO"}) == (None, "")


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------

def test_a_disabled_lane_is_reported_rather_than_silently_skipped(monkeypatch):
    monkeypatch.setattr(discovery.common, "lanes_config", lambda: {"feeds": {"enabled": False}})
    _found, rows, _cursors, _ran, _calls = discovery.run_all(10, {})
    assert [row for row in rows if row["lane"] == "feeds"][0]["errors"] == ["disabled"]


def test_a_weekly_lane_that_ran_yesterday_is_not_due(monkeypatch):
    monkeypatch.setattr(discovery.common, "lanes_config",
                        lambda: {"wikidata": {"enabled": True, "weekly": True}})
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    state = {"lane_last_run": {"wikidata": yesterday}}
    _found, rows, _cursors, ran, _calls = discovery.run_all(10, state)
    assert ran == [] and rows[0]["errors"] == ["not due this week"]


def test_a_weekly_lane_that_ran_a_fortnight_ago_is_due(monkeypatch):
    monkeypatch.setattr(discovery.common, "lanes_config",
                        lambda: {"ticketmaster": {"enabled": True, "weekly": True}})
    old = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
    _found, _rows, _cursors, ran, _calls = discovery.run_all(
        10, {"lane_last_run": {"ticketmaster": old}})
    assert ran == ["ticketmaster"]


def test_a_lane_that_raises_does_not_break_the_cycle(monkeypatch):
    monkeypatch.setattr(discovery.common, "lanes_config", lambda: {"feeds": {"enabled": True}})
    monkeypatch.setattr(feeds, "run", lambda state: 1 / 0)
    _found, rows, _cursors, ran, _calls = discovery.run_all(10, {})
    assert ran == ["feeds"] and "ZeroDivisionError" in rows[0]["errors"][0]


def test_the_budget_caps_how_many_candidates_a_cycle_takes(monkeypatch):
    monkeypatch.setattr(discovery.common, "lanes_config", lambda: {"feeds": {"enabled": True}})
    monkeypatch.setattr(feeds, "run", lambda state: [
        discovery.common.candidate("feeds", "x", f"https://a.ie/{n}") for n in range(50)])
    found, _rows, _cursors, _ran, _calls = discovery.run_all(5, {})
    assert len(found) == 5


def test_every_lane_key_gets_its_own_sources_row(monkeypatch):
    monkeypatch.setattr(discovery.common, "lanes_config", lambda: {"feeds": {"enabled": True}})
    monkeypatch.setattr(feeds, "run", lambda state: [
        discovery.common.candidate("feeds", "a", "https://a.ie/1"),
        discovery.common.candidate("feeds", "b", "https://b.ie/1"),
        discovery.common.candidate("feeds", "b", "https://b.ie/2")])
    _found, rows, _cursors, _ran, _calls = discovery.run_all(10, {})
    assert [(row["key"], row["found"]) for row in rows] == [("a", 1), ("b", 2)]


def test_ticketmaster_without_a_key_skips_instead_of_calling_the_api(monkeypatch):
    from discovery.lanes import ticketmaster
    monkeypatch.setattr(ticketmaster, "_key", lambda: "")
    state = {"config": {}, "budget": 10, "errors": [], "factory_state": {}, "calls": None}
    assert ticketmaster.run(state) == []
    assert "no key" in state["errors"][0][1]


def test_a_ticketmaster_failure_never_writes_the_key_into_the_error(monkeypatch):
    from discovery.lanes import ticketmaster
    monkeypatch.setattr(ticketmaster, "_key", lambda: "s3cr3t-key")
    monkeypatch.setattr(ticketmaster.common, "http_json", lambda *a, **k: _raise_url_error())
    state = {"config": {}, "budget": 10, "errors": [], "factory_state": {}, "calls": None}
    ticketmaster.run(state)
    assert "s3cr3t-key" not in state["errors"][0][1]


def _raise_url_error():
    raise RuntimeError("failed to open https://app.ticketmaster.com/?apikey=s3cr3t-key")


# ---------------------------------------------------------------------------
# The candidate desk
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_desk(monkeypatch, tmp_path):
    monkeypatch.setattr(discovery.common, "CANDIDATES_FILE", tmp_path / "candidates.json")
    return discovery.common


def test_a_new_candidate_lands_on_the_desk_ready_for_review(temp_desk):
    staged = temp_desk.append_candidates(
        [temp_desk.candidate("feeds", "x", "https://a.ie/1")])
    assert [one["status"] for one in staged] == ["needs_review"]


def test_the_same_url_twice_in_one_batch_makes_one_desk_entry(temp_desk):
    temp_desk.append_candidates([temp_desk.candidate("feeds", "x", "https://a.ie/1"),
                                 temp_desk.candidate("feeds", "x", "https://a.ie/1")])
    assert len(temp_desk.load_candidates()) == 1


def test_a_url_coming_back_for_its_recheck_is_researched_again(temp_desk):
    temp_desk.append_candidates([temp_desk.candidate("feeds", "x", "https://a.ie/1")])
    temp_desk.set_candidate_status("https://a.ie/1", "rejected", "not an event")
    staged = temp_desk.append_candidates([temp_desk.candidate("feeds", "x", "https://a.ie/1")])
    assert [one["source_url"] for one in staged] == ["https://a.ie/1"]


def test_a_recheck_clears_the_previous_verdict_off_the_desk_entry(temp_desk):
    temp_desk.append_candidates([temp_desk.candidate("feeds", "x", "https://a.ie/1")])
    temp_desk.set_candidate_status("https://a.ie/1", "needs_input", "no date", "start_date")
    temp_desk.append_candidates([temp_desk.candidate("feeds", "x", "https://a.ie/1")])
    entry = temp_desk.load_candidates()[0]
    assert "reason" not in entry and "missing_field" not in entry


def test_a_recheck_does_not_add_a_second_entry_for_the_same_url(temp_desk):
    temp_desk.append_candidates([temp_desk.candidate("feeds", "x", "https://a.ie/1")])
    temp_desk.append_candidates([temp_desk.candidate("feeds", "x", "https://a.ie/1")])
    assert len(temp_desk.load_candidates()) == 1


# ---------------------------------------------------------------------------
# What a lane may hand promote() that a social candidate never could
# ---------------------------------------------------------------------------

@pytest.fixture
def promote_without_a_model(monkeypatch):
    """The four steps stubbed, so these tests measure what `promote()` does with
    a lane's candidate rather than what a model says about it."""
    monkeypatch.setattr(factory_worker, "classify",
                        lambda text, candidate=None: {"kind": "place",
                                                      "family_relevant": True, "why": ""})
    monkeypatch.setattr(factory_worker, "gather_facts",
                        lambda text: ([{"claim": "it is open", "quote": "open daily"}], ""))
    monkeypatch.setattr(factory_worker, "extract_details",
                        lambda kind, text, facts, hint="": {"opening_hours": "open daily"})
    monkeypatch.setattr(factory_worker, "write_copy", lambda kind, text, facts, name="": {
        "title": "Glendeer Pet Farm", "summary": "A pet farm in Roscommon.",
        "description": ("The farm is open daily and families can meet the animals, walk the "
                        "trails and stop for lunch. " * 12),
        "taxonomy": {"activity_types": ["farm"], "age_bands": ["3-5"],
                     "price_band": "under-10", "setting": "outdoor"}})


def test_a_lanes_coordinates_survive_a_step_that_returns_none(promote_without_a_model):
    candidate = {"source_url": "https://a.ie/farm", "text": "open daily",
                 "location": {"lat": 53.5, "lon": -8.0, "county": "Roscommon",
                              "name": "Glendeer", "country": "IE"}}
    record, _reason, _missing = factory_worker.promote(
        candidate, prefetched=candidate["text"])
    assert (record["location"]["lat"], record["location"]["county"]) == (53.5, "Roscommon")


def test_a_step_that_does_know_the_county_still_wins_over_the_lanes(promote_without_a_model,
                                                                   monkeypatch):
    monkeypatch.setattr(factory_worker, "extract_details",
                        lambda kind, text, facts, hint="": {"county": "Galway"})
    candidate = {"source_url": "https://a.ie/farm", "text": "open daily",
                 "location": {"lat": 53.5, "lon": -8.0, "county": "Roscommon"}}
    record, _reason, _missing = factory_worker.promote(
        candidate, prefetched=candidate["text"])
    assert record["location"]["county"] == "Galway"


def test_a_zero_from_a_lane_is_kept_because_zero_is_a_real_coordinate():
    assert factory_worker._either(0.0, 53.5) == 0.0


def test_an_empty_value_falls_through_to_the_lanes_coordinate():
    assert factory_worker._either(None, 53.5) == 53.5


def test_a_lanes_second_source_reaches_provenance(promote_without_a_model):
    candidate = {"source_url": "https://en.wikivoyage.org/wiki/Algarve", "text": "open daily",
                 "location": {"county": "Cork", "lat": 51.9, "lon": -8.5},
                 "sources": [{"url": "https://en.wikivoyage.org/wiki/Algarve", "fetched_at": "x"},
                             {"url": "http://www.algarvepromotion.pt/", "fetched_at": "x"}]}
    record, _reason, _missing = factory_worker.promote(
        candidate, prefetched=candidate["text"])
    assert [one["url"] for one in record["provenance"]["sources"]] \
        == ["https://en.wikivoyage.org/wiki/Algarve", "http://www.algarvepromotion.pt/"]


def test_a_prefill_on_a_place_overlays_the_extract_step_rather_than_replacing_it(
        promote_without_a_model):
    candidate = {"source_url": "https://a.ie/x", "text": "open daily",
                 "location": {"county": "Cork", "lat": 51.9, "lon": -8.5}}
    record, _reason, _missing = factory_worker.promote(
        candidate, prefetched=candidate["text"], prefill={"seasonal_note": "summer only"})
    assert (record["place"]["opening_hours"], record["place"]["seasonal_note"]) \
        == ("open daily", "summer only")


def test_a_prefill_on_an_event_still_replaces_the_extract_step(monkeypatch):
    monkeypatch.setattr(factory_worker, "classify",
                        lambda text, candidate=None: {"kind": "event", "family_relevant": True})
    monkeypatch.setattr(factory_worker, "gather_facts", lambda text: ([], ""))
    monkeypatch.setattr(factory_worker, "extract_details",
                        lambda *a, **k: pytest.fail("the extract step must be skipped"))
    monkeypatch.setattr(factory_worker, "write_copy",
                        lambda kind, text, facts, name="": {"title": "T", "summary": "S",
                            "description": ("An afternoon of stories and songs for "
                                            "small children at the library. " * 12)})
    factory_worker.promote({"source_url": "https://a.ie/e"}, prefetched="text",
                           prefill={"start_date": "2026-10-26"})


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
