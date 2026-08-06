"""
Main orchestrator: Kids Events Ireland scraper pipeline

Runs all tiers in sequence:
  1. Tier 1 (fast): YourDaysOut.ie + AllEvents.in + FamilyFun.ie + IrelandMe.com + The Ark + Limerick.ie
  2. Tier 2 (Playwright): Facebook groups + DublinFamilyFun.ie + TotsSpots.com + Meetup.com

Then deduplicates across all sources and outputs a clean JSON feed.

Usage:
  python3 main.py                     # Run tiers 1+2
  python3 main.py --tiers "1"         # Only fast sources
  python3 main.py --limit 20          # Scrape more events per source
  python3 main.py --no-dedup          # Skip deduplication (debug)
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
    scrape_familyfun_listing,
    scrape_familyfun_event,
    scrape_irelandme_events,
    scrape_ark_events,
    scrape_limerick_events,
)

# Tier 2: Playwright-based scrapers (requires chromium)
from facebook_scraper import scrape_facebook_all
from facebook_extended_scraper import scrape_extended_facebook_all
from dublinfamilyfun_scraper import scrape_dublinfamilyfun_all
from totsspots_scraper import scrape_totsspots_all
from meetup_scraper import scrape_meetup_all

# Deduplication
from deduplicator import deduplicate_events, to_dict


def run_tier1(limit: int = 10) -> list[dict]:
    """Run fast HTML scrapers (no auth, no browser needed)."""
    raw_events = []

    print("\n=== Tier 1a: YourDaysOut.ie ===")
    ydo_urls = scrape_yourdaysout_listing()
    print(f"  Found {len(ydo_urls)} event URLs (limit: {limit})")
    scraped = 0
    for url in ydo_urls[:limit]:
        evt = scrape_yourdaysout_event(url)
        if evt:
            raw_events.append(evt)
            scraped += 1
    print(f"  Scraped {scraped}/{len(ydo_urls)} events")

    print("\n=== Tier 1b: AllEvents.in ===")
    ae_urls = scrape_allevents_listing()
    print(f"  Found {len(ae_urls)} event URLs (limit: {limit})")
    scraped_ae = 0
    for url in ae_urls[:limit]:
        evt = scrape_allevents_event(url)
        if evt:
            raw_events.append(evt)
            scraped_ae += 1
    print(f"  Scraped {scraped_ae}/{len(ae_urls)} events")

    print("\n=== Tier 1c: FamilyFun.ie ===")
    ff_urls = scrape_familyfun_listing()
    print(f"  Found {len(ff_urls)} event URLs (limit: {limit})")
    scraped_ff = 0
    for url in ff_urls[:limit]:
        evt = scrape_familyfun_event(url)
        if evt:
            raw_events.append(evt)
            scraped_ff += 1
    print(f"  Scraped {scraped_ff}/{len(ff_urls)} events")

    print("\n=== Tier 1d: IrelandMe.com ===")
    im_events = scrape_irelandme_events(limit=limit * 3)
    print(f"  Found {len(im_events)} events")
    raw_events.extend(im_events)

    print("\n=== Tier 1e: The Ark ===")
    ark_events = scrape_ark_events()
    print(f"  Found {len(ark_events)} events")
    raw_events.extend(ark_events)

    print("\n=== Tier 1f: Limerick.ie ===")
    limerick_events = scrape_limerick_events()
    print(f"  Found {len(limerick_events)} events")
    raw_events.extend(limerick_events)

    return raw_events


async def run_tier2_async() -> list[dict]:
    """Run all Playwright-based scrapers. Requires chromium."""
    all_events = []

    print("\n=== Tier 2a: Facebook Events ===")
    try:
        fb_events = await scrape_facebook_all()
        print(f"  Scraped {len(fb_events)} events")
        all_events.extend(fb_events)
    except Exception as e:
        print(f"  Facebook scraping failed: {e}")

    print("\n=== Tier 2b: Extended Facebook Groups ===")
    try:
        ext_events = await scrape_extended_facebook_all()
        print(f"  Scraped {len(ext_events)} events")
        all_events.extend(ext_events)
    except Exception as e:
        print(f"  Extended FB scraping failed: {e}")

    print("\n=== Tier 2c: DublinFamilyFun.ie ===")
    try:
        dff_events = await scrape_dublinfamilyfun_all()
        print(f"  Scraped {len(dff_events)} events")
        all_events.extend(dff_events)
    except Exception as e:
        print(f"  DublinFamilyFun scraping failed: {e}")

    print("\n=== Tier 2d: Tots Spots ===")
    try:
        ts_events = await scrape_totsspots_all()
        print(f"  Scraped {len(ts_events)} events")
        all_events.extend(ts_events)
    except Exception as e:
        print(f"  Tots Spots scraping failed: {e}")

    print("\n=== Tier 2e: Meetup.com ===")
    try:
        meetup_events = await scrape_meetup_all()
        print(f"  Scraped {len(meetup_events)} events")
        all_events.extend(meetup_events)
    except Exception as e:
        print(f"  Meetup scraping failed: {e}")

    return all_events


def run_tier2() -> list[dict]:
    """Run all Playwright-based scrapers."""
    return asyncio.run(run_tier2_async())


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
                        help="Tiers to run (default: '1,2'). 1=Tier 1 (fast), 2=Playwright (all browser-based)")
    parser.add_argument("--output", default="events_output.json",
                        help="Output file path (relative to working directory)")
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
