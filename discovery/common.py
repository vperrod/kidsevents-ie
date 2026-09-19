#!/usr/bin/env python3
"""Plumbing every discovery lane shares: config, HTTP, the candidate shape and
the candidate desk.

A *candidate* is the only thing a lane produces. It is deliberately the same
dict `staging.py` stores for the social crew, so `factory_worker.promote()`
does not care which lane found an item:

    {"source_url", "found_via": "<lane>:<key>", "fetched_at",
     "title"?, "caption"?, "text"?, "kind_hint"?, "county"?,
     "location"?, "prefill"?, "sources"?}

`caption` is text a lane already holds (a feed entry, a dataset row) that
`research_fetch` merges with the live page fetch; `text` short-circuits that
fetch entirely, for a lane that has already crawled the page.
"""

import json
import math
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import factory_worker

BASE = Path(__file__).resolve().parent.parent
LANES_FILE = BASE / "catalog" / "lanes.json"
TEMPLATES_FILE = BASE / "catalog" / "search_templates.json"
CANDIDATES_FILE = BASE / "staged" / "candidates.json"
CACHE_DIR = Path(__file__).resolve().parent / "cache"

# Every lane identifies itself to the services it queries. Wikidata and
# Overpass both rate-limit anonymous clients harder than named ones, and
# both ask for a contact address in their usage policy.
UA = {"User-Agent": "SmallDays/1.0 (+https://smalldays.ie; data@smalldays.ie)"}


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def lanes_config():
    return factory_worker.load_json_store(LANES_FILE, {})


def http_bytes(url, timeout=45, headers=None, data=None):
    request = urllib.request.Request(url, data=data, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def http_text(url, timeout=45, headers=None, data=None):
    return http_bytes(url, timeout, headers, data).decode("utf-8", "ignore")


def http_json(url, timeout=60, headers=None, data=None):
    return json.loads(http_text(url, timeout, {"Accept": "application/json", **(headers or {})}, data))


def cached_text(name, url, max_age_days=30, timeout=120):
    """A big, rarely-changing file (OpenFlights routes) kept on disk instead of
    re-downloaded every cycle. The cache directory is git-ignored."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / name
    if path.exists():
        age_days = (time.time() - path.stat().st_mtime) / 86400
        if age_days < max_age_days:
            return path.read_text(encoding="utf-8", errors="ignore")
    text = http_text(url, timeout=timeout)
    path.write_text(text, encoding="utf-8")
    return text


def candidate(lane, key, source_url, **extra):
    """One candidate in the shape `promote()` reads. Empty values are dropped
    so a lane never has to decide whether to pass a key at all."""
    item = {
        "source_url": source_url,
        "platform": "web",
        "found_via": f"{lane}:{key}",
        "fetched_at": now_iso(),
    }
    item.update({k: v for k, v in extra.items() if v not in (None, "", [], {})})
    return item


def unresearched(candidates):
    """The candidates the ledger has not researched recently.

    A lane that caps how many candidates it offers per source has to drop the
    already-researched ones BEFORE it truncates, or it offers the same first N
    every run and never reaches item N+1 -- `run_all` filtering afterwards
    would just turn the whole batch into zero new. Imported here rather than
    at module level: `ledger` imports this module's `factory_worker` too.
    """
    from discovery import ledger

    return ledger.filter_due(candidates)


def lane_of(item):
    return str(item.get("found_via") or ":").split(":", 1)[0]


def key_of(item):
    return str(item.get("found_via") or ":").split(":", 1)[1]


def load_candidates():
    return factory_worker.load_json_store(CANDIDATES_FILE, [])


def append_candidates(items):
    """Put candidates on the discovery desk, one entry per `source_url`.
    Returns everything the caller should research this cycle.

    A URL already on the desk is NOT simply skipped: `run_all` only ever passes
    candidates the ledger says are due, so a URL arriving again is one whose
    recheck window has expired (an event page 3 days on, a place 30). Its desk
    entry is refreshed back to `needs_review` and it goes out for research
    again -- skipping it would make the recheck windows meaningless after the
    first pass.

    This is the discovery twin of `staged/social_candidates.json`: same keys,
    separate file, because the admin Social page and the mini PC crew both
    address that one by name.
    """
    staged = []
    with factory_worker.output_lock():
        existing = load_candidates()
        by_url = {one.get("source_url"): one for one in existing}
        for item in items:
            url = item.get("source_url")
            if not url:
                continue
            entry = {**by_url.get(url, {}), **item, "status": "needs_review"}
            entry.pop("reason", None)
            entry.pop("missing_field", None)
            if url in by_url:
                existing[existing.index(by_url[url])] = entry
            else:
                existing.append(entry)
            by_url[url] = entry
            staged.append(entry)
        if staged:
            CANDIDATES_FILE.parent.mkdir(exist_ok=True)
            factory_worker.write_json_atomic(CANDIDATES_FILE, existing)
    return staged


def set_candidate_status(source_url, status, reason="", missing_field=""):
    with factory_worker.output_lock():
        items = load_candidates()
        for item in items:
            if item.get("source_url") == source_url:
                item["status"] = status
                # Bound the automatic retries: a `needs_review` verdict means no model
                # lane answered, and without a counter the same candidates are retried
                # every cycle forever (the social sweep had exactly that defect).
                item["research_attempts"] = int(item.get("research_attempts") or 0) + 1
                item["reviewed_at"] = now_iso()
                if reason:
                    item["reason"] = reason[:300]
                if missing_field:
                    item["missing_field"] = missing_field
                factory_worker.write_json_atomic(CANDIDATES_FILE, items)
                return True
    return False


def great_circle_km(lat1, lon1, lat2, lon2):
    """Distance in km between two points on the Earth (mean radius 6371 km)."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = (math.sin(d_phi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2)
    return 6371.0 * 2 * math.asin(math.sqrt(a))
