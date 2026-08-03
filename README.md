# Kids Events Ireland

Aggregator for kids/family events across Ireland. Scrapes multiple sources, deduplicates, and outputs a unified event feed.

## Data Sources

| Source | Tier | Auth Required | Notes |
|--------|------|---------------|-------|
| YourDaysOut.ie | 1 | No | JSON-LD schema.org/Event parsing |
| AllEvents.in | 1 | No | JSON-LD schema.org/Event parsing |
| Facebook Groups | 2 | No (Playwright) | Visible text extraction from group event pages |
| Instagram Hashtags | 3 | **Yes** (sessionid cookie) | Uses instagrapi library (private API) — **requires residential IP** |

## Quick Start

```bash
# Clone
git clone https://github.com/vperrod/kidsevents-ie.git
cd kidsevents-ie

# Python environment
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Install Playwright browser (for Facebook scraper / Tier 2)
playwright install chromium

# Run Tier 1 (YourDaysOut + AllEvents only)
python3 main.py --tiers 1

# Run Tier 1 + 3 (add Instagram — requires sessionid)
python3 main.py --tiers 1,3

# Run all tiers
python3 main.py --tiers 1,2,3
```

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
python3 main.py --tiers "1,3" --limit 15
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

Confidence scoring: YourDaysOut=1.0, AllEvents=0.9, Facebook=0.5, Instagram=0.3

## Architecture

```
cron (every 4 hours) → main.py →
  Tier 1 (2s):  YourDaysOut + AllEvents → JSON-LD → ~25 events
  Tier 2 (60s): Facebook groups     → Playwright → ~20 events (optional)
  Tier 3 (10s): Instagram hashtags  → instagrapi → ~50 events (local only)
  Dedup:        Fuzzy match by title/date/geo
  → events_output.json → Flask API + web frontend

Live at: https://claude-dev-vperrod.westeurope.cloudapp.azure.com/kidsevents/
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
