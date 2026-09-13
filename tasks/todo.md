# Small Days overhaul — execution checklist (plan: tasks/overhaul-plan-2026-09-13.md, approved "go" 2026-09-13 17:05 UTC)

## Phase 0 — unblock (done 2026-09-13)
- [x] re-entrant output_lock, classification outside the lock, hermes absolute path (26ae87f)

## Phase 0b — stop the bleeding (in progress)
- [x] A backend (665e17a, 21 tests green): atomic JSON writes + abort on torn read; past-date filter + prune (135→40 events, 59 past + 36 undated); family/Ireland/title gates in publish; normalize_event keeps the 13 dropped fields (root cause of 0% cost/age/category); completeness confidence; threaded server + run-now lock; google HTML scrape removed; sources.json cleaned + deep listings; 11 dead modules deleted; holidays→places, London rejected. Found: OpenRouter/hermes lane times out on every call → phase 2 before phase 1. Leftover: 4 GB-country + 19 "Ireland"-spelled legacy events (phase 1 migration normalises)
- [x] B web (8d994e6, 42/42 browser checks): toast hidden until saved; date-slot fallback; href scheme guard; confidence = completeness; nav 5; admin `&#039;`; Ready count; Social as paginated cards with visible decisions, filters, search; editorial pagination; single nav + hash routing; failed-run error/log link
- [x] C mini PC crew (local commit 0e2a8e1): TikTok "Navigation rejected" = cold-tab race in the browser automation, recovered by retry (lane was never down; retry now 4 s); queries 17→24 (generic dropped, Ireland-anchored added); Ireland-signal rule; author backfill (Instagram captions only fetchable from the VM → phase 1 research step); `found_via` = `<lane>:<query>`; CLAUDE.md corrected. Forgejo repo `work/kidsevents-social` pending (token lacks write:organization)

## Phase 1 — contract + taxonomy + gate
- [ ] catalog/facets.json (Appendix A + holidays facets), gate_meta validation, normaliser to the §3 contract, migration of existing records, QA gate §4.2

## Phase 2 — model routing + queue
- [ ] local-first → 6 rotating OmniRoute lanes → hermes; per-item budget; needs-input reasons; backlog re-swept

## Phase 3 — discovery lanes (§4.3, §11/12)
## Phase 4 — links + media (maps, official, Instagram/TikTok, licensed hero, embeds, vision gate, alt)
## Phase 5 — admin portal IA (8 areas) — parallel from phase 2
## Phase D — domain, mail, analytics, search — blocked on IEDR activation of smalldays.ie (Victor uploads document)
## Phase 6 — metrics + SEO (after D)
## Phase 7 — public facets + organiser loop

## Review notes
- (append as phases close)
