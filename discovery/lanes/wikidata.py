#!/usr/bin/env python3
"""Lane 6 -- Wikidata museums, attractions, zoos and parks (weekly).

Wikidata is the one free source that reliably holds a venue's official website
(P856) and its Instagram handle (P2003) next to its coordinates, which is
exactly what phase 4's links work will need and what a search result never
gives. Rows that are already on air as a place are dropped by a name-fold plus
a 1 km geo match, so a weekly run adds new venues instead of re-proposing the
catalogue.
"""

import re
import urllib.parse

import contract
import factory_worker
from discovery import common

SPARQL_URL = "https://query.wikidata.org/sparql"

# Museum, zoo, tourist attraction, urban park, aquarium, botanical garden,
# castle, national park -- the classes an Irish family day out actually falls
# into, matched through subclass-of so the long tail comes with them.
_CLASSES = ["Q33506", "Q43501", "Q570116", "Q22698", "Q2281788", "Q167346",
            "Q23413", "Q46169"]

QUERY = """
SELECT ?item ?itemLabel ?site ?instagram ?coord ?countyLabel WHERE {
  VALUES ?class { %s }
  ?item wdt:P17 wd:Q27 ; wdt:P31/wdt:P279* ?class ; wdt:P856 ?site .
  OPTIONAL { ?item wdt:P2003 ?instagram . }
  OPTIONAL { ?item wdt:P625 ?coord . }
  OPTIONAL { ?item wdt:P131 ?county . }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en,ga". }
}
LIMIT %d
"""

_POINT_RE = re.compile(r"Point\(([-0-9.]+) ([-0-9.]+)\)")
_MATCH_KM = 1.0


def build_query(limit):
    return QUERY % (" ".join(f"wd:{one}" for one in _CLASSES), int(limit))


def parse_point(value):
    """Wikidata serialises coordinates as `Point(lon lat)`."""
    match = _POINT_RE.search(str(value or ""))
    return (float(match.group(2)), float(match.group(1))) if match else (None, None)


def parse_bindings(payload):
    """SPARQL returns the cross-product of a venue's classes, its administrative
    areas and its social handles, so one museum arrives three or four times --
    the item id is what identifies a venue, not the row."""
    rows, seen = [], set()
    for binding in ((payload or {}).get("results") or {}).get("bindings") or []:
        name = (binding.get("itemLabel") or {}).get("value", "").strip()
        site = (binding.get("site") or {}).get("value", "").strip()
        qid = (binding.get("item") or {}).get("value", "").rsplit("/", 1)[-1]
        if not name or name.startswith("Q") and name[1:].isdigit() or not site:
            continue
        if qid in seen:
            continue
        seen.add(qid)
        lat, lon = parse_point((binding.get("coord") or {}).get("value"))
        rows.append({
            "name": name,
            "site": site,
            "instagram": (binding.get("instagram") or {}).get("value", "").strip(),
            "lat": lat, "lon": lon,
            "county": contract.normalize_county(
                (binding.get("countyLabel") or {}).get("value", "")),
            "qid": qid,
        })
    return rows


def _fold(text):
    return re.sub(r"[^a-z0-9]+", "", str(text or "").lower())


def _on_air_places():
    return [record for record in
            factory_worker.load_json_store(factory_worker.PLACES_FILE, [])]


def already_known(row, places):
    """True when a published place is the same venue: the same folded name, or
    a point within a kilometre of it."""
    folded = _fold(row["name"])
    for place in places:
        view = contract.legacy_view(place)
        if folded and folded == _fold(view.get("title")):
            return True
        if row["lat"] is None:
            continue
        try:
            lat, lon = float(view.get("latitude")), float(view.get("longitude"))
        except (TypeError, ValueError):
            continue
        if common.great_circle_km(row["lat"], row["lon"], lat, lon) <= _MATCH_KM:
            return True
    return False


def _row_text(row):
    parts = [f"{row['name']} is listed in Wikidata as a visitor attraction in Ireland.",
             f"Official website: {row['site']}."]
    if row["county"]:
        parts.append(f"County: {row['county']}.")
    if row["lat"] is not None:
        parts.append(f"Coordinates: {row['lat']}, {row['lon']}.")
    if row["instagram"]:
        parts.append(f"Instagram: @{row['instagram']}.")
    return " ".join(parts)


def run(state):
    limit = int(state["config"].get("limit", 300))
    url = SPARQL_URL + "?" + urllib.parse.urlencode({"query": build_query(limit),
                                                     "format": "json"})
    try:
        payload = common.http_json(url, timeout=180,
                                   headers={"Accept": "application/sparql-results+json"})
    except Exception as error:
        state["errors"].append(("sparql", f"query failed: {error}"))
        return []
    places = _on_air_places()
    out = []
    for row in parse_bindings(payload):
        if already_known(row, places):
            continue
        location = {"name": row["name"], "lat": row["lat"], "lon": row["lon"],
                    "county": row["county"], "country": "IE"}
        out.append(common.candidate(
            "wikidata", "attractions", row["site"], title=row["name"],
            kind_hint="place", county=row["county"], location=location,
            caption=_row_text(row)))
    # Dropped before the cap, not after: otherwise every weekly run re-offers
    # the same first venues and never reaches the rest of the query.
    return common.unresearched(out)[:state["budget"]]
