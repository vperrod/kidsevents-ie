# Small Days (kidsevents-ie)

Aggregator for kids/family events, places and holiday ideas across Ireland.
Discovers listing pages, extracts structured records, gates them for quality,
and serves the result as JSON to the public site and the admin portal.

Live at: https://claude-dev-vperrod.westeurope.cloudapp.azure.com/kidsevents/

## Pipeline

The modules, and nothing else:

| Module | Role |
|---|---|
| `factory_worker.py` | The factory. One cycle per run: prune events that are over → run the discovery lanes → stage what they found → research each new candidate through `promote()` (fetch → classify → facts → extract → write) → publish through the quality gate. Run hourly by `kidsevents-factory.timer`, one cycle at a time (`daemon.lock`). |
| `discovery/` | The eight lanes that find things to research, plus the URL ledger that stops the factory re-researching what it published last hour — see below. |
| `llm.py` | Model routing. Every LLM call in the project goes through `complete(prompt, kind)`, which picks the cheapest lane that can answer right now. |
| `contract.py` + `gate.py` + `catalog/facets.json` | The record contract, the facet vocabulary and the QA gate — see below. Every record written to a catalogue has passed both. |
| `links.py` + `media.py` | What a record links to and what it looks like. `links.resolve()` finds the official site, the Instagram and the TikTok account (markup → Wikidata → the venue's own site → a search whose result must fold onto the venue's name), verifies each before storing it; `media.attach()` downloads a CC0/CC BY/CC BY-SA hero from Wikimedia Commons or Openverse, puts it through a vision gate, and turns Instagram and TikTok posts into embeds — they are never rehosted. Both run at the end of `promote()` and nightly (`links.py refresh`, `media.py refresh`) — see below. |
| `staging.py` | The staging desk for social candidates collected by the mini PC crew. Appends them to `staged/social_candidates.json` and, with `AUTO_APPROVE=on` (the default), runs each through the same gate immediately. Anything that does not clear it becomes `needs_input` with the one `missing_field` a curator's note would fix, or `rejected`. |
| `server.py` | Flask on `127.0.0.1:8128` (user unit `kidsevents-ie.service`): the public `/api/events`, `/api/places`, `/api/holidays` feeds (the legacy view), the full-contract `/api/v1/*` feeds, the member save API, and the `/admin` portal with its `/admin/api/*` routes. |

```
kidsevents-factory.timer (hourly) → run_discovery_cycle() → discovery.run_all()
                                          ↓ staged/candidates.json
mini PC social crew (ssh)         → staging.py append → staged/social_candidates.json
                                          ↓
                            factory_worker.promote()
              research fetch → classify → facts → extract → write → gate.qa()
                                          ↓
        events_output.json · places_output.json · holidays_output.json
                                          ↓
                              server.py → web/ + /admin
```

### Discovery lanes

`discovery/lanes/<name>.py` each expose `run(state) -> list[Candidate]`, where
a candidate is the same dict the social desk stores: `source_url`,
`found_via: "<lane>:<key>"`, and optionally `title`, `caption` (text the lane
already holds, merged with the live fetch), `text` (a page the lane already
crawled — skips the fetch), `county`, `location` (coordinates from open data),
`prefill` (values computed rather than read) and `sources` (a second grounded
source, which a holiday needs to pass the gate).

