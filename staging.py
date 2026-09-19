#!/usr/bin/env python3
"""Staging desk for social candidates collected on the mini PC.

Reading Instagram/TikTok saved collections needs an already-authenticated
browser session, which only exists on the mini PC's OpenCLI/agent-reach-chrome
daemon. That collector pipes its findings into this script over SSH:

    ssh azureuser@claude-dev-vperrod.westeurope.cloudapp.azure.com \\
        "cd /home/azureuser/kidsevents-ie && venv/bin/python3 staging.py append"

Auto-approve (Victor 2026-09-12: "I don't have time to review all
manually"): every staged candidate eventually runs through the same
enrichment gate the admin Approve button uses (factory_worker.promote -- a
real research fetch and four grounded model steps, published only if the QA
gate passes; never invented). A candidate that doesn't clear that gate
becomes `needs_input` with the one `missing_field` a curator's note could
supply, or `rejected` when nothing anybody types would help -- auto-approve
is a tighter gate applied automatically, not a lower one.

Until 2026-09-16 `append` ran that gate synchronously, one candidate at a
time, right here -- fine for a handful of new items, but a real
classification is minutes long and the mini PC's collector calls `append`
over a single SSH connection with a 600s ceiling. A run that found 19 new
candidates blew that budget and the whole SSH call timed out, losing the
run. `append` now only stages (`needs_review`, fast); `kidsevents-social-sweep`
(scheduled, see sweep_pending below) does the actual auto-approving on its
own next pass, with no per-call time pressure.

All read-modify-write access to this file goes through factory_worker's
shared lock -- confirmed live 2026-09-12 that the sweep and a concurrent
admin action (or the sweep and itself, across iterations) can otherwise
silently clobber each other's writes.
"""

import concurrent.futures
import fcntl
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import factory_worker

BASE = Path(__file__).resolve().parent
STAGED_FILE = BASE / "staged" / "social_candidates.json"
# One sweep at a time. Without this, the scheduled timer firing again mid-run
# (a sweep can legitimately take longer than the timer interval) or a second
# manual invocation picks the SAME pending candidate, both classify it
# independently, and whichever write lands last silently overwrites the
# other's verdict -- found 2026-09-15: a candidate whose place had genuinely
# gone on-air was left showing status "rejected" from a second, later,
# unluckier concurrent classification pass. mark_candidate's per-write lock
# only makes each individual write atomic; it does nothing to stop two
# sweeps from racing to write different verdicts for the same candidate.
SWEEP_LOCK_FILE = BASE / "staged" / "sweep.lock"
# Classification is nearly all waiting on a model, so a few in flight at once
# turns a backlog from hours into minutes. Three is what the mini PC's two
# local slots plus the gateway lanes absorb without either queueing.
SWEEP_WORKERS = int(os.environ.get("SWEEP_WORKERS", "3") or 3)
# How many times the sweep may automatically re-classify the same candidate before it
# leaves the queue alone (an admin resubmit is never capped — see sweep_pending).
MAX_SWEEP_ATTEMPTS = int(os.environ.get("SWEEP_MAX_ATTEMPTS", "3") or 3)

REVIEW_NOTE = (
    "Verify destination, dates, age guidance and price on the organiser "
    "website before publishing."
)


def load_staged():
    return factory_worker.load_json_store(STAGED_FILE, [])


def save_staged(candidates):
    STAGED_FILE.parent.mkdir(exist_ok=True)
    factory_worker.write_json_atomic(STAGED_FILE, candidates)


def mark_candidate(source_url, mutate_fn):
    """Lock, reload fresh from disk, find the candidate, apply mutate_fn(candidate)
    in place, save. Reloading under the lock (not reusing an in-memory copy)
    is what makes this safe against a concurrent admin click or another
    sweep iteration -- returns mutate_fn's return value, or None if the
    candidate isn't there."""
    with factory_worker.output_lock():
        candidates = load_staged()
        candidate = next((c for c in candidates if c.get("source_url") == source_url), None)
        if candidate is None:
            return None
        result = mutate_fn(candidate)
        save_staged(candidates)
        return result


