# Kids Events Ireland

Aggregator for kids/family events across Ireland. Scrapes multiple sources, deduplicates, and outputs a unified event feed.

## Data Sources

| Source | Tier | Auth Required | Notes |
|--------|------|---------------|-------|
| YourDaysOut.ie | 1 | No | JSON-LD schema.org/Event parsing |
| AllEvents.in | 1 | No | JSON-LD schema.org/Event parsing |
| Facebook Groups | 2 | No (scraped via Playwright) | Visible text extraction from group /events/ pages |
| Instagram Hashtags | 3 | **Yes** (sessionid cookie) | Playwright intercepts GraphQL API responses |

## Quick Start

```bash
# Clone
git clone https://github.com/vperrod/kidsevents-ie.git
cd kidsevents-ie

# Python environment
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Run Tier 1 (YourDaysOut + AllEvents only)
python3 main.py --tiers 1

# Run Tier 1 + 2 (add Facebook)
python3 main.py --tiers 1,2

# Run all tiers (requires Instagram setup — see below)
python3 main.py --tiers 1,2,3
```

## Instagram Setup (Required for Tier 3)

Instagram blocks all public/no-auth access. You must provide your Instagram session cookie.

### Option A: Use your own Instagram account (recommended)

1. Log into instagram.com in your browser
2. Open DevTools → Application → Cookies → https://www.instagram.com
3. Copy the value of the `sessionid` cookie
4. Set the environment variable:

```bash
export INSTAGRAM_SESSIONID="your_sessionid_cookie_here"
```

5. Optionally also export `INSTAGRAM_CSRFTOKEN` and `INSTAGRAM_DS_USER_ID` for better reliability.

### Option B: Official Instagram Graph API (Business account)

Requires converting a personal account to Business/Creator + Facebook App with App Review. See the docstring at the top of `instagram_scraper.py` for full instructions.

## Output Format

Events are saved to `events_output.json` with this schema:

```json
{
  "title": "Halloween Kids Disco",
  "description": "...",
  "start_date": "2025-10-30",
  "end_date": "",
  "venue_name": "Community Center",
  "venue_address": "Main Street, Baldoyle",
  "city": "Dublin",
  "county": "County Dublin",
  "country": "Ireland",
  "latitude": "53.384537",
  "longitude": "-6.401467",
  "url": "https://...",
  "cost": "€5 per child",
  "age_group": "Ages 2-10",
  "source": "instagram:kidseventsireland:...",
  "confidence": 0.5,
  "all_sources": ["instagram"],
  "all_urls": ["https://..."]
}
```

## Deduplication

Events from different sources are matched by:
1. Same URL (exact duplicate)
2. Title similarity > 0.8 + dates within 3 days
3. Same title + same location (city/county)
4. Same date + geo proximity (<5km) + title similarity > 0.5

Confidence scoring: YourDaysOut=1.0, AllEvents=0.9, Facebook=0.5, Instagram=0.3

## Architecture

```
cron (every 2-4 hours) → main.py →
  Tier 1 (2s):  YourDaysOut + AllEvents → JSON-LD → ~40 events
  Tier 2 (60s): Facebook 2+ groups     → Playwright → ~30 events
  Tier 3 (30s): Instagram 14 hashtags  → Playwright intercept → ~100 events
  Dedup:        Fuzzy match by title/date/geo
  → events_output.json → web frontend
```

## Requirements

```
instagrapi>=0.12.2
playwright>=1.40.0
requests>=2.31.0
```

Plus Playwright browsers:

```bash
playwright install chromium
```