| Lane | Cadence | What it reads |
|---|---|---|
| `listings` | every cycle | The curated deep listing pages in `sources.json` — a yourdaysout slug for each of the 26 Republic counties plus the hand-picked city and national pages, on a rotating cursor. schema.org `Event` markup becomes candidates directly; otherwise the page is reduced to its own link index in code and one model call picks the links that are things a family can go to. |
| `feeds` | every cycle | The RSS/Atom and ICS feeds in `catalog/lanes.json`. Stdlib parsing (`xml.etree`, a VEVENT reader). |
| `sitemaps` | every cycle | `sitemap.xml` of each curated domain, filtered to `event\|whats-on\|things-to-do\|family\|kids`. |
| `search` | every cycle | `catalog/search_templates.json` × 32 counties × the current season, through the search gateway only. 40 queries a cycle on a rotating cursor. |
| `opendata_places` | weekly | OpenStreetMap Overpass (one bbox per province), `data.gov.ie` CSVs found through its CKAN API, Coillte and Blue Flag pages. Fáilte Ireland is registration-gated and logs "no key" until one exists. |
| `wikidata` | weekly | SPARQL for Irish museums, attractions, zoos and parks with a website (P856) and an Instagram handle (P2003), merged against what is already on air by name-fold and a 1 km geo match. |
| `ticketmaster` | every cycle | Discovery API, `countryCode=IE`, family classification. Needs `TICKETMASTER_API_KEY`; logs "no key" and skips without one. |
| `holidays_seed` | weekly | A cached model-generated seed list of destinations (`catalog/holiday_seeds.json`), `batch` of them researched per run from its Wikivoyage article *and* the official tourism site Wikidata records — two domains, which is what the gate demands of a holiday — with `best_months` computed from Open-Meteo's daily archive and `direct_flight`/`flight_time_from_dublin` from OpenFlights. |

`catalog/lanes.json` holds each lane's `enabled` flag and its sources.
`discovery/ledger.json` (git-ignored) records what was researched and when, so
a URL is looked at once and re-checked no sooner than its kind's window —
3 days for an event, 30 for a place, 60 for a holiday. A URL whose window has
expired is re-staged and researched again, so a listing that changed is picked
up rather than remembered as whatever it was last week. `promote()` answering
"no model lane answered" is a lane outage, not a verdict: the ledger does not
stamp it and the candidate comes back next cycle.

A lane that caps how many candidates it offers per source (`sitemaps`,
`opendata_places`, `wikidata`) drops the already-researched ones *before* that
cap, so each run works further down its list. The others rotate on a cursor
kept in `factory_state.json["lane_cursor"]`.

Every lane writes a `{lane, key, found, new, errors, ms}` row into
`factory_state.json["lanes"]`, which is what the admin Sources area reads.

### Model routing

`llm.complete(prompt, kind)` is the only way this project talks to a model.
It walks the lanes cheapest-first and an unavailable lane is never an error,
just the next lane:

| # | Lane | When it is used |
|---|---|---|
| 1 | `local` — the mini PC's `llama-server`, over the SSH forward on `127.0.0.1:18089` | `/health` is ok, `llamacpp:requests_processing` is below `LOCAL_BUSY_AT` (WanderTold shares those 2 slots), and the prompt is at most `LOCAL_MAX_PROMPT_CHARS`. A dead tunnel falls through in under 2 s. |
| 2 | named free OmniRoute lanes (`ROUTING_LANES`), round-robin | The roster is probed once per process and only the lanes that answer a 5-token test are used; a lane that errors mid-run is parked for `LANE_PARK_SECS`. Free lanes rot constantly (404 / 402 / 429 / "cooling down"). |
| 3 | OmniRoute `auto/best-free` | Nothing named answered. |
| 4 | the `hermes` CLI | Last: slowest lane (22–90 s) and throttled at ~1 req/min per model. On 2026-09-13 it timed out on *every* call, which is what left the factory with no verdicts and why the lanes above exist. |
| 5 | `""` | Nothing answered — callers already treat an empty answer as "no verdict". |

The gateway is on loopback and takes no auth header; no key or token is stored
for any of this. Every attempt appends a line to `routing.jsonl`
(`{ts, kind, lane, model, ms, ok, prompt_chars, err}`), and each cycle/sweep
folds today's lines into `factory_state.json` → `llm`
(`calls_today`, `by_lane.{calls,errors,p50_ms}`, `last_probe`) for the admin
Production view.

The local lane needs the SSH forward to the mini PC, a user unit on this VM:

```bash
systemctl --user status kidsevents-llama-tunnel.service   # ssh -N -L 18089:127.0.0.1:8089 mini-pc
curl -s 127.0.0.1:18089/health
```

Nothing is installed or changed on the mini PC — it is a plain local forward
onto the llama-server it already runs.

### The record contract

