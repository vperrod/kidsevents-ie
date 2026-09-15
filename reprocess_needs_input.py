#!/usr/bin/env python3
"""Re-research every `needs-input` catalogue record through the four grounded
steps, same discipline as `rereseach_catalog.py` and the social sweep: no
supplied patch, no invented facts -- just a fresh `research_fetch` of the
record's own source_url, in case the page has grown, a rate limit has
cleared, or the enrichment search now succeeds where it didn't on the first
pass. A record that still doesn't clear the gate keeps its place in
staged/needs_input.json with an updated reason; one that does is published
and removed. Never touches `rejected` records -- those are a settled verdict.

    venv/bin/python3 reprocess_needs_input.py            # all needs-input records
    venv/bin/python3 reprocess_needs_input.py --limit 5  # smoke test
"""
import argparse
import concurrent.futures
import fcntl
import sys
import time
import urllib.parse
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import factory_worker

BASE = Path(__file__).resolve().parent
NEEDS_INPUT_FILE = BASE / "staged" / "needs_input.json"
LOCK_FILE = BASE / "staged" / "reprocess_needs_input.lock"
WORKERS = int(__import__("os").environ.get("REPROCESS_WORKERS", "3") or 3)


def candidate_for(record):
    """Same shape and same title/caption labelling trick as
    rereseach_catalog.candidate_for, so a re-researched exact-name match
    isn't rejected as "title is the raw caption"."""
    url = (record.get("links") or {}).get("source_url", "")
    host = urllib.parse.urlparse(url).netloc.lower().removeprefix("www.")
    title = str(record.get("title") or "").strip()
    description = str(record.get("description") or "").strip()
    caption = "\n".join(part for part in (
        f"Existing listing: {title}" if title else "", description) if part)
    return {"source_url": url, "caption": caption, "platform": host or "web",
            "found_via": "needs_input_reprocess"}


def process_one(record_id):
    with factory_worker.output_lock():
        records = factory_worker.load_json_store(NEEDS_INPUT_FILE, [])
        record = next((r for r in records if r.get("id") == record_id), None)
    if record is None or record.get("status") != "needs-input":
        return "vanished"
    cand = candidate_for(record)
    if not cand["source_url"]:
        return "no-source-url"
    new_record, reason, missing_field = factory_worker.promote(cand)
    with factory_worker.output_lock():
        records = factory_worker.load_json_store(NEEDS_INPUT_FILE, [])
        idx = next((i for i, r in enumerate(records) if r.get("id") == record_id), None)
        if idx is None:
            return "vanished"
        if new_record:
            published = factory_worker.publish_record(new_record)
            if published:
                del records[idx]
                factory_worker.write_json_atomic(NEEDS_INPUT_FILE, records)
                return "on-air"
            return "duplicate-id"
        records[idx]["reason"] = reason
        records[idx]["missing_field"] = missing_field
        records[idx]["status"] = "needs-input" if missing_field else "rejected"
        records[idx]["reviewed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        factory_worker.write_json_atomic(NEEDS_INPUT_FILE, records)
        return records[idx]["status"]


def process_safely(record_id):
    """process_one, never raising. Same fix as staging.py's sweep
    (2026-09-15): no second, outer timeout wall above research_fetch's and
    promote()'s own already-real timeouts -- that pattern doesn't fail fast
    on a blocked page, it burns the whole outer budget and then can't even
    free the worker, since Python threads can't be killed."""
    try:
        return process_one(record_id)
    except Exception as error:
        factory_worker.log(f"reprocess {record_id[:70]}: {error}")
        return "error"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    LOCK_FILE.parent.mkdir(exist_ok=True)
    lock_fh = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another reprocess run is already in progress -- skipping", file=sys.stderr)
        return 0

    try:
        records = factory_worker.load_json_store(NEEDS_INPUT_FILE, [])
        ids = [r["id"] for r in records if r.get("status") == "needs-input"]
        if args.limit:
            ids = ids[:args.limit]
        print(f"{len(ids)} needs-input records to re-research", file=sys.stderr, flush=True)

        outcomes = Counter()
        started = time.time()
        with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futures = {pool.submit(process_safely, rid): rid for rid in ids}
            for done, future in enumerate(concurrent.futures.as_completed(futures), 1):
                rid = futures[future]
                status = future.result()
                outcomes[status] += 1
                print(f"  [{done}/{len(ids)}] {status} — {rid[:70]}", file=sys.stderr, flush=True)

        elapsed = (time.time() - started) / 60
        print(f"\n{len(ids)} processed in {elapsed:.1f} min", file=sys.stderr)
        for status, count in outcomes.most_common():
            print(f"  {count:4d}  {status}", file=sys.stderr)
    finally:
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()


if __name__ == "__main__":
    main()
