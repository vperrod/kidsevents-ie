#!/usr/bin/env python3
"""Lane 4 -- the search gateway, county by county and season by season.

The old cycle ran one template ("kids events {city} this weekend") against
five cities. This runs the §11.2 template set against all 32 counties, with
the seasonal templates only in the months they mean anything -- searching for
Santa experiences in May burns a query and returns last year's pages.

Counties rotate on a cursor, `max_queries` per cycle, so an hourly timer
covers all 32 several times over in a week: no county is ever more than a few
days from its last sweep, which is what "every county queried at least weekly"
means in practice.

`_src_gateway` only: no direct scrape of any search engine's HTML.
"""

from datetime import date
import re
import urllib.parse

import contract
import factory_worker
from discovery import common

_JUNK = ("pinterest.", "facebook.com/login", "/search?", "tripadvisor.")
_IRELAND_HINT = re.compile(
    r"ireland|eire|dublin|cork|galway|limerick|waterford|kilkenny|sligo|"
    r"wexford|kerry|donegal|mayo|wicklow|meath|kildare|louth|clare|"
    r"tipperary|westmeath|roscommon|offaly|longford|monaghan|cavan|carlow",
    re.IGNORECASE,
)


def templates_for(month, templates=None):
    """The always-on templates plus the seasonal sets whose months include
    `month`."""
    data = templates or factory_worker.load_json_store(common.TEMPLATES_FILE, {})
    out = list(data.get("always") or [])
    for block in (data.get("seasonal") or {}).values():
        if month in (block.get("months") or []):
            out.extend(block.get("templates") or [])
    return out


def expand(templates, counties, year):
    """Every (county, template) pair rendered into a query string, county-major
    so a truncated run still covers whole counties rather than a slice of each."""
    return [(county, template.format(county=county, year=year))
            for county in counties for template in templates]


def run(state):
    config = state["config"]
    today = date.today()
    counties = list(contract.COUNTIES)
    templates = templates_for(today.month)
    if not templates:
        state["errors"].append(("-", "no search templates for this month"))
        return []

    start = int((state["factory_state"].get("lane_cursor") or {}).get("search", 0))
    queries = expand(templates, counties, today.year)
    max_queries = min(int(config.get("max_queries", 40)), len(queries))
    start %= len(queries)
    picked = [queries[(start + i) % len(queries)] for i in range(max_queries)]
    state["cursor"] = (start + max_queries) % len(queries)

    per_query = int(config.get("results_per_query", 6))
    out = []
    for county, query in picked:
        if len(out) >= state["budget"]:
            break
        try:
            results = factory_worker._src_gateway(query, per_query)
        except Exception as error:
            state["errors"].append((county, f"search failed: {error}"))
            continue
        if not results:
            state["errors"].append((county, f"no results: {query}"))
            continue
        out.extend(common.candidate("search", county, result["url"],
                                    title=result.get("title", ""), county=county)
                   for result in results
                   if not any(junk in result["url"].lower() for junk in _JUNK)
                   and (urllib.parse.urlparse(result["url"]).netloc.lower().endswith(".ie")
                        or _IRELAND_HINT.search(
                            f"{result.get('title', '')} {result['url']}")))
    return out
