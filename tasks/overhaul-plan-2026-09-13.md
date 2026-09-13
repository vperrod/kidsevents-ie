# Small Days — portal + ingestion overhaul plan, v3 (2026-09-13: answers, second audit round, discovery inventory + expansion, access check)

Status: v2 for Victor's approval. Built from two audit rounds (WanderTold mechanics on the mini PC; Small Days code, data, runtime; real-browser usability; online research on directories, taxonomy, embeds, holidays, analytics, DNS/mail; infra review of the VM, mini PC, Hostinger mail server, Firebase; content-quality sampling; code review). Nothing is built yet except §0.

## 0. Fixed today (live, pushed 26ae87f)

| Symptom | Root cause (verified) | Fix |
|---|---|---|
| "hundreds waiting for me" (296 needs_review, 294 never judged) | Auto-approve sweep self-deadlocked after item 2 on 09-12 14:19 UTC: `output_lock()` was not re-entrant (flock treats each open() as a new owner) so "classify → publish" blocked on itself. `locks_lock_inode_wait` for 20 h; 22 hourly mini-PC pushes queued behind it; the 09-12 15:03 factory run also hung on the same lock for 20 h. | Re-entrant per thread; classification runs on a snapshot outside the lock. Sweep relaunched 11:07 UTC, 19/296 done by 12:40, no timeouts. |
| Factory run published 0 events | systemd user unit PATH has no `~/.local/bin` → every `hermes` call "No such file". | Absolute path in code; proven on the 11:05 run. |
| Classifier "timed out after 90 s" | OpenRouter free tier ≈ 1 req/min per model per key; one model hammered. All 4 roster models answer in 22–42 s unthrottled. | §4.4 rotate lanes, local model first. |

Not fixed, found today: the mini PC social crew's **TikTok lane is dead since this morning** ("Pre-navigation to tiktok.com failed: Navigation rejected" on every query) while the Instagram lanes still return candidates (46 new at 09:43, resent after the deadlock cleared). WanderTold's crew on the same browser is fine. To triage in phase 0b (browser extension URL allow-list or TikTok bot wall; read-only first per the one-owner rule).

The sweep's first auto-approval is the QA gate in one record: an adult vintage clothing pop-up, dated yesterday (already past), published as an "all ages" family event with the raw truncated caption as its title. Three checks missing: family relevance, past-date rejection, title synthesis.

## 1. Where the product stands

**Counts and completeness**

| Catalogue | Records | price | age | category | official site | maps | instagram | photo | good description |
|---|---|---|---|---|---|---|---|---|---|
| Events | 122 | 0% | 0% | 0% | 100% (source url) | no field | no field | no field | 75% |
| Things to do | 2 | 100% | 50% | 50% | 0% | no field | no field | no field | 50% |
| Holidays | 3 | 100% | 100% | 100% | 0% | no field | no field | no field | 100% |

**Content quality (deterministic sample of 31 events, 44 staged)**
- Events: 42% real family events, 35% adult (stand-up, gigs, "16+" theatre from irelandme.com), 10% evergreen attractions mislabelled as events (familyfun.ie, descriptions are nav-tag lists), 13% empty (ark.ie). 54% of all published events (67/124) have a start date already in the past, one from 2022.
- Places/Holidays are inverted: the three Holidays are Irish day-trip places; the one London item sits in Places and shows as "nearby".
- Staged social: 23% have a nameable Irish venue in the caption; 55% have no caption or no venue; 23% are explicitly outside Ireland. Noise tags: `holidays kids`, `thingstodokids`, and brand spam inside `events ireland`. Good tags: `galwayfamily`, `corkfamily`, `corkwithkids`, `kidsofcork`, `familyeventsireland`, `kidseventsireland`.
- Sources: 22 URLs, 5 cities; all 5 Eventbrite URLs 405, corkbeo anti-bot, visitcork dead, Facebook groups are login walls; 21 of 26 counties have no source. Search discovery crawls section homepages (visitdublin.com/events nav), yielding 2–3 events per run.

