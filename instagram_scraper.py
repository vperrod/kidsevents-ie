"""
Tier 3: Instagram Events scraper

Instagram provides NO public web scraping path. Tested and confirmed:
1. Public web scraping — BLOCKED
   - Hashtag pages (instagram.com/explore/tags/{hashtag}) redirect to /login/ when not authenticated
   - The page is a pure SPA (Single Page Application) — all HTML is a shell (~600KB JS bundle, 0 JSON-LD, 0 embedded data)
   - Instagram Lite (l.instagram.com) is also a pure SPA shell
   - No __NEXT_DATA__, no JSON-LD, no visible data in page source

2. Instagram Graph API — requires Business/Creator account + Facebook App with App Review (2-4 weeks)

3. instagrapi — Primary approach (no Business account needed)
   Uses instagrapi library which calls Instagram's API with your sessionid.
   Requires INSTAGRAM_SESSIONID environment variable (from your Instagram login).
   Falls back gracefully if session is expired/banned.

4. Instagram Graph API — requires Business/Creator account + Facebook App with App Review (2-4 weeks)
"""
import os
import re
import json
from typing import Optional, Callable

# Vision model callable for image OCR — set externally
_vision_model: Optional[Callable] = None


def set_vision_model(model: Callable):
    """Set an external vision model for OCR on image-based Instagram posts."""
    global _vision_model
    _vision_model = model


# --- Hashtags to monitor for Irish kids events ---
KIDS_EVENT_HASHTAGS = [
    "kidseventsireland",
    "kidsactivitiesireland",
    "familyfriendlyireland",
    "dublinwithkids",
    "corkfamily",
    "thingstodokids",
    "irishfamilyfun",
    "mummydublin",
    "kidsindublin",
    "dublinkids",
    "galwayfamily",
    "corkwithkids",
    "kidsofcork",
    "familyeventsireland",
]


def parse_caption_for_event(caption: str) -> dict:
    """Parse an Instagram caption for event details using emoji markers.
    
    Expected format:
      Event Title Here!
      📅 Oct 30th 3pm-5:30pm
      📍 Community Center, Dublin
      🎟️ €5 per child
      👶 Ages 2-10
      #hashtag1 #hashtag2
    
    Returns dict with: title, date_raw, time_raw, location_raw, cost, age_group, hashtags
    """

    result = {
        "title": "",
        "date_raw": "",
        "time_raw": "",
        "location_raw": "",
        "cost": "",
        "age_group": "",
        "hashtags": [],
    }
    if not caption:
        return result

    lines = caption.split("\n")

    # Extract hashtags
    hashtag_pattern = r"#\w+"
    hashtags = re.findall(hashtag_pattern, caption)
    result["hashtags"] = hashtags

    # Title: first meaningful line without emoji markers
    for line in lines:
        stripped = line.strip()
        if stripped and not any(kw in stripped.lower() for kw in [
            "#", "📅", "⏰", "📍", "🎟", "👶", "✨", "👉", "🎉", "🔥", "🎈",
            "date:", "time:", "location:", "cost:", "age:",
        ]):
            result["title"] = stripped[:120]
            break

    # 📅 Date / time
    for line in lines:
        if "📅" in line:
            clean = line.split("📅", 1)[1].strip()
            result["date_raw"] = clean
            # Extract date portion
            date_match = re.search(
                r"((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+\d{1,2}(?:th|st|nd|rd)?"
                r"(?:\s+\d{4})?"
                r"(?:\s+\|\s*\d{1,2}(?::\d{2})?\s*(?:am|pm))?"
                r"(?:\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)\s*-\s*\d{1,2}(?::\d{2})?\s*(?:am|pm)?)?)",
                clean, re.I
            )
            if date_match:
                result["date_raw"] = date_match.group(1)
            # Extract time portion separately
            time_match = re.search(
                r"(\d{1,2}(?::\d{2})?\s*(?:am|pm)\s*(?:-\s*\d{1,2}(?::\d{2})?\s*(?:am|pm))?)",
                clean, re.I
            )
            if time_match:
                result["time_raw"] = time_match.group(1)
            break

    # 📍 Location
    for line in lines:
        if "📍" in line:
            clean = line.split("📍", 1)[1].strip()
            result["location_raw"] = clean
            break

    # 🎟️ Cost
    for line in lines:
        if "🎟" in line:
            clean = line.split("🎟", 1)[1].strip()
            result["cost"] = clean
            break

    # 👶 Age range
    for line in lines:
        if "👶" in line:
            clean = line.split("👶", 1)[1].strip()
            result["age_group"] = clean
            break

    # If no date found with emoji, search for date patterns
    if not result["date_raw"]:
        for line in lines:
            date_match = re.search(
                r"((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+\d{1,2}(?:th|st|nd|rd)?)",
                line, re.I
            )
            if date_match:
                result["date_raw"] = date_match.group(1)
                break

    return result


