#!/usr/bin/env python3
"""Lane 5 -- open data as the Things-to-do seed (weekly).

Open data gives a place its coordinates, its county and often its official
website for free, which is exactly the half of a place record a model must
never guess. What it does not give is anything a parent would read, so the row
never becomes a record on its own: it becomes a candidate carrying its
coordinates in `location` and either the official site as `source_url` (so
`promote()` researches the real page) or, when the dataset has no website, the
row itself as the text everything is grounded in.

Rows with a website are emitted first: those are the ones that can clear the
120-word description the contract asks of a place. A playground with nothing
but a point on a map correctly lands in needs-input.

Fáilte Ireland's Open Data API is the biggest source here (12,000+
attractions) and needs a registered key. Registration with data@smalldays.ie
has not happened yet, so that half logs "no key" and skips.
"""

import csv
import io
import json
import time
import urllib.parse

import contract
from discovery import common

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
CKAN_SEARCH = "https://data.gov.ie/api/3/action/package_search"

# One bbox per province keeps each Overpass query inside its 25 s server-side
# timeout; the island in one query times out.
PROVINCE_BBOX = {
    "Leinster": (52.1, -7.7, 54.1, -5.9),
    "Munster": (51.4, -10.6, 53.2, -6.9),
    "Connacht": (53.0, -10.3, 54.6, -8.0),
    "Ulster": (53.9, -8.9, 55.4, -5.4),
}
OSM_FILTERS = [
    ('leisure', 'playground'), ('leisure', 'water_park'), ('leisure', 'nature_reserve'),
    ('tourism', 'museum'), ('tourism', 'attraction'), ('tourism', 'zoo'),
    ('amenity', 'library'), ('natural', 'beach'),
]
PAGE_CRAWLS = [
    ("coillte", "https://www.coillte.ie/site/recreation-sites/"),
    ("blueflag", "https://beachawards.ie/blue-flag/"),
]


def overpass_query(bbox, filters=None):
    """One Overpass QL query for a province: every wanted tag, nodes and ways."""
    south, west, north, east = bbox
    box = f"({south},{west},{north},{east})"
    parts = "".join(f'node["{k}"="{v}"]{box};way["{k}"="{v}"]{box};'
                    for k, v in (filters or OSM_FILTERS))
    return f"[out:json][timeout:25];({parts});out center tags;"


def parse_overpass(payload):
    """Overpass elements -> `{name, lat, lon, website, kinds}` rows with a name
    and a position. Ways carry their position under `center`."""
    rows = []
    for element in (payload or {}).get("elements") or []:
        tags = element.get("tags") or {}
        name = (tags.get("name") or "").strip()
        center = element.get("center") or {}
        lat = element.get("lat", center.get("lat"))
        lon = element.get("lon", center.get("lon"))
        if not name or lat is None or lon is None:
            continue
        rows.append({
            "name": name,
            "lat": float(lat),
            "lon": float(lon),
            "website": (tags.get("website") or tags.get("contact:website") or "").strip(),
            "kinds": sorted({f"{k}={tags[k]}" for k, _v in OSM_FILTERS if k in tags}),
            "address": " ".join(part for part in (tags.get("addr:street", ""),
                                                  tags.get("addr:city", "")) if part),
            # OSM spells the county three ways and usually not at all; the
            # extract step reads it off the venue's own page when it is missing.
            "county": _first_county(tags, ("addr:county", "is_in:county", "addr:state")),
            # The OSM object's own page is a real, citable URL for a row that
            # has no website of its own -- "osm:node/123" is not.
            "osm_url": f"https://www.openstreetmap.org/{element.get('type', 'node')}/{element.get('id', '')}",
        })
    return rows


def _first_county(tags, keys):
    for key in keys:
        county = contract.normalize_county(tags.get(key, ""))
        if county:
            return county
    return ""


def _row_text(row, dataset):
    """The dataset row written out as the text a record can be grounded in when
    there is no website to fetch."""
    lines = [f"{row['name']} is recorded in the {dataset} open dataset as "
             f"{', '.join(row.get('kinds') or ['a place of interest'])}."]
    if row.get("address"):
        lines.append(f"Address: {row['address']}.")
    if row.get("county"):
        lines.append(f"County: {row['county']}.")
    lines.append(f"Coordinates: {row['lat']}, {row['lon']}.")
    return " ".join(lines)


def _candidates_from_rows(rows, dataset, budget, fallback_url=""):
    """Website-bearing rows first: those are the ones a real description can be
    written from. A row with no website of its own is still citable -- as its
    OpenStreetMap object page, or as the dataset resource it came out of.

    Every row becomes a candidate and the already-researched ones are dropped
    before the cap, so each weekly run works further down a dataset instead of
    re-offering its first sixty rows.
    """
    ordered = sorted(rows, key=lambda row: not row.get("website"))
    out = []
    for row in ordered:
        website = row.get("website") or ""
        row_anchor = (f"{fallback_url}#{contract.slugify(row['name'])}"
                      if fallback_url else "")
        url = website or row.get("osm_url") or row_anchor
        if not url:
            continue
        location = {"name": row["name"], "lat": row["lat"], "lon": row["lon"],
                    "county": row.get("county", ""), "address": row.get("address", ""),
                    "country": "IE"}
        out.append(common.candidate(
            "opendata_places", dataset, url,
            title=row["name"], kind_hint="place", county=row.get("county", ""),
            location=location,
            **({"caption": _row_text(row, dataset)} if website
               else {"text": _row_text(row, dataset)})))
    return common.unresearched(out)[:budget]


