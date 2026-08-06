"""Tier 2c: TotsSpots.com scraper (Playwright)

TotsSpots.com is Ireland's largest directory for baby, toddler and kids
classes/groups. It's a WordPress/WooCommerce site with a custom "listing"
post type that doesn't expose a REST API.

Strategy: Use Playwright to navigate town-based listing pages and parse
the listing cards directly from the rendered HTML.
"""
import asyncio
import json
import re
from typing import Optional

from playwright.async_api import async_playwright
from scrapers import HEADERS, FAMILYFUN_BASE

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# Tots Spots listing pages organized by county/region
COUNTY_PAGES = [
    "https://www.totsspots.com/county/dublin/",
    "https://www.totsspots.com/county/cork/",
    "https://www.totsspots.com/county/galway/",
    "https://www.totsspots.com/county/limerick/",
    "https://www.totsspots.com/county/kerry/",
    "https://www.totsspots.com/county/waterford/",
    "https://www.totsspots.com/county/kilkenny/",
    "https://www.totsspots.com/county/meath/",
    "https://www.totsspots.com/county/louth/",
    "https://www.totsspots.com/county/clare/",
    "https://www.totsspots.com/county/tipperary/",
    "https://www.totsspots.com/county/witness/",
    "https://www.totsspots.com/county/wexford/",
    "https://www.totsspots.com/county/wicklow/",
]


async def scrape_totsspots_listings(url: str, context) -> list[dict]:
    """Scrape listing cards from a Tots Spots county page."""
    events = []
    page = await context.new_page()
    try:
        await page.goto(url, timeout=30000, wait_until="networkidle")
        await asyncio.sleep(3)

        # Handle cookie consent
        for cookie_text in ["Accept", "I agree", "Allow all"]:
            try:
                await page.click(f"text=/{cookie_text}/i", timeout=3000)
                await asyncio.sleep(2)
                break
            except Exception:
                continue

        # Scroll to load all listings
        for _ in range(6):
            await page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
            await asyncio.sleep(2)

        html = await page.content()

        # Tots Spots listing cards:
        # Each listing has: title, description, category, age range, county
        # Find listing entries via link patterns
        listing_urls = re.findall(
            r'href="(https://www\.totsspots\.com/listing/[^"]+)"',
            html
        )

        # Also extract from the page content directly
        # Pattern for listing cards with title, type, age range
        listing_pattern = re.compile(
            r'class="listing[^"]*"[^>]*>.*?href="([^"]+)"[^>]*>(.*?)</a>.*?'
            r'class="[^"]*category[^"]*"[^>]*>(.*?)</.*?>(.*?age[^<]*)',
            re.S
        )

        # Better: parse the JSON data that Next.js/React embeds
        next_data = re.search(
            r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>',
            html, re.S
        )
        if next_data:
            try:
                data = json.loads(next_data.group(1), strict=False)
                props = data.get("props", {}).get("pageProps", {})
                listings = props.get("listings") or props.get("posts") or props.get("data")
                if isinstance(listings, list):
                    for listing in listings:
                        if isinstance(listing, dict):
                            title = listing.get("title", "")
                            desc = listing.get("excerpt", "") or listing.get("description", "")
                            category = listing.get("category", "")
                            age_range = listing.get("age_range", "") or listing.get("ageRange", "")
                            county = listing.get("county", "") or url.split("/county/")[1].rstrip("/") if "/county/" in url else ""
                            link = listing.get("url", "") or listing.get("link", "") or listing.get("slug", "")
                            
                            if title:
                                events.append({
                                    "title": title,
                                    "description": desc[:500] if desc else "",
                                    "start_date": "",  # Tots Spots lists classes, not dated events
                                    "end_date": "",
                                    "url": link,
                                    "venue_name": "",
                                    "venue_address": "",
                                    "city": county.capitalize() if county else "",
                                    "county": county.capitalize() if county else "",
                                    "country": "IE",
                                    "latitude": "",
                                    "longitude": "",
                                    "source": f"totsspots:{link}",
                                })
            except (json.JSONDecodeError, KeyError):
                pass

        # If JSON data didn't work, fall back to HTML parsing
        if not events and listing_urls:
            unique_urls = list(dict.fromkeys(listing_urls))
            # Scrape a few listing detail pages
            for link in unique_urls[:10]:
                events.extend(await scrape_totsspots_detail(link, context))

        # Last resort: extract from visible text
        if not events:
            visible_text = await page.inner_text("body")
            lines = [l.strip() for l in visible_text.split("\n") if l.strip()]
            # Filter for likely listing entries (lines with class/type keywords)
            for line in lines:
                if any(kw in line.lower() for kw in ["parent and toddler", "playgroup", "music", "preschool", "baby"]):
                    events.append({
                        "title": line[:100],
                        "description": "",
                        "start_date": "",
                        "end_date": "",
                        "url": url,
                        "venue_name": "",
                        "venue_address": "",
                        "city": url.split("/county/")[1].rstrip("/").capitalize() if "/county/" in url else "",
                        "county": url.split("/county/")[1].rstrip("/").capitalize() if "/county/" in url else "",
                        "country": "IE",
                        "latitude": "",
                        "longitude": "",
                        "source": f"totsspots:{url}",
                    })

        print(f"  {url}: {len(events)} listings found")
        return events

    except Exception as e:
        print(f"  ERROR scraping {url}: {e}")
        return []
    finally:
        await page.close()


