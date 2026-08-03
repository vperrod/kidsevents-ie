"""
Tier 3: Instagram Events scraper

Instagram provides NO public web scraping path. Tested and confirmed:

1. Public web scraping — BLOCKED
   - Hashtag pages (instagram.com/explore/tags/{hashtag}) redirect to /login/ when not authenticated
   - The page is a pure SPA (Single Page Application) — all HTML is a shell (~600KB JS bundle, 0 JSON-LD, 0 embedded data)
   - Instagram Lite (l.instagram.com) is also a pure SPA shell
   - No __NEXT_DATA__, no JSON-LD, no visible data in page source
   - Even Playwright with full JS execution returns the login page

2. Instagram Graph API (Hashtag Search) — requires Business account
   - Convert IG personal account → Business/Creator (free via app settings)
   - Create Facebook App, add Instagram Graph API product
   - Get long-lived user token (60 days, refreshable)
   - App Review for instagram_public_content_access (7-14 days, may reject)
   - Rate limit: 30 searches/hr, 200 media/hr
   - Returns: caption text, image_url, permalink, timestamp
   - Event details parsed from caption text + image OCR via vision model
   - Cost: Free (with Business account + approved app)

3. Self-hosted approach: throwaway IG account + instagrapi — RECOMMENDED
   - Create a real Instagram account (NOT bot-like — post a few photos, follow people)
   - Login via instagrapi: cl.login(username, password) — this gets you sessionid
   - Use cl.hashtag_medias_paginated_gql(hashtag) — works with the session cookie
   - No Facebook App or Graph API approval needed
   - Rate limit: ~100 requests/hour (Instagram soft-limits regular users)
   - Instagram may ban the account if too aggressive — mitigation: 5-10s delays between requests
   - Cost: Free (just the account creation)

4. Third-party scraping services (paid, as fallback):
   - Apify Instagram Scraper (~$5/1K operations, handles auth + proxies)
   - BrightData (~$15/GB, enterprise-grade rotation)
   - ScraperAPI (~$25/month, handles CAPTCHAs + rotation)

KEY FINDING: Instagram's SPA client only makes GraphQL API calls to /graphql/query/
AFTER authenticating with a sessionid cookie. Without login, it makes ZERO content API calls.
The data is NOT embedded in the HTML — it is fetched asynchronously by JavaScript only for authenticated users.

Caption parsing strategy:
  Instagram posts use emoji markers for event details:
    📅 Oct 30th 3-5:30pm      -> date/time
    📍 Main Street, Baldoyle   -> location
    🎟️ €5 per child            -> cost/price
    👶 Ages 3-10               -> age group
    #kidsactivities           -> tags

Image OCR fallback:
  For posts where the caption is sparse or image-heavy (infographics):
  Use vision model to extract text: date, time, location, description
  Cost: ~$0.01-0.02/image
"""
import re
import asyncio
import aiohttp
from typing import Optional, Callable

# Vision model callable for image OCR — set externally
# Should accept (image_url: str, question: str) -> str
_vision_model: Optional[Callable] = None

def set_vision_model(fn: Callable):
    """Set the vision model callable for image OCR.
    
    fn should accept (image_url: str, question: str) -> str
    """
    global _vision_model
    _vision_model = fn


# Hashtags to monitor for Irish kids/family events
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
    # Regional hashtags
    "galwayfamily",
    "corkwithkids",
    "kidsofcork",
    "familyeventsireland",
]

# Instagram Graph API config — user must provide these
# Get token via: https://developers.facebook.com/tools/explorer/
# Or via instagrapi self-hosted approach (no app needed)
INSTAGRAM_ACCESS_TOKEN = None  # Set via env var or config
INSTAGRAM_USER_ID = None       # Business account user ID

IG_BASE = "https://graph.instagram.com"
IG_API_VERSION = "v19.0"


