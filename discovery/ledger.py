#!/usr/bin/env python3
"""The URL ledger: a URL is researched once, and re-checked no sooner than its
kind's recheck window.

Without it every lane re-finds the same pages every cycle and the factory
spends its whole model budget re-researching what it published last hour. An
event page is worth another look in 3 days (dates move, things sell out); a
place in 30; a holiday destination in 60.

Entries are keyed by URL: `{first_seen, last_seen, kind, status}`. `status` is
the verdict `promote()` reached, so the Sources area can show what a lane's
finds actually turned into.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import factory_worker

LEDGER_FILE = Path(__file__).resolve().parent / "ledger.json"
RECHECK_DAYS = {"event": 3, "place": 30, "holiday": 60}
DEFAULT_RECHECK_DAYS = 3


def load():
    return factory_worker.load_json_store(LEDGER_FILE, {})


def _recheck_days(entry, kind_hint):
    kind = (entry or {}).get("kind") or kind_hint or ""
    return RECHECK_DAYS.get(kind, DEFAULT_RECHECK_DAYS)


def is_due(entry, kind_hint="", now=None):
    """True when this URL has never been researched, or its window has passed."""
    if not entry:
        return True
    stamp = entry.get("last_seen") or entry.get("first_seen") or ""
    try:
        last = datetime.fromisoformat(stamp)
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return now - last >= timedelta(days=_recheck_days(entry, kind_hint))


def filter_due(candidates, data=None, now=None):
    """The candidates worth researching this cycle, deduplicated by URL within
    the batch as well as against the ledger."""
    data = load() if data is None else data
    out, seen = [], set()
    for item in candidates:
        url = item.get("source_url") or ""
        if not url or url in seen:
            continue
        seen.add(url)
        if is_due(data.get(url), item.get("kind_hint", ""), now=now):
            out.append(item)
    return out


def record(url, kind="", status=""):
    """Stamp a verdict against a URL, once per researched candidate.

    `status="lane-failed"` means no model lane answered -- the URL was never
    actually judged, so it must not burn its recheck window. A URL that has
    been researched before keeps its old `last_seen` (it is due again on the
    old clock); one seen for the first time is not written at all, so the next
    cycle finds it again.
    """
    if not url:
        return
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with factory_worker.output_lock():
        data = load()
        entry = data.get(url)
        if entry is None:
            if status == "lane-failed":
                return
            entry = {"first_seen": now}
        if status != "lane-failed":
            entry["last_seen"] = now
        entry["kind"] = kind or entry.get("kind", "")
        entry["status"] = status or entry.get("status", "")
        data[url] = entry
        factory_worker.write_json_atomic(LEDGER_FILE, data)
