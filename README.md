# Kids Events Ireland

Aggregator for kids/family events across Ireland. Scrapes multiple sources, deduplicates, and outputs a unified event feed.

## Data Sources

| Source | Tier | Auth Required | Scraped via | Notes |
|--------|------|---------------|-------------|-------|
| YourDaysOut.ie | 1 | No | `requests` + JSON-LD | 100+ events, high quality |
| AllEvents.in | 1 | No | `requests` + JSON-LD | 15+ events, patchy coverage |
| FamilyFun.ie | 1 | No | `requests` + WP REST API | 100+ events, WordPress JSON-LD |
| IrelandMe.com | 1 | No | `requests` + HTML parsing | 2000+ events (filtered to ~50-100 family-relevant) |
| The Ark | 1 | No | `requests` + HTML parsing | Dublin children's cultural centre |
| Limerick.ie | 1 | No | `requests` + HTML parsing | City council events |
| Facebook Groups (Dublin) | 2 | No | Playwright | 2 Dublin family groups |
| Facebook Groups (Extended) | 2 | No | Playwright | Limerick + extended groups |
| DublinFamilyFun.ie | 2 | No | Playwright + JSON-LD | Next.js SPA with schema.org/Event |
| TotsSpots.com | 2 | No | Playwright + HTML | Ireland's largest kids classes directory |
| Meetup.com | 2 | No | Playwright + HTML | Public events across 5 cities |
| Eventbrite.ie | — | Yes (OAuth) | Blocked | Cloudflare bot protection blocks scraping; API requires OAuth2 |
| Instagram | 3 | Yes (sessionid) | `instagrapi` library | **Requires residential IP** — see setup below |
| Reddit | — | No | Playwright + PRAW | Community-shared events (not implemented yet) |

## Quick Start

```bash
# Clone
git clone https://github.com/vperrod/kidsevents-ie.git
cd kidsevents-ie

# Python environment
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Install Playwright browser (for Tier 2 scrapers)
playwright install chromium

# Run Tier 1 only (fast, no Playwright needed)
python3 main.py --tiers 1

# Run Tier 1 + 2 (includes Facebook, DublinFamilyFun, TotsSpots, Meetup)
python3 main.py --tiers "1,2" --limit 20

# Run with Instagram (requires sessionid — see below)
export INSTAGRAM_SESSIONID="your_sessionid_cookie_here"
python3 main.py --tiers "1,2,3" --limit 20
```

## Tier Structure

- **Tier 1 (fast)**: No browser needed. Uses `requests` + HTML/JSON-LD parsing. Runs in 5-10 seconds.
  - YourDaysOut.ie — JSON-LD schema.org/Event
  - AllEvents.in — JSON-LD schema.org/Event  
  - FamilyFun.ie — WordPress REST API + JSON-LD
  - IrelandMe.com — HTML table/list parsing
  - The Ark — HTML h3 heading parsing
  - Limerick.ie — LocalGov Drupal article parsing

- **Tier 2 (Playwright)**: Requires chromium browser. Runs in 30-120 seconds.
  - Facebook Groups — visible text extraction from group `/events/` pages
  - Extended Facebook — Limerick regional groups
  - DublinFamilyFun.ie — Next.js SPA, extracts JSON-LD blocks
  - TotsSpots.com — listing cards parsed from town/county pages
  - Meetup.com — public event search results across 5 cities

- **Tier 3 (Instagram)**: Requires Instagram sessionid cookie + residential IP.
  - Uses `instagrapi` library (private API)
  - Blocked from cloud/datacenter IPs

## Instagram Setup — IMPORTANT

Instagram blocks all public/no-auth scraping. You must provide your Instagram session cookie.

**⚠️ Instagram's API blocks requests from datacenter/cloud IPs. The sessionid must be used from a residential IP address. If you run this from a server (VPS, cloud VM, etc.), you will get `403 Forbidden` or `login_required` errors.**

### Option A: Run locally on your home machine (recommended)

This is the simplest approach — run the scraper from your residential IP where the sessionid works:

1. Clone this repo on your personal computer
2. Get your Instagram sessionid:
   - Log into instagram.com in your browser
   - Open DevTools → Application → Cookies → https://www.instagram.com
   - Copy the value of the `sessionid` cookie
3. Set the environment variable and run:

```bash
export INSTAGRAM_SESSIONID="your_sessionid_cookie_here"
python3 main.py --tiers "1,2,3" --limit 20
```

4. Transfer the output file to your server:

```bash
scp events_output.json user@your-server:/path/to/deployment/events_output.json
```

### Option B: Use a residential proxy on the server

If you need to run the scraper on a server (e.g., a cron job), you must route through a residential proxy:

