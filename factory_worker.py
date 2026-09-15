#!/usr/bin/env python3
"""
Small Days factory — free-model agent pipeline.

One cycle (`run_discovery_cycle`): prune events that are over, run the
`discovery/` lanes, stage what they found in `staged/candidates.json`, then
research each new candidate through `promote()` -- a research fetch on this VM
followed by the four grounded steps (classify, facts, extract, write) and
`gate.qa`. Nothing is published that has not passed that gate, and nothing in
a record is invented: every value came from a verified quote, from open data,
or from `contract.derive`.

Lifted from the WanderTold factory: parallel multi-source URL discovery,
crawl4AI extraction with nav-chrome filtering, the cheapest-lane-first model
chain (`llm.complete`), and schema.org/Event JSON-LD harvesting.

Run: python3 factory_worker.py [--lane feeds] [--budget 10]
"""
import asyncio
import concurrent.futures
import contextlib
import fcntl
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import random
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import contract
import gate
import llm

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE = Path(__file__).resolve().parent
QUEUE = BASE / "staged"
SOURCES_FILE = BASE / "sources.json"
STATE_FILE = BASE / "factory_state.json"
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

# The event categories and the prompt budget that used to live here belonged to
# `enrich_event`; the vocabulary is `catalog/facets.json` now and each of the
# four steps sets its own budget.

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
CRAWL_WALL_SECS = int(ENV.get("CRAWL_WALL_SECS", "240"))
CRAWL_PAGE_CONCURRENCY = max(1, min(2, int(ENV.get("CRAWL_PAGE_CONCURRENCY", "2"))))
# One cycle per run: the hourly timer must never start a second cycle on top
# of a slow one (they would fight over the same stores and the same shared
# local-model slots). CYCLE_WALL_SECS is the hard stop for a cycle that hangs.
CYCLE_LOCK_FILE = BASE / "daemon.lock"
CYCLE_WALL_SECS = int(ENV.get("CYCLE_WALL_SECS", "2400"))
DISCOVER_WAIT_SECS = int(ENV.get("DISCOVER_WAIT_SECS", "10"))


def log(msg):
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"[{ts}] {msg}", flush=True)


_SEARCH_UA = {
    "User-Agent": "Mozilla/5.0 (compatible; KidsEventsIE/1.0; +https://github.com/vperrod/kidsevents-ie)"
}


# ---------------------------------------------------------------------------
# LLM pipeline (free-first routing — see llm.py)
# ---------------------------------------------------------------------------

def hermes(prompt, model=None, provider=None, kind="llm"):
    """Answer a prompt on the cheapest lane that can (llm.complete): the mini
    PC's local model first, then the rotating free OmniRoute lanes, and only
    then the hermes CLI this function is still named after. Returns "" when
    no lane answered, exactly as before."""
    return llm.complete(prompt, kind, hermes_model=model, hermes_provider=provider)


def extract_obj(text):
    """Extract the first JSON object from text, tolerant of prose.

    Every prompt that calls this (classify/facts/extract/write) asks for a
    JSON *object*; a model that wraps its answer in an array instead used to
    slip through the old `\\[.*\\]` fallback and reach a caller's `.get(...)`
    as a plain list, crashing with `AttributeError: 'list' object has no
    attribute 'get'` (5 records during the 2026-09-13 phase 1b re-research).
    Only ever return a dict or None so that can't happen again; if the model
    answered with a list, take its first dict element instead of discarding
    the whole answer.
    """
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if m:
        try:
            parsed = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, list):
            return next((item for item in parsed if isinstance(item, dict)), None)
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


# ---------------------------------------------------------------------------
# Research fetch — the source text every later step is grounded in
# ---------------------------------------------------------------------------

# A small pool of desktop Chrome UAs. Cycling them keeps the VM crawler from
# being fingerprinted and hard-blocked by yourdaysout / IG / TikTok across the
# many fetches a sweep makes. Every member is Chrome 13x on purpose: IG only
# server-renders the `"caption":{"text":...}` JSON the caption scraper parses
# when it sees a Chrome UA (verified from this VM 2026-09-13; the mini PC's IP
# gets the JS shell, so this VM-side fetch is the only caption source for the
# keyword-search lane).
_BASE_HEADERS = {
    "Accept-Language": "en-IE,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
RESEARCH_UAS = [
    {**_BASE_HEADERS, "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) "
                                     "AppleWebKit/537.36 (KHTML, like Gecko) "
                                     "Chrome/131.0.0.0 Safari/537.36")},
    {**_BASE_HEADERS, "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                     "AppleWebKit/537.36 (KHTML, like Gecko) "
                                     "Chrome/130.0.0.0 Safari/537.36")},
    {**_BASE_HEADERS, "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                     "AppleWebKit/537.36 (KHTML, like Gecko) "
                                     "Chrome/132.0.0.0 Safari/537.36")},
]
# Backward-compatible name used by older discovery/research paths and by
# third-party lane helpers. Keeping one canonical header prevents a missing
# symbol from turning an otherwise recoverable fetch into a lane failure.
RESEARCH_UA = RESEARCH_UAS[0]

def _research_ua():
    """A rotating Chrome UA so repeated fetches aren't all fingerprinted alike."""
    return random.choice(RESEARCH_UAS)

FETCH_GAP_SECS = 4
RESEARCH_MAX_CHARS = 6000
_fetch_clock = {}
_fetch_lock = threading.Lock()

_IG_CAPTION_RE = re.compile(r'"caption"\s*:\s*\{\s*"text"\s*:\s*"((?:[^"\\]|\\.)*)"')
_IG_AUTHOR_RE = re.compile(r'"username"\s*:\s*"([^"]+)"')
_SCRIPT_RE = re.compile(r"<(script|style)\b.*?</\1>", re.S | re.I)


