# Small Days — portal + ingestion overhaul plan (2026-09-13)

Status: DRAFT for Victor's review. Nothing below is built yet except §0.
Evidence: four read-only audits this morning (WanderTold mechanics on the mini PC, Small Days code + data on the VM, online research on directories/taxonomy/sources/analytics, real-browser usability pass of the portal).

## 0. What was actually broken this morning (fixed, live, pushed 26ae87f)

| Symptom Victor saw | Root cause (verified) | Fix |
|---|---|---|
| "hundreds waiting for me" (296 needs_review, 294 with no verdict) | The auto-approve sweep self-deadlocked after item 2 on 09-12 14:19 UTC: `output_lock()` was not re-entrant (flock treats each open() as a new owner), so "classify → publish" blocked on itself. Sat in `locks_lock_inode_wait` for 20 h; 22 hourly mini-PC pushes queued behind it. | Lock is re-entrant per thread; LLM classification now runs on a snapshot **outside** the lock. Sweep relaunched 11:07 UTC. |
| Factory run 09-12 15:03: 0 events for all 5 cities | systemd user unit PATH has no `~/.local/bin` → every `hermes` call: "No such file". | `hermes` resolved to an absolute path in code. |
| Classifier "timed out after 90 s" | OpenRouter free tier: ~1 req/min per model per key; sequential hammering of one model. All 4 roster models answer in 22–42 s when not throttled. | Plan §3.4: rotate lanes, local model first. |

Net: the backlog was never a review problem, it was a stuck process. With the gate working, most of the 296 will resolve to approved / rejected-with-reason without Victor.

## 1. Where the product stands (numbers)

| Catalogue | Records | price | age | category | official site | maps link | instagram | photo | description >80 chars |
|---|---|---|---|---|---|---|---|---|---|
| Events | 122 | 0% | 0% | 0% | 100% (source url) | 0% (no field) | 0% (no field) | 0% (no field) | 75% |
| Things to do (places) | 2 | 100% | 50% | 50% | 0% | 0% | 0% | 0% | 50% |
| Holidays | 3 | 100% | 100% | 100% | 0% | 0% | 0% | 0% | 100% |

- The three output schemas do not even have fields for maps, Instagram, photos, accessibility. R5 is a schema gap, not a fill gap.
- The public site is a single-page app with no per-item URL, no JSON-LD, no sitemap, no canonical, served at `…cloudapp.azure.com/kidsevents/`. SEO metrics cannot exist until it has a domain and crawlable item pages.
- No visitor analytics anywhere (no script, no server counter).
- Sources: 20 URLs across 5 Irish cities, events only. No Things-to-do sources, no Holidays sources. Social search: 15 tags, TikTok 202 / Instagram 102 candidates, 34% with empty caption (rate-limited fetch).
- Admin: one flat nav of 6 views (Overview, Editorial review, Coverage, Collection, Social, Logs). No separation by catalogue, no metrics beyond counts, no bulk actions, no deep links.

## 2. Target model — three catalogues, one contract each

Shared core (every record, every catalogue):

```
id, kind: event|place|holiday, title, slug, summary (≤160 chars), description (120–300 words, grounded)
location: { name, address, city, county (32-county enum), region, country (ISO), lat, lon, ireland: bool }
links: { source_url (where we found it), official_url, maps_url (google.com/maps/search/?api=1&query=…), instagram_url, booking_url }
photos[]: { url|file, credit, licence, source, gate: hero|pass|reject, alt }
taxonomy: { age_bands[] ⊂ {0-2,3-5,6-9,10-12,13+}, price_band ∈ {free,under-10,10-25,25-plus,unknown}, price_detail,
            setting ∈ {indoor,outdoor,both}, activity_types[] (20-value enum), weather: {rainy_ok: bool},
            accessibility[] ⊂ {wheelchair, step-free, buggy, changing-places-toilet, sensory-friendly, elder-friendly} }
provenance: { sources[] (url, fetched_at), facts[] (claim + quote), confidence, last_checked, produced_by (model ids) }
status: on-air | needs-input | rejected, reason, hint
```

