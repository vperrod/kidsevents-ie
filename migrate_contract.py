#!/usr/bin/env python3
"""One-shot migration of the three catalogues onto the record contract.

Every published record predates `contract.py`: a flat dict with whatever keys
the scraper that produced it happened to emit. This maps each one onto the
contract through the legacy facet maps in `catalog/facets.json`, runs it
through `contract.validate` + `gate.qa`, and then splits it three ways:

* **on-air** — stays in its public file, now contract-shaped.
* **needs-input** — one nameable field is missing and a curator's note (or a
  re-research of its source URL) would fix it. Moves to
  `staged/needs_input.json` and OUT of the public file, with `missing_field`
  saying what is missing.
* **rejected** — nothing anybody types makes it publishable (past, not in
  Ireland, not for families). Dropped from the public file.

Idempotent: a record already at `schema_version: 1` is left exactly as it is,
so a second run is a no-op. Dry-run by default -- it prints the table and
touches nothing; `--apply` takes the backup and writes.

    venv/bin/python3 migrate_contract.py            # table only
    venv/bin/python3 migrate_contract.py --apply    # backup, then write
"""

import argparse
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import contract
import factory_worker
import gate

BASE = Path(__file__).resolve().parent
NEEDS_INPUT_FILE = BASE / "staged" / "needs_input.json"
BACKUP_DIR = Path.home() / "backups" / "kidsevents-ie"
STORES = (
    ("events_output.json", "event"),
    ("places_output.json", "place"),
    ("holidays_output.json", "holiday"),
)
BACKED_UP = ["events_output.json", "places_output.json", "holidays_output.json",
             "staged/social_candidates.json"]


