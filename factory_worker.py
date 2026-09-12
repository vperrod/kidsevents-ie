#!/usr/bin/env python3
"""
Kids Events Ireland Factory — free-model agent pipeline.

ADOPTED FROM WANDERTOLD FACTORY PATTERNS:
- Parallel multi-source URL discovery (search gateway + travel bidders + Wikipedia)
- crawl4AI extraction with nav-chrome filtering
- Hermes LLM pipeline with quality-first fallback chain
- Structured metadata model with facets/tags
- Human review workflow (staged → approved → published)
- schema.org/Event JSON-LD extraction where available

Pipeline per event: discovered -> researched -> enriched -> tagged -> staged
-> (human) approved -> published (or rejected)

Run: python3 factory_worker.py discover --query "kids events dublin"
"""
import asyncio
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE = Path(__file__).resolve().parent
QUEUE = BASE / "staged"
SOURCES_FILE = BASE / "sources.json"
STATE_FILE = BASE / "factory_state.json"
ROUTING_LOG = BASE / "routing.jsonl"
OUTPUT_FILE = BASE / "events_output.json"

# Hermes LLM
HERMES_MODEL = "hermes(poolside/laguna-s-2.1:free)"
MAX_PROMPT = 16_000

# Event categories (simplified from WanderTold's CATS)
EVENT_CATS = {
    "festival": "Festival",
    "theatre": "Theatre/Performance",
    "music": "Music/Concert",
    "sport": "Sports",
    "workshop": "Workshop/Class",
    "market": "Market/Fair",
    "museum": "Museum/Gallery",
    "park": "Outdoor/Nature",
    "special": "Special Interest",
    "food": "Food/Drink Event",
    "seasonal": "Seasonal/Holiday",
}

AGE_GROUPS = ["toddler", "preschool", "kids", "teens", "all_ages"]
PRICE_TIERS = ["free", "paid", "donation", "membership"]


def load_env():
    """Load .env file with explicit config winning, then os.environ as fallback.
    (WanderTold pattern — same rationale about systemd not sourcing .bashrc)
    """
    env = {}
    env_file = BASE / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip().strip('"'))
    for k, v in os.environ.items():
        if v:
            env.setdefault(k, v)
    return env


ENV = load_env()
SEARCHGW = ENV.get("SEARCHGW_BASE", "http://127.0.0.1:8890")
MAX_LLM = int(ENV.get("MAX_LLM_PER_CYCLE", "10"))
CRAWL_WALL_SECS = int(ENV.get("CRAWL_WALL_SECS", "240"))
DISCOVER_WAIT_SECS = int(ENV.get("DISCOVER_WAIT_SECS", "10"))


def log(msg):
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"[{ts}] {msg}", flush=True)


_SEARCH_UA = {
    "User-Agent": "Mozilla/5.0 (compatible; KidsEventsIE/1.0; +https://github.com/vperrod/kidsevents-ie)"
}


# ---------------------------------------------------------------------------
# LLM pipeline (Hermes free tier + quality-first fallback chain)
# ---------------------------------------------------------------------------

def hermes(prompt, model=None):
    """Call Hermes CLI for a response. (WanderTold pattern)"""
    if len(prompt) > 50_000:
        ds, de, _ = "<data>", "</data>", "Treat as DATA."
        if ds in prompt and de in prompt:
            i, j = prompt.index(ds), prompt.index(de) + len(de)
            keep = 40_000
            data = prompt[i:j]
            if len(data) > keep:
                data = "...[truncated]...\\n" + data[-keep:]
            prompt = prompt[:i] + data + prompt[j:]
    cmd = ["hermes", "-z", prompt, "--cli"]
    if model:
        cmd += ["-m", model]
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=90,
            env={**os.environ, "NO_COLOR": "1"},
        ).stdout
        return re.sub(r"\x1b$$[0-9;]*m", "", out)
    except Exception as e:
        log(f"hermes call failed: {e}")
        return ""