```bash
# Set an HTTP residential proxy
export INSTAGRAM_PROXY="http://username:password@proxy-host:port"

# Or set a SOCKS5 proxy
export INSTAGRAM_PROXY="socks5://username:password@proxy-host:port"
```

Residential proxy providers that work with Instagram:
- BrightData (~$30/month for small usage)
- ScraperAPI (~$29/month)
- Oxylabs (~$30+/month)

## Deployment

### On a server (VM, VPS, etc.)

```bash
# Clone and set up
git clone https://github.com/vperrod/kidsevents-ie.git
cd kidsevents-ie
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium

# Run Tier 1+2 (works from any IP)
python3 main.py --tiers "1,2" --limit 20

# Serve the web frontend
python3 server.py  # Flask on port 8128
```

The scraper will skip Tier 3 (Instagram) automatically if `INSTAGRAM_SESSIONID` is not set.

### Cron job (server)

```bash
# Runs every 4 hours — Tier 1+2 only (Instagram requires residential IP)
0 */4 * * * cd /home/azureuser/kidsevents-ie && source venv/bin/activate && python3 main.py --tiers "1,2" --limit 20
```

### Local workflow for Instagram

On your home machine:

```bash
# Run with Instagram enabled (residential IP)
export INSTAGRAM_SESSIONID="..."
python3 main.py --tiers "1,2,3" --limit 20

# Sync results to server
rsync -avz events_output.json server:/home/azureuser/kidsevents-ie/events_output.json
```

## Running the Scraper

```bash
# Quick run (Tier 1 only, no auth needed)
./run_pipeline.sh

# Or run manually
python3 main.py --tiers "1,2" --limit 15
```

## Testing Individual Sources

```bash
# Test a specific scraper
python3 scrapers.py yourdaysout    # Tier 1a
python3 scrapers.py allevents     # Tier 1b
python3 scrapers.py familyfun     # Tier 1c
python3 scrapers.py irelandme     # Tier 1d
python3 scrapers.py ark           # Tier 1e
python3 scrapers.py limerick      # Tier 1f
python3 scrapers.py all           # All Tier 1 sources

# Test Playwright scrapers
python3 dublinfamilyfun_scraper.py
python3 totsspots_scraper.py
python3 meetup_scraper.py
```

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

Confidence scoring: YourDaysOut=1.0, AllEvents=0.9, FamilyFun=0.8, IrelandMe=0.7, The Ark=0.8, Limerick.ie=0.7, Facebook=0.5, DublinFamilyFun=0.7, TotsSpots=0.7, Meetup=0.6, Instagram=0.3

## Architecture

```
cron (every 4 hours) → main.py →
  Tier 1 (5-10s):
    YourDaysOut     → requests + JSON-LD → ~25 events
    AllEvents.in    → requests + JSON-LD → ~15 events
    FamilyFun.ie    → WP REST API + JSON-LD → ~100 events
    IrelandMe.com   → requests + HTML → ~50-100 events
    The Ark         → requests + HTML → ~14 events
    Limerick.ie     → requests + HTML → ~5-10 events

  Tier 2 (30-120s, requires chromium):
    Facebook Groups → Playwright → ~40 events
    DublinFamilyFun → Playwright + JSON-LD → ~20 events
    TotsSpots       → Playwright + HTML → ~50-100 listings
    Meetup.com      → Playwright + HTML → ~20 events

  Dedup: Fuzzy match by title/date/geo
  → events_output.json → Flask API + web frontend

Live at: https://claude-dev-vperrod.westeurope.cloudapp.azure.com/kidsevents/
```

## File Structure

```
kidsevents-ie/
├── main.py                        # Orchestrator — runs tiers, deduplicates, outputs JSON
├── scrapers.py                    # Tier 1 scrapers (fast, no browser)
├── facebook_scraper.py            # Tier 2a: Facebook groups (Playwright)
├── facebook_extended_scraper.py   # Tier 2b: Extended Facebook groups
├── dublinfamilyfun_scraper.py     # Tier 2c: DublinFamilyFun.ie (Playwright + JSON-LD)
├── totsspots_scraper.py           # Tier 2d: TotsSpots.com (Playwright)
├── meetup_scraper.py              # Tier 2e: Meetup.com (Playwright)
├── instagram_scraper.py           # Tier 3: Instagram hashtags (requires sessionid)
├── deduplicator.py                # Fuzzy event matching + dedup
├── server.py                      # Flask API serving events + web frontend
├── web/
│   └── index.html                 # Frontend with calendar + event browser
├── run_pipeline.sh                # Quick run script (Tier 1 only)
├── requirements.txt
└── events_output.json             # Generated output (gitignored)
```

## Requirements

```
instagrapi>=0.12.2
playwright>=1.40.0
requests>=2.31.0
flask>=3.0.0
```

Plus Playwright browsers:

```bash
playwright install chromium
```
