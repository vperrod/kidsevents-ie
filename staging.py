#!/usr/bin/env python3
"""Staging desk for social candidates collected on the mini PC.

Reading Instagram/TikTok saved collections needs an already-authenticated
browser session, which only exists on the mini PC's OpenCLI/agent-reach-chrome
daemon. That collector pipes its findings into this script over SSH:

    ssh azureuser@claude-dev-vperrod.westeurope.cloudapp.azure.com \\
        "cd /home/azureuser/kidsevents-ie && venv/bin/python3 staging.py append"

Nothing here publishes: candidates land in staged/social_candidates.json with
status needs_review and are promoted one by one from the admin Social view.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
STAGED_FILE = BASE / "staged" / "social_candidates.json"

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


def write_staged(candidates):
    """Append new candidates, deduplicated by source_url. Returns the count added."""
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


def main():
    if len(sys.argv) < 2 or sys.argv[1] != "append":
        print("usage: staging.py append  (candidate JSON array on stdin)", file=sys.stderr)
        return 2
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
