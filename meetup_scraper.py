"""Tier 2d: Meetup.com scraper (Playwright)

Meetup requires authentication for API access. We use Playwright to
scrape public event listing pages directly.
"""
import asyncio
import json
import re
from urllib.parse import urlparse, parse_qs
from typing import Optional

from playwright.async_api import async_playwright
from scrapers import HEADERS

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# Meetup has topics for kids/family activities
MEETUP_QUERIES = [
    {"text": "kids", "location": "Dublin, Ireland"},
    {"text": "kids", "location": "Cork, Ireland"},
    {"text": "kids", "location": "Galway, Ireland"},
    {"text": "family", "location": "Dublin, Ireland"},
    {"text": "parent", "location": "Dublin, Ireland"},
    {"text": "toddler", "location": "Dublin, Ireland"},
    {"text": "babies", "location": "Dublin, Ireland"},
    {"text": "kids", "location": "Limerick, Ireland"},
    {"text": "kids", "location": "Waterford, Ireland"},
    {"text": "family", "location": "Cork, Ireland"},
]


async def scrape_meetup_search_page(url: str, context) -> list[dict]:
    """Scrape event listings from a Meetup search page."""
    events = []
    page = await context.new_page()
    try:
        await page.goto(url, timeout=30000, wait_until="networkidle")
        await asyncio.sleep(4)

        # Handle cookie consent
        for cookie_text in ["I agree", "Accept all", "Accept All"]:
            try:
                await page.click(f"text=/{cookie_text}/i", timeout=3000)
                await asyncio.sleep(2)
                break
            except Exception:
                continue

        # Scroll to load more events
        for _ in range(4):
            await page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
            await asyncio.sleep(2)

        # Extract event cards
        # Meetup event cards have: title, date, location, link
        event_links = await page.eval_on_selector_all(
            'a[href*="/events/"]',
            """elements => Array.from(new Set(
                elements
                    .filter(a => {
                        const href = a.href || '';
                        return href.includes('/events/') && href.match(/\\/events\\/\\d+/);
                    })
                    .map(a => a.href)
            ))"""
        )

        # Extract event data from cards
        event_cards = await page.eval_on_selector_all(
            'a[href*="/events/"]',
            """elements => elements
                .filter(a => a.href && a.href.match('/events/\\d+'))
                .map(a => {
                    const href = a.href;
                    const card = a.closest('[data-testid="event-card"]') || a.closest('article') || a;
                    const text = card.innerText || '';
                    return { href, text };
                })
                .filter(e => e.text.length > 10)
            """
        )

        seen = set()
        for card in event_cards:
            url = card["href"]
            if url in seen:
                continue
            seen.add(url)

            text = card["text"]
            lines = [l.strip() for l in text.split("\n") if l.strip()]

            if not lines:
                continue

            title = lines[0] if lines else ""

            # Find date-like line and location
            date_str = ""
            venue = ""
            location = ""

            for line in lines[1:6]:
                lower = line.lower()
                # Date patterns: "Sat, Dec 6", "Dec 6, 2025", "at 7:00 PM"
                if re.match(r'^(\w{3},?\s+\w{3,9}\s+\d{1,2})', line) or \
                   re.match(r'^(\w{3,9}\s+\d{1,2},?\s+\d{4})', line):
                    date_str = line
                # Location: contains city name or "in " prefix
                elif any(c in line for c in ["Dublin", "Cork", "Galway", "Limerick", "Waterford", "Ireland"]) or \
                     " in " in lower or "📍" in line:
                    location = line
                    venue = line

            # Extract event ID from URL
            id_match = re.search(r'/events/(\d+)', url)
            event_id = id_match.group(1) if id_match else ""

            if title:
                # Determine location from URL or content
                city = ""
                county = ""
                for c in ["Dublin", "Cork", "Galway", "Limerick", "Waterford", "Kerry", "Kilkenny"]:
                    if c in url or c in location or c in title:
                        city = c
                        county = c
                        break

                events.append({
                    "title": title[:200],
                    "description": "",
                    "start_date": date_str,
                    "end_date": "",
                    "url": url,
                    "venue_name": venue or "",
                    "venue_address": location or "",
                    "city": city,
                    "county": county,
                    "country": "IE",
                    "latitude": "",
                    "longitude": "",
                    "source": f"meetup:{event_id}" if event_id else f"meetup:{url}",
                })

    except Exception as e:
        print(f"  ERROR scraping {url}: {e}")
    finally:
        await page.close()

    return events


async def scrape_meetup_all() -> list[dict]:
    """Scrape Meetup events for kids/family activities across Ireland."""
    all_events = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage",
                  "--disable-blink-features=AutomationDetected"]
        )
        context = await browser.new_context(
            user_agent=UA,
            viewport={"width": 1280, "height": 720},
            locale="en-IE",
        )

        for query in MEETUP_QUERIES:
            search_url = f"https://www.meetup.com/find/?keywords={query['text']}&location={query['location']}"
            try:
                events = await scrape_meetup_search_page(search_url, context)
                all_events.extend(events)
                print(f"  {query['text']} in {query['location']}: {len(events)} events")
                await asyncio.sleep(3)
            except Exception as e:
                print(f"  Query failed {query}: {e}")
                continue

        await browser.close()

    # Dedupe by URL
    seen = set()
    unique = []
    for e in all_events:
        url = e.get("url", "")
        if url not in seen:
            seen.add(url)
            unique.append(e)

    return unique


if __name__ == "__main__":
    events = asyncio.run(scrape_meetup_all())
    print(f"\n=== Total Meetup events: {len(events)} ===")
    for e in events[:5]:
        print(f"\n  Title: {e.get('title', '')}")
        print(f"  Date: {e.get('start_date', '')}")
        print(f"  Venue: {e.get('venue_name', '')}")
        print(f"  Source: {e.get('source', '')}")
