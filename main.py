"""
Main orchestrator: Kids Events Ireland scraper pipeline

Runs all tiers in sequence:
  1. Tier 1: YourDaysOut.ie + AllEvents.in (fast, reliable, JSON-LD)
  2. Tier 2: Facebook group events (Playwright, ~30-60s for 2 groups)
  3. Tier 3: Instagram hashtag API (requires Instagram account session cookies)
  
Then deduplicates across all sources and outputs a clean JSON feed.

Usage:
  python3 main.py                     # Run tiers 1+2
  python3 main.py --tiers "1"         # Only fast sources
  python3 main.py --tiers "1,3"        # YourDaysOut + Instagram
  
Instagram setup:
  Export your Instagram sessionid cookie:
  export INSTAGRAM_SESSIONID="your_session_cookie_here"
  
  To get a sessionid: install instagra-cli and run 'instagra-cli login'
  or use instagrapi Python library to login programmatically.
  
  WARNING: Instagram requires an authenticated account session to access
  hashtag data. No-scraper approach (public API) is blocked since 2024.
"""
import argparse
import asyncio
import json
import os
import sys
from datetime import datetime

# Tier 1: static HTML scrapers (fast, no auth)
from scrapers import (
    scrape_yourdaysout_listing,
    scrape_yourdaysout_event,
    scrape_allevents_listing,
    scrape_allevents_event,
)

# Tier 2: Facebook (requires Playwright + chromium)
from facebook_scraper import scrape_facebook_all

# Tier 3: Instagram (requires sessionid cookie)
from instagram_scraper import scrape_instagram_hashtags, KIDS_EVENT_HASHTAGS

# Deduplication
from deduplicator import deduplicate_events, to_dict


def run_tier1(limit: int = 10) -> list[dict]:
    """Run YourDaysOut + AllEvents scrapers. Fast, no auth needed."""
    raw_events = []

    print("\n=== Tier 1: YourDaysOut.ie ===")
    ydo_urls = scrape_yourdaysout_listing()
    print(f"  Found {len(ydo_urls)} event URLs (limit: {limit})")
    scraped = 0
    for url in ydo_urls[:limit]:
        evt = scrape_yourdaysout_event(url)
        if evt:
            raw_events.append(evt)
            scraped += 1
    print(f"  Scraped {scraped}/{len(ydo_urls)} events")

    print("\n=== Tier 1: AllEvents.in ===")
    ae_urls = scrape_allevents_listing()
    print(f"  Found {len(ae_urls)} event URLs (limit: {limit})")
    scraped_ae = 0
    for url in ae_urls[:limit]:
        evt = scrape_allevents_event(url)
        if evt:
            raw_events.append(evt)
            scraped_ae += 1
    print(f"  Scraped {scraped_ae}/{len(ae_urls)} events")

    return raw_events


async def run_tier2_async() -> list[dict]:
    """Run Facebook group scrapers. Requires Playwright."""
    return await scrape_facebook_all()


def run_tier2() -> list[dict]:
    """Run Facebook group scrapers. Requires Playwright."""
    print("\n=== Tier 2: Facebook Events ===")
    events = asyncio.run(run_tier2_async())
    print(f"  Scraped {len(events)} events")
    return events


def run_tier3() -> list[dict]:
    """Run Instagram hashtag scraper. Requires sessionid cookie."""
    sessionid = os.environ.get("INSTAGRAM_SESSIONID", "")
    if not sessionid:
        print("\n=== Tier 3: Instagram Hashtags ===")
        print(f"  SKIP: No INSTAGRAM_SESSIONID cookie set")
        print(f"  Hashtags to monitor: {KIDS_EVENT_HASHTAGS}")
        print(f"  To enable: set INSTAGRAM_SESSIONID env var (see docstring)")
        return []

    print(f"\n=== Tier 3: Instagram Hashtags ({len(INSTAGRAM_HASHTAGS)} hashtags) ===")
    events = asyncio.run(scrape_instagram_hashtags())
    print(f"  Scraped {len(events)} events")
    return events