Events, Things to do (places) and Holidays are one shape, defined in
`contract.py` and stored in the three catalogue files. `contract.EMPTY_RECORD(kind)`
is the whole shape; `contract.validate(record)` returns the structural
violations; `contract.derive(record)` fills in everything that must never be
authored by a model — `location.region` from the county, `location.ireland`
from the country, `links.maps_url` (a plain `google.com/maps/search/?api=1&query=…`
link, never a billed Places API), the slug and id, and `provenance.confidence`,
which is a measured completeness score rather than a model's self-report.

```
schema_version, id, kind, title (≤80), slug, summary (≤160), description, family_relevant
location:   name, address, city, county (one of the 32), region, country, lat, lon, ireland
links:      source_url, official_url, maps_url, instagram_url, tiktok_url, booking_url
media:      hero { url, file, credit, licence, source, gate, alt } | null, embeds[]
taxonomy:   age_bands[], price_band, price_detail, setting, activity_types[], rainy_ok, accessibility[]
provenance: sources[] {url, fetched_at}, facts[] {claim, quote, source_url}, confidence, last_checked, produced_by[]
status:     on-air | needs-input | rejected, reason, hint
event:      start_date, end_date, times[], recurrence, organizer, booking_required, date_evidence, cancelled
place:      opening_hours, duration_hint, seasonal_note
holiday:    destination_type, holiday_types[], best_seasons[], best_months[], school_breaks[],
            flight_time_from_dublin, direct_flight, budget_band, with_baby_toddler, includes[]
```

`contract.legacy_view(record)` flattens a record onto the flat keys
`web/index.html` reads, which is what the public `/api/*` routes serve; the
full record is at `/api/v1/{events,places,holidays}` and on `/admin/api/*`.
A record with no `schema_version` predates the contract and passes through the
legacy view untouched, so the site keeps working during the migration.

### Facet vocabulary

`catalog/facets.json` is the whole vocabulary: age bands, price bands, setting,
the 20 activity types, accessibility, the 32 counties (region + Northern
Ireland flag + the old allevents county codes), and the holiday facets
including the 2026/27 school-break dates. `gate.gate_meta(taxonomy)` folds a
model's answer onto it through the legacy alias maps and **drops** anything it
still cannot recognise — it never substitutes a default, because an invented
category that looks curated is worse than a missing one. Every drop is counted
into `factory_state.json.facet_drops`, and `gate.facet_gloss(kind)` renders the
same vocabulary into the write prompt so the model picks from it.

### Quality gate

`gate.qa(record, sources_text, on_air_titles)` returns `(ok, reason, missing_field)`
and is the only thing that puts a record on air. The split matters as much as
the verdict: a truthy `missing_field` names one thing a curator's note could
supply (`needs-input`, the item stays in the desk), an empty one means nothing
anybody types would help (`rejected`).

Rejected: not family-relevant · the event is already over · title is the raw
caption · no source at all · not in Ireland (event/place) · Irish record whose
coordinates are not in Ireland · description that is a link list rather than
prose · duplicate of an item already on air.

Needs-input, with the field named: `title` · `summary` · `description` (60
words for an event, 120 for a place or holiday) · `county` · `facts` (nothing
in the source was quoted, so nothing is grounded) · `start_date` ·
`date_evidence` (missing, or not actually present in the source text) ·
`price_band` · `age_bands` · `activity_types` · `address` · `sources` (a
holiday needs two independent domains) · `holiday_types` · `best_seasons` ·
`caption` (the fetch was rate-limited).

### The four steps

`factory_worker.promote(candidate)` is the whole ingestion path: one research
fetch and then at most four model calls per item, each with an explicit JSON
contract and "do not invent values":

1. `research_fetch()` — fetch the candidate's own source from this VM with a
   desktop Chrome UA (TikTok captions come keylessly from
   `tiktok.com/oembed`), one request per 2 s per platform. A 401/403/429 is a
   rate limit, not a bad candidate: it becomes needs-input `caption`.
2. `classify()` — kind, family relevance, country.
3. `gather_facts()` — claims with verbatim quotes. **A quote that is not
   actually a substring of the source text is dropped in code**; this is the
   grounding every later step is limited to.