**Code review (top items)**: every JSON store is written by truncate-in-place with no temp+rename, and every loader silently substitutes an empty list on a torn read and then writes it back (one interrupted write = whole dataset gone); the LLM path publishes past dates (JSON-LD path filters them); `normalize_event` drops the fields the prompt asks for (`image_url`, `suitable_for`, `booking_required`, `phone`, `contact_email`, `duration_hours`) which is why cost/age are 0%; no country gate on `publish_place`; Flask runs single-threaded so one Approve click blocks the public site for up to 90 s; `href` fields are escaped but not scheme-checked; `main.py` + 8 scraper modules are dead code that would clobber the output file if ever run.

**Portal usability (real Chromium, desktop + phone)**: Approve/Reject buttons clipped off-screen for all 296 rows; a "Saved to your small plan" toast is present in the DOM on every page; no pagination (10k–40k px pages); two parallel navs with different labels; no deep links; failed runs show no error; raw `&#039;` in titles. Zero JS errors; production identical to loopback.

**SEO/analytics**: none possible today. Single-page app, no per-item URL, no JSON-LD, no sitemap, no analytics script, served under the Azure hostname. `smalldays.ie` currently has no DNS zone (NXDOMAIN).

## 2. Your answers, recorded, and what they change

| # | You said | What it means for the plan |
|---|---|---|
| 1 | Domain `smalldays.ie` on your Blacknight account | Site moves to `https://smalldays.ie`; `/kidsevents/` becomes a 301. Blacknight has no DNS API, so the records are clicks in cp.blacknight.com (list in §6). |
| 2 | Analytics on the mini PC | Umami (Node + Postgres, ~300 MB) on the mini PC, served as `stats.smalldays.ie` through the VM's Caddy over Tailscale. Mini PC has 1.6 TB disk free; RAM is tight (local model holds most of it) so Umami is sized small. |
| 3 | Domain exists | Search Console: the Firebase project `small-days-ireland` already has a service account on this VM. You need three clicks (§6). |
| 4 | "What is this?" + yes, under @smalldays.ie on your own SMTP | **Fáilte Ireland** is the national tourism authority. Its free Open Data API lists every attraction and activity operators submit to discoverireland.ie, licensed CC BY 4.0. It is the single biggest seed for Things to do. Registration needs an email, so `data@smalldays.ie` gets created first on the Trystful mail server (docker-mailserver on the Hostinger box supports a second domain natively). |
| 5 | Prefer an iframe embed; host if not; wants my take | **Embed.** Instagram's `/p/<code>/embed/` iframe and TikTok's embed both work with no token (tested from this VM today; Meta reopened tokenless embeds on 2026-06-15). Under EU case law (BestWater, VG Bild-Kunst) embedding a public post is lawful; downloading and rehosting needs the poster's licence. WanderTold does download venues' Instagram og:images with no licence field, which is your call there, but Small Days is a public directory with a domain and a brand, so: hero image from licensed sources (Wikimedia Commons, Openverse, the venue's own website with credit), the organiser's post embedded as "as seen on Instagram/TikTok", and rehosting only after the organiser says yes via the claim email (§4.6). |
| 6 | Age and price bands yes | Locked: 0-2 / 3-5 / 6-9 / 10-12 / 13+; free / under €10 / €10-25 / €25+. |
| 7 | Worldwide, as many as you can, plus a season taxonomy | No cap. "Two sources" was a quality bar, not a limit: a destination goes on air once two independent sources agree it exists and is family-friendly. Season taxonomy in §3. |
| 8 | "What is this?" | A **saved collection** is Instagram's bookmark folder (you tap Save on a post and file it, e.g. "Kids"). WanderTold reads yours ("Wandertold" collection) as an idea feed. Small Days' crew has a placeholder for a "Kids" collection but no URL, so that lane is a no-op. You don't need it: tag search runs without it. Default: drop the lane. If you do start saving posts into a collection, tell me its name and I switch it on. |

## 3. Target model: three catalogues, one contract each

Shared core on every record:

```
id, kind: event | place | holiday, title (synthesised, ≤80 chars, never a raw caption), slug, summary (≤160), description (120–300 words, grounded)
location:   name, address, city, county (32-county enum), region, country (ISO), lat, lon, ireland: bool
links:      source_url, official_url, maps_url (google.com/maps/search/?api=1&query=…), instagram_url, tiktok_url, booking_url
media:      hero { url|file, credit, licence, source, gate: hero|pass|reject, alt }, embeds[] { platform, url }
taxonomy:   age_bands[], price_band, price_detail, setting ∈ {indoor, outdoor, both}, activity_types[] (20-value enum), rainy_ok,
            accessibility[] ⊂ {wheelchair, step-free, buggy, changing-places-toilet, sensory-friendly, elder-friendly}
provenance: sources[] (url, fetched_at), facts[] (claim + quote), confidence (= completeness score), last_checked, produced_by
status:     on-air | needs-input | rejected, reason, hint
```

Per kind:
- **Event**: `start_date, end_date, times[], recurrence, organizer, booking_required, sold_out|cancelled, date_evidence` (quoted source text; no date without a quote). Required for air: location, price band, at least one age band, start date ≥ today.
- **Place** (Things to do): `opening_hours, duration_hint, seasonal_note`. Required: location in Ireland, activity type. Price may be `unknown` and is shown as such.
- **Holiday**: `destination_type ∈ {city, resort, region, park, island}, country, holiday_types[] ⊂ {beach, winter-sun, ski, city-break, theme-park, farm-stay, camping-glamping, all-inclusive, villa-self-catering, cruise, road-trip, safari-wildlife, lakes-mountains}, best_seasons[] ⊂ {spring, summer, autumn, winter}, best_months[], school_breaks[] ⊂ {oct-midterm, christmas, feb-midterm, easter, summer} (matched to the Department of Education 2026/27 dates: 26–30 Oct 2026; 23 Dec 2026–5 Jan 2027; 15–19 Feb 2027; 20 Mar–4 Apr 2027; summer), flight_time_from_dublin ∈ {none, <2h, 2-4h, 4-8h, 8h+}, direct_flight: bool, budget_band ∈ {budget, mid, premium, luxury}, with_baby_toddler: bool, includes[]`. Required: two independent sources, country, at least one holiday type and one season. Climate normals from Open-Meteo (free, no key) fill best months.

Facet vocabulary in one file (`catalog/facets.json`, WanderTold shape); every model output hard-validated against it, unknown values dropped and counted. Accessibility emitted as `amenityFeature → LocationFeatureSpecification` for search engines.

Migration: the 3 Holidays move to Things to do; the London item is rejected; all 122 events re-run through the new gate from their source URLs (expect roughly half to be dropped as past, adult, or not an event; the familyfun.ie attractions become Things to do).

## 4. Ingestion: the WanderTold method, applied

### 4.1 Stage map
Discovery lanes → candidate ledger (one queue, dedup by URL + folded name + geo, near-duplicates via the mini PC embedding server) → research (source page, official site, Wikidata, OSM → `facts[]` with quotes) → classify + enrich (kind → kind-specific extraction → facets; every field cites a fact or stays empty) → links → media → QA gate → auto-approve / merge every 15 min (only the merge holds the lock; atomic temp+rename writes; a torn read aborts, never writes back empty) → publish (catalogues, item pages, sitemap, JSON-LD, URL verify).

### 4.2 QA gate, explicit
Reject with a named reason when: kind is `none`; not family-relevant (age-gated, adult comedy, gigs, art exhibitions without a family programme); event date in the past or missing `date_evidence`; `ireland=true` but coordinates outside Ireland, or `ireland=false` for an event/place; title equals or truncates the caption; description under 120 words, filler, or a nav-tag list; duplicate fold of an on-air item; place without an activity type; holiday with fewer than two sources. Needs-input (not reject) only when a single nameable field is missing and a hint could fix it. Items with no caption and no page content are auto-rejected `no-content`.

### 4.3 Discovery lanes (all free)
- **Events**: curated Irish sources extended to all 32 counties with deep listing URLs (not section homepages); JSON-LD Event harvesting; Ticketmaster Discovery within the free daily quota; Instagram/TikTok keyword and hashtag search; per-county "this weekend" search. Drop Eventbrite (405, API closed), corkbeo (anti-bot), Facebook groups (login wall), Songkick/Metro (404).
- **Things to do**: Fáilte Ireland Open Data (registration with `data@smalldays.ie`); data.gov.ie playground and park datasets; OpenStreetMap Overpass seed for the island (playgrounds, museums, attractions, zoos, farms, water parks, libraries); Wikidata (website P856, Instagram P2003); venue accounts found through events; social search.
- **Holidays**: Wikivoyage (CC BY-SA, has family sections and Wikidata IDs) + Wikidata tourist destinations as the backbone; Wikimedia Commons photos; OpenFlights routes cross-checked with the Dublin Airport destinations table for `direct_flight`; Open-Meteo climate normals; Instagram/TikTok family-travel tags; an LLM-generated seed list that is then researched and grounded item by item. OpenTripMap only after its free-tier cap is confirmed by registering.

### 4.4 Model routing (free-first, WanderTold's chain)
Mini PC local model first (yields when its two slots are busy) → six rotating free lanes on OmniRoute → hermes/OpenRouter → give up with a reason. Four short calls per item (classify, extract, facets, description); vision gate on the local model; embeddings on the mini PC. Never a paid key. Six lanes clear ≈300 items/hour.

### 4.5 Social crew (mini PC)
Fix the TikTok lane first (0b). Replace `holidays kids` and `thingstodokids` with `things to do kids ireland`, `school holidays ireland kids`; add `limerickfamily`, `waterfordkids`, `kilkennyfamily`, `sligofamily`; for any generic tag require an Ireland signal (`ireland|dublin|cork|galway|limerick|waterford|kilkenny|sligo|éire`) in caption or author bio before staging; backfill empty captions via oEmbed before classification; hashtag pages; expansion of venue accounts found through events; holiday tags for the Holidays lane. Saved-collection lane dropped unless you say otherwise.

### 4.6 Organiser loop (new)
Every on-air item gets a "Is this your venue? Claim or update" link. Claim form: name, category, address, hours, ages, price, photo upload with a licence tick-box, contacts, socials. Outreach from `hello@smalldays.ie` when a listing goes on air: "you're listed, check it, send us photos we may use". Legitimate-interest B2B email with one-click unsubscribe; sole-trader venues held back until the Irish DPC position is checked. Photos received this way may be rehosted; nothing else is.

## 5. Admin portal: information architecture
Eight areas in one left rail, each a real URL under `/admin/`, badge counts, every list paginated and filterable, state in the URL, phone layout, undo on destructive actions, one time zone: **Overview** (metrics square), **Catalogue** (Events / Things to do / Holidays tabs, facet columns, completeness chips, edit drawer with provenance, bulk unpublish / re-enrich / re-photo), **Needs input** (the only queue; grouped by reason; hint + Resubmit / Reject / Reject-all-like-this; keys j k a r), **Sources & discovery** (per lane: last run, found / on-air / rejected; add source or tag; test now), **Production** (runs with per-stage counts and the error text, mini PC crews, model lanes, failed items with retry), **Photos**, **Metrics**, **Settings** (auto-approve, thresholds, facet editor, tag list). Flask moves behind a multi-worker server so an Approve never blocks the public site.

## 6. Domain, mail, analytics, search (new, from your answers)

**DNS at Blacknight** (cp.blacknight.com → Domains → smalldays.ie → DNS; no API, 1–2 h propagation). Records:

| Record | Name | Value |
|---|---|---|
| A | @ | 51.124.44.241 (the VM) |
| CNAME | www | smalldays.ie |
| CNAME | stats | smalldays.ie |
| MX 10 | @ | mail.trystful.com |
| TXT | @ | `v=spf1 mx include:trystful.com -all` (final form confirmed at mailbox creation) |
| TXT | mail._domainkey | DKIM public key (generated by the mail server when the first @smalldays.ie mailbox is added; I send you the exact string) |
| TXT | _dmarc | `v=DMARC1; p=none; rua=mailto:admin@smalldays.ie` (tightened to reject after 2 weeks of clean reports) |
| TXT | @ | Search Console verification string (from your Google account, §6 Search) |
| CAA | @ | `0 issue "letsencrypt.org"` (optional) |

**VM edge**: a new Caddy site block `smalldays.ie, www.smalldays.ie` with automatic HTTPS, `/admin*` behind the same portal-gate, `www` → apex, and `/kidsevents/*` on the Azure hostname 301 → `smalldays.ie`. Three hard-coded `/kidsevents/` hrefs in the app, `os/links.yaml`, and the Firebase "authorized domains" list updated the same day.

**Mail**: on the Trystful mail server (docker-mailserver 15.1 on the Hostinger box, already multi-domain): `hello@smalldays.ie` (public, outreach, claim replies), `data@smalldays.ie` (registrations: Fáilte Ireland, Bing, IndexNow), `admin@smalldays.ie` (DMARC reports). DKIM key auto-generated per domain. Passwords into the sops secrets store, never a plaintext file.

**Analytics**: Umami 3.x on the mini PC (own compose with its own small Postgres; ports in the free 81xx band, bound to the Tailscale IP; ufw tailnet-only), `stats.smalldays.ie` proxied by the VM's Caddy, tracker script and collect endpoint renamed to survive ad-blockers, custom events for outbound clicks (site, maps, Instagram, booking), search terms, filter use, saves. Its API token in the secrets store feeds the Metrics tab.

**Search**: the existing service account `small-days-auth-verifier@small-days-ireland.iam.gserviceaccount.com` can be the Search Console reader. It cannot enable APIs itself (checked today: 403), so you do three clicks once the domain resolves: (1) enable the Search Console API at the link I send, (2) add a Domain property `smalldays.ie` in Search Console and paste its TXT record at Blacknight, (3) add the service-account email as a Restricted user. Bing Webmaster verification by CNAME, IndexNow key file on the site (Bing/Yandex only; Google needs sitemap + Search Console). On page: server-rendered item pages with JSON-LD (Event, TouristAttraction/LocalBusiness, TouristDestination), sitemap regenerated on publish, canonical, OpenGraph image.

## 7. Metrics tab
Visitors (Umami: pageviews, visitors, top pages, referrers, outbound targets, 24 h / 7 d / 30 d); SEO (Search Console clicks, impressions, CTR, position per page and query; indexed pages; rich-result errors; sitemap freshness); Content (on air per catalogue, added per day and hour, completeness per field, needs-input ageing, per-source yield, model timeouts and lane cooldowns, photo coverage), stored daily so trends exist.

## 8. Public site (what the plan forces)
Item pages with URLs; facet filters (age, price, setting, activity, county map, accessibility, rainy day, near me); cards with hero, price band, age bands, county, links row, last checked; Holidays browsable by school break and by season; the toast hidden until something is actually saved; bottom nav back to five items (Today, Events, Things to do, Holidays, Saved).

## 9. Access status and what I still need from you (v3, after "do it yourself")
Checked 2026-09-13 13:30 UTC with the vault open (master password used in-session only, written nowhere):
- **Blacknight**: the vault item "Blacknight control panel (kinklink.ie DNS)" is rejected by both panels (cp.blacknight.com "Invalid login", cp.blacknighthosting.com "Login Details Incorrect"). One attempt each, then stopped to avoid a lockout. → **Update that vault item with the working login** (or the panel's own username if it isn't the email). Once it works, I do all DNS myself.
- **smalldays.ie**: Blacknight's nameservers already hold a zone (SOA serial 2026091107, A → 172.233.211.187 parking, no MX/TXT), but the .ie registry returns NXDOMAIN: **the domain is not delegated yet**, most likely pending the IEDR "connection to Ireland" check that new .ie registrations go through. → Check your Blacknight/IEDR emails for a documents request; nothing on the internet can reach the domain until this clears.
- **Google**: no Google login exists in the vault and no browser on the mini PC or the Surface holds a Google session (checked both). The Firebase service account cannot enable APIs. → Either sign into Google once in the mini PC's automation browser (the same profile that holds Instagram/TikTok) and I do the Search Console steps from there, or do the three clicks in §6 yourself.
- **Ticketmaster**: a developer account exists in the vault → the Ticketmaster lane needs nothing from you.
- Everything else defaults as written: embed policy, saved-collection lane dropped, Umami on the mini PC, mailboxes as listed. WanderTold "embed, don't download" is filed in the OS backlog (P2).

## 10. Phases and "done"

| # | Phase | Delivers | Done when |
|---|---|---|---|
| 0 | Unblock (done) | lock fix, binary path, sweep | verdicts land; hourly run adds events |
| 0b | Stop the bleeding (this week, no approval needed) | atomic writes + abort on torn read; past-date filter + prune; family-relevance and Ireland gates on the current pipeline; `normalize_event` keeps the asked-for fields; TikTok lane triage; toast/`&#039;`/date-slot/Ready-count bugs; `threaded` server with a run-now lock; dead scrapers deleted | 0 past events on air; no adult items in a re-sample of 30; TikTok lane returns results |
| 1 | Contract + taxonomy + gate | §3 schemas, `facets.json`, hard validation, normaliser, migration | 100% of on-air records validate; ≥90% events with price, age, category |
| 2 | Model routing + queue | §4.4 chain, rotating lanes, needs-input reasons | backlog resolved; needs-input ≤20; p95 classify < 60 s |
| 3 | Discovery lanes | §4.3 for all three catalogues, 32 counties | ≥300 things to do, ≥50 holidays, events in every county |
| 4 | Links + media | official / maps / Instagram / TikTok resolution, licensed hero + embeds, vision gate, alt | ≥90% maps, ≥70% official, ≥40% social link, ≥80% hero |
| 5 | Admin portal | §5 eight areas | you clear the needs-input queue on your phone in under 10 min |
| D | Domain, mail, analytics, search (parallel, starts on your DNS) | §6 | site on smalldays.ie with TLS, mail delivering with DKIM pass, Umami counting, Search Console verified |
| 6 | Metrics + SEO | §7, item pages, JSON-LD, sitemap, IndexNow | tab shows visitor, SEO, content trends; rich-result test passes on 3 pages |
| 7 | Public facets + organiser loop | §8, §4.6 | filters on real data; first claim received; Lighthouse ≥90 mobile |

Order: 0b now → 1 → 2 → 3 → 4 in the pipeline; 5 in parallel from 2; D as soon as DNS resolves; 6 after D; 7 last. Each phase: spec → executor → real-browser verification → commit, push, registry and links updated in the same turn. No approval gates between phases once v2 is approved.

## Appendix A — facet vocabulary
age_bands `0-2, 3-5, 6-9, 10-12, 13+` · price_band `free, under-10, 10-25, 25-plus, unknown` · setting `indoor, outdoor, both` · activity_types `playground, park, museum, farm, soft-play, zoo-wildlife, trail-hike, swimming, water-sports, adventure-climbing, workshop-class, festival, theatre-show, cinema, library, craft, sports, seasonal, food-market, nature-reserve` · accessibility `wheelchair, step-free, buggy, changing-places-toilet, sensory-friendly, elder-friendly` · county: 32 · region: `Leinster, Munster, Connacht, Ulster` · holiday_types, best_seasons, school_breaks, flight_time, budget_band as in §3.

## Appendix B — lifted from WanderTold vs rewritten
Lift: LLM fallback chain + local-first yielding; `facets.json` + gloss + hard validation; photo gate rules + alt-text pass; cycle/daemon locks + merge → push → deploy → verify; two-hop social crew; QA pattern (content fails, name grounding, duplicate fold); Production / Photos / Models / Failed / Scout admin tabs. Rewrite: record schemas, county geography, category vocabulary, scout prompts. Drop: narration and audio. Not copied: downloading Instagram photos without a licence.

## 11. Discovery: what runs today, and what gets added (v3, "extremely important")

### 11.1 Inventory of what actually searches today
| Lane | Runs? | Exactly what | Limits | Gap |
|---|---|---|---|---|
| TikTok keyword search (mini PC) | yes, hourly timer, **but every query rejected since 09-13 morning** | `opencli tiktok search "<q>"` for 17 fixed strings: events ireland, events kids ireland, kids dublin, holidays kids, kidseventsireland, kidsactivitiesireland, familyfriendlyireland, dublinwithkids, corkfamily, thingstodokids, irishfamilyfun, kidsindublin, dublinkids, galwayfamily, corkwithkids, kidsofcork, familyeventsireland | 15 videos per query | no hashtag pages, no account expansion, no captions backfill |
| Instagram account search (mini PC) | yes | `opencli instagram search <q>` → up to 10 accounts per query → 8 recent posts each, max 8 new accounts per run; permalinks by DOM read | 4 s courtesy sleep, shared browser budget with WanderTold | accounts never re-visited once expanded |
| Instagram keyword post search (mini PC) | yes, the lane that found 200 candidates on 09-12 | `instagram.com/explore/search/keyword/?q=<q>` DOM read, 6 scrolls | same 17 strings | captures **URL only**: no caption, no author (a third of the backlog is unjudgeable for this reason) |
| Instagram / TikTok saved collections | no (placeholder "Kids", no URL) | | | dropped per your answer |
| Web search (VM factory, hourly) | yes | one template `kids events {city} this weekend` × 5 cities through searchgw (brave → Gemini grounding → Bing → SearXNG pool → serper) **plus a direct scrape of google.com search HTML** (`_src_schedulex`) | 15 URLs per query | one template only; the Google HTML scrape is a ban risk and goes |
| Curated sites (VM factory) | yes | 22 URLs in sources.json; only `tourism/timeout/eventbrite/familyfriendly/yourdaysout` categories are fetched (Facebook groups and Songkick entries are dead weight) | | 5 cities of 32 counties; homepages not listing pages |
| JSON-LD Event harvest | yes | on crawled pages only | | no sitemap or feed crawl |
| RSS / ICS / open data / Reddit / YouTube / Facebook / Ticketmaster | **no** | | | |
| Your own social signals | **none** | the crew rides your logged-in browser session but reads nothing of yours: no saved collections, no followed accounts, no own posts | | |

Correction to the crew's own CLAUDE.md, which says the unit is "linked but not enabled": the timer is enabled and fires hourly (verified in systemd today).

### 11.2 What gets added (all free; order = priority)
**Web listing pages (deep URLs, not homepages)** — familyfun.ie/family-event-calendar/, familyfun.ie/kids-events-on-now/, mykidstime.com/tag/days-out/, yourdaysout.com per-county slugs (32), discoverireland.ie/events, dublin.ie/whats-on/, purecork.ie/whats-on, thisisgalway.ie/events/, limerick.ie/discover/whats-on, visitwaterford.com/our-events, corkcity.ie festivals-events, county council event pages with ICS export (Cork County, South Dublin, Kildare, Monaghan, then every council on the same civic platform), dublincity.ie/events/library-events-children, dublincitylibrariesevents.ie, fingal.ie/libraries-events + the other library services, heritageireland.ie/visit/family-fun/, museum.ie seasonal programme pages, eventbrite.ie/d/ireland--{city}/family-fun/ (listing pages render; the API is what's closed), allevents.in/{city}/this-weekend, kelloggsculcamps.gaa.ie per county (summer), parkrun.ie junior events.
**Feeds** — ICS/RSS from the councils above; sitemaps of every curated site crawled for `Event` JSON-LD instead of waiting for search to find pages.
**Open data (Things to do seed)** — Fáilte Ireland Open Data API (12,000+ attractions), data.gov.ie tourism-activities-and-attractions, data.gov.ie playground datasets per county + data.smartdublin.ie parks/playgrounds, OpenStreetMap Overpass for Ireland (`leisure=playground`, `tourism=museum|attraction|zoo`, `leisure=water_park`, `amenity=library`, `natural=beach`, `leisure=nature_reserve`), Coillte 260 recreation sites, Blue Flag beaches (beachawards.ie), Wikidata museums/attractions in Ireland with website + Instagram.
**Search templates (searchgw only)** — per county × season: family events {county} this weekend · things to do with kids {town} rainy day · {county} library events children · santa experience {county} 2026 · halloween {county} kids 2026 · summer camps {county} 2027 · toddler groups {county} · soft play {county} · open farm {county} · pet farm {county} · kids workshop {county} october midterm · family festival {county} 2026 · free things to do with kids {county} · baby and toddler classes {county} · school holiday camps {county} february · easter camp {county} 2027 · christmas panto {county} 2026 · playground {county} new · kids activities {county} indoor · junior parkrun {county} · museum family day {county} · library storytime {county}.
**Instagram hashtags (hashtag pages, not just keyword search)** — theme: #irishmammy, #irishparenting, #mumsofireland, #dadsofireland, #dublinparents, #familydaysoutireland, #daysoutwithkidsireland, #irelandwithkids, #kidsactivitiesdublin, #toddlerdublin, #babyfriendlydublin, #babygroupsireland, #softplayireland, #petfarmireland, #openfarmireland, #playgroundireland, #forestschoolireland, #freethingstodoireland, #familyfestivalireland, #museumsforkids; seasonal: #halloweenireland, #santaireland, #christmasireland, #panto2026, #summercampsireland, #midtermireland, #schoolholidaysireland, #eastercampireland, #rainydayireland; county: #dublinwithkids, #corkwithkids, #galwaykids, #galwaywithkids, #limerickfamily, #limerickwithkids, #kilkennywithkids, #wexfordfamily, #kerrywithkids, #donegalfamily, #sligofamily, #mayofamily, #wicklowwithkids, #meathfamily, #kildarekids, #louthfamily, #waterfordkids, #clarefamily, #tipperaryfamily, #westmeathfamily (volume verified per tag before it stays on the list; broad tags like #thingstodoinireland and #discoverireland only with the Ireland+family filter).
**Instagram accounts monitored (posts announce events)** — aggregators @mykidstime, @familyfun.ie, @yourdaysout, @discoverireland.ie, @lovindublin, @thisisgalway, @purecork, @visitwaterford, @failteireland; councils/libraries @dublincitycouncil, @fingalcoco, @librariesfingal, @dlrcoco, @sdublincoco, @corkcitycouncil, @corkcoco, @galwaycitycouncil, @galwaycoco, @limerickcouncil, @waterfordcouncil, @kilkennycoco, @wexfordcoco, @donegalcoco, @mayococo, @librariesireland; heritage/museums @heritageireland, @officeofpublicworks, @nationalmuseumireland, @nationalgalleryireland, @imma_dublin, @epicmuseumdublin, @chesterbeattylibrary; attractions @dublinzoo_official, @fotawildlife, @airfieldestate, @emeraldparkireland, @newbridgehouseandfarm, @lullymorehg, @causeycoolfarm, @brigitsgarden; theatres @ark_dublin, @civictheatretallaght, @paviliontheatre, @everymancork, @limetreetheatre, @gaietytheatre, @axistheatre, @townhallgalway; festivals @stpatricksfestival, @baborofestival, @corkmidsummer, @galwayartsfestival, @kilkennyartsfestival; creators @facesbygrace, @thedalofdinner, @travelmadmum + a one-time expansion from their suggested-accounts panels. Every venue that reaches on-air adds its own handle to this list automatically.
**TikTok** — hashtag pages (`tiktok.com/tag/<tag>`): irishmammy, dublinwithkids, corkwithkids, kidsactivitiesireland, familyfunireland, irishparenting, familydayout; search phrases: family days out ireland, things to do dublin with kids, toddler activities dublin, cork family days out, galway kids activities, halloween ireland kids, santa experience ireland, summer camp ireland, midterm activities ireland, rainy day activities ireland, free things to do ireland kids, petting farm ireland; accounts: the venue/tourism handles above that exist on TikTok, plus `tiktok.com/discover/irish-tiktok-creators` as a rotating seed.
**Ticketmaster Discovery API** — key from the vault's developer account; family/children classification filter, country IE.
**Facebook** — not automatable (groups private, pages behind login walls). Manual spot-check of Irish Mammies and MummyPages pages only.
**Your own signals** — a one-time read of the accounts you follow on Instagram and TikTok as extra seeds (bulk read once, never polled); saved collections only if you start using one.

### 11.3 Rules that make the extra volume safe
Ireland signal required for every generic tag (caption or author bio must mention Ireland or an Irish county/city); captions backfilled by oEmbed before classification; shared Instagram budget with WanderTold (20 page loads per hour, actions spaced by minutes not seconds); hashtag pages read at most twice a day; account posts read once a day; every candidate stamped with lane + query so the Sources area shows yield per lane and dead lanes get switched off; the direct google.com scrape removed.
