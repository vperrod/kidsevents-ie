"""
Tier 1: YourDaysOut.ie + AllEvents.in scrapers
Both serve schema.org/Event JSON-LD — easy to parse, no auth needed.
"""
import requests
import re
import json
import time
import asyncio
from typing import Optional


def fetch_with_retry(url: str, headers: dict, max_retries: int = 3, timeout: int = 15) -> Optional[requests.Response]:
    """Fetch a URL with retries on timeout/error."""
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
            if resp.status_code == 200:
                return resp
            elif resp.status_code in (429, 503, 502):
                # Rate limited or service unavailable — wait and retry
                time.sleep(3 * (attempt + 1))
            else:
                return resp  # Return even on non-200 (e.g., 404)
        except requests.exceptions.Timeout:
            if attempt < max_retries - 1:
                time.sleep(2 * (attempt + 1))
            else:
                return None
        except requests.exceptions.ConnectionError:
            if attempt < max_retries - 1:
                time.sleep(2 * (attempt + 1))
            else:
                return None
    return None

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def parse_jsonld_event(html: str) -> Optional[dict]:
    """Extract the first schema.org/Event JSON-LD block from HTML."""
    blocks = re.findall(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
        html, re.S
    )
    for block in blocks:
        try:
            # strict=False allows raw control characters inside JSON strings
            # (some sites like YourDaysOut embed \r\n directly in description values)
            data = json.loads(block.strip(), strict=False)
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("@type") == "Event":
                    return item
        elif isinstance(data, dict) and data.get("@type") == "Event":
            return data
    return None


def extract_event(event_json: dict, source_url: str = "") -> dict:
    """Normalize a schema.org Event JSON-LD object to our common schema."""
    loc = event_json.get("location", {})
    address = loc.get("address", {})
    if isinstance(address, str):
        address = {"streetAddress": address}
    geo = loc.get("geo", {})

    return {
        "title": event_json.get("name", ""),
        "description": event_json.get("description", ""),
        "start_date": event_json.get("startDate", ""),
        "end_date": event_json.get("endDate", ""),
        "url": event_json.get("url", source_url),
        "venue_name": loc.get("name", "") if isinstance(loc, dict) else str(loc),
        "venue_address": address.get("streetAddress", ""),
        "city": address.get("addressLocality", ""),
        "county": address.get("addressRegion", ""),
        "country": address.get("addressCountry", ""),
        "latitude": geo.get("latitude", "") if isinstance(geo, dict) else "",
        "longitude": geo.get("longitude", "") if isinstance(geo, dict) else "",
        "source": event_json.get("@id", source_url),
    }


# ─── YourDaysOut.ie ───

def scrape_yourdaysout_listing(base_url: str = "https://www.yourdaysout.ie/events-on-in-ireland") -> list[str]:
    """Get all event URLs from a YourDaysOut listing page.

    YourDaysOut redirects .ie -> .com and uses relative paths like
    /events-on-in-county-dublin-ireland/event-name-1234
    """
    resp = fetch_with_retry(base_url, HEADERS, max_retries=3, timeout=15)
    if not resp or resp.status_code != 200:
        return []

    # Event URLs are relative paths ending with a numeric ID, e.g.:
    #   /events-on-in-county-dublin-ireland/gokidsgo-general-admisson-6260
    # Category pages (e.g. /events-on-in-ireland/christmas) don't have numeric IDs
    paths = re.findall(
        r'href="(/events-on-in-county-[^"]+-\d+)"',
        resp.text
    )
    domain = resp.url.split("/")[2] if resp.url else "yourdaysout.com"
    full_urls = [f"https://{domain}{p}" for p in paths]

    # Dedupe
    unique = []
    seen = set()
    for u in full_urls:
        clean = u.split("?")[0].rstrip("/")
        if clean not in seen:
            seen.add(clean)
            unique.append(clean)
    return unique


def scrape_yourdaysout_event(event_url: str) -> Optional[dict]:
    """Scrape a single YourDaysOut event page."""
    resp = fetch_with_retry(event_url, HEADERS, max_retries=3, timeout=15)
    if not resp or resp.status_code != 200:
        return None
    event_json = parse_jsonld_event(resp.text)
    if event_json:
        return extract_event(event_json, source_url=event_url)
    return None


# ─── AllEvents.in ───

def scrape_allevents_listing(url: str = "https://allevents.in/dublin/kids") -> list[str]:
    """Get all event URLs from an AllEvents.in listing page.

    AllEvents embeds event data-link attributes on event-card elements.
    """
    resp = fetch_with_retry(url, HEADERS, max_retries=3, timeout=15)
    if not resp or resp.status_code != 200:
        return []
    links = re.findall(r'data-link="(https://allevents\.in/[^\s"\']+)"', resp.text)
    unique = []
    seen = set()
    for l in links:
        clean = l.split("?")[0].rstrip("/")
        if clean not in seen:
            seen.add(clean)
            unique.append(clean)
    return unique


