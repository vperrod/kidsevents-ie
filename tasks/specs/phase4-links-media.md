# Phase 4 spec — links and media: official site, maps, Instagram/TikTok, licensed hero, embeds, vision gate, alt text

Plan §4.1 (Links + media), §2 decision 5 (embed, don't rehost). Builds on phase 1 contract (`links`, `media`) and phase 2 routing. Exclusions: no organiser claim flow (phase 7), no admin views (phase 5 reads `media`/`links` fields as they appear), no public-site changes beyond what §5 says.

## 1. Links resolution (`links.py`, run in `promote()` after `write`, and as a nightly `links_refresh` for on-air records missing any link)
- `official_url`: JSON-LD `url`/`sameAs` on the source page → Wikidata P856 (by name + county, place/holiday) → the source page's outbound links to a domain whose name folds to the venue name → else "". Verify with a HEAD/GET (200/301) before storing; store `links_checked` timestamp.
- `instagram_url`: scan the official site HTML for `instagram.com/<handle>` (footer/nav) → Wikidata P2003 → searchgw `site:instagram.com "<venue name>" <county>` and accept only when the handle or profile name folds to the venue name (else needs-input `instagram_url` is NOT raised — links are optional, never block on-air). Same for `tiktok_url` (`tiktok.com/@…`).
- `maps_url`: derived by `contract.maps_url()` (already in phase 1). `booking_url`: from JSON-LD `offers.url` or the extract step.
- Social candidates: `links.instagram_url`/`tiktok_url` default to the author's profile URL when the author's bio/handle folds to the venue (the classify step already answers `is_venue_account`; add that boolean to its JSON if missing).

## 2. Media (`media.py`)
- Hero candidates in order: (a) Wikimedia Commons (`action=query&list=search` on the venue name + county, then `imageinfo` with `extmetadata` → licence must be CC0/CC BY/CC BY-SA/PD; keep `Artist`, `LicenseShortName`, `LicenseUrl`); (b) Openverse API (`license_type=commercial`, `license=cc0,by,by-sa`; attribution string from the response); (c) the official site's `og:image` (stored as `source=official-site, licence=""` and flagged `placeholder=true`, used only when (a)/(b) found nothing; shown with a "photo: <domain>" credit and replaced automatically the next time (a)/(b) succeeds); (d) nothing. Never download from Instagram/TikTok; those become `media.embeds[] = [{platform, url}]` from `links.instagram_url`/`tiktok_url` and the candidate's own `source_url` when it is a post/reel/video.
- Storage: `web/media/<kind>/<id>-<hash>.jpg`, resized to max 1600 px wide with Pillow if present in the venv (else stored as fetched, ≤2 MB); public path `/media/...` served by Flask's static route (add to server.py). Disk-only, git-ignored (`web/media/` in `.gitignore`).
- Vision gate on the mini PC local model through the phase 2 tunnel (`/v1/chat/completions` with an image content part; if the local server rejects images, fall back to OmniRoute `auto/best-free` with an image part, else skip the gate and mark `gate="skip"`): rules lifted from WanderTold `photo-gate.py` `RULES` (reject portraits/selfies/crowds-as-subject, maps/logos/menus/posters/screenshots/AI renders, signage naming a different business); verdict `hero|pass|reject` + one-line `why`; `alt` text written by the same call ("Write alt text for this photo of <name> in <county>, ≤120 chars"). Fail-open: a gate error never deletes a photo, only `gate="skip"`.
- Budget: ≤3 candidate downloads per record, ≤1 gate call per candidate, nightly `media_refresh` capped at 200 records.

## 3. Public site (minimal, `web/index.html` only)
- Card hero from `media.hero.url` with the credit line; on the detail modal a links row (site · maps · Instagram · TikTok · book) using `links.*`, and the embeds rendered lazily (`<iframe loading="lazy" src="https://www.instagram.com/p/<code>/embed/">` for Instagram posts/reels, TikTok `https://www.tiktok.com/embed/v2/<id>` — extract ids from the URLs; only render when the modal opens; never on the card grid).
- The public routes still serve `legacy_view`, so add `image_url`, `image_credit`, `official_url`, `instagram_url`, `tiktok_url`, `booking_url`, `embeds` to `legacy_view()` (phase 1 file `contract.py`, additive).

## Verification (real, in the report)
- `pytest test_links.py test_media.py` (name-fold matching, licence filter, embed id extraction, fail-open gate on fixtures).
- Run `links_refresh` + `media_refresh` on 40 on-air places/events: table of official/maps/instagram/tiktok/hero coverage before → after; 5 example records; gate verdict counts and 3 `why` lines; confirm no file under `web/media` came from instagram/tiktok domains (`grep -c instagram media_index.json` = 0).
- Public site: Playwright screenshot of a card with a hero + credit and a detail modal with the links row and one Instagram embed rendered (iframe present, 200), zero console errors.
- Commit + push with the two trailers. Stop.