def apply_verdict(candidate, record, reason, missing_field, hint=""):
    """Apply a classification verdict to the candidate in place and, on
    success, publish the record. Call under mark_candidate's lock.

    A verdict that did not publish splits two ways, which is the whole point
    of the gate: `missing_field` names one thing a curator's note could
    supply, so the candidate becomes `needs_input` and stays in the desk;
    without one, nothing anybody types will make it publishable and it is
    `rejected`. Returns True if it published.
    """
    if hint:
        candidate["hint"] = hint
    candidate["reviewed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    candidate["sweep_attempts"] = int(candidate.get("sweep_attempts") or 0) + 1
    # "no model lane answered" is an outage, not a judgement about the post: park it
    # back on needs_review so it is picked up when the lanes recover, instead of
    # stamping it rejected forever (the discovery desk already does this, staging did
    # not). The attempts counter above still bounds how often it is retried.
    if (reason or "").startswith("no model lane answered"):
        candidate["status"] = "needs_review"
        candidate["reason"] = reason
        candidate.pop("missing_field", None)
        return False
    if not record:
        candidate["status"] = "needs_input" if missing_field else "rejected"
        candidate["reason"] = reason
        if missing_field:
            candidate["missing_field"] = missing_field
        else:
            candidate.pop("missing_field", None)
        return False
    kind = record["kind"]
    published = factory_worker.publish_record(record)
    if not published:
        # publish_record() only says no for a duplicate id (already on-air,
        # nothing lost) or record["status"] != "on-air" (a real bug upstream --
        # promote() should never hand back anything else here). Telling those
        # apart matters: silently calling both "approved" is what let 69 of 78
        # historical approvals vanish with no trace (found 2026-09-15).
        store = factory_worker.STORE_FOR_KIND[kind]
        already_live = any(other.get("id") == record["id"]
                           for other in factory_worker.load_json_store(store, []))
        if not already_live:
            candidate["status"] = "rejected"
            candidate["reason"] = f"publish_record() refused it (status was {record.get('status')!r}) -- not on-air, needs investigation"
            candidate.pop("missing_field", None)
            return False
    candidate["status"] = "approved"
    candidate["review_note"] = f"Approved: classified as a{'n' if kind == 'event' else ''} {kind}."
    candidate.pop("reason", None)
    candidate.pop("missing_field", None)
    return True


def try_approve_by_url(source_url, hint=""):
    """Classify one staged candidate and apply the verdict -- used by the admin
    approve/resubmit route, the sweep, and newly appended candidates. The LLM
    call (up to minutes) runs on a snapshot OUTSIDE the lock; only the
    fresh-read mutation + publish hold it, so hourly appends and admin clicks
    are never queued behind a long classification."""
    snapshot = next((c for c in load_staged() if c.get("source_url") == source_url), None)
    if snapshot is None:
        return False
    record, reason, missing_field = factory_worker.promote(snapshot, hint=hint)
    return mark_candidate(
        source_url, lambda c: apply_verdict(c, record, reason, missing_field, hint=hint)
    ) or False


def write_staged(candidates):
    """Append new candidates, deduplicated by source_url. Returns the count added.

    Used to also auto-approve synchronously here, one candidate at a time,
    right after staging -- fine for the usual handful of new items, but the
    mini PC's collector calls this over a single SSH connection from
    `submit.py` with a 600s ceiling, and a real classification is a crawl
    plus up to four model calls, several minutes each. A run that found 19
    new candidates (2026-09-16) blew that budget and the whole SSH call
    timed out, failing the run and leaving its find uncommitted. AUTO_APPROVE
    still means Victor never has to review these by hand -- it just happens
    on kidsevents-social-sweep's own next pass instead of blocking here."""
    with factory_worker.output_lock():
        existing = load_staged()
        seen = {item.get("source_url") for item in existing}
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        additions = []
        for candidate in candidates:
            if candidate["source_url"] in seen:
                continue
            seen.add(candidate["source_url"])
            additions.append(
                {
                    "platform": candidate.get("platform", ""),
                    "source_url": candidate["source_url"],
                    "caption": candidate.get("caption", ""),
                    "author": candidate.get("author", ""),
                    # Which saved collection or search tag surfaced this post.
                    "found_via": candidate.get("found_via", ""),
                    "captured_at": timestamp,
                    "status": "needs_review",
                    "review_note": REVIEW_NOTE,
                }
            )
        save_staged(existing + additions)
    return len(additions)


def approve_safely(source_url):
    """try_approve_by_url, never raising. WanderTold's pattern (see
    rereseach_catalog.process()): every step this calls down to -- the fetch,
    the crawl, each hermes call -- already has its own real timeout, so a
    blocked or slow page fails in seconds and the item just moves to its next
    step, the same as it does everywhere else in this codebase.

    Until 2026-09-15 this instead wrapped the WHOLE item in a second, outer
    MAX_ITEM_SECS wall on its own single-use executor: an anti-bot block deep
    in one step didn't fail fast, it burned the entire budget, and giving up
    couldn't even free the worker -- Python threads can't be killed, so the
    abandoned classification kept running (and kept using CPU, a search
    gateway slot, a hermes call) after the sweep had already moved on and
    counted it as a timeout. WanderTold has no equivalent of this: it never
    wraps one venue's whole pipeline in a second ceiling above its steps'
    own, which is why the exact same anti-bot walls (242 of them on
    WanderTold in the six hours this was found) never stall it the way they
    were stalling this sweep."""
    try:
        return try_approve_by_url(source_url)
    except Exception as error:
        factory_worker.log(f"sweep {source_url[:70]}: {error}")
        return False


def _factory_cycle_running():
    """True while another factory cycle holds the cycle lock.

    The sweep and the factory share the mini PC's two local-model slots. The sweep is the
    low-yield lane (1.1% of what it classifies publishes) and it runs for up to 45 minutes;
    when it overlapped a cycle on 2026-09-16 the factory's candidates hit their 300 s wall
    and were recorded as rejections. So the sweep yields to the factory: it exits at once
    rather than competing, and its own backlog is picked up on the next hourly pass.
    """
    try:
        handle = open(factory_worker.CYCLE_LOCK_FILE, "w")
    except OSError:
        return False
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    finally:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()
    return False


def sweep_pending():
    """Auto-approve every already-staged needs_review candidate (clears a
    backlog collected before AUTO_APPROVE existed, or after it was off).
    Goes through try_approve_by_url per item -- locked and freshly reloaded
    each time, so a concurrent admin action mid-sweep can't be clobbered by a
    stale in-memory copy -- SWEEP_WORKERS at a time, and logs progress since a
    backlog can be hundreds deep and each item costs a real LLM call.
    Returns (checked, approved)."""
    # Re-run legacy items whose first pass incorrectly treated a location as a
    # dated event (or lacked an address). The classifier now explicitly maps
    # evergreen attractions to `place`, so these can become publishable after
    # research without asking Victor to supply an event date.
    retryable = {"start_date", "address", "county", "description"}
    staged = load_staged()
    pending_urls, parked = [], []
    for c in staged:
        attempts = int(c.get("sweep_attempts") or 0)
        retry = (c.get("status") == "needs_input" and c.get("missing_field") in retryable)
        if c.get("status") != "needs_review" and not retry:
            continue
        # A first pass is always allowed; only the endless re-run is capped. Without this
        # the same candidates are re-classified every hour for a verdict that never
        # changes (measured 2026-09-16: 466 retryable candidates, ~18 h of local-model
        # time per pass, 0 items gained).
        if retry and attempts >= MAX_SWEEP_ATTEMPTS:
            parked.append(c["source_url"])
            continue
        pending_urls.append(c["source_url"])
    if parked:
        print(f"  {len(parked)} candidate(s) parked after {MAX_SWEEP_ATTEMPTS} attempts "
              f"— resubmit from the desk to retry one", file=sys.stderr, flush=True)
    # SWEEP_LIMIT caps one run -- a 600-deep backlog is hours of real LLM calls,
    # and a short sweep is how you check the gate before spending them.
    limit = int(os.environ.get("SWEEP_LIMIT", "0") or 0)
    if limit > 0:
        pending_urls = pending_urls[:limit]
    approved = done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=SWEEP_WORKERS) as pool:
        futures = {pool.submit(approve_safely, url): url for url in pending_urls}
        for future in concurrent.futures.as_completed(futures):
            url = futures[future]
            done += 1
            ok = future.result()
            status = "approved" if ok else "still needs review"
            if ok:
                approved += 1
            print(f"  [{done}/{len(pending_urls)}] {status} — {url[:70]}",
                  file=sys.stderr, flush=True)
    factory_worker.record_llm_stats()
    return len(pending_urls), approved


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("append", "sweep"):
        print("usage: staging.py append  (candidate JSON array on stdin)", file=sys.stderr)
        print("       staging.py sweep   (auto-approve the existing needs_review backlog)",
              file=sys.stderr)
        return 2
    if sys.argv[1] == "sweep":
        if _factory_cycle_running():
            print("factory cycle running — sweep yields this pass", file=sys.stderr)
            return 0
        SWEEP_LOCK_FILE.parent.mkdir(exist_ok=True)
        lock_fh = open(SWEEP_LOCK_FILE, "w")
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("another sweep is already running -- skipping", file=sys.stderr)
            return 0
        try:
            checked, approved = sweep_pending()
        finally:
            fcntl.flock(lock_fh, fcntl.LOCK_UN)
            lock_fh.close()
        print(f"Swept {checked} pending candidates, auto-approved {approved}.")
        return 0
    try:
        candidates = json.loads(sys.stdin.read())
    except json.JSONDecodeError as error:
        print(f"stdin is not valid JSON: {error}", file=sys.stderr)
        return 2
    if not isinstance(candidates, list):
        print("stdin must be a JSON array of candidate objects", file=sys.stderr)
        return 2
    missing = [c for c in candidates if not isinstance(c, dict) or not c.get("source_url")]
    if missing:
        print(f"{len(missing)} candidate(s) have no source_url", file=sys.stderr)
        return 2
    print(f"Staged {write_staged(candidates)} new social candidates.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