def parse_caption_for_event(caption: str) -> dict:
    """Parse an Instagram caption to extract event details.
    
    Looks for emoji markers and date/time patterns.
    """
    result = {
        "title": "",
        "date_raw": "",
        "time_raw": "",
        "location_raw": "",
        "cost": "",
        "age_group": "",
        "description": "",
        "hashtags": [],
    }

    if not caption:
        return result

    # Split by lines/separators
    lines = re.split(r'[\n\u2029]+', caption)

    # Extract hashtags
    hashtags = re.findall(r'#(\w+)', caption)
    result["hashtags"] = hashtags

    # Title: first meaningful line without emoji markers
    for line in lines:
        stripped = line.strip()
        if stripped and not any(kw in stripped.lower() for kw in [
            "#", "📅", "⏰", "📍", "🎟", "👶", "✨", "👉", "🎉", "💥",
            "date:", "time:", "location:", "cost:", "age:",
        ]):
            result["title"] = stripped[:120]
            result["description"] = stripped[:200]
            break

    # 📅 Date / time
    for line in lines:
        if "📅" in line:
            clean = line.split("📅", 1)[1].strip()
            result["date_raw"] = clean
            # Extract date portion
            date_match = re.search(
                r'((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+\d{1,2}(?:th|st|nd|rd)?'
                r'(?:\s+\d{4})?'
                r'(?:\s+\|\s*\d{1,2}(?::\d{2})?\s*(?:am|pm))?'
                r'(?:\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)\s*-\s*\d{1,2}(?::\d{2})?\s*(?:am|pm))?)',
                clean, re.I
            )
            if date_match:
                result["date_raw"] = date_match.group(1)
            # Extract time portion separately
            time_match = re.search(
                r'(\d{1,2}(?::\d{2})?\s*(?:am|pm)\s*-\s*\d{1,2}(?::\d{2})?\s*(?:am|pm))',
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

    # 🎟 Cost
    for line in lines:
        if "🎟" in line:
            clean = line.split("🎟", 1)[1].strip()
            # Remove leading emoji variant selectors
            clean = re.sub(r'^[\u2600-\u27bf\u2190-\u21ff\u2300-\u23ff\u2b00-\u2bff\ufe00-\ufe0f]', '', clean).strip()
            result["cost"] = clean
            break

    # 👶 Age group
    for line in lines:
        if "👶" in line:
            clean = line.split("👶", 1)[1].strip()
            result["age_group"] = clean
            break

    return result


async def extract_text_from_image(image_url: str) -> str:
    """Use vision model to extract text content from an Instagram image."""
    if _vision_model is None:
        return ""
    try:
        return await _vision_model(image_url, question="Extract all text from this image, including dates, times, locations, prices, and event details. Return the raw text.")
    except Exception as e:
        print(f"  Vision error: {e}")
        return ""


async def scrape_hashtag_graphql(sessionid: str, hashtag: str, max_posts: int = 20) -> list[dict]:
    """Scrape Instagram hashtag posts using the private GraphQL endpoint.
    
    This uses instagrapi internally with a real session cookie (no Business account needed).
    The session cookie can be obtained by:
    1. pip install instagrapi
    2. python3 -c "from instagrapi import Client; cl = Client(); cl.login('user','pass')"
    3. Export the sessionid cookie: export INSTAGRAM_SESSIONID='...'
    """
    from instagrapi import Client
    
    # Reconstruct session from sessionid cookie
    cl = Client()
    # Set up the public session with the sessionid
    import os
    proxy = os.environ.get("INSTAGRAM_PROXY", "")
    if proxy:
        cl.set_proxy(proxy)
    
    # Use the sessionid to authenticate
    cl.session = {
        "sessionid": sessionid,
        "csrftoken": "test",
    }
    # Manually set the cookie in the public session
    cl.public.headers.update({"Cookie": f"sessionid={sessionid}; csrftoken=test"})
    
    events = []
    try:
        medias, _ = cl.hashtag_medias_paginated_gql(hashtag, amount=max_posts)
        for m in medias:
            caption = m.get("caption", "") or ""
            parsed = parse_caption_for_event(caption)
            
            event = {
                "title": parsed["title"],
                "date_raw": parsed["date_raw"],
                "time_raw": parsed["time_raw"],
                "location_raw": parsed["location_raw"],
                "cost": parsed["cost"],
                "age_group": parsed["age_group"],
                "description": caption[:500],
                "url": m.get("permalink", "") or f"https://www.instagram.com/p/{m.get('code', '')}/",
                "image_url": m.get("url") or m.get("thumbnail_url", ""),
                "source": f"instagram:{hashtag}:{m.get('code', '')}",
                "hashtags": parsed["hashtags"],
                "timestamp": m.get("timestamp", ""),
            }
            
            # If caption parsing didn't find date/location, try OCR on image
            if not parsed["date_raw"] and event["image_url"] and _vision_model:
                ocr_text = await extract_text_from_image(event["image_url"])
                if ocr_text:
                    ocr_parsed = parse_caption_for_event(ocr_text)
                    if not event["date_raw"]:
                        event["date_raw"] = ocr_parsed["date_raw"]
                    if not event["location_raw"]:
                        event["location_raw"] = ocr_parsed["location_raw"]
            
            events.append(event)
            print(f"  + {event['title'][:40]} | {event.get('date_raw', '')[:20]}")
            
    except Exception as e:
        print(f"  ERROR for #{hashtag}: {e}")
        
    return events


async def scrape_hashtag_api(token: str, user_id: str, hashtag: str, max_posts: int = 20) -> list[dict]:
    """Scrape Instagram hashtag posts via the official Graph API.
    
    Requires:
    1. Instagram Business/Creator account
    2. Facebook App with Instagram Graph API product
    3. User access token with instagram_basic + pages_show_list scopes
    4. App Review approval for instagram_public_content_access
    """
    events = []
    session_timeout = aiohttp.ClientTimeout(total=30)
    
    async with aiohttp.ClientSession(timeout=session_timeout) as session:
        # Step 1: Search for hashtag to get its ID
        search_url = f"{IG_BASE}/{IG_API_VERSION}/ig_hashtag_search"
        params = {"user_id": user_id, "q": hashtag, "access_token": token}
        
        async with session.get(search_url, params=params) as resp:
            if resp.status != 200:
                error = await resp.json()
                print(f"  Hashtag search failed for #{hashtag}: {error}")
                return events
            data = await resp.json()
        
        if not data.get("data"):
            print(f"  No hashtag found: #{hashtag}")
            return events
        
        hashtag_id = data["data"][0].get("id")
        print(f"  Hashtag #{hashtag} → ID: {hashtag_id}")
        
        # Step 2: Get recent media for this hashtag
        media_url = f"{IG_BASE}/{IG_API_VERSION}/{hashtag_id}/recent_media"
        fields = "id,caption,media_url,permalink,timestamp,media_type"
        params = {
            "user_id": user_id,
            "fields": fields,
            "limit": max_posts,
            "access_token": token,
        }
        
        async with session.get(media_url, params=params) as resp:
            if resp.status != 200:
                error = await resp.json()
                print(f"  Media fetch failed: {error}")
                return events
            data = await resp.json()
        
        for item in data.get("data", []):
            caption = item.get("caption", "") or ""
            parsed = parse_caption_for_event(caption)
            
            event = {
                "title": parsed["title"],
                "date_raw": parsed["date_raw"],
                "time_raw": parsed["time_raw"],
                "location_raw": parsed["location_raw"],
                "cost": parsed["cost"],
                "age_group": parsed["age_group"],
                "description": caption[:500],
                "url": item.get("permalink", ""),
                "image_url": item.get("media_url", ""),
                "source": f"instagram:{hashtag}:{item.get('id', '')}",
                "hashtags": parsed["hashtags"],
                "timestamp": item.get("timestamp", ""),
            }
            
            # OCR fallback for image-heavy posts
            if not parsed["date_raw"] and event["image_url"] and _vision_model:
                ocr_text = await extract_text_from_image(event["image_url"])
                if ocr_text:
                    ocr_parsed = parse_caption_for_event(ocr_text)
                    event["date_raw"] = ocr_parsed["date_raw"] or event["date_raw"]
                    event["location_raw"] = ocr_parsed["location_raw"] or event["location_raw"]
            
            events.append(event)
            print(f"  + {event['title'][:40]} | {event.get('date_raw', '')[:20]}")
        
        # Rate limit: Instagram allows 30 hashtag searches/hour + 200 media/hour
        # With 10 hashtags, we use ~30 searches + ~200 media = right at the limit
        await asyncio.sleep(30)  # Be safe and wait between hashtags
    
    return events


async def scrape_instagram_playwright(sessionid: str, hashtags: list[str]) -> list[dict]:
    """Scrape Instagram hashtag posts using Playwright with your session cookies.

    This approach uses YOUR Instagram account login (via sessionid cookie) to:
    1. Load Instagram's web SPA WITH your authentication
    2. Intercept the GraphQL API calls that the authenticated page makes
    3. Extract post data (captions, timestamps, image URLs) from API responses
    4. Parse event details from captions using the emoji-marker parser

    To get your sessionid cookie:
      1. Log into instagram.com in your browser
      2. Open DevTools → Application → Cookies → https://www.instagram.com
      3. Copy the value of the 'sessionid' cookie
      4. Export it: export INSTAGRAM_SESSIONID="your_cookie_value_here"

    This works because Instagram's web app (www.instagram.com) makes GraphQL
    API calls to /graphql/query/ when authenticated. Playwright intercepts
    these responses and extracts the post data directly from the JSON.
    """
    from playwright.async_api import async_playwright

    # Instagram needs several cookies for authentication
    cookies = {
        "sessionid": sessionid,
        "csrftoken": os.environ.get("INSTAGRAM_CSRFTOKEN", "test"),
    }
    # Some users also need ds_user_id
    ds_user_id = os.environ.get("INSTAGRAM_DS_USER_ID", "")
    if ds_user_id:
        cookies["ds_user_id"] = ds_user_id

    all_events = []
    UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

    print(f"  Initializing Playwright with session cookies...")

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage",
                      "--disable-blink-features=AutomationControlled"]
            )
            context = await browser.new_context(
                user_agent=UA,
                viewport={"width": 1280, "height": 720},
                locale="en-US",
            )

            # Set cookies
            cookie_list = [
                {"name": name, "value": value, "domain": ".instagram.com", "path": "/",
                 "httpOnly": name == "sessionid"}
                for name, value in cookies.items()
            ]
            await context.add_cookies(cookie_list)
            print(f"  Set {len(cookie_list)} Instagram cookies")

            page = await context.new_page()

            # Intercept GraphQL responses
            intercepted_posts = []

            async def handle_response(response):
                url = response.url
                # Instagram's web app fetches hashtag data from /graphql/query/
                # The response contains JSON with edge_hashtag_to_media
                if "graphql/query/" in url or "api/graphql" in url:
                    try:
                        body = await response.text()
                        if "shortcode" in body and "edge_hashtag" in body:
                            # Parse the JSON response
                            # Instagram prefixes with `)" or `for (;;);`
                            clean_body = body.lstrip(');"')
                            try:
                                data = json.loads(clean_body)
                                hashtag_data = data.get("data", {}).get("hashtag", {})
                                if hashtag_data:
                                    edges = hashtag_data.get("edge_hashtag_to_media", {}).get("edges", [])
                                    for edge in edges:
                                        node = edge.get("node", {})
                                        if node.get("shortcode"):
                                            intercepted_posts.append(node)
                                    print(f"  Intercepted: {len(edges)} posts from GraphQL response")
                            except json.JSONDecodeError:
                                pass
                    except Exception:
                        pass

            page.on("response", handle_response)

            # Load each hashtag page
            for hashtag in hashtags:
                url = f"https://www.instagram.com/explore/tags/{hashtag}/"
                print(f"\n  Scraping #{hashtag}...")

                try:
                    resp = await page.goto(url, timeout=30000, wait_until="networkidle")
                    await asyncio.sleep(5)  # Wait for initial JS rendering

                    # Scroll to trigger lazy loading
                    for _ in range(3):
                        await page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
                        await asyncio.sleep(2)

                    page_title = await page.title()
                    if "login" in page_title.lower() or "log in" in page_title.lower():
                        print(f"    ERROR: Session invalid — Instagram redirected to login page")
                        print(f"    Your sessionid cookie may be expired. Please refresh it.")
                        continue

                    before_count = len(intercepted_posts)

                    # Wait a bit more for any delayed GraphQL responses
                    await asyncio.sleep(3)

                    new_posts = intercepted_posts[before_count:]
                    print(f"    Found {len(new_posts)} posts")

                    # Parse each post
                    for node in new_posts[:20]:  # Limit per hashtag
                        event = parse_instagram_node(node, hashtag)
                        if event:
                            all_events.append(event)
                            print(f"    + {event['title'][:40]} | {event.get('date_raw', '')[:20]}")

                    # Rate limit: wait between hashtags to avoid detection
                    await asyncio.sleep(5)

                except Exception as e:
                    print(f"    Error scraping #{hashtag}: {e}")
                    continue

            await browser.close()
            print(f"\n  Total: {len(all_events)} events from {len(hashtags)} hashtags")

    except Exception as e:
        print(f"  Critical error: {e}")
        import traceback
        traceback.print_exc()

    return all_events


