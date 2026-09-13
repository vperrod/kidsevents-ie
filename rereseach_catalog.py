#!/usr/bin/env python3
"""Re-research every published record through the four grounded steps.

Phase 1's migration refused to proceed: 0 of 108 legacy records cleared the
gate, because they were scraped (median description 24 words for events, 52
for places) and not one of them carries a quoted `provenance.facts`. Relaxing
the bands would re-admit exactly that boilerplate, so instead every record is
run back through `factory_worker.promote()` from the `source_url` it already
carries -- the same research fetch and the same four model steps a fresh
social candidate gets.

A record that clears the gate replaces its old slot in place (or moves to the
catalogue of the kind `classify` actually says it is). A record that does not
is left exactly as it was: `migrate_contract.py --apply` is the one mechanism
that splits the leftovers into `staged/needs_input.json` and dropped, and it
takes the backup. Run it after this script.

Resumable: every outcome is checkpointed to `recatalog_progress.json`, and a
restart skips the records already in it.

    venv/bin/python3 rereseach_catalog.py            # all 108
    venv/bin/python3 rereseach_catalog.py --limit 2  # smoke test
"""

import argparse
import concurrent.futures
import sys
import threading
import time
import urllib.parse
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import contract
import factory_worker
import gate

BASE = Path(__file__).resolve().parent
LEDGER = BASE / "recatalog_progress.json"
STORES = (("events_output.json", "event"), ("places_output.json", "place"))
# The local lane is the bottleneck and WanderTold shares it; llm.py already
# yields at LOCAL_BUSY_AT, so this is the only throttle in the script.
WORKERS = 4
CHECKPOINT_EVERY = 10

_ledger_lock = threading.Lock()
_own = threading.local()
_store_titles = factory_worker.on_air_titles


def _on_air_titles_excluding_own(kind):
    """`factory_worker.on_air_titles`, minus the title of the record currently
    being re-researched.

    The record is still in its catalogue while its replacement is judged, so
    the stock function would make every record a duplicate of itself and
    `gate.qa` would reject the whole catalogue. Every other title still counts.
    """
    own = gate._fold(getattr(_own, "title", ""))
    return [t for t in _store_titles(kind) if not own or gate._fold(t) != own]


factory_worker.on_air_titles = _on_air_titles_excluding_own


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------

def load_ledger():
    return factory_worker.load_json_store(LEDGER, {"records": {}})