def parse_instagram_media(media: dict, hashtag: str) -> dict:
    """Parse an instagrapi media object into event data."""
    caption = media.get("caption", "") or ""

    parsed = parse_caption_for_event(caption)

    event = {
        "title": parsed["title"] or f"Instagram post #{hashtag}",
        "date_raw": parsed["date_raw"],
        "time_raw": parsed["time_raw"],
        "location_raw": parsed["location_raw"],
        "cost": parsed["cost"],
        "age_group": parsed["age_group"],
        "description": caption[:500] if caption else "",
        "url": f"https://www.instagram.com/p/{media.get('code', '')}/",
        "image_url": media.get("media_url", ""),
        "source": f"instagram:{hashtag}:{media.get('code', '')}",
        "hashtags": parsed["hashtags"],
        "timestamp": str(media.get("timestamp", "")) if media.get("timestamp") else "",
        "confidence": 0.3,
    }
    return event


def scrape_instagram_with_instagrapi(sessionid: str, hashtags: list[str], max_posts: int = 20) -> list[dict]:
    """Scrape Instagram hashtag posts using instagrapi.

    Uses instagrapi library which calls Instagram's API with your sessionid.
    No Business account or Facebook App required.

    IMPORTANT: If you get 'LoginRequired' or '403 Forbidden' errors, Instagram
    is blocking requests from this IP (datacenter/cloud detection). The sessionid
    is valid but Instagram prevents API access from non-residential IPs.
    Solutions:
      1. Run the scraper from a residential IP/VPN
      2. Use a rotating residential proxy (ScraperAPI, BrightData, etc.)
      3. Run locally on your home machine

    Args:
        sessionid: Your Instagram sessionid cookie value
        hashtags: List of hashtag strings (without #) to search
        max_posts: Maximum posts per hashtag

    Returns:
        List of event dicts
    """
    from instagrapi import Client

    cl = Client()
    all_events = []

    print(f"  Logging into Instagram via instagrapi...")
    try:
        cl.login_by_sessionid(sessionid)
        print(f"  Login OK! User: {cl.username} (ID: {cl.user_id})")
    except Exception as e:
        etype = type(e).__name__
        print(f"  Login validation error: {etype}")
        # login_by_sessionid may fail on IP-blocked validation, but
        # sessionid + user_id are still set internally on the Client
        if not cl.sessionid:
            print(f"  ✗ Session not set — cannot proceed. Error: {e}")
            return []
        if not cl.user_id:
            # Try to extract user_id from sessionid
            try:
                cl.user_id = sessionid.split("%3A")[0]
                print(f"  Using extracted user_id: {cl.user_id}")
            except Exception:
                pass
        print(f"  ✓ Session ID is set — trying API calls anyway...")

    for hashtag in hashtags:
        print(f"\n  Scraping #{hashtag}...")
        try:
            medias = cl.hashtag_medias_recent(hashtag, amount=max_posts)
            print(f"    Found {len(medias)} posts")
            for media in medias:
                event = parse_instagram_media(media.to_dict(), hashtag)
                all_events.append(event)
                if event["title"] and len(event["title"]) > 10 and not event["title"].startswith("Instagram"):
                    print(f"    + {event['title'][:50]} | {event.get('date_raw', '')[:25]}")

            # Rate limit to avoid detection
            import time
            time.sleep(3)

        except Exception as e:
            etype = type(e).__name__
            emsg = str(e)
            print(f"    {etype} on #{hashtag}: {emsg[:100]}")
            if "login_required" in emsg.lower() or "login_required" in etype or "forbidden" in etype.lower():
                print(f"    → BLOCKED: Instagram rejected API requests from this IP.")
                print(f"       Instagram's mobile API (i.instagram.com) returns 403/401")
                print(f"       from datacenter/cloud IPs even with valid sessionid.")
                print(f"       Solutions:")
                print(f"         1. Run scraper from a residential IP address")
                print(f"         2. Use a residential proxy service")
                print(f"         3. Run locally on your home machine")
                break
            continue

    print(f"\n  Total: {len(all_events)} events from {len(hashtags)} hashtags")
    return all_events