def scrape_allevents_event(event_url: str) -> Optional[dict]:
    """Scrape a single AllEvents.in event page."""
    resp = fetch_with_retry(event_url, HEADERS, max_retries=3, timeout=15)
    if not resp or resp.status_code != 200:
        return None
    event_json = parse_jsonld_event(resp.text)
    if event_json:
        return extract_event(event_json, source_url=event_url)
    return None


# ─── FamilyFun.ie (WordPress REST API) ───

FAMILYFUN_BASE = "https://www.familyfun.ie"
FAMILYFUN_EVENTS_CATEGORY = 421  # "Events" category ID

def scrape_familyfun_listing() -> list[str]:
    """Get event URLs from FamilyFun.ie via WordPress REST API.
    
    Uses the WP REST API to fetch posts in the Events category.
    Returns list of event page URLs.
    """
    url = f"{FAMILYFUN_BASE}/wp-json/wp/v2/posts?categories={FAMILYFUN_EVENTS_CATEGORY}&per_page=100"
    resp = fetch_with_retry(url, HEADERS, max_retries=3, timeout=15)
    if not resp or resp.status_code != 200:
        return []
    try:
        posts = resp.json()
    except json.JSONDecodeError:
        return []
    return [p.get("link", "") for p in posts if p.get("link")]


def scrape_familyfun_event(event_url: str) -> Optional[dict]:
    """Scrape a single FamilyFun.ie event page.
    
    FamilyFun.ie may have JSON-LD or just HTML — try JSON-LD first, fall back to HTML parsing.
    """
    resp = fetch_with_retry(event_url, HEADERS, max_retries=3, timeout=15)
    if not resp or resp.status_code != 200:
        return None
    html = resp.text

    # Try JSON-LD first
    event_json = parse_jsonld_event(html)
    if event_json:
        evt = extract_event(event_json, source_url=event_url)
        evt["title"] = evt["title"] or ""
        if not evt["title"]:
            # Extract title from <title> tag
            m = re.search(r'<title>(.*?)</title>', html, re.S)
            if m:
                evt["title"] = m.group(1).strip()
        return evt

    # Fallback: parse HTML for title and description
    title = ""
    m = re.search(r'<title>(.*?)</title>', html, re.S)
    if m:
        title = m.group(1).strip().split(" - ")[0]

    # Try to extract city from URL or title
    city = ""
    county = ""
    for c in ["Dublin", "Cork", "Galway", "Limerick", "Waterford", "Kerry", "Cork", "Kilkenny", "Louth", "Meath", "Tipperary", "Clare", "Galway", "Mayo", "Sligo", "Donegal", "Limerick", "Waterford"]:
        if c in title:
            city = c
            county = c
            break

    # Extract description from content paragraphs
    paragraphs = re.findall(r'<p[^>]*>(.*?)</p>', html, re.S)
    description_parts = []
    for p in paragraphs[:5]:
        clean = re.sub(r'<[^>]+>', '', p).strip()
        if clean and len(clean) > 20 and 'cookie' not in clean.lower() and 'privacy' not in clean.lower():
            description_parts.append(clean)

    return {
        "title": title,
        "description": " ".join(description_parts[:3])[:500],
        "start_date": "",  # FamilyFun.ie doesn't always have clear dates in HTML
        "end_date": "",
        "url": event_url,
        "venue_name": "",
        "venue_address": "",
        "city": city,
        "county": county,
        "country": "IE",
        "latitude": "",
        "longitude": "",
        "source": f"familyfun:{event_url}",
    }


# ─── IrelandMe.com ───

