"""Tier 2a: Extended Facebook Groups scraper for kids/family events

Adds more regional Facebook groups beyond Dublin to the Facebook scraping.
"""
import asyncio
import re
from typing import Optional

from playwright.async_api import async_playwright
from scrapers import HEADERS
from facebook_scraper import UA
# Additional Facebook groups for family/kids events in Ireland:
# - Limerick: "Things to do with kids in and around Limerick" (483806072002226)
# - Galway: "Galway bumps,babies,tots and beyond" (search term - not a group ID)
# - Cork: "Kids in Cork" (search term)
# - General: "Love Ireland" (155318481516366)

EXTENDED_FB_GROUPS = {
    "limerick_kids": "483806072002226",
    "love_ireland": "155318481516366",
}

# Reuse the parsing logic from facebook_scraper
from facebook_scraper import scrape_group_events


async def scrape_extended_facebook_all() -> list[dict]:
    """Scrape additional Facebook groups for family events."""
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

        for group_key, group_id in EXTENDED_FB_GROUPS.items():
            try:
                print(f"  Scraping group: {group_key} ({group_id})")
                events = await scrape_group_events(group_id, context)
                all_events.extend(events)
                await asyncio.sleep(3)
            except Exception as e:
                print(f"  Group {group_key} failed: {e}")
                continue

        await browser.close()

    return all_events


if __name__ == "__main__":
    import json
    events = asyncio.run(scrape_extended_facebook_all())
    print(f"\n=== Total extended Facebook events: {len(events)} ===")
    for e in events[:5]:
        print(f"\n  Title: {e.get('title', '')}")
        print(f"  Date: {e.get('date_raw', e.get('start_date', ''))}")
        print(f"  URL: {e.get('url', '')}")
