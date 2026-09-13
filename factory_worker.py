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
import contextlib
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import threading
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
HOLIDAYS_FILE = BASE / "holidays_output.json"
# Places (year-round local activities/venues -- soft play, farms, museums)
# are their own section, distinct from Holidays (bigger curated day-trip
# destinations like Avondale Forest Park). Same shape, separate file/tab.
PLACES_FILE = BASE / "places_output.json"
# events_output.json/places_output.json/holidays_output.json are each
# read-modify-written from more than one process now (the hourly factory
# timer, the admin approve route, the auto-approve sweep) -- confirmed live
# 2026-09-12: an hourly run's "load existing, extend, write" read a stale
# copy while a sweep was mid-flight, silently dropping events the sweep had
# just added. One lock file serializes all three; the operations are small
# JSON read/writes, so contention cost is negligible next to the risk.
_LOCK_FILE = BASE / ".output.lock"
# Re-entrant per thread: flock() treats every open() of the lock file as an
# independent lock owner, so a nested output_lock() in the same thread
# (staging's locked mutation calling publish_event) deadlocked against itself
# -- the backlog sweep sat in locks_lock_inode_wait for 20 h on 2026-09-12/13.
_lock_state = threading.local()


@contextlib.contextmanager
def output_lock():
    depth = getattr(_lock_state, "depth", 0)
    if depth == 0:
        _LOCK_FILE.touch(exist_ok=True)
        _lock_state.fh = open(_LOCK_FILE, "w")
        fcntl.flock(_lock_state.fh, fcntl.LOCK_EX)
    _lock_state.depth = depth + 1
    try:
        yield
    finally:
        _lock_state.depth -= 1
        if _lock_state.depth == 0:
            fcntl.flock(_lock_state.fh, fcntl.LOCK_UN)
            _lock_state.fh.close()
            _lock_state.fh = None


def write_json_atomic(path, obj):
    """Write a JSON store so a crash mid-write can never leave a torn file:
    full content to <path>.tmp in the same directory, fsync, then os.replace
    (atomic within one filesystem). Every store in this project goes through
    here -- truncate-in-place was one interrupted write away from losing the
    whole dataset.
    """
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, ensure_ascii=False)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def load_json_store(path, default):
    """Load a JSON store, or raise.

    A missing or blank file is legitimately `default`. A file with content in
    it that will not parse is an error and must stop the caller: the old
    behaviour (swallow the error, return []) turned one torn read into a write
    that erased the real data.
    """
    path = Path(path)
    if not path.exists():
        return default
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        log(f"{path.name}: cannot be read ({error}) — refusing to continue")
        raise
    if not text.strip():
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        log(f"{path.name}: has content but does not parse ({error}) — refusing to overwrite it")
        raise

# Hermes LLM. `nous` (hermes's default provider) has no credentials on this
# VM (confirmed 2026-09-12: `hermes auth status nous` -> logged out, no
# credentials in the pool) -- route through OpenRouter's free tier instead,
# which already has a working key in ~/.hermes/.env. Override with
# HERMES_PROVIDER/HERMES_MODEL env vars if that ever needs to change.
HERMES_PROVIDER = "openrouter"
HERMES_MODEL = "google/gemma-4-31b-it:free"
# systemd user units get a bare PATH without ~/.local/bin: the 2026-09-12
# 15:03 timer run failed every LLM call with "No such file: 'hermes'" and
# published 0 events for all five cities.
HERMES_BIN = shutil.which("hermes") or str(Path.home() / ".local" / "bin" / "hermes")
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

def hermes(prompt, model=None, provider=None):
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
    cmd = [HERMES_BIN, "-z", prompt, "--cli",
           "--provider", provider or HERMES_PROVIDER,
           "-m", model or HERMES_MODEL]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=90,
            env={**os.environ, "NO_COLOR": "1"},
        )
        if result.returncode != 0 and not result.stdout.strip():
            log(f"hermes call failed (exit {result.returncode}): {result.stderr.strip()[:200]}")
        return re.sub(r"\x1b$$[0-9;]*m", "", result.stdout)
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
"title": short synthesised event name, <= 80 characters — never the raw caption or a truncation of it,
"country": "IE" if the event happens in the Republic of Ireland, "GB" for Northern Ireland or Britain, otherwise "other",
"family_relevant": true only if children can attend and it is aimed at or welcoming to families — false for adult comedy, gigs, club nights, age-gated (16+/18+) events, trade or adult-only shopping events,
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