def _throttle(platform):
    """One request per FETCH_GAP_SECS per platform, across every sweep worker."""
    with _fetch_lock:
        wait = FETCH_GAP_SECS - (time.time() - _fetch_clock.get(platform, 0))
        if wait > 0:
            time.sleep(wait)
        _fetch_clock[platform] = time.time()


# Statuses that mean "slow down", not "this URL is bad" -- retry these with
# backoff instead of failing the candidate straight to needs-input.
_RETRYABLE_STATUS = {429, 502, 503}
_BACKOFF_SECS = [3.0, 6.0, 12.0]


MAX_RETRY_AFTER_SECS = 30.0


def _retry_after(error):
    """Honour a server's Retry-After header (seconds) if it sent one, capped
    at MAX_RETRY_AFTER_SECS. Found 2026-09-15: galwaycomedyfestival.ie sent
    "Retry-After: 3600" on a 503, and this was sleeping the full hour,
    literally, inside one candidate's research_fetch -- three attempts in a
    row is up to three hours one URL can hold a worker thread. Since the
    2026-09-14 parallel-research change, the cycle function doesn't return
    until every submitted thread does, so that one URL was silently keeping
    daemon.lock held long after "cycle outcomes" logged, blocking every
    later scheduled or manual trigger with "another cycle is running" for
    hours with no visible error. A slow site should cost this one candidate
    a short wait, never the whole pipeline's ability to run again."""
    headers = getattr(error, "headers", None) or {}
    value = headers.get("Retry-After")
    if value:
        try:
            return min(float(value), MAX_RETRY_AFTER_SECS)
        except ValueError:
            return 0.0
    return 0.0


def _http_text(url, timeout=25, retries=3):
    """Fetch `url` as decoded HTML, retrying transient 429/503/502 responses.

    A single blocked request is the #1 reason candidates fall to
    needs-input "caption" -- retrying with backoff and a rotated UA lets the
    readable detail pages (yourdaysout event pages, etc.) through so the
    four-step pipeline actually has text to write a contract-length description
    from. Hard failures (401/403/404, a genuinely blocked URL) still raise and
    are filed by the caller as needs-input.
    """
    last = None
    for attempt in range(retries + 1):
        try:
            request = urllib.request.Request(url, headers=_research_ua())
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as error:
            last = error
            if error.code in _RETRYABLE_STATUS and attempt < retries:
                wait = _retry_after(error) or _BACKOFF_SECS[attempt]
                log("_http_text %s: HTTP %s -- retry %d/%d after %.0fs" % (
                    urllib.parse.urlparse(url).netloc, error.code,
                    attempt + 1, retries, wait))
                time.sleep(wait)
                continue
            raise
    # Only reachable when a retryable error exhausted its attempts: re-raise
    # the last one. (A success returns inside the loop; a hard error raises.)
    assert last is not None
    raise last


MINI_PC_HOST = os.environ.get("MINI_PC_HOST", "mini-pc")


def _mini_pc_authenticated_fetch(url, timeout=60):
    """Read a page through the mini PC's already-logged-in Chrome session
    (same account fetch.py's discovery lanes use), via SSH to a small bridge
    script there (verify_fetch.py). A bare unauthenticated request from this
    VM gets an Instagram/TikTok anti-bot wall -- a real browser with real
    cookies mostly doesn't. Returns "" (never raises) on any failure: SSH
    unreachable, the shared browser session busy, opencli erroring -- callers
    fall back to their own unauthenticated attempt exactly as before this
    existed (found and wired 2026-09-15, Victor: "I thought using our skill
    was enough" -- it was capable, it just wasn't plumbed into this step)."""
    try:
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", MINI_PC_HOST,
             "cd ~/tools/kidsevents-social && python3 verify_fetch.py " + shlex.quote(url)],
            capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            log(f"mini-pc verify_fetch {url[:70]}: ssh exit {result.returncode}: "
                f"{result.stderr.strip()[:200]}")
            return ""
        data = json.loads(result.stdout.strip().splitlines()[-1]) if result.stdout.strip() else {}
        if data.get("error"):
            log(f"mini-pc verify_fetch {url[:70]}: {data['error']}")
            return ""
        return data.get("text") or ""
    except Exception as error:
        log(f"mini-pc verify_fetch {url[:70]}: {error}")
        return ""


def _instagram_caption(url):
    authenticated = _mini_pc_authenticated_fetch(url)
    if authenticated:
        return authenticated
    html = _http_text(url)
    match = _IG_CAPTION_RE.search(html)
    caption = json.loads('"' + match.group(1) + '"') if match else ""
    author = _IG_AUTHOR_RE.search(html)
    return "\n".join(x for x in (f"Account: {author.group(1)}" if author else "", caption) if x)


def _tiktok_caption(url):
    """TikTok's oEmbed endpoint is keyless and returns the caption as `title`
    -- usually enough on its own, so it goes first; the authenticated bridge
    only runs when oEmbed comes back empty (a private/removed/rate-limited
    video)."""
    data = json.loads(_http_text(
        "https://www.tiktok.com/oembed?" + urllib.parse.urlencode({"url": url})))
    author = data.get("author_name") or ""
    oembed = "\n".join(x for x in (f"Account: {author}" if author else "", data.get("title", "")) if x)
    if oembed:
        return oembed
    return _mini_pc_authenticated_fetch(url)


