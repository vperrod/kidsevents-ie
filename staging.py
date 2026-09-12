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
the caption, published only if it yields a real date; never invented). A
candidate that doesn't clear that gate just stays needs_review, same as
before -- auto-approve is a tighter gate applied automatically, not a lower
one. Set AUTO_APPROVE=off in .env to go back to fully manual review.
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


def try_auto_approve(candidate):
    """Run one needs_review candidate through the same classification+publish
    gate the admin Approve button uses -- a dated event, or an evergreen
    place/activity (playground, farm, museum) that's there all year, same
    schema and tab as the existing curated Holidays section. Mutates
    candidate in place (status/reviewed_at) on success; leaves it untouched
    (still needs_review) on failure -- neither classification landing is not
    an error, it just means a human still has to look at it."""
    kind, record = factory_worker.promote_candidate(candidate)
    if not record:
        return False
    (factory_worker.publish_event if kind == "event" else factory_worker.publish_place)(record)
    candidate["status"] = "approved"
    candidate["reviewed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    candidate["review_note"] = f"Auto-approved: classified as a{'n' if kind == 'event' else ''} {kind}."
    return True


def write_staged(candidates):
    """Append new candidates, deduplicated by source_url. Returns the count added
    (auto-approved candidates count as added -- they still land in the ledger,
    just already published)."""
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
            try_auto_approve(candidate)
    save_staged(existing + additions)
    return len(additions)


def sweep_pending():
    """Auto-approve every already-staged needs_review candidate (clears a
    backlog collected before AUTO_APPROVE existed, or after it was off).
    Saves after every candidate -- each one costs a real LLM call, a backlog
    can be hundreds deep, and losing all progress to one interruption on a
    save-only-at-the-end version is exactly what happened the first time
    this ran. Returns (checked, approved)."""
    candidates = load_staged()
    pending = [c for c in candidates if c.get("status") == "needs_review"]
    approved = 0
    for i, candidate in enumerate(pending, 1):
        if try_auto_approve(candidate):
            approved += 1
        save_staged(candidates)
        print(f"  [{i}/{len(pending)}] {'approved' if candidate['status'] == 'approved' else 'still needs review'} — {candidate.get('source_url', '')[:70]}",
              file=sys.stderr, flush=True)
    return len(pending), approved


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
