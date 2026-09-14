#!/usr/bin/env python3
"""Discovery: the lanes that find things for the factory to research.

Every lane is a module under `discovery/lanes/` exposing

    run(state) -> list[Candidate]

where `state` carries the lane's slice of `catalog/lanes.json`, the budget it
may spend this cycle, the factory state (for its rotation cursor) and an
`errors` list it appends `(key, message)` to. A lane never publishes and never
calls a model except where its spec says so -- it finds URLs and hands them to
`factory_worker.promote()`, which is the only thing allowed to decide what a
URL is.

`run_all()` is the single entry point: it enforces the per-cycle budget, the
weekly cadence of the slow lanes, the URL ledger, and produces the per-(lane,
key) rows the admin Sources area reads out of `factory_state.json["lanes"]`.
"""

import importlib
import time
from datetime import datetime, timedelta, timezone

import factory_worker
from discovery import common, ledger

# Order is priority: the cheap lanes that lean on the existing crawl/search
# infrastructure run first, so a budget-limited cycle spends it on them.
LANE_ORDER = ["listings", "feeds", "sitemaps", "search",
              "opendata_places", "wikidata", "ticketmaster", "holidays_seed"]
WEEKLY_DAYS = 7


def _is_due_weekly(name, factory_state, now):
    stamp = (factory_state.get("lane_last_run") or {}).get(name)
    if not stamp:
        return True
    try:
        last = datetime.fromisoformat(stamp)
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return now - last >= timedelta(days=WEEKLY_DAYS)


def run_all(budget, factory_state=None, only=None):
    """`(candidates, rows, cursors, ran, calls)` for one cycle.

    `candidates` are ledger-filtered and capped at `budget`; `rows` are the
    `{lane, key, found, new, errors, ms}` records for the Sources area;
    `cursors`, `ran` and `calls` are the rotation, cadence and quota state the
    caller persists into `factory_state.json`.
    """
    config = common.lanes_config()
    factory_state = factory_state or {}
    now = datetime.now(timezone.utc)
    seen_ledger = ledger.load()
    picked, rows, cursors, ran, calls = [], [], {}, [], {}

    for name in LANE_ORDER:
        if name not in config or (only and name not in only):
            continue                       # a lane with no config block does not exist
        lane_config = config[name] or {}
        if not lane_config.get("enabled"):
            rows.append(_row(name, "-", 0, 0, ["disabled"], 0))
            continue
        if lane_config.get("weekly") and not _is_due_weekly(name, factory_state, now):
            rows.append(_row(name, "-", 0, 0, ["not due this week"], 0))
            continue
        remaining = budget - len(picked)
        if remaining <= 0:
            rows.append(_row(name, "-", 0, 0, ["budget spent"], 0))
            continue

        state = {"config": lane_config, "budget": remaining, "errors": [],
                 "factory_state": factory_state, "cursor": None, "calls": None}
        started = time.time()
        try:
            found = importlib.import_module(f"discovery.lanes.{name}").run(state) or []
        except Exception as error:                       # a broken lane is not a broken cycle
            factory_worker.log(f"lane {name} failed: {error}")
            state["errors"].append(("-", f"{type(error).__name__}: {error}"))
            found = []
        elapsed_ms = int((time.time() - started) * 1000)

        fresh = ledger.filter_due(found, data=seen_ledger, now=now)[:remaining]
        picked.extend(fresh)
        rows.extend(_rows_for(name, found, fresh, state["errors"], elapsed_ms))
        if state["cursor"] is not None:
            cursors[name] = state["cursor"]
        if state["calls"] is not None:
            calls[name] = {"day": now.date().isoformat(), "count": state["calls"]}
        ran.append(name)
    return picked, rows, cursors, ran, calls


def _row(lane, key, found, new, errors, ms):
    return {"lane": lane, "key": key, "found": found, "new": new,
            "errors": errors, "ms": ms}


def _rows_for(lane, found, fresh, errors, elapsed_ms):
    """One row per key the lane touched, so a dead source is visible as its own
    line rather than averaged into the lane's total."""
    fresh_urls = {item.get("source_url") for item in fresh}
    by_key = {}
    for item in found:
        key = common.key_of(item)
        counts = by_key.setdefault(key, [0, 0])
        counts[0] += 1
        counts[1] += 1 if item.get("source_url") in fresh_urls else 0
    for key, _message in errors:
        by_key.setdefault(key, [0, 0])
    share_ms = int(elapsed_ms / max(len(by_key), 1))
    return [_row(lane, key, counts[0], counts[1],
                 [message for other, message in errors if other == key], share_ms)
            for key, counts in sorted(by_key.items())] or [
        _row(lane, "-", 0, 0, [message for _key, message in errors], elapsed_ms)]