Per kind:
- **Event**: `start_date, end_date, times[], recurrence, organizer, booking_required, sold_out|cancelled` + `date_evidence` (quoted source text — WanderTold rule, no date without a quote). Location + price are required to go on air.
- **Place** (Things to do): `opening_hours, duration_hint, seasonal_note`. Location required; price may be `unknown` but shown as such.
- **Holiday**: `destination_type (city|resort|region|park), country, best_months[], travel: {from_dublin_hours, direct_flights: bool}, family_features[]`, `includes[]` (attractions). Worldwide, `ireland=false` for most.

Facet vocabulary lives in one file (`catalog/facets.json`, same shape as WanderTold's) and every LLM output is hard-validated against it (`gate_meta` pattern: unknown values dropped and counted, never silently coerced). Age bands, price bands and the 20 activity types are in Appendix A. Accessibility is emitted to search engines as `amenityFeature → LocationFeatureSpecification` (schema.org has no accessibility property on Place).

Migration: the 122 existing events are re-run through the new enrichment (they have source URLs, so the crawl can re-fetch); anything that cannot reach the on-air bar goes to needs-input with a reason, not deleted.

## 3. Ingestion — the WanderTold method, applied

### 3.1 Stage map (mirrors worker.py on the mini PC)

| Stage | Small Days implementation | Lock |
|---|---|---|
| Discovery lanes | per catalogue (§3.2); each lane writes `discovery/<lane>.jsonl` candidates with `found_via` | none |
| Candidate ledger | `staged/candidates.json` — one queue for all kinds, dedup by URL + name-fold + geo (bge-m3 embeddings on the mini PC for near-duplicate titles) | output lock |
| Research | fetch source page(s) + official site + Wikidata/OSM enrichment → `facts[]` with quotes | none |
| Classify + enrich | LLM: kind (event/place/holiday/none) → kind-specific extraction → facet assignment; each step a separate short prompt; every field must cite a fact or be empty | none |
| Links | official site from JSON-LD/`sameAs`/Wikidata P856; Instagram from official-site HTML scan → Wikidata P2003 → `site:instagram.com` search (needs confirmation); maps URL built, never Places API | none |
| Photos | Commons → Openverse → official-site og:image (placeholder only) → organiser Instagram **link** (never rehosted); vision gate on the mini PC local model (HERO/PASS/REJECT, WanderTold `photo-gate.py` rules) + alt text | none |
| QA gate | required fields per kind; `name_grounded` (title appears in the sources); date evidence for events; no filler descriptions; duplicate-fold check; coords inside Ireland when `ireland=true` | none |
| Auto-approve / merge | pass → on-air immediately; fail → needs-input with a **specific** reason and the one field that would unblock it; timer every 15 min like `wt-merge-staged` | output lock (merge only) |
| Publish | write catalogue files → regenerate item pages + sitemap + JSON-LD → verify URLs | output lock |

### 3.2 Discovery lanes per catalogue (all free, no paid keys)

Events: curated Irish sources (familyfun.ie, yourdaysout, city tourism sites — 20 today, extend to all 32 counties); JSON-LD `Event` harvesting on every crawled page; Ticketmaster Discovery within its free 5,000/day quota; Instagram/TikTok keyword+hashtag search (existing mini-PC crew); "this weekend" web search per county. Eventbrite (API closed since 2020) and Meetup (paid key) are out.

Things to do: Fáilte Ireland Open Data API (attractions/activities, CC BY 4.0 — needs a portal registration, §7); data.gov.ie playgrounds/parks datasets (DLR, Wicklow, Westmeath, DCC); OpenStreetMap Overpass (`leisure=playground`, `tourism=museum|attraction|zoo`, farms, `leisure=water_park`, libraries) as the seed list for the whole island; Wikidata for known museums/attractions (website + Instagram); social search; venue accounts discovered from events.

Holidays: no Irish-source analogue exists. Lanes: editorial seed list (Victor's + LLM-generated list of family destinations, each then researched and grounded), Wikidata `TouristDestination`s with `touristType=families`, Instagram/TikTok tags (`familytravel`, `holidayswithkids`, `kidsholidays`, …), and the existing `holidays kids` search results (56 candidates already staged). Every holiday needs ≥2 independent sources before on-air.

### 3.3 Review desk policy (what reaches Victor)

- Only items that fail the gate on a **single, nameable** field reach the queue, and each shows: the reason, the field, a proposed value (if the model had a low-confidence guess) and a one-line hint box → resubmit. Bulk "apply hint to all similar" for repeated reasons (e.g. "London — outside Ireland: reject all").
- Items with no caption and no page content are auto-rejected with reason `no-content`, not queued.
- Target: ≤20 open questions at any time; anything older than 14 days auto-archives.

### 3.4 Model routing (free-first, identical to WanderTold's chain)

- Order: mini PC `llama-server` (Qwen3.5-35B, yield when `requests_processing ≥ 3`) → OmniRoute lanes rotated per call (nemotron-3-super, minimax-m2.7, mimo-v2.5, hy3, step-3.7-flash, longcat) → hermes/openrouter (gemma, dots) → give up with reason. Never a paid key; ChatGPT Plus lane not used for bulk.
- Per-item budget: classify (1 short call, dots/local), extract (1), facets (1, constrained), description (1, nemotron-super), links/photos are non-LLM except the vision gate (local Qwen vision).
- Vision + embeddings on the mini PC (`:8089`, `:8090`); text via OmniRoute from the VM (tunnel already up).
- Throughput math: 6 rotating lanes × ~1 req/min each ≈ 300 items/hour worst case; the current 296 backlog clears in ~1 h once §3.4 lands (today's sweep uses one model and will take longer).

### 3.5 Social crew (mini PC) changes

Keep the two-hop design. Add: hashtag pages (`instagram.com/explore/tags/<tag>`), per-account expansion of venues found via events, TikTok search for the holiday tags, caption backfill via oEmbed when the DOM fetch was rate-limited (34% empty captions today), and `found_via` + `platform` stamped on every candidate (already). Still needs Victor's "Kids" saved-collection URL to activate the collection lane.

## 4. Admin portal — information architecture

Top-level areas (left rail, each a real URL `/admin/<area>`, deep-linkable, badge counts):

1. **Overview** — the metrics square: on-air per catalogue, added today / this hour / last 7 days (sparkline), needs-input count, last factory run + next, source health, model lane health. One screen, no scrolling.
2. **Catalogue** — tabs Events / Things to do / Holidays. Table with facets as columns, completeness chips (missing photo, missing price…), search, filter by county/status, inline edit drawer with the full record and its provenance/facts. Bulk: unpublish, re-enrich, re-photo.
3. **Needs input** — the only review queue. Grouped by reason; per item: what we know, the exact blocker, proposed value, hint box, Resubmit / Reject / Reject-all-like-this. Keyboard j/k/a/r.
4. **Sources & discovery** — per lane: enabled, last run, candidates found / on-air yield / rejected, add a source or a tag inline, "test this source now".
5. **Production** — runs (duration, per-stage counts, failures), crews on the mini PC (social, photo gate), model lanes (calls, timeouts, cooldowns), Failed items with retry.
6. **Photos** — coverage per catalogue, no-hero list, gate rejects with the model's reason, upload/replace.
7. **Metrics** — §5.
8. **Settings** — auto-approve on/off, thresholds, facet vocabulary editor, tag list.

Rules: every list paginated + filterable; state in the URL; visible focus; mobile layout (Victor reviews from his phone); no destructive action without an undo toast; every timestamp in one zone with the zone shown.

## 5. Metrics tab

- **Visitors**: self-hosted **Umami** on the VM (Node + Postgres in Docker, ~300 MB; VM has ~10 GB free RAM, 40 GB disk). Tracking script on the public site with custom events: outbound clicks to official site / maps / Instagram / booking per item, search terms, filter usage, saves. Umami's REST API feeds the admin tab (pageviews, visitors, top pages, top referrers, top outbound targets, 24 h / 7 d / 30 d). Fallback if Victor prefers no container: GoatCounter single binary + SQLite (counts only).
- **SEO**: Google Search Console API (clicks, impressions, CTR, position per page/query, 16 months). Requires a **real domain** + Search Console verification + a service account key (Victor, §7). Bing Webmaster API + IndexNow for Bing/Yandex. On-page: per-item server-rendered pages with JSON-LD (`Event`, `TouristAttraction`/`LocalBusiness`, `TouristDestination`), `sitemap.xml` regenerated on publish, canonical, OpenGraph image. Admin shows: indexed pages, pages with rich-result errors, sitemap freshness.
- **Content KPIs**: on-air per catalogue, added per day/hour, completeness % per field, needs-input ageing, per-source yield, model timeouts, photo coverage. Already partly in `/admin/api/metrics`; extended, and stored daily so trends exist.

## 6. Public site (only what the plan above forces)

Item pages with URLs; facet filters (age, price, setting, activity, county map, accessibility, "rainy day", "near me" via browser geolocation); item card shows photo, price band, age bands, county, links row (site · maps · Instagram · book), "last checked" and source; Things to do gets its own section (done 09-12, not yet with facets); mobile bottom nav back to 5 items (Today, Events, Things to do, Holidays, Saved — Guides folds into Today).

## 7. Decisions for Victor (each one line, answer in any order)

1. **Domain**: which domain for Small Days? (SEO metrics are meaningless on the Azure hostname; `small-days-ireland` is the Firebase project name.)
2. **Analytics host**: Umami on the VM (default) or on the mini PC (fleet convention, needs your yes)?
3. **Search Console**: once the domain exists, you verify it and create a service-account JSON; I wire it.
4. **Fáilte Ireland Open Data**: register the developer portal account (free) — under your email.
5. **Instagram photos**: policy = link to the organiser's Instagram and never rehost their images (Meta's terms). Photos come from Commons / Openverse / official site og:image. OK?
6. **Age bands** 0-2 / 3-5 / 6-9 / 10-12 / 13+ and price bands free / <€10 / €10-25 / €25+ — OK, or your cut-offs?
7. **Holidays scope**: worldwide, ≥2 sources per destination, ~20/month target. OK?
8. **"Kids" saved collection URL** (Instagram + TikTok) — still outstanding since 09-12.

## 8. Phases, order, acceptance

| # | Phase | Delivers | Done when (measured) |
|---|---|---|---|
| 0 | Unblock (DONE today) | lock fix, hermes path, sweep running | backlog verdicts land; factory run adds events |
| 1 | Contract + taxonomy + gate | schemas §2, `facets.json`, `gate_meta`, normalizer, migration of 122 events | 100% of on-air records validate; ≥90% events have price + age + category |
| 2 | Model routing + queue | WanderTold chain (local → 6 lanes → hermes), rotating lanes, per-item budget, needs-input reasons | 296 backlog resolved; needs-input ≤20; p95 classify < 60 s |
| 3 | Discovery lanes | §3.2 lanes for all three catalogues, 32 counties | ≥300 Things to do on air, ≥50 holidays, events in every county |
| 4 | Links + photos | official/maps/Instagram resolution, photo pipeline + vision gate + alt | ≥90% maps, ≥70% official site, ≥40% Instagram, ≥80% with a gated photo |
| 5 | Admin portal | IA §4, 8 areas, deep links, bulk actions, mobile | Victor can clear the needs-input queue on his phone in <10 min |
| 6 | Metrics + SEO | Umami, GSC, item pages, JSON-LD, sitemap, KPIs stored daily | Metrics tab shows visitors/SEO/content trends; rich-result test passes on 3 sample pages |
| 7 | Public facets | §6 | filters work on real data; Lighthouse ≥90 mobile |

Order is 1 → 2 → 3 → 4 in the pipeline, with 5 built in parallel from phase 2 (executor by file ownership), 6 after the domain decision, 7 last. Each phase: spec → `opus-executor` → real-browser verification → commit + push + registry/links update in the same turn. No phase gates between them once approved.

## Appendix A — facet vocabulary (draft)

- age_bands: `0-2, 3-5, 6-9, 10-12, 13+` (matches `typicalAgeRange` free-text convention and Time Out's tiers; Hoop uses a slider — bands read better in a directory)
- price_band: `free, under-10, 10-25, 25-plus, unknown` (store real `price_detail`; band derived)
- setting: `indoor, outdoor, both`
- activity_types (20): `playground, park, museum, farm, soft-play, zoo-wildlife, trail-hike, swimming, water-sports, adventure-climbing, workshop-class, festival, theatre-show, cinema, library, craft, sports, seasonal, food-market, nature-reserve`
- accessibility: `wheelchair, step-free, buggy, changing-places-toilet, sensory-friendly, elder-friendly`
- county: the 32 counties; region: `Leinster, Munster, Connacht, Ulster`; `ireland: bool`

## Appendix B — what is lifted from WanderTold vs rewritten

Lift: LLM fallback chain + `_local_is_free` yielding; `facets.json` + `facet_gloss`/`gate_meta` validation; `photo-gate.py` rules + `photo-alt.py`; `cycle.lock`/`daemon.lock` + merge → push → deploy → verify shape; social two-hop crew; `content_fails` / `name_grounded` QA pattern; admin tabs Production / Photos / Models / Failed / Scout.
Rewrite: record schemas (dates, price, age, recurrence, holidays), geography (county-based, not city-radius), category vocabulary, scout prompts, and drop narration/audio entirely.

## 9. Portal usability audit (real Chromium, desktop + phone, console/network captured)

No JS errors, no failed requests; production pixel-identical to loopback. Problems are structural.

| # | Sev | Issue | Covered by |
|---|---|---|---|
| 1 | P0 | Social review: Approve/Reject + hint box clipped off-screen for all 296 rows, no scroll cue | §4 Needs-input, card layout, sticky actions |
| 2 | P0 | Public: "Saved to your small plan" banner permanently shown, overlaps content on Home/Places/Holidays/Guides; Saved page is empty | immediate bug fix |
| 3 | P0 | No pagination anywhere (10k–40k px pages) | §4 rule |
| 4 | P1 | Editorial/Social table columns off-screen on phone, no cue | §4 phone layout |
| 5 | P1 | 0/122 items with parent-facing essentials ("Cost to check", "Ages to check" everywhere) | phases 1–2 |
| 6 | P1 | Two parallel admin navs with different labels; 27% of phone viewport lost | §4 single rail |
| 7 | P1 | Dateless listing fallback text collides with title on Find | immediate bug fix |
| 8 | P1 | Reject reasons truncated mid-word | §4 |
| 9 | P1 | No deep links (admin tabs, public event modal); reload loses the item | §4 + §6 item pages |
| 10 | P1 | Failed collection run: no error, no log link | §4 Production |
| 11 | P2 | "Warner Bros. Studio Tour London" as "nearby"; stray "." for empty category | §2 ireland flag + QA gate |
| 12 | P2 | Raw `&#039;` in Editorial titles (double escape) | immediate bug fix |
| 13 | P2 | Zero photos on the public site | phase 4 |
| 14 | P2 | "Confidence: 100% captured" next to unresolved fields | §2 |
| 15 | P3 | "Ready" chip without count; raw ISO timestamps | §4 |

Nielsen (admin, 0–4): control 3, consistency 3, recognition 3, flexibility 3, error recovery 3, error prevention 2, minimalism 2, status 1, real-world match 1, help 1. Adult stand-up shows appear in the family feed (QA gate §3.1). Items 2, 7, 12 are fixed before phase 1.