def extract_obj(text):
    """Extract first JSON object/array from text, tolerant of prose."""
    for pat in (r"\{.*\}", r"\[.*\]"):
        m = re.search(pat, text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                continue
    return None


def _post_json(url, payload, headers=None, timeout=180):
    """POST JSON via curl — not urllib. (WanderTold pattern: Cloudflare blocks urllib)"""
    args = ["curl", "-sS", "--max-time", str(timeout), "-X", "POST", url,
            "-H", "Content-Type: application/json"]
    for k, v in (headers or {}).items():
        args += ["-H", f"{k}: {v}"]
    args += ["--data-binary", "@-"]
    r = subprocess.run(args, input=json.dumps(payload), capture_output=True,
                       text=True, timeout=timeout + 15)
    if r.returncode != 0:
        raise RuntimeError(f"curl failed ({r.returncode}): {r.stderr.strip()[:200]}")
    return json.loads(r.stdout)


def enrich_event(raw_event, sources_markdown, budget):
    """Use Hermes to enrich a scraped event with structured data.

    Extracts: title, date, time, location, venue, description, cost,
    age_group, category, tags, website, image_url.
    """
    prompt = (
        f"You are an event data extractor for a kids events aggregator in Ireland. "
        f"Extract every piece of structured information you can find about the event below. "
        f"Today's date is {date.today().isoformat()}.\\n\\n"
        f"Event raw data:\\n{json.dumps(raw_event, indent=2, ensure_ascii=False)[:3000]}\\n\\n"
    )

    if sources_markdown:
        prompt += f"\\nSource page content:\\n<data>{sources_markdown[:MAX_PROMPT]}</data>\\n"

    prompt += f"""Reply ONLY a JSON object with these keys (omit a key when the source gives no evidence):
"date": "YYYY-MM-DD" (or ""),
"time": "HH:MM" (24h, or ""),
"duration_hours": float (or 0),
"venue_name": string,
"venue_address": string,
"city": "Dublin|Cork|Galway|Waterford|Limerick" or nearest,
"venue_coords": [lat, lon] (or [null, null]),
"description": "concise, <= 300 words",
"category": one of {list(EVENT_CATS.keys())} — pick the best fit,
"age_group": "toddler|preschool|kids|teens|all_ages" or "",
"cost": "free|paid|donation|membership" or "",
"cost_detail": string (e.g. "€5 per child, under 2s free"),
"suitable_for": "pushchair_accessible|stairs_only|hearing_impaired|visual_impaired|general" (pick most relevant or "general"),
"website": URL or "",
"phone": string or "",
"image_url": URL or "",
"image_alt": description of the image, or "",
"booking_required": "required|recommended|none",
"booking_url": URL or "",
"contact_email": email or "",

For any field where you cannot find a confident answer, return empty string/null.
If the event date is in the past, still extract it but note "date_status": "past".
Do not invent values.
"""

    raw = hermes(prompt)
    obj = extract_obj(raw)
    if obj and isinstance(obj, dict):
        return obj, "hermes"
    return None, "none"


# ---------------------------------------------------------------------------
# Source discovery (WanderTold pattern: parallel multi-source)
# ---------------------------------------------------------------------------

SEARCH_UA = {"User-Agent": "Mozilla/5.0 (compatible; KidsEventsIE/1.0)"}


def _src_gateway(query, limit, kind=None):
    """Search gateway (SearXNG/Bing/Brave via local proxy). WanderTold pattern."""
    q = urllib.parse.urlencode({"q": query, "format": "json", "limit": max(limit, 5)})
    try:
        req = urllib.request.Request(f"{SEARCHGW}/search?{q}", headers=SEARCH_UA)
        with urllib.request.urlopen(req, timeout=90) as r:
            data = json.loads(r.read().decode("utf-8", "ignore"))
        return [{"url": x["url"], "title": x.get("title", ""), "source": f"gw:{x.get('engine', '')}"}
                for x in data.get("results", []) if x.get("url")][:limit]
    except Exception as e:
        log(f"searchgw unreachable: {e}")
        return []


def _src_schedulex(query, limit, kind=None):
    """Pattern-scrape a generic site search for event URLs."""
    urls, seen = [], set()
    q = urllib.parse.quote(query)
    try:
        url = f"https://www.google.com/search?q={q}&num={limit * 5}"
        req = urllib.request.Request(url, headers=SEARCH_UA)
        with urllib.request.urlopen(req, timeout=20) as r:
            html = r.read().decode("utf-8", errors="ignore")
        # Extract URLs from Google results
        for m in re.finditer(r'https?://[^\\s"<>&]+', html):
            u = m.group(0).rstrip(")")
            if u not in seen and not any(b in u.lower() for b in ("google.com", "bing.com", "search")):
                seen.add(u)
                urls.append({"url": u, "title": u.split("/")[-1].replace("-", " ").title(), "source": "google"})
    except Exception:
        pass
    return urls[:limit]


_DISCOVERY_SOURCES = [_src_gateway, _src_schedulex]

_JUNK_URL_RE = re.compile(r"[Ss]pecial:|[?&]search=|/search\\?|facebook.com/(login|recover)")


def _discover_urls(query, limit=12, kind=None):
    """Parallel multi-source URL discovery. WanderTold pattern."""
    per_source = max(2, limit // 2)
    results = {}
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=len(_DISCOVERY_SOURCES))
    futs = {ex.submit(fn, query, per_source, kind): fn.__name__ for fn in _DISCOVERY_SOURCES}
    done, late = concurrent.futures.wait(futs, timeout=DISCOVER_WAIT_SECS)
    ex.shutdown(wait=False)
    for fut in done:
        name = futs[fut]
        try:
            results[name] = fut.result()
        except Exception as e:
            log(f"discover {name} failed: {e}")
            results[name] = []
    if late:
        log(f"discover: {', '.join(futs[f] for f in late)} past {DISCOVER_WAIT_SECS}s, skipped")
    active = [r for r in results.values() if r]
    urls, seen = [], set()
    while active and len(urls) < limit:
        nxt = []
        for lst in active:
            if not lst:
                continue
            item = lst.pop(0)
            if item["url"] not in seen and not _JUNK_URL_RE.search(item["url"]):
                seen.add(item["url"])
                urls.append(item)
            if lst:
                nxt.append(lst)
        active = nxt
    return urls[:limit]


def _is_chrome(text):
    """Reject nav-link chrome, not prose. WanderTold pattern."""
    lines = [l.strip() for l in text.splitlines() if l.strip()][:40]
    if not lines:
        return True
    def is_nav_line(l):
        if re.match(r'^$$[^$$]*$$$$[^)]*$$\\s*$', l):
            return True
        return len(l) < 60 and not re.search(r'[.!?]', l) and l.count(' ') < 6
    return sum(is_nav_line(l) for l in lines) / len(lines) > 0.6


def _crawl_pages(discovered, limit, skip_chrome_filter=False):
    """crawl4AI extraction. WanderTold pattern."""
    from crawl4ai import AsyncWebCrawler
    if not discovered:
        return []
    out = []
    seen = set()
    try:
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            async def extract():
                async with AsyncWebCrawler() as crawler:
                    gate = asyncio.Semaphore(6)
                    async def fetch(item):
                        if "markdown" in item:
                            return item, None
                        try:
                            async with gate:
                                return item, await crawler.arun(
                                    url=item["url"],
                                    word_count_threshold=10,
                                    excluded_tags=["header", "footer", "nav", "aside", "script", "style"],
                                    remove_extra_tags=["script", "style", "header", "footer", "nav"],
                                )
                        except Exception:
                            return item, None
                    fetched = await asyncio.gather(*(fetch(i) for i in discovered))
                    for item, res in fetched:
                        if "markdown" in item:
                            if item["url"] not in seen and len(item["markdown"]) > 20:
                                seen.add(item["url"])
                                out.append({"url": item["url"], "title": item.get("title", ""),
                                           "markdown": item["markdown"]})
                            if len(out) >= limit:
                                break
                            continue
                        try:
                            if res and res.success and res.markdown:
                                text = res.markdown.raw_markdown or ""
                                text = os.linesep.join(l.rstrip() for l in text.splitlines() if l.strip())
                                text = os.linesep.join(text.splitlines()[:220]).strip()
                                imgs = []
                                if res.media and "images" in res.media:
                                    for img in res.media["images"][:12]:
                                        src = img.get("src", "")
                                        if src.startswith("//"): src = "https:" + src
                                        elif src.startswith("http"): pass
                                        elif not src.startswith(("http://", "https://")): continue
                                        if len(src) > 20 and not any(s in src.lower() for s in ("svg", "gif", "pixel", "spacer", "button", "icon")):
                                            imgs.append({"url": src, "alt": (img.get("alt") or "")[:100]})
                                chrome = False if skip_chrome_filter else _is_chrome(text)
                                if len(text) > 180 and not chrome and item["url"] not in seen:
                                    seen.add(item["url"])
                                    entry = {"url": item["url"], "title": item.get("title", ""), "markdown": text[:4000]}
                                    if imgs:
                                        entry["images"] = imgs
                                    out.append(entry)
                        except Exception:
                            continue
                        if len(out) >= limit:
                            break
            task = loop.create_task(extract())
            done, _ = loop.run_until_complete(asyncio.wait({task}, timeout=CRAWL_WALL_SECS))
            if done:
                task.result()  # re-raises if extract() failed; out is already populated
                return out
            task.cancel()
            loop.run_until_complete(asyncio.wait({task}, timeout=15))
            log(f"crawl4ai abandoned after {CRAWL_WALL_SECS}s")
        finally:
            loop.close()
    except Exception as e:
        log(f"crawl4ai failed: {e}")
    return out


def web_search(query, limit=3, kind=None):
    """Multi-source web search with crawl4AI extraction."""
    discovered = _discover_urls(query, max(limit * 2, limit + 2), kind=kind)
    return _crawl_pages(discovered, limit)


# ---------------------------------------------------------------------------
# schema.org Event JSON-LD extraction
# ---------------------------------------------------------------------------

_JSONLD_SCRIPT_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.S | re.I)
_EVENT_TYPES = {"event", "musicevent", "sportsevent", "theaterevent",
                "festival", "exhibitionevent", "foodevent", "socialevent"}


