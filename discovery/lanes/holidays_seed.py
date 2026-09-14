#!/usr/bin/env python3
"""Lane 8 -- family holiday destinations (weekly).

The holidays catalogue has no crawlable listing to harvest: nobody publishes
"family destinations for Irish parents" as a feed. So the seed list is the one
thing here a model is allowed to produce -- one cached call for `seed_size`
names -- and every name is then researched like any other candidate, from two
independent sources:

  1. its Wikivoyage article (CC BY-SA, written for travellers, has a family
     section more often than not);
  2. the official tourism site Wikidata records as P856 for the same place.

Two sources is not decoration: `gate._qa_holiday` refuses a holiday whose
provenance has fewer than two distinct domains, so a destination that only
resolves one of the two correctly lands in needs-input.

The numbers a model must never guess are computed, not written: `best_months`
from Open-Meteo's daily archive (three years of real maxima and rainfall),
`direct_flight` and `flight_time_from_dublin` from OpenFlights' route table
measured great-circle from Dublin at 800 km/h.

`batch` destinations are researched per weekly run, cursor-rotated, so the
list fills in over a couple of months rather than in one sitting that would eat
a night of model budget. `seed_size` is 50 because a free lane asked for a
hundred names in one reply returned nothing at all (2026-09-13); fifty answers.
"""

import csv
import re
import urllib.parse
from datetime import date
from pathlib import Path

import factory_worker
from discovery import common

SEEDS_FILE = Path(__file__).resolve().parent.parent.parent / "catalog" / "holiday_seeds.json"
WIKIVOYAGE_API = "https://en.wikivoyage.org/w/api.php"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
ARCHIVE_API = "https://archive-api.open-meteo.com/v1/archive"
OPENFLIGHTS_ROUTES = "https://raw.githubusercontent.com/jpatokal/openflights/master/data/routes.dat"
OPENFLIGHTS_AIRPORTS = "https://raw.githubusercontent.com/jpatokal/openflights/master/data/airports.dat"

IRISH_AIRPORTS = ("DUB", "ORK", "SNN")
CRUISE_SPEED_KMH = 800.0
_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")
_SEASON_OF_MONTH = {12: "winter", 1: "winter", 2: "winter", 3: "spring", 4: "spring",
                    5: "spring", 6: "summer", 7: "summer", 8: "summer",
                    9: "autumn", 10: "autumn", 11: "autumn"}


# ---------------------------------------------------------------------------
# The seed list (one model call, cached)
# ---------------------------------------------------------------------------

def seed_list(size=100):
    """The cached seed list, generated once. Returns [] when no lane answered --
    the next weekly run tries again rather than caching an empty list."""
    cached = factory_worker.load_json_store(SEEDS_FILE, {})
    if cached.get("destinations"):
        return _deduplicate(cached["destinations"])
    prompt = (
        "You advise Irish parents on family holidays abroad.\n\n"
        f"List {size} destinations Irish families actually travel to with children "
        "-- cities, coastal resorts, regions, islands and national parks, spread "
        "across Europe and the long-haul destinations that are realistic from "
        "Dublin.\n\n"
        'Reply ONLY a JSON object:\n'
        '{"destinations": [{"name": the place as a traveller would name it, '
        '"country": its ISO 3166-1 alpha-2 code}]}'
    )
    answer = factory_worker.extract_obj(
        factory_worker.hermes(prompt, kind="holiday-seed")) or {}
    destinations = [{"name": str(one.get("name") or "").strip(),
                     "country": str(one.get("country") or "").strip().upper()[:2]}
                    for one in (answer.get("destinations") or [])[:size]
                    if isinstance(one, dict) and one.get("name")]
    destinations = _deduplicate(destinations)
    if destinations:
        factory_worker.write_json_atomic(
            SEEDS_FILE, {"generated_at": common.now_iso(), "destinations": destinations})
    return destinations


def _deduplicate(destinations):
    """One entry per name. A free lane asked for fifty destinations happily
    lists Paphos twice, and each duplicate would cost a full research pass
    before the gate's duplicate-title fold rejected the second record."""
    seen, out = set(), []
    for one in destinations:
        folded = re.sub(r"[^a-z0-9]+", "", str(one.get("name", "")).lower())
        if not folded or folded in seen:
            continue
        seen.add(folded)
        out.append(one)
    return out


# ---------------------------------------------------------------------------
# Source 1 -- Wikivoyage
# ---------------------------------------------------------------------------

