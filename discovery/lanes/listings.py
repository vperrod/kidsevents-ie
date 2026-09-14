#!/usr/bin/env python3
"""Lane 1 -- the curated deep listing pages, every county.

`sources.json` now carries a yourdaysout slug for each of the 26 Republic
counties plus the hand-picked listing pages for the cities and a `_national`
block; this lane rotates through them so every page is crawled within a few
cycles rather than the same five every hour.

A crawled page yields candidates two ways. schema.org `Event` markup is
machine-readable, so those become event candidates with their dates pre-filled
and the markup quoted as the date evidence. A page with no markup is reduced
in code to its own link index -- title plus URL, chrome dropped -- and one
model call picks at most `split_max` of those links; each pick becomes a
candidate `promote()` researches on its own page. If nothing is picked (a
rate-limited lane, or a page that is genuinely one item) the page itself is
the candidate and the classify step decides what it is.

The link index exists because a listing page's content is not where a page's
content usually is: `_crawl_pages`' default 4,000-character budget on a
yourdaysout county page contains nothing but a cookie consent notice, and the
first real link sits at character 27,836. Crawling wide and reducing to links
is both cheaper and more accurate than feeding a model more prose.
"""

import re
import urllib.parse
from datetime import date, timedelta

import contract
import factory_worker
from discovery import common

# A listing page's links live well past the prompt budget a single-item page
# needs, so it is crawled wide and then reduced to its link index before any
# model sees it.
_CRAWL_LINES = 1500
_CRAWL_CHARS = 60000
_MAX_LINKS = 120
_MD_LINK_RE = re.compile(r"\[([^\]]{0,120})\]\((https?://[^)\s]+)")
_CHROME_URL_RE = re.compile(
    r"/(auth|login|signin|sign-in|register|account|cart|basket|privacy|terms|"
    r"cookie|contact|about|newsletter|feed|rss|tag|category|categories|author|"
    r"wp-content|wp-json)\b|\.(png|jpe?g|gif|svg|webp|pdf|ico|css|js)(\?|$)", re.I)


def _listing_urls():
    """Every curated listing URL, tagged with the county it belongs to."""
    out = []
    for key, block in sorted(factory_worker._load_city_sources().items()):
        county = contract.normalize_county(key.lstrip("_"))
        for category, value in sorted(block.items()):
            for url in (value if isinstance(value, list) else [value]):
                if str(url).startswith("http"):
                    out.append({"url": url, "county": county, "key": key})
    return out


def _page_key(page_url, fallback):
    return urllib.parse.urlparse(page_url).netloc.removeprefix("www.") or fallback


def link_index(markdown, page_url):
    """The page's own links, as `[{title, url}]`.

    Sending a model 40,000 characters of listing page is neither affordable nor
    necessary: what a listing offers is links, and a link index of a hundred
    titles fits any lane's context. Obvious chrome (sign-in, cookie policy,
    category hubs, images) is dropped in code, so the model only ever judges
    plausible items.
    """
    host = urllib.parse.urlparse(page_url).netloc.removeprefix("www.")
    seen, out = set(), []
    for title, url in _MD_LINK_RE.findall(markdown or ""):
        url = url.rstrip(".,);\"'")
        # The host has to be the link's host: "familyfun.ie" is also a substring
        # of instagram.com/familyfun.ie/.
        if urllib.parse.urlparse(url).netloc.removeprefix("www.") != host:
            continue
        if url in seen or _CHROME_URL_RE.search(url):
            continue
        if url.rstrip("/") == page_url.rstrip("/"):
            continue
        title = re.sub(r"\s+", " ", title).strip()
        if not title or len(title) < 3:
            continue
        seen.add(url)
        out.append({"title": title[:120], "url": url})
        if len(out) >= _MAX_LINKS:
            break
    return out


