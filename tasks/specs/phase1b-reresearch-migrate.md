# Phase 1b spec — re-research the existing catalogue, then apply the migration

Why: phase 1's migration dry run correctly refused to proceed (0 of 108 records would survive). The cause is real, not a bad threshold: 51 of 61 events and all 47 places fail the §2 description word bands (median 24/52 words vs a 60/120 floor), and zero legacy records carry a quoted `provenance.facts`. These records were scraped, several straight from nav-tag lists (the 09-13 content-quality audit's worst examples), never "researched" under phase 1's rules. Relaxing the bands would re-admit exactly that boilerplate. The fix is to run the existing catalogue through the real four-step pipeline before migrating, using the `source_url` every record already has.

Builds on: phase 1 (`contract.py`, `gate.py`, `factory_worker.promote()`, `research_fetch`/`classify`/`gather_facts`/`extract_details`/`write_copy`, `migrate_contract.py`), phase 2 (`llm.py` routing — local lane verified live at ~99.85% success, 7–60s/call). Exclusions: no new discovery (phase 3), no photo/media work (phase 4), no schema changes — reuse phase 1's contract and gate exactly as written.

## Task
1. `rereseach_catalog.py` (new, one-shot, resumable): for every record in `events_output.json` and `places_output.json` (108 total), build a candidate shape from the existing record (`source_url`, `caption` = the old `title` + `description` concatenated as a starting hint — NOT trusted as fact, only as a pointer for `research_fetch` — `platform` inferred from the URL domain, `found_via="recatalog:<old id or index>"`), then call `factory_worker.promote(candidate)` exactly as a fresh social candidate would be. On success, replace the old record's slot with the new contract record (keep a mapping old→new so nothing is duplicated); on `needs-input`/`rejected`, write it to `staged/needs_input.json` / drop it, same as phase 1 already does for fresh candidates, and log the reason.
2. Concurrency: 4 workers (the local lane is the bottleneck; WanderTold shares it — stay under `LOCAL_BUSY_AT`, phase 2's llm.py already enforces this, just don't add your own throttle on top). Checkpoint progress every 10 records to a resumable ledger (`recatalog_progress.json`: url → outcome) so a restart skips completed ones. Expect roughly 15–25 minutes wall time for 108 records at the measured 9s median local latency × 4 calls × 108 ÷ 4 workers.
3. After the re-research pass completes: run `venv/bin/python3 migrate_contract.py --apply` (already written, idempotent, takes its own backup first). Report the new kept/needs-input/rejected table — expect it to look very different from phase 1's dry run now that descriptions are grounded.
4. If a meaningful fraction (say, more than a third) still fails the gate after re-research, that is real signal about content quality, not a bug — report it plainly with the top reasons, do not loosen the gate to force a number.

## Scope lock
- MUST edit/create only: `rereseach_catalog.py`, `recatalog_progress.json` (git-ignored — add it to `.gitignore`), `tasks/todo.md`. MUST NOT touch `contract.py`, `gate.py`, `factory_worker.py`'s pipeline functions, `llm.py`, or any file under `web/`.
- MUST take the backup via `migrate_contract.py`'s existing `--apply` path — do not write a second backup mechanism.
- Respect the factory's `daemon.lock`: check `systemctl --user is-active kidsevents-factory.service` and the lock file before starting; if the hourly cycle is running, wait for it or note that you ran alongside it safely (the lock only guards one cycle at a time, not this script — confirm your writes go through `output_lock()`/`publish_record()` so they can't race the timer).

## Evidence required in the final report (≤50 lines)
- Progress ledger summary: 108 processed, outcome counts, wall time, calls by lane (should still be dominated by `local`).
- The migration table after `--apply`: kept/needs-input/rejected per file, top 5 reasons if any bucket is non-trivial, backup path.
- `curl /api/events | jq length` and one full contract record from `/api/v1/events` showing a real quoted fact.
- `pytest -q` still green (should be unaffected — you're not touching gate logic).
- Playwright screenshot of `/` after `systemctl --user restart kidsevents-ie.service` showing the new (probably smaller) on-air count with zero console errors.
- Commit + push (`rereseach_catalog.py`, `.gitignore`, `tasks/todo.md`, plus the migrated data files) with `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>` and `Claude-Session: https://claude.ai/code/session_01BsEd8XKU1NMdhWwev3Xnpw`.
- Stop. Do not start phase 3 or phase 4.
