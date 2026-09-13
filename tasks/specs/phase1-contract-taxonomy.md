# Phase 1 spec — one contract, facet vocabulary, QA gate, migration

Plan: `tasks/overhaul-plan-2026-09-13.md` §3, §4.2. Repo: /home/azureuser/kidsevents-ie (branch main, Forgejo origin). Builds on phase 0b (atomic writes, gates in publish, `write_json_atomic`, `prune_past_events`). Exclusions: no changes under `web/` beyond what §6 says; no model-routing changes (phase 2); no new discovery lanes (phase 3); no photo pipeline (phase 4).

## 1. New files
- `catalog/facets.json` — the vocabulary (below). One file, loaded once.
- `contract.py` — `KINDS`, `EMPTY_RECORD(kind)`, `validate(record) -> list[str]` (violations), `completeness(record) -> float`, `legacy_view(record) -> dict` (flat shape the current `web/index.html` reads: `title, description, start_date, end_date, venue_name, venue_address, city, county, country, latitude, longitude, url, source, cost, age_group, category, confidence, region, location, price_range, source_url, source_name, booking_url`), `slugify(title, county)`.
- `gate.py` — `gate_meta(taxonomy) -> (clean_taxonomy, dropped:list)`; `qa(record, sources_text) -> (ok, reason, missing_field)` implementing §4.2.
- `migrate_contract.py` — one-shot, idempotent (`schema_version: 1` marker per record), backs up to `~/backups/kidsevents-ie/pre-phase1-<stamp>.tgz` first.
- `test_contract.py`, `test_gate.py`, `test_migrate.py` (pytest, no network/LLM).

## 2. Record contract (store this shape in events/places/holidays output files)
```
{ "schema_version": 1, "id": "<kind>-<slug>", "kind": "event|place|holiday", "title": str≤80, "slug": str,
  "summary": str≤160, "description": str,
  "location": {"name","address","city","county","region","country","lat","lon","ireland": bool},
  "links": {"source_url","official_url","maps_url","instagram_url","tiktok_url","booking_url"},
  "media": {"hero": {"url","file","credit","licence","source","gate","alt"} | null, "embeds": [{"platform","url"}]},
  "taxonomy": {"age_bands": [], "price_band": "", "price_detail": "", "setting": "", "activity_types": [], "rainy_ok": bool|null, "accessibility": []},
  "provenance": {"sources": [{"url","fetched_at"}], "facts": [{"claim","quote","source_url"}], "confidence": float, "last_checked": iso, "produced_by": [str]},
  "status": "on-air|needs-input|rejected", "reason": "", "hint": "",
  "event":   {"start_date","end_date","times": [],"recurrence","organizer","booking_required","date_evidence","cancelled": bool}   # kind=event only
  "place":   {"opening_hours","duration_hint","seasonal_note"}                                                                 # kind=place only
  "holiday": {"destination_type","holiday_types": [],"best_seasons": [],"best_months": [],"school_breaks": [],"flight_time_from_dublin","direct_flight": bool|null,"budget_band","with_baby_toddler": bool|null,"includes": []} }
```
`maps_url` is always derived: `https://www.google.com/maps/search/?api=1&query=` + urlencoded (`lat,lon` if both, else `name, address, county, Ireland`). Never a Places API. `region` derived from `county` (table in facets.json). `ireland` = country == "IE". `confidence` = `completeness()`.

Required to be `on-air` (else `needs-input` with `missing_field`, or `rejected`):
- all kinds: title, description ≥120 words (holiday/place) or ≥60 words (event), summary, `location.county` (for IE) or `location.country` (holiday), at least one `provenance.sources`, `provenance.facts` non-empty, `links.source_url`.
- event: `event.start_date` ≥ today, `event.date_evidence` (quote containing the day or month), `taxonomy.price_band` ≠ "" , ≥1 `age_bands`, `location.ireland` true.
- place: `location.ireland` true, ≥1 `activity_types`, lat/lon or address.
- holiday: ≥2 distinct source domains, `location.country`, ≥1 `holiday_types`, ≥1 `best_seasons`.

## 3. facets.json
```
age_bands: ["0-2","3-5","6-9","10-12","13+"]           (legacy map: toddler→[0-2,3-5], preschool→[3-5], kids→[6-9,10-12], teens→[13+], all_ages→all five)
price_band: ["free","under-10","10-25","25-plus","unknown"]  (derive from a euro amount when price_detail has one: 0→free, <10, ≤25, >25; legacy cost: free→free, donation→free, paid/membership→unknown unless amount)
setting: ["indoor","outdoor","both"]
activity_types: ["playground","park","museum","farm","soft-play","zoo-wildlife","trail-hike","swimming","water-sports","adventure-climbing","workshop-class","festival","theatre-show","cinema","library","craft","sports","seasonal","food-market","nature-reserve"]
   (legacy category map: festival→festival, theatre→theatre-show, music→festival, sport→sports, workshop→workshop-class, market→food-market, museum→museum, park→park, special→seasonal, food→food-market, seasonal→seasonal, "Indoor play"→soft-play, "Nature"→nature-reserve)
accessibility: ["wheelchair","step-free","buggy","changing-places-toilet","sensory-friendly","elder-friendly"]  (legacy suitable_for: pushchair_accessible→buggy)
county: the 32 counties (Antrim…Wicklow), with region: Leinster/Munster/Connacht/Ulster and the 6 NI counties flagged `ni: true`; `_COUNTY_CODES` from factory_worker moves here.
holiday.destination_type: ["city","resort","region","park","island"]
holiday.holiday_types: ["beach","winter-sun","ski","city-break","theme-park","farm-stay","camping-glamping","all-inclusive","villa-self-catering","cruise","road-trip","safari-wildlife","lakes-mountains"]
holiday.best_seasons: ["spring","summer","autumn","winter"]
holiday.school_breaks: {"oct-midterm":"2026-10-26/2026-10-30","christmas":"2026-12-23/2027-01-05","feb-midterm":"2027-02-15/2027-02-19","easter":"2027-03-20/2027-04-04","summer":"2027-06-26/2027-08-31"}
holiday.flight_time_from_dublin: ["none","under-2h","2-4h","4-8h","8h-plus"]
holiday.budget_band: ["budget","mid","premium","luxury"]
```
`gate_meta` drops any value not in the vocabulary (after alias mapping), records `dropped` and never substitutes a default. Counts of drops per field go to `factory_state.json.facet_drops` (for the Production area).