def _split_listing(links, page_url, limit):
    """One model call: which of the page's links are things a family can go to."""
    if not links:
        return []
    numbered = "\n".join(f"{i}. {one['title']} -> {one['url']}"
                         for i, one in enumerate(links, 1))
    prompt = (
        "You read the link index of a what's-on page from an Irish family "
        "activities site.\n\n"
        "Pick ONLY links that are a single event, venue or activity a family "
        "could go to. Reject category and tag hubs, listings of listings, "
        "adverts, accounts, and anything that is not a thing to go to.\n\n"
        f"Page: {page_url}\n"
        f"Links:\n<data>{numbered[:8000]}</data>\n\n"
        "Reply ONLY a JSON object:\n"
        f'{{"picks": [the numbers of at most {limit} links, best first]}}'
    )
    answer = factory_worker.extract_obj(
        factory_worker.hermes(prompt, kind="listing-split")) or {}
    picked = []
    for number in (answer.get("picks") or [])[:limit]:
        try:
            picked.append(links[int(number) - 1])
        except (TypeError, ValueError, IndexError):
            continue
    return picked


def run(state):
    config = state["config"]
    urls = _listing_urls()
    if not urls:
        return []
    per_cycle = min(int(config.get("pages_per_cycle", 10)), len(urls))
    start = int((state["factory_state"].get("lane_cursor") or {}).get("listings", 0)) % len(urls)
    picked = [urls[(start + i) % len(urls)] for i in range(per_cycle)]
    state["cursor"] = (start + per_cycle) % len(urls)

    pages = factory_worker._crawl_pages(
        [{"url": one["url"]} for one in picked], len(picked), skip_chrome_filter=True,
        max_lines=_CRAWL_LINES, max_chars=_CRAWL_CHARS) or []
    county_of = {one["url"]: one["county"] for one in picked}
    fetched = {page["url"] for page in pages}
    for one in picked:
        if one["url"] not in fetched:
            state["errors"].append((_page_key(one["url"], one["key"]), "page did not crawl"))

    today = date.today()
    horizon = today + timedelta(days=factory_worker.EVENT_HORIZON_DAYS)
    split_max = int(config.get("split_max", 10))
    out = []
    for page in pages:
        county = county_of.get(page["url"], "")
        key = _page_key(page["url"], "listing")
        markdown = page.get("markdown") or ""
        # One page at a time, so an event's county is the county of the page it
        # was actually marked up on rather than a guess from its own URL.
        marked_up = factory_worker._extract_jsonld_events([page["url"]], today, horizon)
        if marked_up:
            out.extend(_from_jsonld(marked_up, page, county, key))
            continue
        links = link_index(markdown, page["url"])
        try:
            items = _split_listing(links, page["url"], split_max)
        except Exception as error:
            state["errors"].append((key, f"split failed: {error}"))
            items = []
        detail = [one for one in (_from_split(item, page, county, key) for item in items)
                  if one]
        if detail:
            out.extend(detail)
        else:
            # No markup, nothing split out: the page itself is the candidate and
            # the classify step decides what it is. Worth seeing in the Sources
            # area -- a hub page that never splits is one it keeps rejecting.
            state["errors"].append(
                (key, f"nothing split out of {len(links)} links on the listing"))
            out.append(common.candidate("listings", key, page["url"],
                                        text=markdown[:factory_worker.RESEARCH_MAX_CHARS],
                                        county=county, kind_hint="event"))
    return out


def _from_jsonld(events, page, county, key):
    """schema.org events on a listing page: dates already machine-readable."""
    out = []
    for event in events:
        url = event.get("url") or page["url"]
        prefill = factory_worker._jsonld_prefill(event, "")
        prefill["county"] = county
        out.append(common.candidate(
            "listings", key, url, title=event.get("title", ""),
            caption=_jsonld_caption(event), county=county,
            kind_hint="event", prefill=prefill))
    return out


def _jsonld_caption(event):
    """The markup rendered as text, so the quoted date evidence the prefill
    carries is provably inside the text the gate checks it against."""
    parts = [event.get("title", ""), event.get("description", ""),
             f"Venue: {event.get('venue', '')}" if event.get("venue") else "",
             f"Price: {event.get('price', '')}" if event.get("price") else "",
             factory_worker._jsonld_evidence(event)]
    return "\n".join(part for part in parts if part)


def _from_split(item, page, county, key):
    url = str(item.get("url") or "").strip()
    title = str(item.get("title") or "").strip()[:200]
    if not url.startswith("http"):
        return None
    return common.candidate("listings", key, url, title=title, county=county,
                            kind_hint="event",
                            caption=f"Listed on {page['url']} as: {title}" if title else "")