_DISCOVERY_SOURCES = [_src_gateway]

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
    return load_json_store(SOURCES_FILE, {})


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
    curated = _crawl_city_sources(
        city, ("tourism", "timeout", "familyfriendly", "yourdaysout", "listings")) or []

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

MAX_TITLE_CHARS = 120
EVENT_HORIZON_DAYS = 60


def date_window_reason(event, today, horizon):
    """Why this event's date puts it outside the publishable window, or None.

    This is the `today <= date <= horizon` check the JSON-LD path already
    applied; the LLM path had none, which is how a pop-up that ended the day
    before went on air.
    """
    start_str = (event.get("start_date") or "")[:10]
    if not start_str:
        return "no usable date was found"
    end_str = (event.get("end_date") or start_str)[:10]
    try:
        start = date.fromisoformat(start_str)
        end = date.fromisoformat(end_str)
    except ValueError:
        return f"date {start_str!r} is not a usable YYYY-MM-DD date"
    if end < today:
        return f"the event is already over (ended {end.isoformat()})"
    if start > horizon:
        return f"starts {start.isoformat()}, beyond the {(horizon - today).days}-day horizon"
    return None


def event_key(event):
    """Merge identity of a published event: title + start date + venue."""
    return ":".join([
        str(event.get("title", "")),
        str(event.get("start_date", "")),
        str(event.get("venue_name", "")),
    ]).lower()


def _title_is_caption(title, caption):
    """True when the "title" is just the source caption, or the front of it.

    The 2026-09-12 auto-approve published an adult vintage pop-up with the raw
    truncated caption as its title -- `caption[:120]` is a literal prefix of
    the caption, which is exactly what this catches.
    """
    t = " ".join((title or "").split()).lower()
    c = " ".join((caption or "").split()).lower()
    if not t or not c:
        return False
    return t == c or (len(t) >= 20 and c.startswith(t))


def _title_reject_reason(record, caption=""):
    title = (record.get("title") or "").strip()
    if not title:
        return "no title"
    if len(title) > MAX_TITLE_CHARS:
        return f"title is {len(title)} characters (max {MAX_TITLE_CHARS})"
    if _title_is_caption(title, caption):
        return "title is the raw caption, not a synthesised title"
    return None


def event_reject_reason(event, caption=""):
    """Why this normalized event must not be published, or None if it may be.

    `caption` is the social post text the record came from, when there is one;
    the title/caption check is a no-op without it.
    """
    country = (event.get("country") or "").strip()
    if country != "IE":
        return f"country is {country or 'unknown'}, not IE"
    if event.get("family_relevant") is False:
        return "not family-relevant"
    reason = _title_reject_reason(event, caption)
    if reason:
        return reason
    if not (event.get("start_date") or "").strip():
        return "no start_date"
    return None


def place_reject_reason(place, caption=""):
    """Why this place must not be published, or None if it may be."""
    country = (place.get("country") or "").strip()
    if country != "IE":
        return f"country is {country or 'unknown'}, not IE"
    if place.get("family_relevant") is False:
        return "not family-relevant"
    return _title_reject_reason(place, caption)


def publish_event(event, caption=""):
    """Append a normalized event to events_output.json, deduped by event_key.

    Returns True if it was new (actually written), False if it was rejected by
    the quality gate or a matching event already existed. Shared by the admin
    approve route, auto-promotion and the discovery cycle so everything
    publishes through the exact same gate.
    """
    reason = event_reject_reason(event, caption)
    if reason:
        log(f"rejected event {event.get('title', '')[:60]!r}: {reason}")
        return False
    with output_lock():
        events = load_json_store(OUTPUT_FILE, [])
        key = event_key(event)
        if any(event_key(e) == key for e in events):
            return False
        events.append(event)
        events.sort(key=lambda e: (e.get("start_date", ""), -len(e.get("all_sources", []))))
        write_json_atomic(OUTPUT_FILE, events)
    return True