def _osm(state, budget):
    out = []
    sleep_secs = int(state["config"].get("overpass_sleep_secs", 10))
    per_province = max(1, budget // len(PROVINCE_BBOX))
    for index, (province, bbox) in enumerate(PROVINCE_BBOX.items()):
        if len(out) >= budget:
            break
        if index:
            time.sleep(sleep_secs)          # Overpass asks for a gap between queries
        try:
            payload = json.loads(common.http_text(
                OVERPASS_URL, timeout=180,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data=urllib.parse.urlencode({"data": overpass_query(bbox)}).encode()))
        except Exception as error:
            state["errors"].append((f"osm-{province.lower()}", f"overpass failed: {error}"))
            continue
        rows = parse_overpass(payload)
        for row in rows:
            row.setdefault("county", "")
        out.extend(_candidates_from_rows(rows, f"osm-{province.lower()}", per_province))
    return out


def ckan_csv_resources(query, rows=5):
    """data.gov.ie is a CKAN instance: ask it where a dataset's CSV lives
    rather than hardcoding a resource URL that moves."""
    url = f"{CKAN_SEARCH}?" + urllib.parse.urlencode({"q": query, "rows": rows})
    payload = common.http_json(url, timeout=60)
    found = []
    for package in (payload.get("result") or {}).get("results") or []:
        for resource in package.get("resources") or []:
            if (resource.get("format") or "").upper() == "CSV" and resource.get("url"):
                found.append({"dataset": package.get("name", query), "url": resource["url"]})
                break
    return found


def parse_place_csv(text, limit=200):
    """A CSV of places -> the same row shape Overpass produces. Column names
    vary per publisher, so the header is matched loosely.

    Irish council datasets very often publish Irish Transverse Mercator
    eastings/northings under headings like `X`/`Y`, which look like numbers and
    are not degrees. A row whose coordinates are outside the WGS84 range is
    dropped rather than published at a point in the Atlantic -- reprojecting
    would mean a new dependency for a minority of the rows.
    """
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    rows = []
    for raw in reader:
        lowered = {(k or "").strip().lstrip("﻿").lower(): (v or "").strip()
                   for k, v in raw.items()}
        name = _first(lowered, ("name", "playground", "title", "site_name", "location_name"))
        lat = _first_coord(lowered, ("latitude", "lat", "gps_lat", "y"), 90)
        lon = _first_coord(lowered, ("longitude", "long", "lon", "gps_long", "x"), 180)
        if not name or lat is None or lon is None:
            continue
        rows.append({
            "name": name, "lat": lat, "lon": lon,
            "website": _first(lowered, ("website", "url", "web")),
            # "street" also matches a `Streetview_Link` column, and a Google
            # Maps URL is not an address.
            "address": _no_url(_first(lowered, ("address", "address1", "street"))),
            "county": contract.normalize_county(_first(lowered, ("county", "local_authority"))),
            "kinds": [],
        })
        if len(rows) >= limit:
            break
    return rows


def _first(row, names):
    """An exact header match, then a substring one -- but only for headings long
    enough to mean something: "lon" is a substring of "Location" and "x" of
    nearly everything."""
    for name in names:
        if row.get(name):
            return row[name]
    for name in (one for one in names if len(one) >= 4):
        for key, value in row.items():
            if name in key and value:
                return value
    return ""


def _no_url(value):
    return "" if str(value).startswith("http") else value


def _first_coord(row, names, limit):
    """The first column matching one of `names` whose value is a real degree.

    Order alone is not enough: a Roscommon playground CSV carries both
    `WGS84Latitude` and a bare `y` holding the Irish grid northing, and taking
    the first header that matched would put the playground in the Atlantic.
    """
    for key in _matching_keys(row, names):
        try:
            value = float(row[key])
        except (TypeError, ValueError):
            continue
        if -limit <= value <= limit:
            return value
    return None


def _matching_keys(row, names):
    for name in names:
        if name in row:
            yield name
    for name in (one for one in names if len(one) >= 4):
        for key in row:
            if name in key and key not in names:
                yield key


def _datagov(state, budget):
    out = []
    per_dataset = int(state["config"].get("rows_per_dataset", 60))
    for query in state["config"].get("ckan_queries") or []:
        if len(out) >= budget:
            break
        try:
            resources = ckan_csv_resources(query)
        except Exception as error:
            state["errors"].append((f"datagov-{query}", f"CKAN search failed: {error}"))
            continue
        if not resources:
            state["errors"].append((f"datagov-{query}", "no CSV resource in the search results"))
        for resource in resources[:2]:
            try:
                rows = parse_place_csv(common.http_text(resource["url"], timeout=90), per_dataset)
            except Exception as error:
                state["errors"].append((f"datagov-{resource['dataset']}", f"CSV unreadable: {error}"))
                continue
            if not rows:
                # Almost always an Irish-grid-only publisher -- visible in the
                # Sources area rather than looking like an empty dataset.
                state["errors"].append((f"datagov-{resource['dataset']}",
                                        "no row carried a usable name and WGS84 position"))
            out.extend(_candidates_from_rows(rows, f"datagov-{resource['dataset']}",
                                             min(per_dataset, budget - len(out)),
                                             fallback_url=resource["url"]))
    return out


def _page_lane(budget):
    """Coillte's recreation sites and the Blue Flag list are ordinary pages, so
    they go through the same listing treatment: the page is the candidate and
    `promote()` decides what it is."""
    return [common.candidate("opendata_places", key, url, kind_hint="place")
            for key, url in PAGE_CRAWLS][:budget]


def run(state):
    budget = state["budget"]
    if not (state["config"].get("failte_api_key") or "").strip():
        state["errors"].append(("failte", "no key -- registration with data@smalldays.ie pending"))
    out = _osm(state, max(1, budget // 2))
    out.extend(_datagov(state, max(0, budget - len(out))))
    out.extend(_page_lane(max(0, budget - len(out))))
    return out
