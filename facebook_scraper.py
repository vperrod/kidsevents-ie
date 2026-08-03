"""
Tier 2: Facebook Events scraper (optimized)

Strategy:
  1. Single Playwright browser instance with multiple pages
  2. Load each group's /events/ page, scroll, handle cookie wall
  3. Parse visible text: both "Upcoming" and "Past events" sections
  4. Each section has alternating date/title lines — robust regex for multiple locales
  5. Match event IDs (extracted from URLs) to entries by order
  6. Skip individual event detail pages (Facebook requires login for full view)
     — instead, scrape more group pages and parse all data from listing text

Groups monitored:
  - Family Friendly events - Dublin area (2419438288216012)
  - FAMILY EVENTS IN DUBLIN or CLOSE BY (663773570330499)
"""
import re
import asyncio
import random
from typing import Optional
from playwright.async_api import async_playwright

FACEBOOK_GROUPS = {
    "dublin_family_friendly": "2419438288216012",
    "dublin_family_events": "663773570330499",
    # Add via collaborators:
    # "cork_family_events": "...",
    # "galway_family_events": "...",
}

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# Date patterns that Facebook renders:
#   "Thu, Sep 24 at 8:00 PM IST"       (English, US format with time)
#   "Thu, Jul 23"                       (English, no year, no time)
#   "Thu, Oct 30, 2025"                 (English, with year)
#   "Thu, Nov 1, 2025"                  (English, with year)
#   "do, 30 okt. 2025"                  (Dutch)
#   "Sat, 1 nov. 2025"                  (Dutch, no leading zero)
DATE_LINE_RE = re.compile(
    r'^(\w{2,9},?\s+'  # Day name + optional comma
    r'(?:\w{3,9}\s+\d{1,2},?\s+\d{4}'  # Mon DD, YYYY
    r'|\w{3,9}\s+\d{1,2}(?:\s+at\s+\d{1,2}:\d{2}\s*(?:AM|PM|IST|GMT|UTC))?'  # Mon DD at HH:MM
    r'|\w{3,9}\.?\s+\d{1,2}(?:\s+\d{4})?'  # Dutch: okt. 30 2025 or okt. 30
    r'))',
    re.I
)

# Also try a broader match
DATE_LINE_BROAD_RE = re.compile(
    r'^(\w{3,9}[,\.]?\s+\w{2,9}[.,]?\s+\d{1,2}(?:[,:]\s+\d{1,2}:\d{2}\s*(?:AM|PM|IST|GMT|UTC))?(?:\s+\d{4})?)',
    re.I
)