def deduplicate_and_output(raw_events: list[dict], output_file: str = None):
    """Run deduplication and save results."""
    print("\n=== Deduplication ===")
    print(f"  Input: {len(raw_events)} raw events")
    deduped = deduplicate_events(raw_events)
    print(f"  Output: {len(deduped)} unique events")

    # Convert to dict format
    results = [to_dict(e) for e in deduped]

    # Sort by date and confidence
    results.sort(key=lambda e: (e.get("start_date", ""), -e.get("confidence", 0)))

    if output_file:
        with open(output_file, "w") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
        print(f"  Saved to {output_file}")

    # Print summary
    print(f"\n=== Event Feed Summary ===")
    print(f"  Total unique events: {len(results)}")

    # Group by source
    by_source = {}
    for e in results:
        for s in e.get("all_sources", [e.get("source", "unknown")]):
            by_source[s] = by_source.get(s, 0) + 1
    for src, count in sorted(by_source.items(), key=lambda x: -x[1]):
        print(f"    {src}: {count}")

    # Group by county/city
    by_county = {}
    for e in results:
        c = e.get("county", "").strip() or e.get("city", "").strip() or "Unknown"
        by_county[c] = by_county.get(c, 0) + 1
    print(f"\n  By location:")
    for county, count in sorted(by_county.items(), key=lambda x: -x[1])[:10]:
        print(f"    {county}: {count}")

    # Print upcoming events (next 14 days)
    today = datetime.now()
    upcoming = [e for e in results if e.get("start_date")]
    upcoming_this_week = []
    for e in upcoming:
        try:
            d = e["start_date"][:10] if len(e["start_date"]) >= 10 else ""
            if d:
                dt = datetime.strptime(d, "%Y-%m-%d")
                if 0 <= (dt - today).days <= 14:
                    upcoming_this_week.append(e)
        except:
            pass

    print(f"\n  Upcoming (next 14 days): {len(upcoming_this_week)}")
    for e in upcoming_this_week[:10]:
        print(f"    {e['start_date']}: {e.get('title', '')[:60]}")

    return results


def main():
    parser = argparse.ArgumentParser(description="Kids Events Ireland scraper pipeline")
    parser.add_argument("--tiers", default="1,2",
                        help="Tiers to run (default: '1,2'). 1=Tier 1 (YourDaysOut+AllEvents), 2=Facebook, 3=Instagram")
    parser.add_argument("--output", default="/tmp/kidsevents_scraper/events_output.json",
                        help="Output file path")
    parser.add_argument("--limit", type=int, default=10,
                        help="Max events to scrape per source (default: 10)")
    parser.add_argument("--no-dedup", action="store_true",
                        help="Skip deduplication")
    args = parser.parse_args()

    tiers = [int(t) for t in args.tiers.split(",")]
    all_raw = []

    if 1 in tiers:
        all_raw.extend(run_tier1(limit=args.limit))

    if 2 in tiers:
        all_raw.extend(run_tier2())

    if 3 in tiers:
        all_raw.extend(run_tier3())

    if args.no_dedup:
        results = [to_dict(e) if hasattr(e, "to_dict") else e for e in all_raw]
    else:
        results = deduplicate_and_output(all_raw, args.output)

    # Print first few results
    print(f"\n=== Sample events ===")
    for e in results[:5]:
        print(f"\n  Title: {e.get('title', '')}")
        print(f"  Date: {e.get('start_date', '')}")
        print(f"  Venue: {e.get('venue_name', '')}")
        print(f"  Location: {e.get('city', '')}, {e.get('county', '')}")
        print(f"  Sources: {e.get('all_sources', [])}")
        print(f"  URLs: {e.get('all_urls', [])[:3]}")


if __name__ == "__main__":
    main()
