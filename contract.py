#!/usr/bin/env python3
"""One record contract for every catalogue Small Days publishes.

Events, Things to do (places) and Holidays used to be three ad-hoc dicts with
overlapping-but-different keys, so nothing could be validated, deduplicated or
rendered the same way twice. This module is the single shape they all take
(see `tasks/specs/phase1-contract-taxonomy.md` §2) plus the derivations that
must never be guessed by a model: the region of a county, the Google Maps
link, the slug, and `confidence` (which is a measured completeness score, not
a model's self-report).

`legacy_view()` flattens a contract record back onto the keys the current
`web/index.html` reads, so the public endpoints keep their response shape
while the stores move to the contract underneath.
"""

import json
import re
import urllib.parse
from pathlib import Path

BASE = Path(__file__).resolve().parent
FACETS_FILE = BASE / "catalog" / "facets.json"

KINDS = ("event", "place", "holiday")

with open(FACETS_FILE, encoding="utf-8") as _fh:
    FACETS = json.load(_fh)

COUNTIES = FACETS["counties"]
COUNTY_ALIASES = FACETS["county_aliases"]
# Republic of Ireland county codes as used by allevents.in's schema.org
# addressRegion field — raw codes like "DN" were leaking straight to parents.
COUNTY_CODES = {v["code"]: name for name, v in COUNTIES.items() if v["code"]}

MAX_TITLE = 80
MAX_SUMMARY = 160

# Words a description needs before it is worth a parent's time, per kind.
MIN_DESCRIPTION_WORDS = {"event": 60, "place": 120, "holiday": 120}


def region_for(county):
    """Province of an Irish county, or "" for anything not in the 32."""
    entry = COUNTIES.get(normalize_county(county))
    return entry["region"] if entry else ""


def normalize_county(county):
    """Fold a county code, an alias or a "County X" spelling onto one of the 32
    canonical names; return "" when it is not one of them."""
    raw = str(county or "").strip()
    if not raw:
        return ""
    if raw.upper() in COUNTY_CODES:
        return COUNTY_CODES[raw.upper()]
    folded = re.sub(r"^(county|co\.?)\s+", "", raw, flags=re.I).strip()
    if folded in COUNTIES:
        return folded
    lowered = folded.lower()
    if lowered in COUNTY_ALIASES:
        return COUNTY_ALIASES[lowered]
    for name in COUNTIES:
        if name.lower() == lowered:
            return name
    return ""


def is_northern_ireland(county):
    entry = COUNTIES.get(normalize_county(county))
    return bool(entry and entry["ni"])


def slugify(title, county=""):
    """URL slug for a record: folded title, plus the county when there is one
    (two "Storytime" listings in different counties are different items)."""
    parts = [title or "", normalize_county(county)]
    slug = re.sub(r"[^a-z0-9]+", "-", " ".join(p for p in parts if p).lower()).strip("-")
    return slug[:90].strip("-")


def maps_url(location):
    """A plain Google Maps search link — never a Places API call, which needs a
    billed key. Coordinates when we have both, otherwise the written address."""
    lat, lon = location.get("lat"), location.get("lon")
    if lat not in (None, "") and lon not in (None, ""):
        query = f"{lat},{lon}"
    else:
        parts = [location.get("name"), location.get("address"), location.get("county")]
        parts = [str(p).strip() for p in parts if str(p or "").strip()]
        if not parts:
            return ""
        if location.get("country", "IE") == "IE":
            parts.append("Ireland")
        query = ", ".join(parts)
    return "https://www.google.com/maps/search/?api=1&query=" + urllib.parse.quote(query)