async def scrape_group_events(group_id: str, context) -> list[dict]:
    """Scrape event entries from a Facebook group events page.

    Extracts event IDs from page URLs and parses date+title from visible text.
    No individual event page visits needed — all data from listing page.
    """
    page = await context.new_page()
    try:
        url = f"https://www.facebook.com/groups/{group_id}/events/"
        await page.goto(url, timeout=30000, wait_until="networkidle")
        await asyncio.sleep(5)

        # Handle cookie consent wall
        for cookie_text in ["Allow all cookies", "Accept All", "Accepteer alles"]:
            try:
                await page.click(f'text="{cookie_text}"', timeout=3000)
                await asyncio.sleep(3)
                break
            except Exception:
                continue

        # Scroll to load more events
        for _ in range(6):
            await page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
            await asyncio.sleep(2)

        # Get event IDs from page source
        content = await page.content()
        event_ids = re.findall(r'/events/(\d{15,20})', content)
        unique_ids = list(dict.fromkeys(event_ids))

        # Get visible text and parse event entries
        visible_text = await page.inner_text("body")
        lines = [l.strip() for l in visible_text.split("\n") if l.strip()]

        # UI noise to skip
        ui_noise = {
            "log in", "forgot account?", "join group", "more", "about",
            "discussion", "featured", "events", "media", "invite",
            "see more", "see more on facebook", "email or phone number",
            "password", "create new account", "allow the use of cookies",
            "essential cookies", "cookies from other companies",
            "your cookie choices", "about cookies", "what are cookies?",
            "learn more", "why do we use", "what are meta",
            "manage your ad", "more information", "controlling cookies",
            "decline optional", "allow all", "accept all", "accepteer alles",
            "no upcoming events.", "no upcoming events", "geen aankomende",
        }

        # Find section boundaries
        section_starts = []
        for i, line in enumerate(lines):
            low = line.lower()
            if "upcoming events" in low or "aanstaande" in low or "gepland" in low:
                section_starts.append((i, "upcoming"))
            elif "past events" in low or "verleden" in low or "verstreken" in low:
                section_starts.append((i, "past"))
            elif "no upcoming events" in low or "geen" in low:
                section_starts.append((i, "no_upcoming"))

        if not section_starts:
            section_starts = [(0, "all")]

        # Parse date+title pairs from each section
        all_entries = []
        for idx, (start, sec_type) in enumerate(section_starts):
            end = section_starts[idx + 1][0] if idx + 1 < len(section_starts) else len(lines)
            section_lines = lines[start + 1:end]

            i = 0
            while i < len(section_lines):
                line = section_lines[i]
                low = line.lower()

                if low in ui_noise:
                    i += 1
                    continue

                # Check if this line looks like a date
                is_date = bool(DATE_LINE_RE.match(line))
                if not is_date:
                    is_date = bool(DATE_LINE_BROAD_RE.match(line))

                # Additional heuristic: line has a 3-letter day abbrev at start
                if not is_date and re.match(r'^\w{2,3}[,\.]\s', line):
                    is_date = True

                if is_date:
                    # Look ahead for the title
                    title = ""
                    for j in range(i + 1, min(i + 4, len(section_lines))):
                        candidate = section_lines[j]
                        if candidate.lower() not in ui_noise and len(candidate) > 2:
                            title = candidate
                            break
                    all_entries.append({
                        "date_raw": line,
                        "title": title,
                    })
                    i += 2
                else:
                    i += 1

        # Match entries to event IDs by order
        events = []
        for idx, entry in enumerate(all_entries):
            eid = unique_ids[idx] if idx < len(unique_ids) else ""
            entry["event_id"] = eid
            entry["url"] = f"https://www.facebook.com/events/{eid}" if eid else ""
            entry["source"] = f"facebook:{eid}" if eid else ""
            entry["group_id"] = group_id
            events.append(entry)

        print(f"  Group {group_id}: {len(unique_ids)} IDs, {len(events)} entries")
        for e in events[:6]:
            print(f"    {e['date_raw'][:30]} | {e['title'][:55]} | ID: {e['event_id'][-8:]}")

        return events
    except Exception as e:
        print(f"  ERROR scraping group {group_id}: {e}")
        return []
    finally:
        await page.close()


async def scrape_facebook_all():
    """Scrape all configured Facebook groups."""
    all_events = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"]
        )
        context = await browser.new_context(
            user_agent=UA,
            viewport={"width": 1280, "height": 720},
            locale="en-US",
        )

        for group_key, group_id in FACEBOOK_GROUPS.items():
            try:
                print(f"\n=== Scraping group: {group_key} ===")
                events = await scrape_group_events(group_id, context)
                all_events.extend(events)
                await asyncio.sleep(random.uniform(2, 5))
            except Exception as e:
                print(f"  Group {group_key} failed: {e}")
                continue

        await browser.close()

    return all_events


if __name__ == "__main__":
    events = asyncio.run(scrape_facebook_all())
    print(f"\n=== Total Facebook events: {len(events)} ===")
    for e in events[:8]:
        print(f"\n  Title: {e.get('title', '')}")
        print(f"  Date: {e.get('date_raw', '')}")
        print(f"  URL: {e.get('url', '')}")
        print(f"  Source: {e.get('source', '')}")