## 4. LLM steps (keep `hermes()` from phase 0b; phase 2 swaps the transport)
Per candidate, at most four calls, each a small prompt with an explicit JSON contract and `Do not invent values`:
1. **classify**: input = title/caption + page text (≤6k chars) → `{"kind": "event|place|holiday|none", "family_relevant": bool, "country": "IE|GB|…", "why": ≤120 chars}`. `none`/false → rejected with `why`.
2. **facts**: → `{"facts": [{"claim","quote"}] (≤12), "name": venue/organiser name}`; every quote must be a verbatim substring of the page text (verify in code; drop the fact if not) — this is the grounding.
3. **extract** (kind-specific): event → dates/times/organiser/booking/price_detail/age hints + `date_evidence` (must be one of the fact quotes); place → hours/duration/seasonal; holiday → destination fields. Values must be traceable to a fact quote or left empty (the prompt says so; the gate checks `date_evidence`).
4. **write**: title (≤80, never the caption), summary (≤160), description (120–300 words for place/holiday, 60–200 for event) using ONLY the facts; plus `taxonomy` chosen from the vocabulary rendered by `facet_gloss()` (the exact allowed values per field). `gate_meta` validates.
Existing prompts `enrich_event`/`extract_place` are replaced by these; `promote_candidate` becomes `promote(candidate) -> (record|None, reason, missing_field)` using the four steps, and `run_discovery_cycle`'s per-page extraction uses the same steps (JSON-LD events pre-fill `event.*` and skip step 3).

### 4b. Research fetch for social candidates (verified 2026-09-13)
Before step 1, fetch the candidate's `source_url` from THIS VM with a plain Chrome desktop User-Agent: an Instagram permalink (`/p/<code>/`, `/reel/<code>/`) returns HTML containing `"caption":{"text":…}` and the author handle; a TikTok video URL's caption comes from `https://www.tiktok.com/oembed?url=<url>` (keyless). Cache the fetched text in `provenance.sources[0]` (`url`, `fetched_at`) and use it as the page text for steps 1–4. Rate-limit fetches to one per 2 s per platform; on HTTP 429/403 the candidate becomes `needs-input` with `missing_field="caption"` (never rejected for that). The mini PC crew cannot get Instagram captions (login/JS shell from that IP), so this VM-side fetch is the only caption source for the keyword-search lane.

## 5. Migration (run once by the executor, with the backup)
- Every current record → contract via the legacy maps; `status="on-air"` only if it passes `validate()` + `qa()`, else `status="needs-input"` with `missing_field` and it moves to `staged/needs_input.json` (new file, same lock) and OUT of the public file. Expect roughly half of the 122 events to move out (adult/past/attraction-as-event were partly pruned in 0b). Print a table: kept / needs-input / rejected per file with the top 5 reasons.
- The public JSON endpoints serve `legacy_view()` of on-air records so the current frontend keeps working unchanged; add `/api/v1/{events,places,holidays}` serving the full contract; `/admin/api/*` serve full records.
- `staged/social_candidates.json`: unchanged shape, but `status` gains `needs_input` (with `missing_field`) alongside `needs_review`/`approved`/`rejected`; the admin desk already renders `reason`/`hint`.

## 6. server.py / web
- Only additive route changes (`/api/v1/*`), `legacy_view` on the old routes. `web/index.html` untouched in this phase. `web/admin.html`: show `missing_field` next to the reason on Social rows if present (one-line change).

## 7. Verification (real, in the report)
- pytest on the three new test files + phase 0b tests, all green; `py_compile` all modules.
- Migration table with counts; `curl /api/events | jq length` and `/api/v1/events | jq '.[0]'` showing the contract; frontend loads with zero console errors (playwright screenshot of `/` after `systemctl --user restart kidsevents-ie.service`).
- One real end-to-end promote on 3 staged candidates (choose ones with captions naming Irish venues, e.g. `found_via` corkfamily/galwayfamily): show the four-call trace, the resulting record or the reason + missing_field.
- Commit + push (`Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>` and `Claude-Session: https://claude.ai/code/session_01BsEd8XKU1NMdhWwev3Xnpw` trailers).

Stopping condition: all of §7 shown; nothing beyond this spec.
