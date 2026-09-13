# Small Days overhaul — execution checklist (plan: tasks/overhaul-plan-2026-09-13.md, approved "go" 2026-09-13 17:05 UTC)

## Phase 0 — unblock (done 2026-09-13)
- [x] re-entrant output_lock, classification outside the lock, hermes absolute path (26ae87f)

## Phase 0b — stop the bleeding (in progress)
- [x] A backend (665e17a, 21 tests green): atomic JSON writes + abort on torn read; past-date filter + prune (135→40 events, 59 past + 36 undated); family/Ireland/title gates in publish; normalize_event keeps the 13 dropped fields (root cause of 0% cost/age/category); completeness confidence; threaded server + run-now lock; google HTML scrape removed; sources.json cleaned + deep listings; 11 dead modules deleted; holidays→places, London rejected. Found: OpenRouter/hermes lane times out on every call → phase 2 before phase 1. Leftover: 4 GB-country + 19 "Ireland"-spelled legacy events (phase 1 migration normalises)
- [x] B web (8d994e6, 42/42 browser checks): toast hidden until saved; date-slot fallback; href scheme guard; confidence = completeness; nav 5; admin `&#039;`; Ready count; Social as paginated cards with visible decisions, filters, search; editorial pagination; single nav + hash routing; failed-run error/log link
- [x] C mini PC crew (local commit 0e2a8e1): TikTok "Navigation rejected" = cold-tab race in the browser automation, recovered by retry (lane was never down; retry now 4 s); queries 17→24 (generic dropped, Ireland-anchored added); Ireland-signal rule; author backfill (Instagram captions only fetchable from the VM → phase 1 research step); `found_via` = `<lane>:<query>`; CLAUDE.md corrected. Forgejo repo `work/kidsevents-social` pending (token lacks write:organization)

## Phase 1 — contract + taxonomy + gate
- [x] `catalog/facets.json` (32 counties + region + NI flag + the allevents codes, legacy alias maps, holiday facets with the 2026/27 school-break dates); `contract.py` (`EMPTY_RECORD`/`validate`/`derive`/`completeness`/`legacy_view`/`slugify`/`maps_url`/`region_for`); `gate.py` (`gate_meta` + `gate_holiday` drop-and-count, `facet_gloss`, `qa` with the needs-input/rejected split); the four grounded steps in `factory_worker.py` (`research_fetch` → `classify` → `gather_facts` → `extract_details` → `write_copy`, quotes verified in code, JSON-LD pre-fills `event.*` and skips extract) behind `promote()`; `staging.py` stores `missing_field` and the `needs_input` status; `server.py` serves `legacy_view` on `/api/*` and full records on `/api/v1/*`; 66 new tests (101 green)
- [x] **the migration is applied** (backup `~/backups/kidsevents-ie/pre-phase1-20260913T230009Z.tgz`): events 4 on-air / 51 needs-input / 4 rejected, places 4 / 45 / 0, holidays 0/0/0. 96 records moved to `staged/needs_input.json`, nothing deleted. The surviving reason is still the §2 word band (46 events, 45 places) — but now measured against re-researched copy, not scraped boilerplate.
- [x] **Phase 1b**: `rereseach_catalog.py` re-researches the catalogue through `promote()` (real fetch + the four grounded steps), resumable via `recatalog_progress.json` (git-ignored). Two passes, 80 min of wall time: 6 on-air, 57 needs-input, 14 rejected, and **29 never got a verdict** (27 `lane-failed`, 2 `error`) — the free OmniRoute lanes were 429-rate-limited for most of the run and the local llama slots were busy with the hourly cycle, so `classify` answered only 64% of its calls. Those 29 stayed legacy and are parked in `needs_input.json`; re-running the script retries exactly them when the lanes recover. Found on the way: `extract_obj` can return a JSON *list*, which `extract_details`/`write_copy`'s `or {}` does not catch, so `details.get(...)` raises `AttributeError: 'list' object has no attribute 'get'` (5 records in pass 1); and `publish_record` re-sorts the whole store on every publish, so nothing may key a record by its position in the file.
- [ ] cleanup (low priority, bundle later): `_is_chrome`'s nav-link regex is dead (matches nothing, branch never taken); some prompt strings emit a literal `\n` instead of a newline

## Phase 2 — model routing + queue
- [x] local-first → rotating OmniRoute lanes → `auto/best-free` → hermes (`llm.complete`, every attempt logged to `routing.jsonl`); `kidsevents-llama-tunnel.service` on the VM forwards 18089 → mini PC 8089; `factory_worker.hermes()` is now a thin wrapper; sweep runs `SWEEP_WORKERS` (3) with a `MAX_ITEM_SECS` wall; `factory_state.json.llm` = calls/errors/p50 per lane; hourly cycle takes `daemon.lock` and skips if the last one still runs (`CYCLE_WALL_SECS`). Backlog re-sweep belongs to phase 1 (needs the four-step pipeline + `needs_input` semantics)

## Phase 3 — discovery lanes (§4.3, §11/12)
## Phase 4 — links + media (maps, official, Instagram/TikTok, licensed hero, embeds, vision gate, alt)
## Phase 5 — admin portal IA (8 areas)
- [x] 0ca9087: eight areas, hash routes, badges, paginated everywhere, keyboard j/k/a/r, undo semantics, explicit "not available yet" states (64/64 browser checks). Plug points for phase 1 (`needsInputFeed`, `decisionHistory`) and phase 2 (`llmLanes`). Left disabled until endpoints exist: bulk actions, add source, photo upload, settings editing
## Phase D — domain, mail, analytics, search — domain blocked on IEDR activation (Victor uploads document)
- [x] Umami 3.3.1 on the mini PC (`~/tools/smalldays-umami`, unit `smalldays-umami`, 100.105.72.86:8150, secrets `mini-pc/umami.env` sops 5298dd2, website id 1dbff85a-b495-4436-ac7e-d43034c03517, tracker `/sd.js` + `/api/sd`)
- [ ] VM Caddy site block smalldays.ie + www + stats → after delegation
- [ ] mailboxes hello@/data@/admin@ on the Trystful docker-mailserver + DNS (MX/SPF/DKIM/DMARC) → after delegation
- [ ] Search Console (Victor's 3 clicks) + Bing + IndexNow → after delegation
## Phase 6 — metrics + SEO (after D)
## Phase 7 — public facets + organiser loop

## Review notes
- (append as phases close)