def EMPTY_RECORD(kind):
    """A complete, empty record of `kind` — every key the contract defines, so
    no producer has to remember the shape and no consumer has to guard."""
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}")
    record = {
        "schema_version": 1,
        "id": "",
        "kind": kind,
        "title": "",
        "slug": "",
        "summary": "",
        "description": "",
        "family_relevant": True,
        "location": {
            "name": "", "address": "", "city": "", "county": "", "region": "",
            "country": "IE", "lat": None, "lon": None, "ireland": True,
        },
        "links": {
            "source_url": "", "official_url": "", "maps_url": "",
            "instagram_url": "", "tiktok_url": "", "booking_url": "",
        },
        "media": {"hero": None, "embeds": []},
        "taxonomy": {
            "age_bands": [], "price_band": "", "price_detail": "", "setting": "",
            "activity_types": [], "rainy_ok": None, "accessibility": [],
        },
        "provenance": {
            "sources": [], "facts": [], "confidence": 0.0,
            "last_checked": "", "produced_by": [],
        },
        "status": "needs-input",
        "reason": "",
        "hint": "",
    }
    if kind == "event":
        record["event"] = {
            "start_date": "", "end_date": "", "times": [], "recurrence": "",
            "organizer": "", "booking_required": "", "date_evidence": "",
            "cancelled": False,
        }
    elif kind == "place":
        record["place"] = {"opening_hours": "", "duration_hint": "", "seasonal_note": ""}
    else:
        record["holiday"] = {
            "destination_type": "", "holiday_types": [], "best_seasons": [],
            "best_months": [], "school_breaks": [], "flight_time_from_dublin": "",
            "direct_flight": None, "budget_band": "", "with_baby_toddler": None,
            "includes": [],
        }
    return record


def derive(record):
    """Fill in every value the contract says is derived, never authored:
    region from county, ireland from country, maps_url from the location,
    slug/id from the title, confidence from completeness. Mutates and returns
    the record so producers can build one in any order and finish here."""
    location = record.setdefault("location", {})
    location["county"] = normalize_county(location.get("county"))
    location["region"] = region_for(location.get("county"))
    location["country"] = (location.get("country") or "").strip() or "IE"
    location["ireland"] = location["country"] == "IE"
    record.setdefault("links", {})["maps_url"] = maps_url(location)
    record["slug"] = record.get("slug") or slugify(record.get("title", ""), location.get("county"))
    record["id"] = record.get("id") or f"{record.get('kind', '')}-{record['slug']}"
    record.setdefault("provenance", {})["confidence"] = completeness(record)
    return record


# ---------------------------------------------------------------------------
# Validation (shape only — the on-air policy lives in gate.qa)
# ---------------------------------------------------------------------------

_TOP_LEVEL = {
    "schema_version": int, "id": str, "kind": str, "title": str, "slug": str,
    "summary": str, "description": str, "family_relevant": bool,
    "location": dict, "links": dict, "media": dict, "taxonomy": dict,
    "provenance": dict, "status": str, "reason": str, "hint": str,
}
_STATUSES = ("on-air", "needs-input", "rejected")


def validate(record):
    """Structural violations of the contract, as a list of sentences. Empty
    list means the shape is right — it says nothing about whether the record
    is good enough to publish, which is `gate.qa`'s job."""
    problems = []
    if not isinstance(record, dict):
        return ["record is not an object"]
    kind = record.get("kind")
    if kind not in KINDS:
        return [f"kind is {kind!r}, not one of {list(KINDS)}"]
    if record.get("schema_version") != 1:
        problems.append(f"schema_version is {record.get('schema_version')!r}, not 1")
    for key, expected in _TOP_LEVEL.items():
        if key not in record:
            problems.append(f"missing {key}")
        elif not isinstance(record[key], expected):
            problems.append(f"{key} is {type(record[key]).__name__}, not {expected.__name__}")
    if record.get("status") not in _STATUSES:
        problems.append(f"status is {record.get('status')!r}, not one of {list(_STATUSES)}")
    if len(record.get("title") or "") > MAX_TITLE:
        problems.append(f"title is {len(record['title'])} characters (max {MAX_TITLE})")
    if len(record.get("summary") or "") > MAX_SUMMARY:
        problems.append(f"summary is {len(record['summary'])} characters (max {MAX_SUMMARY})")
    if kind not in record:
        problems.append(f"missing the {kind} block")
    elif not isinstance(record[kind], dict):
        problems.append(f"the {kind} block is not an object")
    for other in KINDS:
        if other != kind and other in record:
            problems.append(f"carries a {other} block but kind is {kind}")
    blank = [f for f in _empty_record_fields(kind) if f not in record.get(kind, {})]
    if blank:
        problems.append(f"the {kind} block is missing {', '.join(sorted(blank))}")
    return problems


