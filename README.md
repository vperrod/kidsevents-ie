# Small Days (kidsevents-ie)

Aggregator for kids/family events, places and holiday ideas across Ireland.
Discovers listing pages, extracts structured records, gates them for quality,
and serves the result as JSON to the public site and the admin portal.

Live at: https://claude-dev-vperrod.westeurope.cloudapp.azure.com/kidsevents/

## Pipeline

Three modules, nothing else:

| Module | Role |
|---|---|
| `factory_worker.py` | The factory. One cycle per run: prune events that are over → discover URLs (local search gateway + the curated deep listing pages in `sources.json`) → crawl with crawl4AI → extract (schema.org `Event` JSON-LD first, Hermes LLM for free-text pages) → normalise → publish through the quality gate. Run hourly by `kidsevents-factory.timer`. |
| `staging.py` | The staging desk for social candidates collected by the mini PC crew. Appends them to `staged/social_candidates.json` and, with `AUTO_APPROVE=on` (the default), runs each through the same gate immediately. Anything that does not clear it stays `needs_review` with a stored `reason`. |
| `server.py` | Flask on `127.0.0.1:8128` (user unit `kidsevents-ie.service`): the public `/api/events`, `/api/places`, `/api/holidays` feeds, the member save API, and the `/admin` portal with its `/admin/api/*` routes. |

```
kidsevents-factory.timer (hourly) → factory_worker.run_discovery_cycle()
mini PC social crew (ssh)         → staging.py append → factory_worker.promote_candidate()
                                       ↓ quality gate ↓
        events_output.json · places_output.json · holidays_output.json
                                       ↓
                              server.py → web/ + /admin
```

### Quality gate

Nothing is written to a catalogue without passing `event_reject_reason()` /
`place_reject_reason()`, and every rejection is logged with its reason:

- `country` must be `IE` (Northern Ireland and Britain fold to `GB`, everything else to `other`)
- `family_relevant` must not be false
- the title must exist, be at most 120 characters, and must not be the source caption or the front of it
- an event must have a `start_date`, and it must fall inside `today … today + 60 days`

`confidence` is not a model self-report: it is the fraction of ten fields a
parent actually needs (`start_date`, `venue_name`, `city`, `county`,
lat/lon, `cost`, `age_group`, `category`, a description of at least 120
characters, `website`) that are filled in.

### Storage

Every JSON store is written with `write_json_atomic()` (temp file in the same
directory, fsync, `os.replace`) and read with `load_json_store()`, which
returns the default only for a missing or blank file — a file with content in
it that will not parse raises rather than reporting an empty catalogue.
`output_lock()` (re-entrant per thread) serialises every read-modify-write
across the factory timer, the admin approve route and the sweep.

## Running it

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
playwright install chromium

venv/bin/python3 factory_worker.py            # one discovery cycle, all cities
TARGET_CITIES=galway venv/bin/python3 factory_worker.py   # one city

venv/bin/python3 staging.py sweep             # classify the needs_review backlog
SWEEP_LIMIT=5 venv/bin/python3 staging.py sweep           # …just the first 5

venv/bin/python3 server.py                    # Flask on 127.0.0.1:8128
venv/bin/python3 -m pytest -q                 # tests (no network, no LLM)
```

Useful environment (from `.env` or the systemd unit): `TARGET_CITIES`,
`MAX_LLM_PER_CYCLE`, `CRAWL_WALL_SECS`, `DISCOVER_WAIT_SECS`, `SEARCHGW_BASE`,
`AUTO_APPROVE`, `SWEEP_LIMIT`, `HERMES_PROVIDER`, `HERMES_MODEL`.

## Output format

`events_output.json` is a list of records shaped like this:

```json
{
  "title": "Toddler Storytime at Pearse Street Library",
  "description": "...",
  "start_date": "2026-09-20",
  "end_date": "2026-09-20",
  "time": "10:30",
  "duration_hours": 1.0,
  "venue_name": "Pearse Street Library",
  "venue_address": "138-144 Pearse St, Dublin 2",
  "city": "Dublin",
  "county": "Dublin",
  "country": "IE",
  "family_relevant": true,
  "latitude": "53.3441",
  "longitude": "-6.2527",
  "url": "https://...",
  "website": "https://...",
  "image_url": "https://...",
  "image_alt": "...",
  "cost": "free",
  "cost_detail": "Free, no booking needed",
  "age_group": "toddler",
  "category": "workshop",
  "suitable_for": "pushchair_accessible",
  "booking_required": "none",
  "booking_url": "",
  "phone": "+353 1 222 8488",
  "contact_email": "libraries@dublincity.ie",
  "source": "web:https://...",
  "all_sources": ["https://..."],
  "all_urls": ["https://..."],
  "confidence": 0.9
}
```

See `EVENT_DATA_CONTRACT.md` for what the public site must show for every
event, and `DESIGN.md` for the front-end.

## Sources

`sources.json` holds the curated deep listing URLs per city, by category
(`tourism`, `timeout`, `familyfriendly`, `yourdaysout`, `listings`). Only
categories listed in `discover_events()` are crawled. Entries that go dead or
start blocking get removed rather than retried — Eventbrite (405), Songkick,
Facebook groups (login wall) and `visitcork.com` (broken certificate) are all
out for that reason.

## File structure

```
kidsevents-ie/
├── factory_worker.py        # discovery, extraction, normalisation, quality gate
├── staging.py               # social candidate staging desk + auto-approve sweep
├── server.py                # Flask API + admin portal
├── firebase_auth.py         # member identity verification
├── member_store.py          # member saves (sqlite)
├── sources.json             # curated listing URLs per city
├── web/                     # index.html (public) + admin.html
├── staged/                  # social_candidates.json
├── systemd/                 # unit examples
└── events_output.json · places_output.json · holidays_output.json
```