def backup():
    """Tar the four stores before anything is rewritten. Returns the path."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive = BACKUP_DIR / f"pre-phase1-{stamp}.tgz"
    subprocess.run(["tar", "czf", str(archive), "-C", str(BASE), *BACKED_UP], check=True)
    return archive


# ---------------------------------------------------------------------------
# Legacy -> contract
# ---------------------------------------------------------------------------

def _coord(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _legacy_taxonomy(old):
    """The old flat fields folded onto the facet vocabulary. `gate_meta` does
    the folding and drops anything it does not recognise.

    The legacy `price_range: "check"` is a placeholder the old prompt emitted
    when it found no price, not a price -- carrying it into `price_detail`
    would dress an absent fact up as a stated one.
    """
    price = "" if str(old.get("price_range", "")).strip().lower() == "check" else old.get("price_range", "")
    return {
        "age_bands": [old.get("age_group", "")],
        "price_band": old.get("cost") or price or "",
        "price_detail": old.get("cost_detail") or price or old.get("cost") or "",
        "setting": "",
        "activity_types": [old.get("category", "")],
        "accessibility": [old.get("suitable_for", "")],
        "rainy_ok": None,
    }


def legacy_to_contract(old, kind):
    """One published record, mapped onto the contract. Nothing is invented:
    a field the old record does not carry stays empty and the gate decides
    what that costs."""
    record = contract.EMPTY_RECORD(kind)
    record["title"] = str(old.get("title") or "").strip()[:contract.MAX_TITLE]
    record["summary"] = " ".join(str(old.get("description") or "").split())[:contract.MAX_SUMMARY]
    record["description"] = str(old.get("description") or "").strip()
    record["family_relevant"] = old.get("family_relevant") is not False

    city, county = factory_worker.normalize_location(
        old.get("city") or old.get("location") or "", old.get("county", ""))
    location = record["location"]
    location["name"] = str(old.get("venue_name") or old.get("location") or "")[:200]
    location["address"] = str(old.get("venue_address") or "")[:300]
    location["city"] = city
    location["county"] = county
    location["country"] = factory_worker.normalize_country(old.get("country"))
    location["lat"] = _coord(old.get("latitude"))
    location["lon"] = _coord(old.get("longitude"))

    source_url = old.get("source_url") or old.get("url") or ""
    record["links"].update({
        "source_url": source_url,
        "official_url": old.get("website") or (old.get("url") if old.get("source_url") else "") or "",
        "booking_url": old.get("booking_url") or "",
        "instagram_url": source_url if "instagram.com" in source_url else "",
        "tiktok_url": source_url if "tiktok.com" in source_url else "",
    })
    if old.get("image_url"):
        record["media"]["hero"] = {
            "url": old["image_url"], "file": "", "credit": "", "licence": "",
            "source": source_url, "gate": "", "alt": old.get("image_alt", ""),
        }

    taxonomy, dropped = gate.gate_meta(_legacy_taxonomy(old))
    record["taxonomy"] = taxonomy

    if kind == "event":
        record["event"].update({
            "start_date": str(old.get("start_date") or "")[:10],
            "end_date": str(old.get("end_date") or old.get("start_date") or "")[:10],
            "times": [old["time"]] if old.get("time") else [],
            "booking_required": old.get("booking_required") or "",
            "organizer": old.get("venue_name") or "",
        })

    urls = [u for u in ([source_url] + list(old.get("all_urls") or [])) if u]
    record["provenance"].update({
        "sources": [{"url": url, "fetched_at": ""} for url in dict.fromkeys(urls)],
        "facts": [],
        "last_checked": "",
        "produced_by": ["migrate_contract"],
    })
    return contract.derive(record), dropped


# ---------------------------------------------------------------------------
# The migration
# ---------------------------------------------------------------------------

def classify_record(old, kind, on_air_titles):
    """`(record, verdict, reason, missing_field, dropped)` for one legacy record."""
    if old.get("schema_version") == 1:
        status = old.get("status", "on-air")
        return old, status, old.get("reason", ""), old.get("missing_field", ""), []
    record, dropped = legacy_to_contract(old, kind)
    problems = contract.validate(record)
    if problems:
        record["status"], record["reason"] = "rejected", "; ".join(problems[:3])
        return record, "rejected", record["reason"], "", dropped
    # The legacy record carries no page text, so `date_evidence` cannot be
    # checked against a source here -- the gate skips that check when there is
    # no source text, and still requires the quote itself.
    ok, reason, missing_field = gate.qa(record, "", on_air_titles)
    if ok:
        record["status"] = "on-air"
        return record, "on-air", "", "", dropped
    record["status"] = "needs-input" if missing_field else "rejected"
    record["reason"] = reason
    if missing_field:
        record["missing_field"] = missing_field
    return record, record["status"], reason, missing_field, dropped


def migrate_store(name, kind, apply_changes):
    """Migrate one catalogue. Returns (kept, needs_input, counts, reasons, dropped)."""
    path = BASE / name
    old_records = factory_worker.load_json_store(path, [])
    kept, needs_input, dropped = [], [], []
    counts, reasons = Counter(), Counter()
    titles = []
    for old in old_records:
        record, verdict, reason, _missing, facet_drops = classify_record(old, kind, titles)
        dropped += facet_drops
        counts[verdict] += 1
        if verdict == "on-air":
            kept.append(record)
            titles.append(contract.legacy_view(record).get("title", ""))
        else:
            # Tally the class of reason, not the instance: "description is 23
            # words" and "...is 36 words" are one finding, not two.
            reasons[re.sub(r"\d+", "N", reason) or "(no reason given)"] += 1
            if verdict == "needs-input":
                needs_input.append(record)
    if apply_changes:
        factory_worker.write_json_atomic(path, kept)
    return kept, needs_input, counts, reasons, dropped


def run(apply_changes):
    archive = backup() if apply_changes else None
    rows, all_needs_input, all_dropped = [], [], []
    for name, kind in STORES:
        with factory_worker.output_lock():
            kept, needs_input, counts, reasons, dropped = migrate_store(name, kind, apply_changes)
        all_needs_input.extend(needs_input)
        all_dropped += dropped
        rows.append((name, sum(counts.values()), counts, reasons))
    if apply_changes:
        gate.record_drops(all_dropped)
        with factory_worker.output_lock():
            NEEDS_INPUT_FILE.parent.mkdir(exist_ok=True)
            existing = factory_worker.load_json_store(NEEDS_INPUT_FILE, [])
            known = {r.get("id") for r in existing}
            factory_worker.write_json_atomic(
                NEEDS_INPUT_FILE,
                existing + [r for r in all_needs_input if r.get("id") not in known])
    return archive, rows, all_needs_input


def print_table(archive, rows, apply_changes):
    print(f"{'file':<24}{'total':>7}{'on-air':>9}{'needs-input':>13}{'rejected':>10}")
    for name, total, counts, _reasons in rows:
        print(f"{name:<24}{total:>7}{counts['on-air']:>9}"
              f"{counts['needs-input']:>13}{counts['rejected']:>10}")
    for name, _total, _counts, reasons in rows:
        if not reasons:
            continue
        print(f"\ntop reasons — {name}")
        for reason, count in reasons.most_common(5):
            print(f"  {count:>4}  {reason[:96]}")
    print("\nbackup: " + (str(archive) if archive else "not taken (dry run)"))
    if not apply_changes:
        print("dry run — nothing was written. Re-run with --apply to migrate.")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true",
                        help="take the backup and write the migrated stores")
    args = parser.parse_args()
    archive, rows, needs_input = run(args.apply)
    print_table(archive, rows, args.apply)
    if args.apply:
        print(f"needs-input records written to {NEEDS_INPUT_FILE}: {len(needs_input)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