def _empty_record_fields(kind):
    return set(EMPTY_RECORD(kind)[kind])


# ---------------------------------------------------------------------------
# Completeness (the number published as `confidence`)
# ---------------------------------------------------------------------------

def completeness(record):
    """Fraction 0-1 of the fields a parent actually needs that are filled in.

    This replaces the model's own confidence self-report, which was a constant
    ("high" for every web-extracted event) and told nobody anything.
    """
    location = record.get("location") or {}
    taxonomy = record.get("taxonomy") or {}
    provenance = record.get("provenance") or {}
    kind = record.get("kind")
    checks = [
        bool(record.get("title")),
        bool(record.get("summary")),
        len((record.get("description") or "").split()) >= MIN_DESCRIPTION_WORDS.get(kind, 60),
        bool(location.get("name")),
        bool(location.get("county") or location.get("country") != "IE"),
        location.get("lat") not in (None, "") and location.get("lon") not in (None, ""),
        bool(taxonomy.get("price_band")),
        bool(taxonomy.get("age_bands")),
        bool(taxonomy.get("activity_types") or (record.get("holiday") or {}).get("holiday_types")),
        bool((record.get("links") or {}).get("source_url")),
        bool(provenance.get("facts")),
    ]
    if kind == "event":
        event = record.get("event") or {}
        checks += [bool(event.get("start_date")), bool(event.get("date_evidence"))]
    elif kind == "place":
        place = record.get("place") or {}
        checks += [bool(place.get("opening_hours")), bool(taxonomy.get("setting"))]
    else:
        holiday = record.get("holiday") or {}
        checks += [bool(holiday.get("best_seasons")), bool(holiday.get("budget_band"))]
    return round(sum(bool(c) for c in checks) / len(checks), 2)


# ---------------------------------------------------------------------------
# Legacy view (what the current frontend reads)
# ---------------------------------------------------------------------------

_PRICE_LABELS = {
    "free": "Free", "under-10": "Under €10", "10-25": "€10–25",
    "25-plus": "€25+", "unknown": "",
}
_AGE_LABELS = {
    "0-2": "0–2", "3-5": "3–5", "6-9": "6–9", "10-12": "10–12", "13+": "13+",
}


def legacy_view(record):
    """Flatten a contract record onto the flat keys `web/index.html` reads.

    A record that is not in the contract yet (schema_version missing) is
    already in this shape and is passed through, so the public endpoints keep
    working during the migration.
    """
    if record.get("schema_version") != 1:
        return record
    location = record.get("location") or {}
    links = record.get("links") or {}
    taxonomy = record.get("taxonomy") or {}
    event = record.get("event") or {}
    provenance = record.get("provenance") or {}
    source_url = links.get("source_url", "")
    price = taxonomy.get("price_detail") or _PRICE_LABELS.get(taxonomy.get("price_band", ""), "")
    return {
        "title": record.get("title", ""),
        "description": record.get("description", ""),
        "start_date": event.get("start_date", ""),
        "end_date": event.get("end_date") or event.get("start_date", ""),
        "venue_name": location.get("name", ""),
        "venue_address": location.get("address", ""),
        "city": location.get("city", ""),
        "county": location.get("county", ""),
        "country": location.get("country", ""),
        "latitude": "" if location.get("lat") in (None, "") else str(location["lat"]),
        "longitude": "" if location.get("lon") in (None, "") else str(location["lon"]),
        "url": links.get("official_url") or source_url,
        "source": _source_name(source_url),
        "cost": price,
        "age_group": ", ".join(_AGE_LABELS.get(b, b) for b in taxonomy.get("age_bands", [])),
        "category": (taxonomy.get("activity_types") or [""])[0],
        "confidence": provenance.get("confidence", 0.0),
        "region": location.get("region", ""),
        "location": location.get("name") or location.get("city") or location.get("county", ""),
        "price_range": price or "check",
        "source_url": source_url,
        "source_name": _source_name(source_url),
        "booking_url": links.get("booking_url", ""),
    }


def _source_name(url):
    """The host a record came from, without the www. — index.html shows this
    as "Captured from X"."""
    host = urllib.parse.urlparse(url or "").netloc
    return host[4:] if host.startswith("www.") else host