def parse_instagram_node(node: dict, hashtag: str) -> Optional[dict]:
    """Parse an Instagram media node from GraphQL response into event data."""
    # Instagram's GraphQL returns media objects with various field names
    # depending on whether it's a regular post, carousel, or reel

    caption = ""
    # Try different caption field locations
    if "edge_media_to_caption" in node:
        edges = node.get("edge_media_to_caption", {}).get("edges", [])
        if edges:
            caption = edges[0].get("node", {}).get("text", "")
    if not caption:
        caption = node.get("caption", "") or ""

    parsed = parse_caption_for_event(caption)

    # Get image URL
    image_url = node.get("display_url", "")
    if not image_url and node.get("thumbnail_src"):
        image_url = node.get("thumbnail_src")
    if not image_url:
        # Carousel items
        carousel = node.get("edge_media_to_carousel", {}).get("edges", [])
        if carousel:
            image_url = carousel[0].get("node", {}).get("display_url", "")

    # Get permalink
    permalink = node.get("permalink", "")
    if not permalink:
        shortcode = node.get("shortcode", "")
        permalink = f"https://www.instagram.com/p/{shortcode}/" if shortcode else ""

    # Get timestamp
    timestamp = node.get("taken_at_timestamp", "")
    if not timestamp:
        timestamp = node.get("timestamp", "")

    # Get media type
    media_type = node.get("__typename", "GraphImage")

    event = {
        "title": parsed["title"],
        "date_raw": parsed["date_raw"],
        "time_raw": parsed["time_raw"],
        "location_raw": parsed["location_raw"],
        "cost": parsed["cost"],
        "age_group": parsed["age_group"],
        "description": caption[:500] if caption else "",
        "url": permalink,
        "image_url": image_url,
        "source": f"instagram:{hashtag}:{node.get('shortcode', '')}",
        "hashtags": parsed["hashtags"],
        "timestamp": str(timestamp) if timestamp else "",
        "media_type": media_type,
    }

    # OCR fallback for image-heavy posts (infographics)
    if not parsed["date_raw"] and image_url and _vision_model:
        ocr_text = asyncio.get_event_loop().run_until_complete(
            extract_text_from_image(image_url)
        )
        if ocr_text:
            ocr_parsed = parse_caption_for_event(ocr_text)
            event["date_raw"] = ocr_parsed["date_raw"] or event["date_raw"]
            event["location_raw"] = ocr_parsed["location_raw"] or event["location_raw"]
            if ocr_parsed["date_raw"]:
                event["description"] += f"\n[OCR]: {ocr_text[:200]}"

    return event