def _page_text(url):
    html = _SCRIPT_RE.sub(" ", _http_text(url))
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def research_fetch(candidate):
    """Fetch the candidate's own source from THIS VM; return
    `(text, source, missing_field)`.

    `text` is what every later step is grounded in (the stored caption plus
    whatever the live fetch adds); `source` is the `provenance.sources[0]`
    entry. A 401/403/429 is a rate limit, not a bad candidate: it yields
    `missing_field="caption"` (needs-input), never a rejection.
    """
    url = candidate.get("source_url", "")
    caption = (candidate.get("caption") or "").strip()
    if not url:
        return caption, None, "" if caption else "caption"
    host = urllib.parse.urlparse(url).netloc.lower()
    platform = (candidate.get("platform") or host or "web").lower()
    fetched = ""
    try:
        _throttle(platform)
        if "instagram.com" in host:
            fetched = _instagram_caption(url)
        elif "tiktok.com" in host:
            fetched = _tiktok_caption(url)
        else:
            fetched = _page_text(url)
    except urllib.error.HTTPError as error:
        if error.code in (401, 403, 429):
            log(f"research_fetch {host}: HTTP {error.code} (rate limited) — caption needed by hand")
            return caption, None, "" if caption else "caption"
        log(f"research_fetch {url[:70]}: HTTP {error.code}")
    except Exception as error:
        log(f"research_fetch {url[:70]}: {error}")

    text = _merge_text(caption, fetched)
    # Social posts are often short pointers (“visit this farm…”) rather than
    # complete listings. Before parking them for a missing description, use the
    # author/caption as a search key to locate the venue's own page and address.
    # This is especially important for evergreen places: they do not need an
    # event date, but they do need a verifiable location.
    if platform in ("instagram", "tiktok") and len(text.split()) < 120:
        try:
            query = " ".join(x for x in (candidate.get("author", ""), caption[:180], "Ireland") if x)
            for page in web_search(query, limit=2, kind="place"):
                extra = page.get("markdown", "")
                if extra:
                    text = _merge_text(text, extra)
        except Exception as error:
            log(f"social enrichment {host}: {error}")
    source = {"url": url, "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if not text.strip():
        return "", source, "caption"
    return text[:RESEARCH_MAX_CHARS], source, ""


def _merge_text(caption, fetched):
    """The stored caption is usually a truncation of the live one; keep the
    superset instead of feeding the model the same words twice."""
    if not fetched:
        return caption
    if not caption or caption[:60] in fetched:
        return fetched
    return caption + "\n\n" + fetched


# ---------------------------------------------------------------------------
# The four steps — classify, facts, extract, write (at most four model calls
# per candidate, each a small prompt with an explicit JSON contract)
# ---------------------------------------------------------------------------

_NO_INVENTING = ("Use ONLY what the source text below actually says. Do not invent values. "
                 "Leave a field empty rather than guessing it.\n\n")


def classify(text, candidate=None):
    """Step 1 — what is this, is it for families, and where?"""
    author = (candidate or {}).get("author", "")
    prompt = (
        "You sort sources for a family-days-out guide in Ireland.\n\n"
        + _NO_INVENTING
        + (f"Account/author: {author}\n" if author else "")
        + f"Source text:\n<data>{text[:RESEARCH_MAX_CHARS]}</data>\n\n"
        + 'Reply ONLY a JSON object:\n'
        + '{"kind": "event" for something happening on specific dates, "place" for a venue or '
          'activity a family can visit any time, "holiday" for a destination to travel to, '
          'or "none" if it is none of those,\n'
        + 'Important: a social post may describe an evergreen place, attraction, playground, '
          'farm, museum, beach, trail or activity without giving a date. If it names or clearly '
          'describes a visitable location and no specific date is stated, classify it as "place", '
          'not "event". Only use "event" when the source states something happening on a date.\n'
        + '"family_relevant": true only if children can come and it is aimed at or welcoming to '
          'families - false for adult comedy, gigs, club nights, age-gated (16+/18+) events, '
          'trade or adult-only shopping events,\n'
        + '"country": ISO code of where it is - "IE" for the Republic of Ireland, "GB" for '
          'Northern Ireland or Britain, otherwise the real code,\n'
        + '"is_venue_account": true only if the account or author above IS the venue, '
          'organiser or destination itself rather than a visitor posting about it,\n'
        + '"why": at most 120 characters saying why}'
    )
    return extract_obj(hermes(prompt, kind="classify"))


def gather_facts(text):
    """Step 2 — the grounding. Every fact must quote the source verbatim, and a
    quote that is not actually in the text is dropped here, in code: that is
    what stops the later steps inventing details. Returns (facts, name)."""
    prompt = (
        "You pull quotable facts out of a source for a family-days-out guide.\n\n"
        + _NO_INVENTING
        + f"Source text:\n<data>{text[:RESEARCH_MAX_CHARS]}</data>\n\n"
        + 'Reply ONLY a JSON object:\n'
        + '{"facts": [{"claim": what the source establishes, "quote": the exact words from the '
          'source that establish it, copied character for character}] (at most 12),\n'
        + '"name": the venue, organiser or destination name as the source writes it, or ""}'
    )
    obj = extract_obj(hermes(prompt, kind="facts")) or {}
    haystack = _fold_spaces(text)
    facts = []
    for fact in (obj.get("facts") or [])[:12]:
        if not isinstance(fact, dict):
            continue
        quote = str(fact.get("quote") or "").strip()
        if quote and _fold_spaces(quote) in haystack:
            facts.append({"claim": str(fact.get("claim") or "")[:300], "quote": quote[:300]})
    return facts, str(obj.get("name") or "")[:200]


def _fold_spaces(text):
    return re.sub(r"\s+", " ", str(text or "")).strip().casefold()


_EXTRACT_FIELDS = {
    "event": (
        '{"start_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD" or "", "times": ["HH:MM"], '
        '"recurrence": e.g. "every Saturday" or "", "organizer": "", '
        '"booking_required": "required|recommended|none", "booking_url": "", '
        '"price_detail": exactly what the source says it costs, or "", '
        '"date_evidence": the quote from the verified facts above that states the date, '
        'copied exactly, "cancelled": true only if the source says it is cancelled}'
    ),
    "place": (
        '{"opening_hours": "", "duration_hint": how long a visit takes, or "", '
        '"seasonal_note": "", "price_detail": exactly what the source says it costs, or "", '
        '"booking_url": ""}'
    ),
    "holiday": (
        '{"destination_type": "city|resort|region|park|island", "holiday_types": [], '
        '"best_seasons": [], "best_months": [], "school_breaks": [], '
        '"flight_time_from_dublin": "none|under-2h|2-4h|4-8h|8h-plus", '
        '"direct_flight": true/false/null, "budget_band": "budget|mid|premium|luxury", '
        '"with_baby_toddler": true/false/null, "includes": [], "price_detail": ""}'
    ),
}


def extract_details(kind, text, facts, location_hint=""):
    """Step 3 — the kind-specific fields. Every value must be traceable to one
    of the verified quotes; the gate re-checks `date_evidence` against them."""
    prompt = (
        f"You are filling in the {kind} fields of a family-days-out listing.\n"
        f"Today is {date.today().isoformat()}.\n\n"
        + _NO_INVENTING
        + "Verified quotes from the source (the only evidence you may use):\n"
        + json.dumps(facts, ensure_ascii=False)[:4000] + "\n\n"
        + (f"Location context: {location_hint}\n" if location_hint else "")
        + f"Source text:\n<data>{text[:RESEARCH_MAX_CHARS]}</data>\n\n"
        + "Reply ONLY a JSON object:\n"
        + _EXTRACT_FIELDS[kind]
        + '\nplus "name", "address", "city", "county" (the Irish county, spelled out), '
          '"country" (ISO code), "lat" and "lon" (numbers, only if the source states them).'
    )
    return extract_obj(hermes(prompt, kind="extract")) or {}


def write_copy(kind, text, facts, name=""):
    """Step 4 — the words a parent reads, plus the taxonomy, chosen from the
    vocabulary `gate.facet_gloss()` renders and validated by `gate.gate_meta`."""
    band = "120 to 300 words" if kind in ("place", "holiday") else "60 to 200 words"
    prompt = (
        "You write listings for Small Days, a family-days-out guide in Ireland. "
        "Plain, warm and factual; no marketing language.\n\n"
        + _NO_INVENTING
        + (f"Name: {name}\n" if name else "")
        + "Verified quotes from the source (the only evidence you may use):\n"
        + json.dumps(facts, ensure_ascii=False)[:4000] + "\n\n"
        + f"Source text:\n<data>{text[:RESEARCH_MAX_CHARS]}</data>\n\n"
        + 'Reply ONLY a JSON object:\n'
        + '{"title": a synthesised name, at most 80 characters, never the caption or the first '
          'words of it,\n'
        + '"summary": one line, at most 160 characters,\n'
        + f'"description": {band}, using only the facts above,\n'
        + '"taxonomy": an object using EXACTLY these fields and these allowed values:\n'
        + gate.facet_gloss(kind) + "}"
    )
    return extract_obj(hermes(prompt, kind="write")) or {}


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
        rows = []
        for x in data.get("results", []):
            url, title = x.get("url"), x.get("title", "")
            if not url or _JUNK_URL_RE.search(url) or _LOW_SIGNAL_TEXT_RE.search(f"{title} {url}"):
                continue
            rows.append({"url": url, "title": title, "source": f"gw:{x.get('engine', '')}"})
        return rows[:limit]
    except Exception as e:
        log(f"searchgw unreachable: {e}")
        return []


_DISCOVERY_SOURCES = [_src_gateway]

_JUNK_URL_RE = re.compile(
    r"[Ss]pecial:|[?&]search=|/search\?|facebook.com/(login|recover)|"
    r"/(?:recipes?|calculators?|apps?|advice|tips|pregnancy|quotes?|poems?|"
    r"indoor-activities|activities-for-kids)(?:[/\?#]|$)|"
    r"(?:nhs\.uk|abcmouse\.com|booking\.com)/"
)
_LOW_SIGNAL_TEXT_RE = re.compile(
    r"\b(?:recipe|recipes|cake ideas|mobile apps?|pregnancy|parenting advice|"
    r"quotes?|poem|calculator|indoor activities?|rainy[- ]day ideas)\b",
    re.IGNORECASE,
)


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


def _crawl_pages(discovered, limit, skip_chrome_filter=False,
                 max_lines=220, max_chars=4000):
    """crawl4AI extraction. WanderTold pattern.

    The line and character caps are the prompt budget for a page that IS the
    item. A listing page is not: its links start well past them (a
    yourdaysout county page spends its first 4,000 characters on a cookie
    consent notice and reaches its first real link at character 27,836 --
    measured 2026-09-13), so the discovery lane that harvests those links asks
    for a bigger slice.
    """
    from crawl4ai import AsyncWebCrawler
    if not discovered:
        return []
    out = []
    seen = set()
    try:
        import psutil
        children_before = {p.pid for p in psutil.Process(os.getpid()).children(recursive=True)}
    except Exception:
        psutil = None
        children_before = set()
    timed_out = False
    try:
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            async def extract():
                async with AsyncWebCrawler() as crawler:
                    gate = asyncio.Semaphore(CRAWL_PAGE_CONCURRENCY)
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
                                text = os.linesep.join(text.splitlines()[:max_lines]).strip()
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
                                    entry = {"url": item["url"], "title": item.get("title", ""), "markdown": text[:max_chars]}
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
            timed_out = True
        finally:
            loop.close()
            if timed_out and psutil is not None:
                current = [p for p in psutil.Process(os.getpid()).children(recursive=True)
                           if p.pid not in children_before]
                for proc in current:
                    try:
                        proc.terminate()
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
                _, alive = psutil.wait_procs(current, timeout=3)
                for proc in alive:
                    try:
                        proc.kill()
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
                if current:
                    log(f"crawl4ai timeout reaped {len(current)} child process(es)")
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


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

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
    if event.get("schema_version") == 1:
        return publish_record(event)
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
            dates = contract.legacy_view(event)
            when = (dates.get("end_date") or dates.get("start_date") or "").strip()
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


def publish_place(place, caption=""):
    """Append a place to places_output.json (year-round local activities/
    venues -- its own section, separate from the curated Holidays
    destinations), deduped by title+location. Returns True if newly written,
    False if the quality gate rejected it or it was already there.
    """
    if place.get("schema_version") == 1:
        return publish_record(place)
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


# ---------------------------------------------------------------------------
# Contract records: one store per kind, one writer
# ---------------------------------------------------------------------------

STORE_FOR_KIND = {"event": OUTPUT_FILE, "place": PLACES_FILE, "holiday": HOLIDAYS_FILE}


def on_air_titles(kind):
    """Titles already published in this kind's catalogue, for the duplicate
    fold. Reads both shapes, so it works mid-migration."""
    return [contract.legacy_view(record).get("title", "")
            for record in load_json_store(STORE_FOR_KIND[kind], [])]


def publish_record(record):
    """Append an on-air contract record to its catalogue, deduped by id. The
    single writer for contract records -- the admin approve route, the sweep
    and the discovery cycle all come through here, so they all pass the same
    gate and share one atomic, locked read-modify-write."""
    if record.get("status") != "on-air":
        log(f"refusing to publish {record.get('id', '')!r}: status is {record.get('status')!r}")
        return False
    store = STORE_FOR_KIND[record["kind"]]
    with output_lock():
        records = load_json_store(store, [])
        if any(other.get("id") == record["id"] for other in records):
            return False
        records.append(record)
        records.sort(key=lambda one: contract.legacy_view(one).get("start_date") or "")
        write_json_atomic(store, records)
    return True


def _coord(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _either(first, second):
    """`first` unless it is empty -- and 0.0 is a real latitude, so `or` will
    not do."""
    return second if first in (None, "") else first


def _safe_url(value):
    url = str(value or "").strip()
    return url if url.startswith(("http://", "https://")) else ""


def _country_code(value):
    """A real ISO code when the model gave one (holidays are abroad), folded
    onto IE/GB/other otherwise."""
    raw = str(value or "").strip()
    if re.fullmatch(r"[A-Za-z]{2}", raw):
        return raw.upper()
    return normalize_country(raw)


def _build_record(kind, candidate, source, facts, name, details, written, verdict):
    """Assemble one contract record out of the four steps' answers. Nothing is
    invented here: every value either came from a step or is derived by
    `contract.derive`, and the taxonomy is whatever survives `gate_meta`."""
    record = contract.EMPTY_RECORD(kind)
    record["title"] = str(written.get("title") or "").strip()[:contract.MAX_TITLE]
    record["summary"] = str(written.get("summary") or "").strip()[:contract.MAX_SUMMARY]
    record["description"] = str(written.get("description") or "").strip()
    # Same trust-the-lane rule as the kind override in `promote()`: a
    # candidate the lane already vetted (holidays_seed only ever seeds real
    # family destinations) keeps `family_relevant` true regardless of
    # classify()'s guess. Without this, gate.qa()'s own `family_relevant`
    # check (gate.py, separate from promote()'s) still rejected a trusted
    # holiday on the exact same Ireland-first misreading -- the promote()
    # fix alone stopped the early rejection but this field fed the later one.
    trusted_kind = str(candidate.get("kind_hint") or "").strip().lower() in contract.KINDS
    record["family_relevant"] = trusted_kind or verdict.get("family_relevant") is not False

    # A discovery lane that read the location out of open data (OSM, Wikidata,
    # a council CSV) knows it better than any model reading prose: its
    # `location` is the fallback for everything the extract step left empty.
    known = candidate.get("location") or {}
    city, county = normalize_location(details.get("city") or known.get("city", ""),
                                      details.get("county") or known.get("county", ""))
    location = record["location"]
    location["name"] = str(details.get("name") or name or known.get("name") or "")[:200]
    location["address"] = str(details.get("address") or known.get("address") or "")[:300]
    location["city"] = city
    location["county"] = county
    location["country"] = _country_code(details.get("country") or known.get("country")
                                        or verdict.get("country"))
    location["lat"] = _coord(_either(details.get("lat"), known.get("lat")))
    location["lon"] = _coord(_either(details.get("lon"), known.get("lon")))

    url = candidate.get("source_url", "")
    links = record["links"]
    links["source_url"] = url
    links["official_url"] = _safe_url(details.get("official_url") or details.get("website"))
    links["booking_url"] = _safe_url(details.get("booking_url"))
    if "instagram.com" in url:
        links["instagram_url"] = url
    elif "tiktok.com" in url:
        links["tiktok_url"] = url

    taxonomy, dropped = gate.gate_meta({**(written.get("taxonomy") or {}),
                                        "price_detail": details.get("price_detail")
                                        or (written.get("taxonomy") or {}).get("price_detail", "")})
    record["taxonomy"] = taxonomy

    if kind == "event":
        record["event"].update({
            "start_date": str(details.get("start_date") or "")[:10],
            "end_date": str(details.get("end_date") or details.get("start_date") or "")[:10],
            "times": [str(t) for t in (details.get("times") or [])][:6],
            "recurrence": str(details.get("recurrence") or "")[:120],
            "organizer": str(details.get("organizer") or "")[:200],
            "booking_required": str(details.get("booking_required") or "")[:20],
            "date_evidence": str(details.get("date_evidence") or "")[:300],
            "cancelled": details.get("cancelled") is True,
        })
    elif kind == "place":
        record["place"].update({
            "opening_hours": str(details.get("opening_hours") or "")[:300],
            "duration_hint": str(details.get("duration_hint") or "")[:120],
            "seasonal_note": str(details.get("seasonal_note") or "")[:300],
        })
    else:
        holiday, holiday_dropped = gate.gate_holiday({**details, **(written.get("taxonomy") or {})})
        record["holiday"].update(holiday)
        dropped += holiday_dropped

    # A holiday must be grounded in two independent domains (gate._qa_holiday),
    # which one fetch can never supply -- lane 8 researches both and hands them
    # over on the candidate.
    sources = [source] if source else []
    known_urls = {one.get("url") for one in sources}
    for extra in candidate.get("sources") or []:
        if extra.get("url") and extra["url"] not in known_urls:
            known_urls.add(extra["url"])
            sources.append(extra)

    record["provenance"].update({
        "sources": sources,
        "facts": [{**fact, "source_url": url} for fact in facts],
        "last_checked": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "produced_by": ["classify", "facts", "extract", "write"],
    })
    gate.record_drops(dropped)
    return contract.derive(record)


# Every field name `gate.qa()`'s `_needs(...)` calls can name, and where it
# lives on a record. `facts` and `sources` append rather than overwrite --
# a curator adding a second source domain, or confirming a date they can see
# on the page, does not erase what a fact-checked run already found.
PATCHABLE_FIELDS = {
    "title": ("scalar", ["title"]),
    "summary": ("scalar", ["summary"]),
    "description": ("scalar", ["description"]),
    "county": ("scalar", ["location", "county"]),
    "address": ("scalar", ["location", "address"]),
    "country": ("scalar", ["location", "country"]),
    "start_date": ("scalar", ["event", "start_date"]),
    "date_evidence": ("scalar", ["event", "date_evidence"]),
    "price_band": ("scalar", ["taxonomy", "price_band"]),
    "age_bands": ("list", ["taxonomy", "age_bands"]),
    "activity_types": ("list", ["taxonomy", "activity_types"]),
    "holiday_types": ("list", ["holiday", "holiday_types"]),
    "best_seasons": ("list", ["holiday", "best_seasons"]),
    "facts": ("fact", None),
    "sources": ("source", None),
}


def _set_path(record, path, value):
    node = record
    for key in path[:-1]:
        node = node.setdefault(key, {})
    node[path[-1]] = value


def _as_list(value):
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


def apply_patch(record, patch):
    """A curator's fix for exactly the field `missing_field` named -- never a
    value the curator didn't type. `facts` is the one place a human, not a
    source page, supplies a "fact": it is stamped `curator-confirmed` rather
    than pretending it is a verbatim quote from the original page."""
    for field, value in (patch or {}).items():
        kind_of_field = PATCHABLE_FIELDS.get(field)
        if not kind_of_field or value in (None, ""):
            continue
        shape, path = kind_of_field
        if shape == "scalar":
            _set_path(record, path, str(value).strip())
        elif shape == "list":
            _set_path(record, path, _as_list(value))
        elif shape == "fact":
            record.setdefault("provenance", {}).setdefault("facts", []).append(
                {"claim": "curator-confirmed", "quote": str(value).strip()[:300]})
        elif shape == "source":
            record.setdefault("provenance", {}).setdefault("sources", []).append(
                {"url": str(value).strip(), "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    return record


def patch_and_regate(record, patch):
    """Apply a curator's patch to a needs-input record and ask the gate again.
    No re-fetch happens here (`sources_text=""`): a date_evidence quote that
    was already verified against the real page when the record was first
    built stays verified; a new one only a curator typed cannot be re-checked
    against a page this function never sees, which is exactly why `facts`
    patches are stamped `curator-confirmed` instead of a plain quote."""
    apply_patch(record, patch)
    contract.derive(record)
    return gate.qa(record, "", on_air_titles(record["kind"]))


def publish_needs_input_record(record):
    """A patched record that just passed the gate: publish it and say so, the
    same status the four-step pipeline gives a record on its first pass."""
    record["status"] = "on-air"
    record["reason"] = ""
    return publish_record(record)


def promote(candidate, hint="", prefetched=None, prefill=None):
    """Turn one candidate into a publishable contract record.

    Returns `(record|None, reason, missing_field)`. A truthy `missing_field`
    means one nameable thing is missing and a curator's note could fix it
    (needs-input); an empty one with no record means rejected.

    `hint` is a curator's note from the admin Social page ("it's the
    playground on Main St, every Saturday") -- folded into the source text
    before any step runs, same do-not-invent discipline either way.
    `prefetched` skips the research fetch (the discovery cycle already has the
    page text); `prefill` supplies `event.*` from schema.org JSON-LD, which
    skips the extract step. For a place or a holiday `prefill` instead overlays
    the values a discovery lane computed rather than read (a destination's
    climate months, its flight time from Dublin) on top of what the extract
    step found. At most four model calls, one per step.
    """
    if prefetched is None:
        text, source, missing = research_fetch(candidate)
    else:
        text, source, missing = prefetched, {
            "url": candidate.get("source_url", ""),
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }, ""
    if hint:
        text = f"{text}\n\nAdditional context from a curator: {hint}".strip()
        missing = ""
    if not text.strip():
        return None, ("No caption or page text could be read from this post (a rate-limited "
                      "fetch) -- nothing to classify from. Add a note below with what it's "
                      "about."), missing or "caption"

    # A source whose entire fetched text is shorter than the smallest contract
    # description floor cannot satisfy MIN_DESCRIPTION_WORDS for ANY kind without
    # the write step inventing facts, which _NO_INVENTING forbids -- so the gate
    # would reject it on `description` no matter what. That wastes the four model
    # calls (classify/facts/extract/write) below plus the research fetch. Park it
    # as needs-input and skip the steps. A real curator note (folded into `text`
    # above via `hint`) can still push a candidate over the floor, so this is
    # recoverable -- needs-input, not rejected.
    _source_words = len(text.split())
    _min_description = min(contract.MIN_DESCRIPTION_WORDS.values())
    is_social = str(candidate.get("platform", "")).lower() in ("instagram", "tiktok")
    if _source_words < _min_description and not is_social:
        return None, (
            "Source text is only %d words, below the %d-word minimum the contract "
            "requires for a description (events %d, places/holidays %d). The write "
            "step uses only the facts above and cannot invent them -- add a richer "
            "source link or a curator note with the missing details below."
        ) % (
            _source_words, _min_description,
            contract.MIN_DESCRIPTION_WORDS["event"],
            contract.MIN_DESCRIPTION_WORDS["place"],
        ), "description"

    verdict = classify(text, candidate)
    if not verdict:
        return None, "no model lane answered the classify step", ""
    why = str(verdict.get("why") or "").strip()[:120]
    # A lane that already knows what its own candidate is (holidays_seed hands
    # a curated destination's own Wikivoyage/Wikidata pages, always a holiday)
    # beats a free-text guess: found 2026-09-14, a classify call read a
    # destination's Wikivoyage article as "place" and the on-island-of-Ireland
    # gate then rejected Benidorm for being in Spain -- correct for a real
    # place, wrong for a hinted holiday the model misread.
    kind_hint = str(candidate.get("kind_hint") or "").strip().lower()
    kind = kind_hint if kind_hint in contract.KINDS else str(verdict.get("kind") or "").strip().lower()
    if kind not in contract.KINDS:
        return None, f"not an event, place or holiday: {why or kind or 'no verdict'}", ""
    # Same reasoning as the kind override just above: a candidate the lane
    # already vetted for family relevance (holidays_seed only ever seeds
    # "destinations Irish families actually travel to with children") should
    # not be re-litigated by a classify prompt that is written Ireland-first
    # and, found immediately after the kind fix above, reads "not Ireland" as
    # "not family-relevant" for a holiday abroad.
    if not kind_hint and verdict.get("family_relevant") is False:
        return None, f"not family-relevant: {why}", ""

    facts, name = gather_facts(text)
    # An empty answer from a step is a lane outage, not a verdict: `extract_obj`
    # returns None for "" and every step falls back to {}. Saying so here is
    # what keeps the discovery ledger honest -- a write step that never
    # answered would otherwise be filed as needs-input "no title", stamped, and
    # not looked at again for days, when all that happened was a busy night.
    if prefill and kind == "event":
        details = dict(prefill)
    else:
        details = extract_details(kind, text, facts, candidate.get("county", ""))
        if not details and not prefill:
            return None, "no model lane answered the extract step", ""
        details.update({k: v for k, v in (prefill or {}).items() if v not in (None, "", [])})
    written = write_copy(kind, text, facts, name)
    if not written:
        return None, "no model lane answered the write step", ""

    record = _build_record(kind, candidate, source, facts, name, details, written, verdict)
    problems = contract.validate(record)
    if problems:
        return None, "record does not match the contract: " + "; ".join(problems[:3]), ""

    ok, reason, missing_field = gate.qa(record, text, on_air_titles(kind))
    if not ok:
        record["status"] = "needs-input" if missing_field else "rejected"
        record["reason"] = reason
        return None, reason, missing_field
    if kind == "event":
        window = date_window_reason(contract.legacy_view(record), date.today(),
                                    date.today() + timedelta(days=EVENT_HORIZON_DAYS))
        if window:
            return None, window, ""
    attach_links_and_media(record, candidate, verdict)
    record["status"] = "on-air"
    return record, "", ""


def attach_links_and_media(record, candidate, verdict):
    """Resolve the record's outbound links and give it a hero photo and its
    embeds (phase 4). Imported here, not at module level: both modules import
    this one. Runs on records that have already passed the gate -- a rejected
    record is not worth a Commons search -- and never blocks publication: a
    listing with no photo and no Instagram is still a listing."""
    import links
    import media

    for step, run in (("links", lambda: links.resolve(record, candidate, verdict)),
                      ("media", lambda: media.attach(record))):
        try:
            run()
        except Exception as error:
            log(f"{step} {record.get('id', '')}: {error}")


def promote_candidate(candidate, hint=""):
    """`(kind, record, reason)` -- the three-tuple form of `promote()` kept for
    callers that only need the verdict, not the missing field."""
    record, reason, _missing_field = promote(candidate, hint=hint)
    return (record["kind"] if record else None), record, reason


def _jsonld_prefill(event, city):
    """schema.org gives the dates as machine-readable markup, so the extract
    step has nothing to add. The evidence quote is the markup itself, which is
    appended to the grounded text so the gate can verify it like any other."""
    return {
        "start_date": event.get("start", ""),
        "end_date": event.get("end", "") or event.get("start", ""),
        "name": event.get("venue", ""),
        "city": city,
        "price_detail": event.get("price", ""),
        "official_url": event.get("url", ""),
        "date_evidence": _jsonld_evidence(event),
    }


def _jsonld_evidence(event):
    return f"schema.org startDate {event.get('start', '')}"


def load_state():
    return load_json_store(STATE_FILE, {"fails": {}, "last_run": None, "sources_hit": {}})


def save_state(state):
    write_json_atomic(STATE_FILE, state)


def record_llm_stats():
    """Fold today's routing.jsonl into factory_state.json so the admin
    Production view shows which lanes actually answered. Called at the end of
    a cycle and of a sweep; under the lock because both can be running."""
    with output_lock():
        state = load_state()
        state["llm"] = llm.stats()
        save_state(state)


# One cycle researches at most this many candidates. The lanes find far more
# than that -- the ledger and this cap are what keep an hourly timer from
# turning a good discovery run into a queue nothing ever drains.
MAX_CANDIDATES_PER_CYCLE = int(ENV.get("MAX_CANDIDATES_PER_CYCLE", "60"))
# Research stops at this fraction of the cycle wall so the state write at the
# end always happens; the rest of the desk is picked up by the next cycle.
_RESEARCH_SHARE = 0.8
# `promote()` is four sequential free-model calls per candidate, 60-90s each
# -- researching one candidate at a time (found 2026-09-14, Victor: "your
# pace is ridiculous") meant a cycle could only ever get through a couple
# dozen candidates before the wall, no matter how many the discovery lanes
# found. Candidates are independent (each writes its own record; the shared
# stores are already lock-protected), so `run_discovery_cycle` now researches
# `DISCOVERY_WORKERS` at a time, the same pattern `staging.sweep_pending()`
# already uses for its own backlog.
DISCOVERY_WORKERS = int(ENV.get("DISCOVERY_WORKERS", "6"))
MAX_ITEM_SECS = int(ENV.get("MAX_ITEM_SECS", "300"))


def _promote_within_budget(item):
    """`promote()` on its own thread with a hard wall -- a worker thread
    cannot be killed, so a call stuck past `MAX_ITEM_SECS` is abandoned (it
    still finishes and publishes under the usual lock if it ever returns)
    while the pool moves on to the next candidate."""
    runner = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        return runner.submit(promote, item, prefetched=item.get("text"),
                              prefill=item.get("prefill")).result(timeout=MAX_ITEM_SECS)
    finally:
        runner.shutdown(wait=False)
# The desk and the ledger name the same verdict differently on purpose: the
# desk speaks `staging.py`'s vocabulary, so one admin view can render both
# candidate files, while the ledger records the contract's own word.
_DESK_STATUS = {"on-air": "approved", "needs-input": "needs_input",
                "rejected": "rejected", "lane-failed": "needs_review"}


def run_discovery_cycle(budget=None, only=None):
    """One factory cycle: run the discovery lanes, stage what they found, and
    research each new candidate through `promote()`.

    Returns the outcome counts. `lane-failed` is not a rejection -- it is "no
    model lane answered", which the ledger deliberately does not stamp, so the
    candidate comes back next cycle when the free lanes have recovered.
    """
    import discovery
    from discovery import common as candidates, ledger

    prune_past_events()
    found, rows, cursors, ran, calls = discovery.run_all(
        budget or MAX_CANDIDATES_PER_CYCLE, load_state(), only=only)
    staged = candidates.append_candidates(found)
    log(f"discovery: {len(found)} candidates from {len(ran)} lanes, "
        f"{len(staged)} to research (new, plus any due for a recheck)")

    outcomes = {"on-air": 0, "needs-input": 0, "rejected": 0, "lane-failed": 0}
    deadline = time.time() + CYCLE_WALL_SECS * _RESEARCH_SHARE
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=DISCOVERY_WORKERS) as pool:
        futures = {}
        for item in staged:
            if time.time() > deadline:
                log(f"cycle wall reached after submitting {len(futures)} candidates — the rest wait for the next run")
                break
            futures[pool.submit(_promote_within_budget, item)] = item
        for future in concurrent.futures.as_completed(futures):
            item = futures[future]
            done += 1
            try:
                record, reason, missing_field = future.result()
            except concurrent.futures.TimeoutError:
                record, reason, missing_field = None, f"timed out after {MAX_ITEM_SECS}s", ""
            if record and publish_record(record):
                status, kind = "on-air", record["kind"]
            elif record:
                status, kind = "rejected", record["kind"]
                reason = "already published under this id"
            elif reason.startswith("no model lane answered"):
                status, kind = "lane-failed", item.get("kind_hint", "")
            else:
                status = "needs-input" if missing_field else "rejected"
                kind = item.get("kind_hint", "")
            outcomes[status] += 1
            ledger.record(item["source_url"], kind, status)
            candidates.set_candidate_status(item["source_url"], _DESK_STATUS[status],
                                            reason, missing_field)
            log(f"  [{done}/{len(futures)}] {status} — {item['source_url'][:70]} {reason[:60]}")

    # Re-read under the lock rather than saving the copy loaded above: a
    # concurrent sweep's record_llm_stats() and gate.record_drops() write the
    # same file while a cycle runs, and a stale write here would erase them.
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with output_lock():
        state = load_state()
        state["lanes"] = rows
        state.setdefault("lane_cursor", {}).update(cursors)
        state.setdefault("lane_last_run", {}).update({name: now for name in ran})
        state.setdefault("lane_calls", {}).update(calls)
        state["discovery"] = {"found": len(found), "staged": len(staged), "outcomes": outcomes}
        state["last_run"] = now
        state["total_events"] = len(load_json_store(OUTPUT_FILE, []))
        state["llm"] = llm.stats()
        save_state(state)
    log(f"cycle outcomes: {outcomes}")
    return outcomes


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
    parser.add_argument("--lane", action="append", default=None,
                        help="Run only this discovery lane (repeatable)")
    parser.add_argument("--budget", type=int, default=None,
                        help=f"Candidates to research this cycle (default {MAX_CANDIDATES_PER_CYCLE})")
    args = parser.parse_args()

    # The hourly timer fires whether or not the previous cycle finished.
    # Skipping (not queueing) is the WanderTold pattern: a cycle that is still
    # running is doing the same work this one would.
    CYCLE_LOCK_FILE.touch(exist_ok=True)
    cycle_lock = open(CYCLE_LOCK_FILE, "w")
    try:
        fcntl.flock(cycle_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("previous cycle still running — skipping this run")
        return
    # Nothing inside a cycle is allowed to hang the hourly timer for ever; every
    # store write is atomic and locked, so dying here cannot corrupt one.
    watchdog = threading.Timer(CYCLE_WALL_SECS, lambda: (
        log(f"cycle exceeded CYCLE_WALL_SECS={CYCLE_WALL_SECS} — aborting"), os._exit(2)))
    watchdog.daemon = True
    watchdog.start()
    try:
        outcomes = run_discovery_cycle(budget=args.budget, only=args.lane)
        log(f"Discovery cycle complete: {outcomes['on-air']} published")
    finally:
        watchdog.cancel()
        fcntl.flock(cycle_lock, fcntl.LOCK_UN)
        cycle_lock.close()


if __name__ == "__main__":
    main()