def prune_past_events():
    """Drop published events that are over: end_date, or start_date when there
    is no end_date, earlier than today UTC. A record with neither date can
    never be shown on a calendar and is dropped with them (the gate refuses to
    publish undated events at all now). Returns the number removed.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    with output_lock():
        events = load_json_store(OUTPUT_FILE, [])
        kept, past, undated = [], 0, 0
        for event in events:
            when = (event.get("end_date") or event.get("start_date") or "").strip()
            if not when:
                undated += 1
            elif when < today:
                past += 1
            else:
                kept.append(event)
        if past or undated:
            write_json_atomic(OUTPUT_FILE, kept)
    log(f"prune_past_events: dropped {past} past + {undated} undated, {len(kept)} remain")
    return past + undated


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


_COUNTRY_ALIASES = {
    "ie": "IE", "ireland": "IE", "republic of ireland": "IE", "eire": "IE", "éire": "IE",
    "gb": "GB", "uk": "GB", "united kingdom": "GB", "great britain": "GB",
    "northern ireland": "GB", "england": "GB", "scotland": "GB", "wales": "GB",
}


def normalize_country(value):
    """Fold whatever a source called the country onto IE / GB / other.

    Published records carried three spellings of the same country ("IE",
    "Ireland") plus raw "GB", so a country gate could not be written against
    them until they all mean one thing.
    """
    raw = str(value or "").strip()
    if not raw:
        return "IE"
    return _COUNTRY_ALIASES.get(raw.lower(), "other")


def completeness(record):
    """Fraction 0-1 of the fields a parent actually needs that are filled in.

    This replaces the model's own confidence self-report, which was a constant
    ("high" for every web-extracted event) and told nobody anything.
    """
    checks = (
        bool(record.get("start_date")),
        bool(record.get("venue_name")),
        bool(record.get("city")),
        bool(record.get("county")),
        bool(record.get("latitude")) and bool(record.get("longitude")),
        bool(record.get("cost")),
        bool(record.get("age_group")),
        bool(record.get("category")),
        len(record.get("description") or "") >= 120,
        bool(record.get("website")),
    )
    return round(sum(checks) / len(checks), 2)


def normalize_event(raw):
    """Map a factory event onto the published contract shape.

    Handles both raw shapes that reach events_output.json: the LLM-enriched one
    (date / venue_coords / cost_detail) and the JSON-LD one (start / end /
    venue / price). Missing values stay empty — never guessed.

    Every field the enrichment prompt asks for is kept here. Dropping them was
    the actual reason the published feed showed 0% category and near-0% cost
    and age: the model answered, and this function threw the answers away.
    """
    city, county = normalize_location(raw.get("city", ""), raw.get("county", ""))
    coords = raw.get("venue_coords") or []
    lat = coords[0] if len(coords) > 0 else None
    lon = coords[1] if len(coords) > 1 else None
    start_date = raw.get("start_date") or raw.get("date") or raw.get("start") or ""
    url = raw.get("url") or raw.get("website") or ""
    try:
        duration_hours = float(raw.get("duration_hours") or 0)
    except (TypeError, ValueError):
        duration_hours = 0.0
    event = {
        "title": raw.get("title", ""),
        "description": raw.get("description", ""),
        "start_date": start_date,
        "end_date": raw.get("end_date") or raw.get("end") or start_date,
        "time": raw.get("time", ""),
        "duration_hours": duration_hours,
        "venue_name": raw.get("venue_name") or raw.get("venue") or "",
        "venue_address": raw.get("venue_address", ""),
        "city": city,
        "county": county,
        "country": normalize_country(raw.get("country")),
        "family_relevant": raw.get("family_relevant", True),
        "latitude": "" if lat is None else str(lat),
        "longitude": "" if lon is None else str(lon),
        "url": url,
        "website": raw.get("website", ""),
        "image_url": raw.get("image_url", ""),
        "image_alt": raw.get("image_alt", ""),
        "cost": raw.get("cost") or raw.get("cost_detail") or raw.get("price") or "",
        "cost_detail": raw.get("cost_detail") or raw.get("price") or "",
        "age_group": raw.get("age_group", ""),
        "category": raw.get("category", ""),
        "suitable_for": raw.get("suitable_for", ""),
        "booking_required": raw.get("booking_required", ""),
        "booking_url": raw.get("booking_url", ""),
        "phone": raw.get("phone", ""),
        "contact_email": raw.get("contact_email", ""),
        "source": raw.get("source", ""),
        "all_sources": raw.get("all_sources", []),
        "all_urls": raw.get("all_urls") or ([url] if url else []),
    }
    event["confidence"] = completeness(event)
    return event


def extract_place(caption, source_url, platform, author=""):
    """Ask whether a caption describes a real, evergreen place or activity --
    a playground, farm, museum, adventure park, class -- rather than a dated
    event. Returns a holidays_output.json-shaped dict, or None. Same
    do-not-invent discipline as enrich_event: an empty/vague caption (common
    on a rate-limited fetch) correctly yields nothing rather than a guess.
    The account handle is real, verifiable data (often literally the venue's
    name, e.g. "leisuredomeashbourne") -- pass it along, don't rely on
    caption text alone.
    """
    account_line = f"Account/author: {author}\n" if author else ""
    prompt = (
        "You are a places-and-activities curator for a kids/family day-out "
        "guide in Ireland. The text below is a social media caption. Decide "
        "whether it describes a REAL, NAMED, evergreen place or activity a "
        "family could visit any time (a playground, farm, museum, adventure "
        "park, class, attraction) -- NOT a one-off dated event. The account "
        "name is real data you can use to identify the venue (e.g. an "
        'account "leisuredomeashbourne" is the venue "Leisuredome, '
        'Ashbourne") -- don\'t invent details beyond what the handle and '
        "caption actually support.\n\n"
        f"{account_line}"
        f"Caption:\n<data>{caption[:2000]}</data>\n\n"
        'If it is NOT a real identifiable evergreen place, reply exactly: {"is_place": false}\n\n'
        "If it IS, reply ONLY a JSON object:\n"
        '{"is_place": true,\n'
        '"title": short synthesised place name, <= 80 characters — never the raw caption or a truncation of it,\n'
        '"country": "IE" if it is in the Republic of Ireland, "GB" for Northern Ireland or Britain, otherwise "other",\n'
        '"family_relevant": true only if children are welcome and it is somewhere a family would actually bring kids — false for adult-only venues, bars, nightlife or age-gated attractions,\n'
        '"description": "concise, <= 300 words",\n'
        '"region": "Leinster|Munster|Connacht|Ulster" or "",\n'
        '"county": string or "",\n'
        '"location": string (place name, town),\n'
        '"category": string (e.g. "Nature", "Indoor play", "Farm", "Museum"),\n'
        '"price_range": string or "check",\n'
        '"age_group": string or "",\n'
        '"booking_url": URL or ""}\n\n'
        "Do not invent values you cannot support from the text."
    )
    obj = extract_obj(hermes(prompt))
    if not obj or not isinstance(obj, dict) or not obj.get("is_place") or not obj.get("title"):
        return None
    return {
        "title": str(obj.get("title", ""))[:200],
        "description": obj.get("description", "") or "",
        "region": obj.get("region", "") or "",
        "county": obj.get("county", "") or "",
        "location": obj.get("location", "") or "",
        "country": normalize_country(obj.get("country")),
        "family_relevant": obj.get("family_relevant", True),
        "latitude": None,
        "longitude": None,
        "category": obj.get("category", "") or "",
        "price_range": obj.get("price_range") or "check",
        "age_group": obj.get("age_group", "") or "",
        "source_url": source_url,
        "source_name": platform,
        "booking_url": obj.get("booking_url", "") or "",
    }


def publish_place(place, caption=""):
    """Append a place to places_output.json (year-round local activities/
    venues -- its own section, separate from the curated Holidays
    destinations), deduped by title+location. Returns True if newly written,
    False if the quality gate rejected it or it was already there.
    """
    reason = place_reject_reason(place, caption)
    if reason:
        log(f"rejected place {place.get('title', '')[:60]!r}: {reason}")
        return False
    with output_lock():
        places = load_json_store(PLACES_FILE, [])
        key = (place.get("title", "").lower(), place.get("location", "").lower())
        if any((p.get("title", "").lower(), p.get("location", "").lower()) == key for p in places):
            return False
        places.append(place)
        write_json_atomic(PLACES_FILE, places)
    return True


def promote_candidate(candidate, hint=""):
    """Turn one staged social candidate into a publishable event or evergreen
    place. Returns ("event", record, None), ("place", record, None), or
    (None, None, reason).

    `hint` is optional extra context a curator typed in on the admin Social
    page ("it's the playground on Main St, every Saturday") -- folded into
    the caption before either classification runs, same do-not-invent LLM
    gate either way, just with more to go on.

    (None, None, reason) means neither classification found enough to
    publish -- the candidate stays staged, and `reason` is what to show a
    human so they know what's missing rather than just "still pending".
    """
    caption = candidate.get("caption") or ""
    if hint:
        caption = f"{caption}\n\nAdditional context from a curator: {hint}".strip()
    source_url = candidate.get("source_url", "")
    platform = candidate.get("platform", "social")
    today = date.today()
    horizon = today + timedelta(days=EVENT_HORIZON_DAYS)
    enriched, _model = enrich_event(
        {"title": caption[:120], "url": source_url, "source": platform},
        caption,
        {"llm": 1},
    )
    if enriched and enriched.get("date"):
        website = enriched.get("website", "")
        enriched["title"] = enriched.get("title") or caption[:120]
        # The contract wants the page the event was captured from, not an
        # organiser page the model inferred — that rides along in all_urls.
        enriched["url"] = source_url
        enriched["source"] = f"{platform}:{source_url[:100]}"
        enriched["all_sources"] = [source_url]
        enriched["all_urls"] = [source_url] + ([website] if website else [])
        record = normalize_event(enriched)
        window = date_window_reason(record, today, horizon)
        if window:
            return None, None, window
        gate = event_reject_reason(record, caption)
        if gate:
            return None, None, gate
        return "event", record, None

    place = extract_place(caption, source_url, platform, candidate.get("author", ""))
    if place:
        gate = place_reject_reason(place, caption)
        if gate:
            return None, None, gate
        return "place", place, None

    if not (candidate.get("caption") or "").strip() and not hint:
        reason = "No caption text was captured for this post (a rate-limited fetch) — nothing to classify from. Add a note below with what it's about."
    else:
        reason = (
            "Checked as both a dated event and an evergreen place: no usable "
            "date was found, and the caption/account don't clearly name a "
            "specific real venue. Add a note below (a date, or the actual "
            "venue name) and resubmit."
        )
    return None, None, reason


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
            event = normalize_event({
                **enriched,
                "title": enriched.get("title") or raw_event["title"],
                "city": enriched.get("city") or city,
                "url": page["url"],
                "source": raw_event["source"],
                "all_sources": [page["url"]],
                "all_urls": [page["url"]] + ([website] if website else []),
            })
            # Same window the JSON-LD branch above already enforces.
            window = date_window_reason(event, today, horizon)
            if window:
                log(f"rejected event {event.get('title', '')[:60]!r}: {window}")
                continue
            events.append(event)

    return events


def load_state():
    return load_json_store(STATE_FILE, {"fails": {}, "last_run": None, "sources_hit": {}})


def save_state(state):
    write_json_atomic(STATE_FILE, state)


def run_discovery_cycle():
    """Main factory cycle — discover, extract, enrich, write staged events."""
    state = load_state()
    prune_past_events()
    today = date.today()
    horizon = today + timedelta(days=EVENT_HORIZON_DAYS)

    # Target cities (from env or default to Irish cities)
    target_cities = ENV.get("TARGET_CITIES", "dublin,cork,galway,waterford,limerick").split(",")
    target_cities = [c.strip() for c in target_cities if c.strip()]

    all_events = []

    for city in target_cities:
        # Build search query
        # A generic topic phrase ("kids events X Ireland family activities")
        # ranks brand homepages, not listings -- confirmed 2026-09-12 against
        # the live search gateway: a dated/"this weekend" phrasing surfaces
        # actual event-listing and calendar pages (eventbrite .../events--this-
        # weekend/, dublin.ie/whats-on/, dublinevents.com/events/kids-children/)
        # instead of dublinzoo.ie/, familyfun.ie/, tiktok.com/discover/... .
        query = f"kids events {city} this weekend"

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

    # Publish through publish_event so discovery passes the same quality gate
    # as the admin approve route. Holding the lock across the loop keeps the
    # read-modify-write atomic against a concurrent approve/sweep -- confirmed
    # live 2026-09-12, a stale read here silently dropped events a concurrent
    # sweep had just added.
    with output_lock():
        published = sum(1 for ev in all_events if publish_event(ev))
        total = len(load_json_store(OUTPUT_FILE, []))
    log(f"Total events: {total} ({published} new)")

    state["last_run"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    state["total_events"] = total
    save_state(state)

    return published


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