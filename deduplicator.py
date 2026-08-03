"""
Tier 4: Deduplication and normalization layer

Merges events from YourDaysOut, AllEvents.in, Facebook, and Instagram
into a single canonical list, removing duplicates.

Deduplication strategy:
  1. Exact match: same source URL (obvious duplicates from same source)
  2. Fuzzy match: same title (normalized) + date within 2 days + same venue/city
  3. Geolocation match: events within 500m of each other on the same date
  4. Confidence scoring: higher confidence from structured sources (YourDaysOut/AllEvents)
     than from scraped FB/IG sources
"""
import re
import json
from datetime import datetime, timedelta
from typing import Optional
from dataclasses import dataclass, field
from difflib import SequenceMatcher

# Source priority (higher = more trustworthy)
SOURCE_PRIORITY = {
    "yourdaysout": 100,
    "allevents": 90,
    "facebook": 50,
    "instagram": 30,
}

# Confidence decay over time (days since event)
def confidence_decay(days_old: int) -> float:
    """Reduce confidence for older events."""
    if days_old > 30:
        return 0.3
    elif days_old > 14:
        return 0.6
    elif days_old > 7:
        return 0.8
    return 1.0


def normalize_title(title: str) -> str:
    """Normalize event title for comparison."""
    if not title:
        return ""
    # Lowercase, remove punctuation and common words
    t = title.lower().strip()
    t = re.sub(r'[^\w\s]', ' ', t)
    t = re.sub(r'\b(the|a|an|at|in|on|for|with|and|or|de|la|du|le)\b', '', t, flags=re.I)
    t = re.sub(r'\s+', ' ', t).strip()
    return t


def normalize_location(loc: str) -> str:
    """Normalize location string for comparison."""
    if not loc:
        return ""
    l = loc.lower().strip()
    l = re.sub(r'[^\w\s]', ' ', l)
    l = re.sub(r'\s+', ' ', l).strip()
    # Remove common prefixes
    l = re.sub(r'^(county|city of|the)\s+', '', l)
    return l