def wikivoyage_extract(name, chars=4000):
    """`(title, text, url)` for a destination's Wikivoyage article, or ("","","")."""
    payload = common.http_json(
        f"{WIKIVOYAGE_API}?action=query&prop=extracts&explaintext=1&redirects=1"
        f"&format=json&exchars={chars}&titles={urllib.parse.quote(name)}",
        timeout=60)
    for page in ((payload.get("query") or {}).get("pages") or {}).values():
        if "missing" in page or not page.get("extract"):
            continue
        title = page.get("title", name)
        return (title, page["extract"],
                "https://en.wikivoyage.org/wiki/" + urllib.parse.quote(title.replace(" ", "_")))
    return "", "", ""


# ---------------------------------------------------------------------------
# Source 2 -- the official tourism site, via Wikidata P856
# ---------------------------------------------------------------------------

def wikidata_official_site(name):
    """`(url, qid, lat, lon)` for the destination's Wikidata item."""
    search = common.http_json(
        f"{WIKIDATA_API}?action=wbsearchentities&format=json&language=en&limit=1"
        f"&search={urllib.parse.quote(name)}", timeout=60)
    hits = search.get("search") or []
    if not hits:
        return "", "", None, None
    qid = hits[0]["id"]
    entity = common.http_json(
        f"{WIKIDATA_API}?action=wbgetentities&format=json&props=claims&ids={qid}", timeout=60)
    claims = ((entity.get("entities") or {}).get(qid) or {}).get("claims") or {}
    site = _claim_value(claims, "P856")
    coord = _claim_value(claims, "P625")
    coord = coord if isinstance(coord, dict) else {}
    return (site if isinstance(site, str) else "", qid,
            coord.get("latitude"), coord.get("longitude"))


def _claim_value(claims, prop):
    """The first non-empty value of a Wikidata property, whatever its datatype."""
    for claim in claims.get(prop) or []:
        value = ((claim.get("mainsnak") or {}).get("datavalue") or {}).get("value")
        if value is not None:
            return value
    return None


# ---------------------------------------------------------------------------
# The computed facts
# ---------------------------------------------------------------------------

def best_months(lat, lon, today=None):
    """Months whose three-year mean daily maximum is 18-30 C and whose mean
    rainfall is in the drier half of that destination's year. A family beach
    holiday in a 34 C August is not a recommendation."""
    today = today or date.today()
    end = date(today.year - 1, 12, 31)
    start = date(end.year - 2, 1, 1)
    payload = common.http_json(
        f"{ARCHIVE_API}?latitude={lat}&longitude={lon}"
        f"&start_date={start.isoformat()}&end_date={end.isoformat()}"
        "&daily=temperature_2m_max,precipitation_sum&timezone=UTC", timeout=120)
    return months_from_archive(payload)