async def scrape_instagram_hashtags() -> list[dict]:
    """Scrape Instagram hashtags for Irish kids events.

    Uses one of three approaches depending on configuration:

    1. If INSTAGRAM_SESSIONID env var is set:
       Self-hosted scraping via Playwright with session cookies.
       This loads Instagram's web SPA WITH your session, intercepting
       the GraphQL API calls that the authenticated page makes.
       (No Business account needed — just a real Instagram login)

    2. If INSTAGRAM_ACCESS_TOKEN + INSTAGRAM_USER_ID env vars are set:
       Official Instagram Graph API (requires Business account + App Review)

    3. If neither is set:
       Returns empty list with setup instructions
    """
    import os
    
    sessionid = os.environ.get("INSTAGRAM_SESSIONID", "")
    token = os.environ.get("INSTAGRAM_ACCESS_TOKEN", "")
    user_id = os.environ.get("INSTAGRAM_USER_ID", "")
    
    if sessionid:
        print(f"  Using Playwright with session cookie (your Instagram login)")
        print(f"  Intercepting Instagram's GraphQL API calls for {len(KIDS_EVENT_HASHTAGS)} hashtags...")
        all_events = await scrape_instagram_playwright(sessionid, KIDS_EVENT_HASHTAGS)
        return all_events
    
    elif token and user_id:
        print(f"  Using Instagram Graph API (Business account)")
        all_events = []
        for hashtag in KIDS_EVENT_HASHTAGS:
            events = await scrape_hashtag_api(token, user_id, hashtag, max_posts=20)
            all_events.extend(events)
        return all_events
    
    else:
        print(f"\n=== Instagram Scraping Setup Required ===")
        print(f"  Option A (recommended, free): Set INSTAGRAM_SESSIONID cookie")
        print(f"    Install instagrapi: pip install instagrapi")
        print(f"    Login: python3 -c \"from instagrapi import Client; cl = Client(); cl.login('user', 'pass')\"")
        print(f"    Extract sessionid: cl.sessionid")
        print(f"    Export: export INSTAGRAM_SESSIONID='your_sessionid'")
        print(f"    Add proxy (optional): export INSTAGRAM_PROXY='http://proxy:port'")
        print(f"")
        print(f"  Option B (official API): Set INSTAGRAM_ACCESS_TOKEN + INSTAGRAM_USER_ID")
        print(f"    Requires Instagram Business account + Facebook App review")
        print(f"    Setup: https://developers.facebook.com/docs/instagram-api/guides/hastag-search")
        print(f"")
        print(f"  Hashtags monitored: {len(KIDS_EVENT_HASHTAGS)}")
        print(f"  No Instagram data will be collected without credentials.")
        return []


if __name__ == "__main__":
    # Quick test of caption parser
    print("=== Testing caption parser ===")
    test_caption = """Spooky Halloween Kids Disco! 🎃👻

📅 Oct 30th 3pm-5:30pm
📍 Main Street, Baldoyle, Co. Dublin
🎟️ €5 per child (adults free)
👶 Ages 2-10
✨ Face painting, disco dancing, trick or treating!

#kidsactivitiesireland #dublinkids #halloween #familyevents
"""
    result = parse_caption_for_event(test_caption)
    for k, v in result.items():
        print(f"  {k}: {v}")