def similarity(a: str, b: str) -> float:
    """Calculate string similarity ratio (0.0-1.0)."""
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def parse_any_date(date_str: str) -> Optional[datetime]:
    """Parse various date formats into a datetime object."""
    if not date_str:
        return None

    s = date_str.strip()

    # ISO format: 2026-10-25T18:00:00+00:00
    m = re.match(r'(\d{4})-(\d{2})-(\d{2})', s)
    if m:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))

    # Facebook: "Thu, Oct 30, 2025" or "Thu, Oct 30, 2025 at 3:00 PM GMT"
    m = re.match(r'.*,?\s+(\w{3,9})\s+(\d{1,2}),?\s+(\d{4})', s, re.I)
    if m:
        months = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
                  "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
        month_str = m.group(1)[:3].lower()
        month = months.get(month_str)
        if month:
            return datetime(int(m.group(3)), month, int(m.group(2)))

    # Instagram: "Oct 30th" or "October 30th"
    m = re.match(r'(\w{3,9})\s+(\d{1,2})(?:th|st|nd|rd)?\s+(\d{4})?', s, re.I)
    if m:
        months = {"january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
                  "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
                  "november": 11, "december": 12,
                  "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
                  "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
        month = months.get(m.group(1).lower())
        if month:
            year = int(m.group(3)) if m.group(3) else datetime.now().year
            return datetime(year, month, int(m.group(2)))

    # Facebook Dutch: "do, 30 okt. 2025"
    m = re.match(r'\w{2,3}[.,]?\s+(\d{1,2})\s+(\w{3,9})\.?\s+(\d{4})?', s, re.I)
    if m:
        months = {"januari": 1, "februari": 2, "maart": 3, "april": 4, "mei": 5,
                  "juni": 6, "juli": 7, "augustus": 8, "september": 9, "oktober": 10,
                  "november": 11, "december": 12,
                  "jan": 1, "feb": 2, "mrt": 3, "apr": 4, "mei": 5, "jun": 6,
                  "jul": 7, "aug": 8, "sep": 9, "okt": 10, "nov": 11, "dec": 12}
        month = months.get(m.group(2).lower())
        if month:
            year = int(m.group(3)) if m.group(3) else datetime.now().year
            return datetime(year, month, int(m.group(1)))

    # "Thu, Jul 23" (no year)
    m = re.match(r'.*,?\s+(\w{3,9})\s+(\d{1,2})', s, re.I)
    if m:
        months = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
                  "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
        month = months.get(m.group(1)[:3].lower())
        if month:
            year = datetime.now().year
            return datetime(year, month, int(m.group(2)))

    return None


@dataclass
class Event:
    """Canonical event record after deduplication."""
    title: str = ""
    description: str = ""
    start_date: str = ""  # ISO date string
    end_date: str = ""
    venue_name: str = ""
    venue_address: str = ""
    city: str = ""
    county: str = ""
    country: str = ""
    latitude: str = ""
    longitude: str = ""
    url: str = ""
    cost: str = ""
    age_group: str = ""
    source: str = ""  # e.g., "yourdaysout:6362" or "facebook:2038716913632824"
    confidence: float = 0.5  # 0.0-1.0
    all_urls: list = field(default_factory=list)
    all_sources: list = field(default_factory=list)


def events_match(e1: Event, e2: Event) -> bool:
    """Determine if two events are the same event from different sources.

    Matching criteria (any of these must be true with sufficient confidence):
    1. Same title + dates within 3 days
    2. Same title + same location (city/county)
    3. Same date + venues within 500m (geohash proximity)
    4. Title similarity > 0.8 + dates within 2 days
    """
    # 1. Same source URL = exact duplicate
    if e1.url and e2.url and e1.url == e2.url:
        return True

    # 2. Title similarity
    t1 = normalize_title(e1.title)
    t2 = normalize_title(e2.title)
    title_sim = similarity(t1, t2)

    # 3. Date comparison
    d1 = parse_any_date(e1.start_date or e1.date_raw if hasattr(e1, 'date_raw') else e1.start_date)
    d2 = parse_any_date(e2.start_date or e2.date_raw if hasattr(e2, 'date_raw') else e2.start_date)

    date_diff = abs((d1 - d2).days) if d1 and d2 else 999

    # 4. Location comparison
    loc1 = normalize_location(e1.city or e1.venue_name or "")
    loc2 = normalize_location(e2.city or e2.venue_name or "")
    loc_sim = similarity(loc1, loc2)

    # 5. Geo proximity (if both have coordinates)
    geo_close = False
    if e1.latitude and e2.latitude:
        try:
            lat1, lon1 = float(e1.latitude), float(e1.longitude or 0)
            lat2, lon2 = float(e2.latitude), float(e2.longitude or 0)
            # Rough km distance
            dist = ((lat1 - lat2) ** 2 + (lon1 - lon2) ** 2) ** 0.5 * 111
            geo_close = dist < 5  # within ~5km
        except (ValueError, TypeError):
            pass

    # Matching rules:
    # Rule A: High title similarity + dates close
    if title_sim > 0.8 and date_diff <= 3:
        return True

    # Rule B: High title similarity + same city/location
    if title_sim > 0.85 and (loc_sim > 0.7 or geo_close):
        return True

    # Rule C: Exact date + geo proximity + decent title similarity
    if date_diff <= 1 and geo_close and title_sim > 0.5:
        return True

    # Rule D: Same title + exact date match
    if title_sim > 0.9 and date_diff <= 0:
        return True

    return False


def merge_events(existing: Event, new: Event) -> Event:
    """Merge a new event into an existing one, preferring higher-confidence data."""
    # Source priority
    def src_pri(s: str) -> int:
        for prefix, pri in SOURCE_PRIORITY.items():
            if s.startswith(prefix):
                return pri
        return 10

    # For each field, prefer the value from the higher-confidence source
    # If both sources are equal priority, prefer the one with more data
    existing_pri = src_pri(existing.source)
    new_pri = src_pri(new.source)

    merged = existing

    # Track all sources
    merged.all_sources = list(set(existing.all_sources + [existing.source, new.source]))
    merged.all_urls = list(set(existing.all_urls + [existing.url, new.url]))

    # Merge fields: prefer non-empty values from higher priority source
    fields = ["title", "description", "start_date", "end_date", "venue_name",
              "venue_address", "city", "county", "country", "latitude", "longitude",
              "url", "cost", "age_group"]

    for field_name in fields:
        existing_val = getattr(existing, field_name)
        new_val = getattr(new, field_name)

        if not existing_val and new_val:
            setattr(merged, field_name, new_val)
        elif existing_val and new_val and existing_val != new_val:
            if new_pri > existing_pri:
                setattr(merged, field_name, new_val)
            # If equal priority, keep existing (first seen)

    # Update confidence: take max, boost if multiple sources agree
    merged.confidence = max(existing.confidence, new.confidence, 0.7 if len(merged.all_sources) > 1 else 0.5)

    return merged


def deduplicate_events(raw_events: list[dict]) -> list[Event]:
    """Take raw events from all scrapers and return deduplicated canonical events.

    Raw events can have different field names depending on source:
    - YourDaysOut/AllEvents: title, start_date, end_date, venue_name, etc.
    - Facebook: title, date_raw, location_raw, event_id
    - Instagram: title, date_raw, location_raw, cost, age_group
    """
    # Normalize all to Event objects
    events = []
    for raw in raw_events:
        # Determine source from the source field or URL
        source = raw.get("source", "")
        url = raw.get("url", "")

        # Infer source type from URL or source field
        if "yourdaysout" in source or "yourdaysout" in url or source.startswith("yourdaysout"):
            source_type = "yourdaysout"
        elif "allevents" in source or "allevents" in url:
            source_type = "allevents"
        elif "facebook" in source or "facebook" in url:
            source_type = "facebook"
        elif "instagram" in source or "instagram" in url:
            source_type = "instagram"
        else:
            source_type = source.split(":")[0] if ":" in source else "unknown"

        event = Event(
            title=raw.get("title", ""),
            description=raw.get("description", ""),
            start_date=raw.get("start_date", raw.get("date_raw", "")),
            end_date=raw.get("end_date", ""),
            venue_name=raw.get("venue_name", raw.get("location_raw", "")),
            venue_address=raw.get("venue_address", ""),
            city=raw.get("city", ""),
            county=raw.get("county", ""),
            country=raw.get("country", "IE"),
            latitude=str(raw.get("latitude", "")),
            longitude=str(raw.get("longitude", "")),
            url=raw.get("url", ""),
            cost=raw.get("cost", ""),
            age_group=raw.get("age_group", ""),
            source=raw.get("source", source_type),
            confidence=SOURCE_PRIORITY.get(source_type, 10) / 100.0,
            all_sources=[source_type],
            all_urls=[raw.get("url", "")],
        )
        events.append(event)

    # Sort by confidence descending (high-confidence sources first)
    events.sort(key=lambda e: e.confidence, reverse=True)

    # Deduplicate
    deduped = []
    for event in events:
        matched = False
        for existing in deduped:
            if events_match(existing, event):
                merged = merge_events(existing, event)
                # Replace existing with merged
                idx = deduped.index(existing)
                deduped[idx] = merged
                matched = True
                break
        if not matched:
            deduped.append(event)

    return deduped


def to_dict(event: Event) -> dict:
    """Convert Event dataclass to dict for JSON serialization."""
    return {
        "title": event.title,
        "description": event.description,
        "start_date": event.start_date,
        "end_date": event.end_date,
        "venue_name": event.venue_name,
        "venue_address": event.venue_address,
        "city": event.city,
        "county": event.county,
        "country": event.country,
        "latitude": event.latitude,
        "longitude": event.longitude,
        "url": event.url,
        "cost": event.cost,
        "age_group": event.age_group,
        "source": event.source,
        "confidence": round(event.confidence, 2),
        "all_sources": event.all_sources,
        "all_urls": event.all_urls,
    }


if __name__ == "__main__":
    # Quick test
    from scrapers import scrape_yourdaysout_listing, scrape_yourdaysout_event, scrape_allevents_listing, scrape_allevents_event

    print("=== Collecting raw events ===")
    raw_events = []

    # YourDaysOut
    ydo_urls = scrape_yourdaysout_listing()
    for url in ydo_urls[:5]:
        evt = scrape_yourdaysout_event(url)
        if evt:
            raw_events.append(evt)

    # AllEvents
    ae_urls = scrape_allevents_listing()
    for url in ae_urls[:5]:
        evt = scrape_allevents_event(url)
        if evt:
            raw_events.append(evt)

    print(f"\nRaw events collected: {len(raw_events)}")

    # Deduplicate
    deduped = deduplicate_events(raw_events)
    print(f"After dedup: {len(deduped)} events")

    for e in deduped:
        d = to_dict(e)
        print(f"\n  Title: {d['title']}")
        print(f"  Start: {d['start_date']}")
        print(f"  Venue: {d['venue_name']}")
        print(f"  City: {d['city']}")
        print(f"  Sources: {d['all_sources']}")
        print(f"  Confidence: {d['confidence']}")