def months_from_archive(payload):
    daily = (payload or {}).get("daily") or {}
    temps, rains = {}, {}
    for stamp, temp, rain in zip(daily.get("time") or [],
                                 daily.get("temperature_2m_max") or [],
                                 daily.get("precipitation_sum") or []):
        month = int(str(stamp)[5:7])
        if temp is not None:
            temps.setdefault(month, []).append(temp)
        if rain is not None:
            rains.setdefault(month, []).append(rain)
    if not temps:
        return []
    mean_temp = {m: sum(v) / len(v) for m, v in temps.items()}
    mean_rain = {m: sum(v) / len(v) for m, v in rains.items() if v}
    median_rain = sorted(mean_rain.values())[len(mean_rain) // 2] if mean_rain else None
    picked = [m for m in sorted(mean_temp)
              if 18 <= mean_temp[m] <= 30
              and (median_rain is None or mean_rain.get(m, 0) <= median_rain * 1.1)]
    return [_MONTHS[m - 1] for m in picked]


def seasons_for(month_names):
    seasons = {_SEASON_OF_MONTH[_MONTHS.index(name) + 1]
               for name in month_names if name in _MONTHS}
    return [season for season in ("spring", "summer", "autumn", "winter") if season in seasons]


def airport_table():
    """`{iata: (lat, lon)}` from OpenFlights, cached on disk."""
    table = {}
    for line in common.cached_text("airports.dat", OPENFLIGHTS_AIRPORTS).splitlines():
        fields = next(iter(csv.reader([line])), [])
        if len(fields) > 7 and len(fields[4]) == 3 and fields[4] != "\\N":
            try:
                table[fields[4]] = (float(fields[6]), float(fields[7]))
            except ValueError:
                continue
    return table


def routes_from_ireland():
    """`{destination IATA}` reachable non-stop from Dublin, Cork or Shannon."""
    destinations = set()
    for line in common.cached_text("routes.dat", OPENFLIGHTS_ROUTES).splitlines():
        fields = line.split(",")
        if len(fields) > 4 and fields[2] in IRISH_AIRPORTS and len(fields[4]) == 3:
            destinations.add(fields[4])
    return destinations


def flight_band(hours):
    if hours is None:
        return ""
    if hours < 2:
        return "under-2h"
    if hours < 4:
        return "2-4h"
    if hours < 8:
        return "4-8h"
    return "8h-plus"


def flight_facts(lat, lon, airports=None, direct=None):
    """`(direct_flight, flight_time_from_dublin)` -- the nearest airport with a
    non-stop route from Ireland, measured great-circle from Dublin."""
    if lat is None or lon is None:
        return None, ""
    airports = airports if airports is not None else airport_table()
    direct = direct if direct is not None else routes_from_ireland()
    dublin = airports.get("DUB")
    if not dublin:
        return None, ""
    nearest, nearest_km = None, None
    for code, (a_lat, a_lon) in airports.items():
        distance = common.great_circle_km(lat, lon, a_lat, a_lon)
        if distance > 150:                 # not this destination's airport
            continue
        if nearest_km is None or distance < nearest_km:
            nearest, nearest_km = code, distance
    if nearest is None:
        return None, ""
    hours = common.great_circle_km(dublin[0], dublin[1], *airports[nearest]) / CRUISE_SPEED_KMH
    return nearest in direct, flight_band(hours)


# ---------------------------------------------------------------------------
# The lane
# ---------------------------------------------------------------------------

def run(state):
    config = state["config"]
    destinations = seed_list(int(config.get("seed_size", 100)))
    if not destinations:
        state["errors"].append(("seed", "no model lane answered the seed-list call"))
        return []
    batch = min(int(config.get("batch", 10)), len(destinations), state["budget"])
    start = int((state["factory_state"].get("lane_cursor") or {}).get("holidays_seed", 0))
    start %= len(destinations)
    picked = [destinations[(start + i) % len(destinations)] for i in range(batch)]
    state["cursor"] = (start + batch) % len(destinations)

    airports, direct = None, None
    out = []
    for destination in picked:
        name = destination["name"]
        try:
            title, text, voyage_url = wikivoyage_extract(name)
        except Exception as error:
            state["errors"].append((name, f"Wikivoyage failed: {error}"))
            continue
        if not text:
            state["errors"].append((name, "no Wikivoyage article"))
            continue
        try:
            site, _qid, lat, lon = wikidata_official_site(name)
        except Exception as error:
            state["errors"].append((name, f"Wikidata failed: {error}"))
            site, lat, lon = "", None, None
        if not site:
            state["errors"].append((name, "no official tourism site on Wikidata (P856)"))

        months = []
        if lat is not None:
            try:
                months = best_months(lat, lon)
            except Exception as error:
                state["errors"].append((name, f"climate normals failed: {error}"))
        try:
            if airports is None:
                airports, direct = airport_table(), routes_from_ireland()
            has_direct, band = flight_facts(lat, lon, airports, direct)
        except Exception as error:
            state["errors"].append((name, f"flight table failed: {error}"))
            has_direct, band = None, ""

        official_text = ""
        if site:
            try:
                official_text = factory_worker._page_text(site)[:3000]
            except Exception as error:
                state["errors"].append((name, f"official site unreadable: {error}"))

        out.append(common.candidate(
            "holidays_seed", destination.get("country") or "seed", voyage_url,
            title=title or name, kind_hint="holiday",
            text="\n\n".join(part for part in (text, official_text) if part),
            sources=[{"url": voyage_url, "fetched_at": common.now_iso()}]
                    + ([{"url": site, "fetched_at": common.now_iso()}] if official_text else []),
            location={"name": title or name, "country": destination.get("country") or "",
                      "lat": lat, "lon": lon},
            prefill={"country": destination.get("country") or "",
                     "best_months": months, "best_seasons": seasons_for(months),
                     "direct_flight": has_direct, "flight_time_from_dublin": band}))
    return out