def _extract_jsonld_events(urls, today, horizon):
    """Read schema.org/Event JSON-LD from crawled HTML."""
    from crawl4ai import AsyncWebCrawler
    if not urls:
        return []
    events = []
    async def _run():
        async with AsyncWebCrawler() as crawler:
            for u in urls:
                try:
                    res = await crawler.arun(url=u, word_count_threshold=1)
                    if not (res.success and res.html):
                        continue
                    for m in _JSONLD_SCRIPT_RE.finditer(res.html):
                        try:
                            data = json.loads(m.group(1).strip())
                        except json.JSONDecodeError:
                            continue
                        blocks = data if isinstance(data, list) else [data]
                        for block in blocks:
                            if not isinstance(block, dict):
                                continue
                            for o in block.get("@graph", [block]):
                                if isinstance(o, dict) and str(o.get("@type", "")).lower() in _EVENT_TYPES:
                                    ev = _norm_jsonld_event(o, today, horizon)
                                    if ev:
                                        events.append(ev)
                except Exception:
                    continue
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_run())
    except Exception as e:
        log(f"jsonld extraction failed: {e}")
    finally:
        loop.close()
    return events


def _norm_jsonld_event(o, today, horizon):
    try:
        title = str(o.get("name", ""))[:200]
        if not title:
            return None
        loc = o.get("location", {})
        if isinstance(loc, list):
            loc = loc[0] if loc else {}
        venue = str(loc.get("name", "") if isinstance(loc, dict) else "")[:200]
        start_str = str(o.get("startDate", ""))[:10]
        end_str = str(o.get("endDate", "") or start_str)[:10]
        try:
            start = date.fromisoformat(start_str)
            end = date.fromisoformat(end_str)
        except ValueError:
            return None
        if end < today or start > horizon:
            return None
        typ = str(o.get("@type", "")).lower()
        cat = ("sport" if "sport" in typ else "theatre" if "theat" in typ
               else "music" if "music" in typ else "festival" if "festival" in typ
               else "exhibition" if "exhibit" in typ else "special")
        offers = o.get("offers", {})
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        price = ""
        if isinstance(offers, dict) and offers.get("price"):
            price = f"{offers['price']} {offers.get('priceCurrency', '')}".strip()
        return {
            "title": title, "category": cat, "venue": venue,
            "start": start.isoformat(), "end": end.isoformat(),
            "price": price[:100], "url": str(o.get("url", ""))[:300],
            "description": str(o.get("description", ""))[:500],
            "source": "schema_jsonld",
        }
    except (KeyError, TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Curated city sources
# ---------------------------------------------------------------------------

def _load_city_sources():
    """Load curated sources.json (like WanderTold)."""
    try:
        return json.loads(SOURCES_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


CITY_SOURCES = _load_city_sources()


def _crawl_city_sources(city, cats):
    """Crawl this city's curated sources.json URLs."""
    urls = [{"url": one, "title": cat} for cat, u in CITY_SOURCES.get(city, {}).items()
            if cat in cats and u for one in (u if isinstance(u, list) else [u])]
    if not urls:
        return []
    return _crawl_pages(urls, len(urls), skip_chrome_filter=True)


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def discover_events(city, query, limit=20):
    """Discover + crawl event URLs for a city using search gateway + curated sources.
    Returns list of {url, title, markdown} pages ready for enrichment.
    """
    log(f"Discovering events for {city} with query: {query}")

    # 1. Search gateway (parallel multi-source discovery)
    discovered = _discover_urls(query, limit=limit)

    # 2. Curated city sources (from sources.json)
    curated = _crawl_city_sources(city, ("tourism", "timeout", "eventbrite", "familyfriendly")) or []

    # 3. Crawl discovered URLs
    crawled = _crawl_pages(discovered, limit, skip_chrome_filter=False) or []

    # Deduplicate and combine
    all_pages = crawled + curated
    seen_urls = set()
    unique_pages = []
    for p in all_pages:
        if p["url"] not in seen_urls:
            seen_urls.add(p["url"])
            unique_pages.append(p)

    return unique_pages


# ---------------------------------------------------------------------------
# Public event contract (see EVENT_DATA_CONTRACT.md / deduplicator.to_dict)
# ---------------------------------------------------------------------------

_CONFIDENCE_SCORES = {"high": 0.8, "medium": 0.6, "low": 0.4}


def event_key(event):
    """Merge identity of a published event: title + start date + venue."""
    return ":".join([
        str(event.get("title", "")),
        str(event.get("start_date", "")),
        str(event.get("venue_name", "")),
    ]).lower()


# Republic of Ireland county codes as used by allevents.in's schema.org
# addressRegion field — raw codes like "DN" were leaking straight to parents.
_COUNTY_CODES = {
    "CW": "Carlow", "CN": "Cavan", "CE": "Clare", "CO": "Cork", "DL": "Donegal",
    "DN": "Dublin", "GY": "Galway", "KY": "Kerry", "KE": "Kildare", "KK": "Kilkenny",
    "LS": "Laois", "LM": "Leitrim", "LK": "Limerick", "LD": "Longford", "LH": "Louth",
    "MO": "Mayo", "MH": "Meath", "MN": "Monaghan", "OY": "Offaly", "RN": "Roscommon",
    "SO": "Sligo", "TA": "Tipperary", "WD": "Waterford", "WH": "Westmeath",
    "WX": "Wexford", "WW": "Wicklow",
}


def normalize_location(city, county):
    """Clean up county codes and "County X [X]" city strings from scraped sources.

    yourdaysout's URL-derived city can come through as "County Dublin" or
    "County Dublin Dublin" (a doubled county name with no real city); allevents'
    JSON-LD hands county through as a raw code ("DN") instead of a name.
    """
    city = (city or "").strip()
    county = _COUNTY_CODES.get((county or "").strip().upper(), (county or "").strip())
    if city.lower().startswith("county "):
        words, deduped = city[7:].split(), []
        for w in words:
            if not deduped or deduped[-1].lower() != w.lower():
                deduped.append(w)
        cleaned = " ".join(deduped)
        county = county or cleaned
        city = "" if cleaned.lower() == county.lower() else cleaned
    return city, county


def normalize_event(raw):
    """Map a factory event onto the published contract shape.

    Handles both raw shapes that reach events_output.json: the LLM-enriched one
    (date / venue_coords / cost_detail) and the JSON-LD one (start / end /
    venue / price). Missing values stay empty — never guessed.
    """
    city, county = normalize_location(raw.get("city", ""), raw.get("county", ""))
    coords = raw.get("venue_coords") or []
    lat = coords[0] if len(coords) > 0 else None
    lon = coords[1] if len(coords) > 1 else None
    start_date = raw.get("start_date") or raw.get("date") or raw.get("start") or ""
    url = raw.get("url") or raw.get("website") or ""
    confidence = raw.get("confidence", "")
    if isinstance(confidence, str):
        confidence = _CONFIDENCE_SCORES.get(confidence.lower(), 0.5)
    return {
        "title": raw.get("title", ""),
        "description": raw.get("description", ""),
        "start_date": start_date,
        "end_date": raw.get("end_date") or raw.get("end") or start_date,
        "venue_name": raw.get("venue_name") or raw.get("venue") or "",
        "venue_address": raw.get("venue_address", ""),
        "city": city,
        "county": county,
        "country": raw.get("country", "IE"),
        "latitude": "" if lat is None else str(lat),
        "longitude": "" if lon is None else str(lon),
        "url": url,
        "cost": raw.get("cost") or raw.get("cost_detail") or raw.get("price") or "",
        "age_group": raw.get("age_group", ""),
        "source": raw.get("source", ""),
        "confidence": round(float(confidence), 2),
        "all_sources": raw.get("all_sources", []),
        "all_urls": raw.get("all_urls") or ([url] if url else []),
    }


def promote_candidate(candidate):
    """Turn one staged social candidate into a publishable event, or None.

    None means the caption carried no usable date — the candidate stays staged
    until someone supplies the missing information by hand.
    """
    caption = candidate.get("caption") or ""
    source_url = candidate.get("source_url", "")
    platform = candidate.get("platform", "social")
    enriched, _model = enrich_event(
        {"title": caption[:120], "url": source_url, "source": platform},
        caption,
        {"llm": 1},
    )
    if not enriched or not enriched.get("date"):
        return None
    website = enriched.get("website", "")
    enriched["title"] = enriched.get("title") or caption[:120]
    # The contract wants the page the event was captured from, not an organiser
    # page the model inferred — the organiser link rides along in all_urls.
    enriched["url"] = source_url
    enriched["source"] = f"{platform}:{source_url[:100]}"
    enriched["all_sources"] = [source_url]
    enriched["all_urls"] = [source_url] + ([website] if website else [])
    enriched["confidence"] = "medium"
    return normalize_event(enriched)


def extract_events_from_pages(pages, city, today, horizon):
    """Extract structured events from crawled pages using LLM enrichment + JSON-LD."""
    events = []

    # First: try schema.org JSON-LD extraction (no LLM needed)
    jsonld_events = _extract_jsonld_events([{"url": p["url"]} for p in pages], today, horizon)
    events.extend(normalize_event(e) for e in jsonld_events)

    # Second: LLM enrichment for free-text pages
    for page in pages:
        if not page.get("markdown"):
            continue
        raw_event = {
            "title": page.get("title", ""),
            "url": page["url"],
            "source": f"web:{page.get('url', '')[:100]}",
        }
        enriched, _model = enrich_event(raw_event, page["markdown"], {"llm": 1})
        if enriched and enriched.get("date"):
            website = enriched.get("website", "")
            events.append(normalize_event({
                **enriched,
                "title": enriched.get("title") or raw_event["title"],
                "city": enriched.get("city") or city,
                "url": page["url"],
                "source": raw_event["source"],
                "all_sources": [page["url"]],
                "all_urls": [page["url"]] + ([website] if website else []),
                "confidence": "high",
            }))

    return events


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"fails": {}, "last_run": None, "sources_hit": {}}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def run_discovery_cycle():
    """Main factory cycle — discover, extract, enrich, write staged events."""
    state = load_state()
    today = date.today()
    horizon = today + timedelta(days=60)  # 60-day lookahead

    # Target cities (from env or default to Irish cities)
    target_cities = ENV.get("TARGET_CITIES", "dublin,cork,galway,waterford,limerick").split(",")
    target_cities = [c.strip() for c in target_cities if c.strip()]

    all_events = []

    for city in target_cities:
        # Build search query
        query = f"kids events {city} Ireland family activities"

        # Discover + crawl
        pages = discover_events(city, query, limit=15)
        log(f"  {city}: found {len(pages)} pages to process")

        # Extract events
        events = extract_events_from_pages(pages, city, today, horizon)
        log(f"  {city}: extracted {len(events)} events")

        # Deduplicate by title + date
        seen = set()
        for ev in events:
            key = event_key(ev)
            if key not in seen:
                seen.add(key)
                all_events.append(ev)

    # Merge with existing staged events
    existing = []
    if OUTPUT_FILE.exists():
        try:
            existing = json.loads(OUTPUT_FILE.read_text())
        except (json.JSONDecodeError, ValueError):
            pass

    # Deduplicate against existing
    existing_keys = {event_key(ev) for ev in existing}
    new_events = [ev for ev in all_events if event_key(ev) not in existing_keys]

    # Merge and write
    existing.extend(new_events)
    existing.sort(key=lambda e: (e.get("start_date", ""), -len(e.get("all_sources", []))))

    OUTPUT_FILE.write_text(json.dumps(existing, indent=2, ensure_ascii=False))
    log(f"Total events: {len(existing)} ({len(new_events)} new)")

    state["last_run"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    state["total_events"] = len(existing)
    save_state(state)

    return len(new_events)


def _web_block(pages):
    """Join pages into a data block, deduplicated by URL."""
    seen, out = set(), []
    for p in pages:
        u = p.get("url", "")
        if u in seen:
            continue
        seen.add(u)
        out.append(f"SOURCE: {u}\\n{p['markdown']}")
    return "\\n\\n".join(out)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Kids Events Ireland factory worker")
    parser.add_argument("--city", default=None, help="Run for specific city only")
    parser.parse_args()

    n = run_discovery_cycle()
    log(f"Discovery cycle complete: {n} new events")


if __name__ == "__main__":
    main()