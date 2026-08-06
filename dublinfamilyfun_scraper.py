"""Tier 2b: DublinFamilyFun.ie scraper (Playwright + JSON-LD)

DublinFamilyFun.ie is a Next.js SPA. It embeds schema.org/Event JSON-LD
in <script type="application/ld+json"> blocks. We use Playwright to
render the page and extract these blocks.
"""
import asyncio
import json
import re
from typing import Optional

from playwright.async_api import async_playwright
from scrapers import parse_jsonld_event, extract_event, HEADERS

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


async def scrape_dublinfamilyfun_page(url: str, context) -> list[dict]:
    """Scrape a single DublinFamilyFun page for event JSON-LD blocks."""
    events = []
    try:
        page = await context.new_page()
        await page.goto(url, timeout=30000, wait_until="networkidle")
        await asyncio.sleep(3)

        # Extract JSON-LD blocks
        ld_scripts = await page.eval_on_selector_all(
            'script[type="application/ld+json"]',
            "elements => elements.map(e => e.innerHTML)"
        )

        for script_text in ld_scripts:
            try:
                data = json.loads(script_text, strict=False)
            except json.JSONDecodeError:
                continue

            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and item.get("@type") == "Event":
                        evt = extract_event(item, source_url=url)
                        events.append(evt)
            elif isinstance(data, dict) and data.get("@type") == "Event":
                evt = extract_event(data, source_url=url)
                events.append(evt)

        await page.close()
    except Exception as e:
        print(f"  ERROR scraping {url}: {e}")
        if 'page' in dir():
            await page.close()

    return events


async def scrape_dublinfamilyfun_all() -> list[dict]:
    """Scrape DublinFamilyFun.ie events from listing pages."""
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

        # Start from the events listing page
        listing_url = "https://www.dublinfamilyfun.ie/whats-on/"
        page = None
        try:
            page = await context.new_page()
            await page.goto(listing_url, timeout=30000, wait_until="networkidle")
            await asyncio.sleep(3)

            # Handle cookie consent
            for cookie_text in ["Accept all cookies", "Accept All", "I agree"]:
                try:
                    await page.click(f"text=/{cookie_text}/i", timeout=3000)
                    await asyncio.sleep(2)
                    break
                except Exception:
                    continue

            # Scroll to load more
            for _ in range(4):
                await page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
                await asyncio.sleep(2)

            # Find event links
            event_urls = await page.eval_on_selector_all(
                'a[href*="/whats-on/"]',
                "elements => Array.from(new Set(elements.map(a => a.href)))"
            )
            event_urls = [u for u in event_urls if u and 'dublinfamilyfun.ie' in u]

            # Also try direct category pages
            category_urls = [
                "https://www.dublinfamilyfun.ie/whats-on/",
                "https://www.dublinfamilyfun.ie/venues/",
            ]

            # Get more links by paginating
            for page_num in range(2, 5):
                cat_url = f"https://www.dublinfamilyfun.ie/whats-on/page/{page_num}/"
                try:
                    await page.goto(cat_url, timeout=20000, wait_until="networkidle")
                    await asyncio.sleep(2)
                    more_urls = await page.eval_on_selector_all(
                        'a[href*="/whats-on/"]',
                        "elements => Array.from(new Set(elements.map(a => a.href)))"
                    )
                    event_urls.extend([u for u in more_urls if u and 'dublinfamilyfun.ie' in u])
                except Exception:
                    continue

            # Dedupe
            event_urls = list(dict.fromkeys(event_urls))
            print(f"  Found {len(event_urls)} event URLs")

            # Scrape individual events
            for url in event_urls[:50]:
                evt = await scrape_dublinfamilyfun_page(url, context)
                all_events.extend(evt)
                await asyncio.sleep(0.5)

        except Exception as e:
            print(f"  ERROR on listing page: {e}")
        finally:
            try:
                if page:
                    await page.close()
            except Exception:
                pass

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
    events = asyncio.run(scrape_dublinfamilyfun_all())
    print(f"\n=== Total DublinFamilyFun events: {len(events)} ===")
    for e in events[:5]:
        print(f"\n  Title: {e.get('title', '')}")
        print(f"  Date: {e.get('start_date', '')}")
        print(f"  Venue: {e.get('venue_name', '')}")
        print(f"  Source: {e.get('source', '')}")