4. `extract_details()` — the kind-specific fields. A page carrying schema.org
   `Event` JSON-LD pre-fills `event.*` and skips this step.
5. `write_copy()` — title, summary, description and the taxonomy, from the
   verified facts only.

### Links and media

`promote()` finishes a record it has already put on air by resolving its links
and attaching its media. Neither can block publication: both are wrapped, and a
venue with no website and no photo is still a venue.

**Links** (`links.py`). `official_url` comes from the source page's JSON-LD
(`url`/`sameAs`), then Wikidata `P856` disambiguated by county, then an
outbound link on the source page whose domain folds onto the venue's name — and
is stored only after it answers a request. Instagram and TikTok come from the
same markup, from Wikidata (`P2003`/`P7085`), from the official site's own
footer, or from a `site:instagram.com "<venue>" <county>` search, and a handle
is accepted **only when it folds onto the venue's name**: a listing page's
`sameAs` names the publisher's account, not the venue's. A record found on
Instagram or TikTok keeps the author's account when `classify` says
`is_venue_account`. `links_checked` stamps the pass; `links.py refresh` re-runs
the records that are still missing one, at most every 14 days each.

**Media** (`media.py`). A hero photo is downloaded from Wikimedia Commons or
Openverse, and only under CC0, CC BY, CC BY-SA or public domain, with the
photographer and the licence kept beside it for the credit line. When neither
has anything, the site's own `og:image` is stored flagged `placeholder` and is
replaced the first time a licensed one turns up. Every candidate goes through a
vision gate — one call, local model first, rules lifted from WanderTold's
`photo-gate.py` — which answers `hero`/`pass`/`reject` and writes the alt text
in the same reply; a gate that cannot answer marks it `skip` and keeps the
photo, because an outage is not a verdict. At most 3 downloads and 3 gate calls
per record, 200 records per `media.py refresh`.