async def scrape_instagram_hashtags() -> list[dict]:
    """Scrape Instagram hashtag posts for Irish kids events.

    Uses one of two approaches depending on configuration:

    1. If INSTAGRAM_SESSIONID env var is set:
       Self-hosted scraping via instagrapi (private API).
       Requires a real Instagram account sessionid cookie.
       No Business account conversion needed.

       To get your sessionid:
         1. Log into instagram.com in your browser
         2. DevTools → Application → Cookies → instagram.com
         3. Copy 'sessionid' cookie value
         4. export INSTAGRAM_SESSIONID="your_value_here"

    2. If INSTAGRAM_ACCESS_TOKEN + INSTAGRAM_USER_ID env vars are set:
       Official Instagram Graph API (requires Business account + App Review)

    3. If neither is set:
       Returns empty list with setup instructions
    """
    sessionid = os.environ.get("INSTAGRAM_SESSIONID", "")
    token = os.environ.get("INSTAGRAM_ACCESS_TOKEN", "")
    user_id = os.environ.get("INSTAGRAM_USER_ID", "")

    if sessionid:
        print(f"  Using instagrapi (private API) with sessionid cookie")
        print(f"  Hashtags to monitor: {KIDS_EVENT_HASHTAGS}")
        events = scrape_instagram_with_instagrapi(sessionid, KIDS_EVENT_HASHTAGS, max_posts=20)
        return events

    elif token and user_id:
        print(f"  Using Instagram Graph API with access token")
        events = []
        for hashtag in KIDS_EVENT_HASHTAGS[:5]:
            try:
                hashtag_events = scrape_hashtag_api(hashtag)
                events.extend(hashtag_events)
            except Exception as e:
                print(f"  #{hashtag}: API error - {e}")
        print(f"  Total: {len(events)} events from Instagram Graph API")
        return events

    else:
        print(f"  SKIP: No INSTAGRAM_SESSIONID cookie set")
        print(f"  Hashtags to monitor: {KIDS_EVENT_HASHTAGS}")
        print(f"  To enable: set INSTAGRAM_SESSIONID env var (see docstring)")
        return []


# Legacy functions kept for reference / Graph API approach
async def scrape_hashtag_graphql(sessionid: str, hashtag: str, max_posts: int = 20) -> list[dict]:
    """Deprecated: Use scrape_instagram_with_instagrapi instead."""
    return []


def scrape_hashtag_api(hashtag: str) -> list[dict]:
    """Official Instagram Graph API approach (requires Business account)."""
    return []


# Re-export for main.py
async def _async_wrapper():
    pass
