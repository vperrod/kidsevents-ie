#!/usr/bin/env python3
"""Lane 7 -- Ticketmaster Discovery API, family classification, Ireland.

Keyless by design until the key exists: the Discovery API answers 401 without
one and there is nothing to be gained from calling it. The key lives in the
vault item "developer-account.ticketmaster.com" and belongs in `.env` as
`TICKETMASTER_API_KEY`.

`.env` is loaded by `kidsevents-factory.service` through `EnvironmentFile=`,
so anything written there is in the factory's process environment: fine for an
API key, never for anything that must not be in a process listing, and never
logged -- every message below names the lane, never the key.

The free tier is 5,000 calls a day. `daily_call_cap` in `catalog/lanes.json`
is the lane's own much smaller ceiling and the count is written to
`factory_state.json["lane_calls"]` so the quota is visible rather than
assumed.
"""

import urllib.parse
from datetime import date, datetime, timedelta, timezone

import factory_worker
from discovery import common

API_URL = "https://app.ticketmaster.com/discovery/v2/events.json"


def _key():
    return (factory_worker.ENV.get("TICKETMASTER_API_KEY") or "").strip()


def calls_today(factory_state):
    block = (factory_state.get("lane_calls") or {}).get("ticketmaster") or {}
    return block.get("count", 0) if block.get("day") == date.today().isoformat() else 0


def parse_events(payload):
    """Discovery API events -> `{name, url, start, venue, city, county, info}`."""
    rows = []
    for event in ((payload or {}).get("_embedded") or {}).get("events") or []:
        start = ((event.get("dates") or {}).get("start") or {}).get("localDate", "")
        venues = ((event.get("_embedded") or {}).get("venues") or [{}])
        venue = venues[0] if venues else {}
        rows.append({
            "name": event.get("name", ""),
            "url": event.get("url", ""),
            "start": start,
            "venue": venue.get("name", ""),
            "city": ((venue.get("city") or {}).get("name") or ""),
            "county": ((venue.get("state") or {}).get("name") or ""),
            "info": event.get("info") or event.get("pleaseNote") or "",
        })
    return [row for row in rows if row["name"] and row["url"]]


def _caption(row):
    parts = [row["name"], row["info"],
             f"Venue: {row['venue']}" if row["venue"] else "",
             f"City: {row['city']}" if row["city"] else "",
             f"Ticketmaster lists the date as {row['start']}" if row["start"] else ""]
    return "\n".join(part for part in parts if part)


def run(state):
    key = _key()
    if not key:
        state["errors"].append(("discovery", "no key -- vault item "
                                "developer-account.ticketmaster.com not yet in .env "
                                "as TICKETMASTER_API_KEY"))
        return []
    used = calls_today(state["factory_state"])
    if used >= int(state["config"].get("daily_call_cap", 50)):
        state["errors"].append(("discovery", f"daily call cap reached ({used})"))
        return []

    query = urllib.parse.urlencode({
        "apikey": key, "countryCode": "IE", "classificationName": "Family",
        "size": int(state["config"].get("size", 100)), "sort": "date,asc",
        "startDateTime": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endDateTime": (datetime.now(timezone.utc)
                        + timedelta(days=factory_worker.EVENT_HORIZON_DAYS)
                        ).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    try:
        payload = common.http_json(f"{API_URL}?{query}", timeout=90)
    except Exception as error:
        # The key is in the query string, so anything that stringifies the URL
        # would put it in factory_state.json and in the admin's Sources view.
        state["errors"].append(("discovery",
                                f"API call failed: {str(error).replace(key, '<key>')}"))
        return []
    state["calls"] = used + 1

    out = []
    for row in parse_events(payload):
        if len(out) >= state["budget"]:
            break
        prefill = {"start_date": row["start"], "end_date": row["start"],
                   "name": row["venue"], "city": row["city"], "county": row["county"],
                   "date_evidence": (f"Ticketmaster lists the date as {row['start']}"
                                     if row["start"] else "")}
        out.append(common.candidate(
            "ticketmaster", "discovery", row["url"], title=row["name"],
            caption=_caption(row), county=row["county"], kind_hint="event",
            prefill=prefill if row["start"] else None))
    return out