async def scrape_totsspots_detail(url: str, context) -> list[dict]:
    """Scrape a single Tots Spots listing detail page."""
    page = await context.new_page()
    events = []
    try:
        await page.goto(url, timeout=20000, wait_until="networkidle")
        await asyncio.sleep(2)

        # Try JSON-LD
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
                    if isinstance(item, dict) and item.get("@type") in ("Event", "LocalBusiness"):
                        # Tots Spots listings use LocalBusiness schema
                        if isinstance(item, dict):
                            events.append({
                                "title": item.get("name", ""),
                                "description": item.get("description", ""),
                                "start_date": "",
                                "end_date": "",
                                "url": item.get("url", url),
                                "venue_name": item.get("name", ""),
                                "venue_address": item.get("address", ""),
                                "city": item.get("address", {}).get("addressLocality", "") if isinstance(item.get("address"), dict) else "",
                                "county": "",
                                "country": "IE",
                                "latitude": "",
                                "longitude": "",
                                "source": f"totsspots:{url}",
                            })
            elif isinstance(data, dict):
                if data.get("@type") in ("Event", "LocalBusiness"):
                    events.append({
                        "title": data.get("name", ""),
                        "description": data.get("description", ""),
                        "start_date": "",
                        "end_date": "",
                        "url": data.get("url", url),
                        "venue_name": data.get("name", ""),
                        "venue_address": str(data.get("address", "")),
                        "city": data.get("address", {}).get("addressLocality", "") if isinstance(data.get("address"), dict) else "",
                        "county": "",
                        "country": "IE",
                        "latitude": "",
                        "longitude": "",
                        "source": f"totsspots:{url}",
                    })

        # Fallback: extract from page content
        if not events:
            title_m = re.search(r'<h1[^>]*>(.*?)</h1>', await page.content(), re.S)
            title = re.sub(r'<[^>]+>', '', title_m.group(1)).strip() if title_m else ""
            desc_m = re.search(r'<p[^>]*>(.+?)</p>', await page.content(), re.S)
            desc = re.sub(r'<[^>]+>', '', desc_m.group(1)).strip() if desc_m else ""
            
            if title:
                events.append({
                    "title": title,
                    "description": desc[:500],
                    "start_date": "",
                    "end_date": "",
                    "url": url,
                    "venue_name": "",
                    "venue_address": "",
                    "city": "",
                    "county": "",
                    "country": "IE",
                    "latitude": "",
                    "longitude": "",
                    "source": f"totsspots:{url}",
                })
    except Exception as e:
        print(f"  ERROR scraping detail {url}: {e}")
    finally:
        await page.close()

    return events[:1]


async def scrape_totsspots_all() -> list[dict]:
    """Scrape Tots Spots listings across all Irish counties."""
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

        for county_url in COUNTY_PAGES:
            try:
                events = await scrape_totsspots_listings(county_url, context)
                all_events.extend(events)
                await asyncio.sleep(2)
            except Exception as e:
                print(f"  County page failed: {county_url}: {e}")
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
    events = asyncio.run(scrape_totsspots_all())
    print(f"\n=== Total Tots Spots listings: {len(events)} ===")
    for e in events[:5]:
        print(f"\n  Title: {e.get('title', '')}")
        print(f"  County: {e.get('county', '')}")
        print(f"  Source: {e.get('source', '')}")