def save_ledger(ledger):
    with _ledger_lock:
        ledger["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        factory_worker.write_json_atomic(LEDGER, ledger)


# ---------------------------------------------------------------------------
# Legacy record -> candidate
# ---------------------------------------------------------------------------

def _url(old):
    """Events carry `url`, places carry `source_url`; both mean the page."""
    return old.get("source_url") or old.get("url") or ""


def candidate_for(old, key):
    """The candidate shape `promote()` takes, built from what the old record
    already knows. The old title and description go in as the caption -- a
    pointer for the fetch and a fallback when the page is unreadable, never
    trusted as fact; the four steps only quote what the source says.

    The caption is labelled rather than starting with the bare title because
    `gate._title_is_caption` rejects a record whose title is a prefix of the
    caption -- a re-researched "Dublin Zoo" would otherwise be thrown out for
    being named what it is called.
    """
    url = _url(old)
    host = urllib.parse.urlparse(url).netloc.lower().removeprefix("www.")
    title = str(old.get("title") or "").strip()
    description = str(old.get("description") or "").strip()
    caption = "\n".join(part for part in (
        f"Existing listing: {title}" if title else "", description) if part)
    return {
        "source_url": url,
        "caption": caption,
        "platform": host or "web",
        "found_via": f"recatalog:{key}",
    }


def identity(old):
    """What makes this record itself, for the ledger.

    Not its position: `factory_worker.publish_record` re-sorts the whole
    catalogue every time the hourly cycle publishes something, so an index
    taken at the start of a run points at a different record by the end of it.
    """
    return f"{_url(old)}|{old.get('title') or ''}"


def _is_old(record, old):
    """The legacy record in a freshly-read store, matched without an id (no
    legacy record has one). `schema_version` keeps it off contract records the
    factory published while this ran."""
    return (record.get("schema_version") != 1
            and record.get("title") == old.get("title")
            and _url(record) == _url(old))


# ---------------------------------------------------------------------------
# Writing the new record back
# ---------------------------------------------------------------------------

def _has_id(records, record_id, skip=None):
    return any(other.get("id") == record_id
               for index, other in enumerate(records) if index != skip)


def apply_success(name, old, record):
    """Swap the new record into the catalogue, under the lock the factory and
    the admin routes write with (they hold it for one read-modify-write, so an
    hourly cycle running alongside this can neither clobber nor be clobbered).

    Returns the outcome: `on-air`, `vanished` (the factory's prune took the old
    record while this item was in flight) or `duplicate-id`.
    """
    source_path = BASE / name
    target_path = factory_worker.STORE_FOR_KIND[record["kind"]]
    with factory_worker.output_lock():
        records = factory_worker.load_json_store(source_path, [])
        index = next((i for i, other in enumerate(records) if _is_old(other, old)), None)
        if index is None:
            return "vanished"
        if source_path == target_path:
            if _has_id(records, record["id"], skip=index):
                return "duplicate-id"
            records[index] = record
            factory_worker.write_json_atomic(source_path, records)
            return "on-air"
        # `classify` says this record is a different kind than the catalogue it
        # was published in, so it moves.
        others = factory_worker.load_json_store(target_path, [])
        if _has_id(others, record["id"]):
            return "duplicate-id"
        records.pop(index)
        factory_worker.write_json_atomic(source_path, records)
        others.append(record)
        others.sort(key=lambda one: contract.legacy_view(one).get("start_date") or "")
        factory_worker.write_json_atomic(target_path, others)
        return "on-air"


# ---------------------------------------------------------------------------
# The pass
# ---------------------------------------------------------------------------

def process(item):
    """One record: fetch, four steps, gate, write. Never raises -- a lane or a
    fetch blowing up is one item's outcome, not the run's."""
    key, name, old = item
    _own.title = old.get("title") or ""
    started = time.time()
    entry = {"url": _url(old), "title": _own.title, "key": key,
             "identity": identity(old)}
    try:
        record, reason, missing_field = factory_worker.promote(candidate_for(old, key))
        if record:
            entry["outcome"] = apply_success(name, old, record)
            entry["reason"] = "" if entry["outcome"] == "on-air" else entry["outcome"]
            entry["new_id"] = record["id"]
            entry["kind"] = record["kind"]
        elif reason.startswith("no model lane answered"):
            # Every lane was rate-limited or down for this item. That says
            # nothing about the record, so it is retried on the next run
            # instead of being written off as rejected.
            entry["outcome"], entry["reason"] = "lane-failed", reason
        else:
            entry["outcome"] = "needs-input" if missing_field else "rejected"
            entry["reason"] = reason
            entry["missing_field"] = missing_field
    except Exception as error:  # noqa: BLE001 -- one item must not end the pass
        entry["outcome"] = "error"
        entry["reason"] = f"{type(error).__name__}: {error}"[:300]
    entry["secs"] = round(time.time() - started, 1)
    entry["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return key, entry


# An outcome that says something about the record is final; one that says the
# lanes were down is retried on the next run.
RETRYABLE = ("lane-failed", "error")


def pending(ledger, limit):
    """Every legacy record without a final outcome in the ledger."""
    items = []
    for name, kind in STORES:
        for index, old in enumerate(factory_worker.load_json_store(BASE / name, [])):
            done = ledger["records"].get(identity(old))
            if old.get("schema_version") == 1 or (done and done["outcome"] not in RETRYABLE):
                continue
            items.append((f"{kind}#{index}", name, old))
    return items[:limit] if limit else items


def run(limit):
    ledger = load_ledger()
    ledger.setdefault("records", {})
    ledger.setdefault("started_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    items = pending(ledger, limit)
    print(f"{len(items)} records to re-research ({len(ledger['records'])} already done)",
          file=sys.stderr, flush=True)
    started = time.time()
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(process, item) for item in items]
        for future in concurrent.futures.as_completed(futures):
            key, entry = future.result()
            ledger["records"][entry["identity"]] = entry
            done += 1
            print(f"  [{done}/{len(items)}] {entry['outcome']:<12} {key:<10} "
                  f"{entry['secs']:>5}s  {entry.get('reason', '')[:70]}",
                  file=sys.stderr, flush=True)
            if done % CHECKPOINT_EVERY == 0:
                save_ledger(ledger)
    save_ledger(ledger)
    factory_worker.record_llm_stats()
    return ledger, time.time() - started


def summarise(ledger, elapsed):
    outcomes = Counter(entry["outcome"] for entry in ledger["records"].values())
    print(f"\n{len(ledger['records'])} records in the ledger, {elapsed / 60:.1f} min this run")
    for outcome, count in outcomes.most_common():
        print(f"  {count:>4}  {outcome}")
    reasons = Counter(entry.get("reason", "")[:90]
                      for entry in ledger["records"].values()
                      if entry["outcome"] not in ("on-air",) and entry.get("reason"))
    if reasons:
        print("\ntop reasons")
        for reason, count in reasons.most_common(5):
            print(f"  {count:>4}  {reason}")
    print(f"\nledger: {LEDGER}")
    print("next: venv/bin/python3 migrate_contract.py --apply")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limit", type=int, default=0,
                        help="stop after N records (smoke test); 0 means all")
    args = parser.parse_args()
    ledger, elapsed = run(args.limit)
    summarise(ledger, elapsed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
