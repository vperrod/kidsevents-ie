# Event ingestion audit — 10 September 2026

## What is actually running

The active systemd unit runs `factory_worker.py` with no command-line flags.
It discovers pages for Dublin, Cork, Galway, Waterford and Limerick through a
local search gateway, crawls those pages with crawl4AI/Playwright, then uses
schema.org Event JSON-LD where present and Hermes extraction for page text.
It writes directly to `events_output.json`.

The unit is configured with `Restart=always`, although the worker is a
one-cycle program. It therefore restarts after finishing instead of allowing
the four-hour timer to control the cadence. The status showed hundreds of
restarts. This should be changed by the administrator of the root-owned unit
to `Type=oneshot` and `Restart=no` (or the worker should become a deliberate
long-running process).

## Social sources: current reality

The server environment has Instagram and TikTok-related settings, but the
active command does **not** include `--social`, so neither social collector is
currently part of the automatic production run.

- Instagram mode discovers public post URLs through the search gateway, then
  attempts a Playwright fetch with the configured session/cookie. It does not
  ingest a saved Instagram collection.
- The former TikTok mode discovers public result URLs through the search
  gateway, calls TikTok's public oEmbed endpoint, and falls back to a browser
  fetch. It does **not** access a user's private TikTok collection.
- The older `main.py` scraper pipeline is documented in the README, but it is
  not the service that is currently running.

## Feed quality observed

The current public feed contains 126 records. It is dominated by historical
FamilyFun-derived records and has no populated structured `cost` or
`age_group` values. That is why the interface now shows **Cost to check** and
**Ages to check** rather than pretending to know. A calendar filter correctly
excludes records without a verified date.

There is also a schema mismatch to fix before treating the factory output as
trusted: the worker produces `date`, `venue_coords`, `cost_detail` and status
fields, while the public site consumes `start_date`, `latitude`, `longitude`
and the established public event contract. It currently merges data without a
normalisation and publication-quality gate.

## Required correction before expanding automated ingestion

1. Make the factory publish only canonical event records through one
   normaliser, with `start_date`, source URL, location/map fields, category,
   age guidance and price status.
2. Send incomplete or undated discoveries to `staged/` for review instead of
   silently merging them into the public feed.
3. Fix the root-owned service restart policy so the timer controls cadence.
4. Add a per-source run report: discovered, extracted, rejected, staged and
   published; surface this in the admin panel.
5. Enable a separately rate-limited social job only after its output has the
   same review gate. Never use private collection cookies as an import method.

## Holiday ideas

`holidays_output.json` is intentionally separate from dated events. The first
three entries link to the original destination or tourism source and each has
Google Maps and an organiser/options link in the interface. The import shape
for TikTok shares is documented in `HOLIDAY_IDEA_IMPORT.md`; shared video URLs
are leads for editorial verification, not publish-ready facts.

## WanderTold-equivalent saved collections

`social_collection_ingest.py` now mirrors WanderTold's collection reader:
OpenCLI opens the private Instagram/TikTok collection in the account owner's
authenticated Chrome profile, enumerates the rendered post links (including
lazy-loaded items), and writes de-duplicated candidates to
`staged/social_candidates.json` with `needs_review` status. It deliberately
does not publish to either public feed. The accompanying user-service/timer
files in `systemd/` use a one-shot, hourly cadence.

This VM's OpenCLI daemon currently has no connected browser profile, and the
Kids collection URL has not yet been configured. Those two prerequisites are
the only remaining connection steps before the collection reader can run.