Instagram and TikTok photos are **never** downloaded. A post becomes
`media.embeds[] = [{platform, url}]` and is rendered as an iframe by the
browser looking at it, only when a detail modal opens. Files live in
`web/media/<kind>/` (git-ignored, served by Flask's static route at `/media/…`)
and `media_index.json` records what each one is and where it came from.

Both refreshes run nightly, links first so a record has its official site
before media looks for one: `kidsevents-links-refresh.timer` (03:00 UTC) and
`kidsevents-media-refresh.timer` (03:30 UTC, `After=` the links unit), both
`Type=oneshot` matching `kidsevents-factory.timer`, unit files in
`~/.config/systemd/user/` on the VM (not tracked in this repo — a plain
`ExecStart=venv/bin/python3 links.py refresh 200` / `media.py refresh`).

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

venv/bin/python3 factory_worker.py            # one discovery cycle, every enabled lane
venv/bin/python3 factory_worker.py --lane feeds --budget 10   # one lane, 10 candidates

venv/bin/python3 staging.py sweep             # classify the needs_review backlog
SWEEP_LIMIT=5 venv/bin/python3 staging.py sweep           # …just the first 5
SWEEP_WORKERS=6 venv/bin/python3 staging.py sweep         # …6 items in flight (default 3)

venv/bin/python3 links.py refresh 200          # nightly: on-air records still missing a link
venv/bin/python3 media.py refresh 200          # nightly: on-air records still missing a hero

venv/bin/python3 server.py                    # Flask on 127.0.0.1:8128
venv/bin/python3 -m pytest -q                 # tests (no network, no LLM)
```

Useful environment (from `.env`, the systemd unit, or the command line):
`MAX_CANDIDATES_PER_CYCLE`, `TICKETMASTER_API_KEY`, `CRAWL_WALL_SECS`, `CYCLE_WALL_SECS`, `DISCOVER_WAIT_SECS`,
`SEARCHGW_BASE`, `AUTO_APPROVE`, `SWEEP_LIMIT`, `SWEEP_WORKERS`,
`MAX_ITEM_SECS`, and the routing knobs `LOCAL_LLM_URL`, `LOCAL_BUSY_AT`,
`LOCAL_MAX_PROMPT_CHARS`, `OMNIROUTE_URL`, `ROUTING_LANES`, `LANE_TRIES`,
`LANE_PARK_SECS`, `HERMES_PROVIDER`, `HERMES_MODEL`.

## Output format

`events_output.json`, `places_output.json` and `holidays_output.json` hold
contract records (the shape above). The public feeds serve
`contract.legacy_view()` of the on-air ones, which is the flat shape the
current front end reads:

```json
{
  "title": "Toddler Storytime at Pearse Street Library",
  "description": "...",
  "start_date": "2026-09-20",
  "end_date": "2026-09-20",
  "venue_name": "Pearse Street Library",
  "venue_address": "138-144 Pearse St, Dublin 2",
  "city": "Dublin",
  "county": "Dublin",
  "country": "IE",
  "latitude": "53.3441",
  "longitude": "-6.2527",
  "url": "https://...",
  "source": "dublincity.ie",
  "cost": "Free, no booking needed",
  "age_group": "0-2, 3-5",
  "category": "library",
  "confidence": 0.85,
  "region": "Leinster",
  "location": "Pearse Street Library",
  "price_range": "Free, no booking needed",
  "source_url": "https://...",
  "source_name": "dublincity.ie",
  "booking_url": ""
}
```

### Migration

`migrate_contract.py` maps the pre-contract records onto the contract through
the legacy facet maps, then splits them: on-air stays in the public file,
needs-input moves to `staged/needs_input.json` with its `missing_field`, and
rejected is dropped. It is dry-run by default (prints the table, writes
nothing) and idempotent — a record already at `schema_version: 1` is left
exactly as it is:

```bash
venv/bin/python3 migrate_contract.py            # the table only
venv/bin/python3 migrate_contract.py --apply    # backup to ~/backups/kidsevents-ie, then write
```

See `EVENT_DATA_CONTRACT.md` for what the public site must show for every
event, and `DESIGN.md` for the front-end.

## Sources

`sources.json` holds the curated deep listing URLs, keyed by county (plus
`_national`), by category (`tourism`, `timeout`, `familyfriendly`,
`yourdaysout`, `listings`). Every `http` value under every key is crawled by
the `listings` lane on a rotating cursor. Entries that go dead or start
blocking get removed rather than retried — Eventbrite (405), Songkick,
Facebook groups (login wall) and `visitcork.com` (broken certificate) are all
out for that reason.

`catalog/lanes.json` holds the other lanes' sources. Of the four council
calendar exports the plan named, only Monaghan's actually serves
`text/calendar`: Cork County, South Dublin and Kildare answer `?ical=1` with
their ordinary HTML page (probed 2026-09-13). Add a feed here once it exists —
the lane checks the payload, not the status code.

## File structure

```
kidsevents-ie/
├── factory_worker.py        # discovery, research fetch, the four steps, publishing
├── contract.py              # the one record contract + the legacy view
├── gate.py                  # facet vocabulary fold + the on-air decision
├── migrate_contract.py      # one-shot, idempotent migration onto the contract
├── catalog/facets.json      # the facet vocabulary (counties, ages, prices, activities)
├── catalog/lanes.json       # per-lane enable flag + that lane's sources
├── catalog/search_templates.json  # the search lane's templates, always-on and seasonal
├── discovery/               # the eight lanes + the URL ledger
├── llm.py                   # model routing: local → free gateway lanes → hermes
├── links.py                 # official site, Instagram, TikTok — verified, never guessed
├── media.py                 # licensed hero photo + vision gate; social posts as embeds
├── staging.py               # social candidate staging desk + auto-approve sweep
├── server.py                # Flask API + admin portal
├── firebase_auth.py         # member identity verification
├── member_store.py          # member saves (sqlite)
├── sources.json             # curated listing URLs per county
├── web/                     # index.html (public) + admin.html
├── web/media/               # hero photos, git-ignored, served at /media/… (media_index.json)
├── staged/                  # candidates.json, social_candidates.json, needs_input.json
├── systemd/                 # unit examples
└── events_output.json · places_output.json · holidays_output.json
```
