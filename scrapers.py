"""
Tier 1: YourDaysOut.ie + AllEvents.in scrapers
Both serve schema.org/Event JSON-LD — easy to parse, no auth needed.
"""
import requests
import re
import json
import time
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


if __name__ == "__main__":
    print("=== YourDaysOut ===")
    ydo_urls = scrape_yourdaysout_listing()
    print(f"Found {len(ydo_urls)} event URLs")
    for u in ydo_urls[:2]:
        print(f"  Scraping: {u}")
        sample = scrape_yourdaysout_event(u)
        if sample:
            print(json.dumps(sample, indent=2, ensure_ascii=False))
        else:
            print("  No data extracted")

    print("\n=== AllEvents.in ===")
    ae_urls = scrape_allevents_listing()
    print(f"Found {len(ae_urls)} event URLs")
    if ae_urls:
        sample = scrape_allevents_event(ae_urls[0])
        print(json.dumps(sample, indent=2, ensure_ascii=False) if sample else "No data extracted")