def scrape_irelandme_events(limit: int = 100) -> list[dict]:
    """Scrape events from IrelandMe.com.

    IrelandMe lists events across Ireland with category filtering including "Family".
    """
    base_url = "https://irelandme.com/events/"
    resp = fetch_with_retry(base_url, HEADERS, max_retries=3, timeout=20)
    if not resp or resp.status_code != 200:
        return []

    html = resp.text
    base_domain = "https://irelandme.com"

    # Extract event paths from href attributes
    event_paths = re.findall(r'href="(/events/[a-z0-9-]+/?)"', html)
    event_links = [base_domain + p.rstrip("/") for p in event_paths]

    # Also check for family-specific pages
    family_match = re.search(r'href="([^"]*family[^"]*)"', html, re.I)
    if family_match:
        family_url_str = family_match.group(1)
        if not family_url_str.startswith("http"):
            family_url_str = base_domain + family_url_str
        family_resp = fetch_with_retry(family_url_str, HEADERS, max_retries=3, timeout=20)
        if family_resp and family_resp.status_code == 200:
            family_html = family_resp.text
            additional_paths = re.findall(r'href="(/events/[a-z0-9-]+/?)"', family_html)
            event_links.extend([base_domain + p.rstrip("/") for p in additional_paths])

    # Also try pagination for more events
    for page_num in range(2, 4):
        pag_url = f"{base_domain}/events/page/{page_num}/"
        pag_resp = fetch_with_retry(pag_url, HEADERS, max_retries=3, timeout=15)
        if pag_resp and pag_resp.status_code == 200:
            pag_paths = re.findall(r'href="(/events/[a-z0-9-]+/?)"', pag_resp.text)
            event_links.extend([base_domain + p.rstrip("/") for p in pag_paths])

    # Dedupe
    unique_links = list(dict.fromkeys(event_links))

    events = []
    for url in unique_links[:limit]:
        evt = scrape_irelandme_event(url)
        if evt:
            events.append(evt)
    return events


def scrape_irelandme_event(event_url: str) -> Optional[dict]:
    """Scrape a single IrelandMe event page."""
    resp = fetch_with_retry(event_url, HEADERS, max_retries=3, timeout=15)
    if not resp or resp.status_code != 200:
        return None
    
    html = resp.text

    # Try JSON-LD first
    event_json = parse_jsonld_event(html)
    if event_json:
        return extract_event(event_json, source_url=event_url)

    # Parse HTML
    title_m = re.search(r'<title>(.*?)</title>', html, re.S)
    title = title_m.group(1).strip() if title_m else ""

    # Extract date from meta or content
    # IrelandMe uses "3 Aug" format in listings
    date_text = ""
    for pattern in [
        r'<meta[^>]*property="article:published_time"[^>]*content="([^"]+)"',
        r'<meta[^>]*name="date"[^>]*content="([^"]+)"',
        r'(\d{1,2}\s+[A-Z][a-z]{2,8}\s+\d{4})',
    ]:
        m = re.search(pattern, html, re.I | re.S)
        if m:
            date_text = m.group(1)
            break

    # Extract venue
    venue = ""
    venue_m = re.search(r'class="venue[^"]*"[^>]*>(.*?)</', html, re.S)
    if venue_m:
        venue = re.sub(r'<[^>]+>', '', venue_m.group(1)).strip()

    # Extract description (first paragraph after title)
    desc_m = re.search(r'<p[^>]*>(.+?)</p>', html, re.S)
    description = re.sub(r'<[^>]+>', '', desc_m.group(1)).strip() if desc_m else ""

    # Extract city from content
    city = ""
    for c in ["Dublin", "Cork", "Galway", "Limerick", "Waterford", "Kerry", "Kilkenny", "Louth", "Meath"]:
        if re.search(r'\b' + c + r'\b', title + description, re.I):
            city = c
            break

    return {
        "title": title.split(" - ")[0].strip() if " - " in title else title,
        "description": description[:500],
        "start_date": date_text,
        "end_date": "",
        "url": event_url,
        "venue_name": venue,
        "venue_address": "",
        "city": city,
        "county": city if city else "",
        "country": "IE",
        "latitude": "",
        "longitude": "",
        "source": f"irelandme:{event_url}",
    }


# ─── The Ark (ark.ie) ───

