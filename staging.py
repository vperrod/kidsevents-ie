#!/usr/bin/env python3
"""Staging desk for social candidates collected on the mini PC.

Reading Instagram/TikTok saved collections needs an already-authenticated
browser session, which only exists on the mini PC's OpenCLI/agent-reach-chrome
daemon. That collector pipes its findings into this script over SSH:

    ssh azureuser@claude-dev-vperrod.westeurope.cloudapp.azure.com \\
        "cd /home/azureuser/kidsevents-ie && venv/bin/python3 staging.py append"

AUTO_APPROVE (same pattern as WanderTold's factory, on by default -- Victor
2026-09-12: "I don't have time to review all manually"): every newly staged
candidate is immediately run through the same enrichment gate the admin
Approve button uses (factory_worker.promote_candidate -- real LLM read of
the caption, published only if it yields a real date or a real identifiable
place; never invented). A candidate that doesn't clear that gate just stays
needs_review with a stored `reason` explaining why -- auto-approve is a
tighter gate applied automatically, not a lower one. Set AUTO_APPROVE=off in
.env to go back to fully manual review.

All read-modify-write access to this file goes through factory_worker's
shared lock -- confirmed live 2026-09-12 that the sweep and a concurrent
admin action (or the sweep and itself, across iterations) can otherwise
silently clobber each other's writes.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import factory_worker

BASE = Path(__file__).resolve().parent
STAGED_FILE = BASE / "staged" / "social_candidates.json"
AUTO_APPROVE = os.environ.get("AUTO_APPROVE", "on").strip().lower() != "off"

REVIEW_NOTE = (
    "Verify destination, dates, age guidance and price on the organiser "
    "website before publishing."
)


def load_staged():
    try:
        return json.loads(STAGED_FILE.read_text()) if STAGED_FILE.exists() else []
    except json.JSONDecodeError:
        return []


def save_staged(candidates):
    STAGED_FILE.parent.mkdir(exist_ok=True)
    STAGED_FILE.write_text(json.dumps(candidates, indent=2, ensure_ascii=False) + "\n")


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


def classify_and_apply(candidate, hint=""):
    """Run one candidate through the event/place gate and, on success, publish
    it. Mutates candidate in place either way: on success sets status/
    reviewed_at/review_note; on failure sets `reason` (why not) and, if given,
    `hint` (what a curator already tried) so the admin page can show it.
    Returns True if it published."""
    kind, record, reason = factory_worker.promote_candidate(candidate, hint=hint)
    if not record:
        candidate["reason"] = reason
        if hint:
            candidate["hint"] = hint
        return False
    (factory_worker.publish_event if kind == "event" else factory_worker.publish_place)(record)
    candidate["status"] = "approved"
    candidate["reviewed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    candidate["review_note"] = f"Approved: classified as a{'n' if kind == 'event' else ''} {kind}."
    candidate.pop("reason", None)
    if hint:
        candidate["hint"] = hint
    return True


def try_approve_by_url(source_url, hint=""):
    """Locked, fresh-read version of classify_and_apply for an existing staged
    candidate -- used by the admin approve/resubmit route and by the sweep."""
    return mark_candidate(source_url, lambda c: classify_and_apply(c, hint=hint)) or False


def write_staged(candidates):
    """Append new candidates, deduplicated by source_url. Returns the count added
    (auto-approved candidates count as added -- they still land in the ledger,
    just already published)."""
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
        if AUTO_APPROVE:
            for candidate in additions:
                classify_and_apply(candidate)
        save_staged(existing + additions)
        return len(additions)


def sweep_pending():
    """Auto-approve every already-staged needs_review candidate (clears a
    backlog collected before AUTO_APPROVE existed, or after it was off).
    Goes through try_approve_by_url per item -- locked and freshly reloaded
    each time, so a concurrent admin action mid-sweep can't be clobbered by a
    stale in-memory copy -- and logs progress since a backlog can be hundreds
    deep and each item costs a real LLM call. Returns (checked, approved)."""
    pending_urls = [c["source_url"] for c in load_staged() if c.get("status") == "needs_review"]
    approved = 0
    for i, url in enumerate(pending_urls, 1):
        ok = try_approve_by_url(url)
        if ok:
            approved += 1
        print(f"  [{i}/{len(pending_urls)}] {'approved' if ok else 'still needs review'} — {url[:70]}",
              file=sys.stderr, flush=True)
    return len(pending_urls), approved


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("append", "sweep"):
        print("usage: staging.py append  (candidate JSON array on stdin)", file=sys.stderr)
        print("       staging.py sweep   (auto-approve the existing needs_review backlog)",
              file=sys.stderr)
        return 2
    if sys.argv[1] == "sweep":
        checked, approved = sweep_pending()
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