def scrape_ark_events() -> list[dict]:
    """Scrape events from The Ark (ark.ie/events).
    
    The Ark is a Dublin cultural centre for children ages 2-12.
    Events are listed as article blocks with title, date, and description.
    """
    url = "https://ark.ie/events"
    resp = fetch_with_retry(url, HEADERS, max_retries=3, timeout=15)
    if not resp or resp.status_code != 200:
        return []

    html = resp.text

    # Try JSON-LD first
    events = []
    ld_blocks = re.findall(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
        html, re.S
    )
    for block in ld_blocks:
        try:
            data = json.loads(block.strip(), strict=False)
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("@type") == "Event":
                    evt = extract_event(item, source_url="https://ark.ie/events")
                    events.append(evt)
        elif isinstance(data, dict) and data.get("@type") == "Event":
            evt = extract_event(data, source_url="https://ark.ie/events")
            events.append(evt)

    if events:
        return events[:50]

    # Fallback: parse h3 headings from HTML
    # The Ark lists events/workshops as h3 headings
    headings = re.findall(r'<h3[^>]*>(.*?)</h3>', html, re.S)

    seen = set()
    for title_html in headings:
        title = re.sub(r'<[^>]+>', '', title_html).strip()
        if not title or title in seen:
            continue
        if len(title) < 5:
            continue
        # Skip UI noise
        lower_title = title.lower()
        if "filter" in lower_title or "category" in lower_title or "view all" in lower_title or "load more" in lower_title:
            continue

        seen.add(title)

        # Extract date from title
        start_date = ""
        for pattern in [
            r"(\d{1,2}\s+[A-Z][a-z]{2,8}\s+\d{4})",
            r"(\d{1,2}\s+[A-Z][a-z]{2,8})",
        ]:
            m = re.search(pattern, title)
            if m:
                start_date = m.group(1)
                break

        # Extract event link
        full_link = re.search(r'href="([^"]+)"', title_html)
        link = full_link.group(1) if full_link else ""
        if link and not link.startswith("http"):
            link = f"https://ark.ie{link}"

        events.append({
            "title": title,
            "description": "",
            "start_date": start_date,
            "end_date": "",
            "url": link or "https://ark.ie/events",
            "venue_name": "The Ark, Dublin",
            "venue_address": "21a Eustace Street, Temple Bar, Dublin 8",
            "city": "Dublin",
            "county": "Dublin",
            "country": "IE",
            "latitude": "53.3438",
            "longitude": "-6.2654",
            "source": f"theark:{title[:50]}",
        })

    return events[:50]


def scrape_limerick_events() -> list[dict]:
    """Scrape events from Limerick.ie council site."""
    # Limerick.ie uses LocalGov Drupal — events are in a standard format
    events = []
    for url in [
        "https://www.limerick.ie/events",
        "https://www.limerick.ie/discover/eat-see-do/family-fun",
    ]:
        resp = fetch_with_retry(url, HEADERS, max_retries=3, timeout=15)
        if not resp or resp.status_code != 200:
            continue
        html = resp.text
        
        # LocalGov Drupal event listings
        event_blocks = re.findall(
            r'<article[^>]*class="[^"]*event[^"]*"[^>]*>(.*?)</article>',
            html, re.S
        )
        
        for block in event_blocks:
            title_m = re.search(r'<h[123][^>]*>(.*?)</h[123]>', block, re.S)
            title = re.sub(r'<[^>]+>', '', title_m.group(1)).strip() if title_m else ""
            
            link_m = re.search(r'href="([^"]+)"', block)
            link = link_m.group(1) if link_m else ""
            
            date_m = re.search(r'(\d{1,2}\s+[A-Z][a-z]{2,8}\s+\d{4})', block)
            start_date = date_m.group(1) if date_m else ""
            
            venue_m = re.search(r'class="venue[^"]*"[^>]*>(.*?)</', block, re.S)
            venue = re.sub(r'<[^>]+>', '', venue_m.group(1)).strip() if venue_m else ""
            
            desc_m = re.search(r'<p[^>]*>(.+?)</p>', block, re.S)
            desc = re.sub(r'<[^>]+>', '', desc_m.group(1)).strip() if desc_m else ""
            
            if title and start_date:
                events.append({
                    "title": title,
                    "description": desc[:500],
                    "start_date": start_date,
                    "end_date": "",
                    "url": link,
                    "venue_name": venue or "",
                    "city": "Limerick",
                    "county": "Limerick",
                    "country": "IE",
                    "latitude": "",
                    "longitude": "",
                    "source": f"limerickie:{link}",
                })
    
    return events


# ─── Facebook Groups (additional) ───

# Additional Facebook groups to scrape (found during research):
# - "Things to do with kids in and around Limerick" (483806072002226)
# - "Galway bumps,babies,tots and beyond" (Galwayparents)
# - "Limerick With Kids" (search term)

ADDITIONAL_FB_GROUPS = {
    "limerick_kids": "483806072002226",
}


def scrape_facebook_group_extended(group_id: str, context) -> list[dict]:
    """Scrape events from additional Facebook groups.
    
    Uses same pattern as facebook_scraper.py but for additional groups.
    """
    from facebook_scraper import scrape_group_events
    return asyncio.run(scrape_group_events(group_id, context))


if __name__ == "__main__":
    import sys
    print("This module provides scraping functions. Import from main.py to run the full pipeline.")
    print("Available functions:")
    print("  - scrape_yourdaysout_listing() / scrape_yourdaysout_event(url)")
    print("  - scrape_allevents_listing() / scrape_allevents_event(url)")
    print("  - scrape_familyfun_listing() / scrape_familyfun_event(url)")
    print("  - scrape_irelandme_events(limit=N)")
    print("  - scrape_ark_events()")
    print("  - scrape_limerick_events()")
