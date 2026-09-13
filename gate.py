#!/usr/bin/env python3
"""The QA gate: a facet vocabulary that is enforced, and one on-air decision.

Two jobs, both of them "say no with a reason":

* `gate_meta()` folds a model's taxonomy onto the vocabulary in
  `catalog/facets.json`. Anything it cannot recognise is **dropped and
  counted** — never quietly replaced with a default, which is how a made-up
  category ends up looking like a curated one. The counts land in
  `factory_state.json["facet_drops"]` so the Production view shows which
  fields the models keep inventing.
* `qa()` answers whether one record may go on air, and if not, whether a
  human note could fix it. `(ok, reason, missing_field)`: a truthy
  `missing_field` means needs-input (one nameable thing is missing and a hint
  would fix it); an empty one means rejected (nothing a curator can type will
  make this publishable). That is the distinction `staging.py` turns into the
  `needs_input` vs `rejected` status.

Spec: `tasks/specs/phase1-contract-taxonomy.md` §2/§3, plan §4.2.
"""

import re
from datetime import date, datetime, timezone
from urllib.parse import urlparse

import contract

FACETS = contract.FACETS

# field -> (allowed values, legacy alias map, is it a list?)
_META_FIELDS = {
    "age_bands": (FACETS["age_bands"]["values"], FACETS["age_bands"]["legacy"], True),
    "price_band": (FACETS["price_band"]["values"], FACETS["price_band"]["legacy"], False),
    "setting": (FACETS["setting"]["values"], {}, False),
    "activity_types": (FACETS["activity_types"]["values"], FACETS["activity_types"]["legacy"], True),
    "accessibility": (FACETS["accessibility"]["values"], FACETS["accessibility"]["legacy"], True),
}
_HOLIDAY_FIELDS = {
    "destination_type": (FACETS["holiday"]["destination_type"]["values"], {}, False),
    "holiday_types": (FACETS["holiday"]["holiday_types"]["values"], {}, True),
    "best_seasons": (FACETS["holiday"]["best_seasons"]["values"], {}, True),
    "school_breaks": (list(FACETS["holiday"]["school_breaks"]), {}, True),
    "flight_time_from_dublin": (FACETS["holiday"]["flight_time_from_dublin"]["values"], {}, False),
    "budget_band": (FACETS["holiday"]["budget_band"]["values"], {}, False),
}

# Ireland's bounding box — a record that says "Ireland" with coordinates in
# Spain is wrong about one of the two, and we cannot tell which.
_IE_BOX = (51.3, 55.5, -10.8, -5.3)

_EURO_RE = re.compile(r"(?:€|eur\s*)\s*([0-9]+(?:[.,][0-9]{1,2})?)", re.I)
_MONTHS = ("january", "february", "march", "april", "may", "june", "july",
           "august", "september", "october", "november", "december")


def _map_value(value, allowed, aliases):
    """One value folded onto the vocabulary, or None if it is not in it."""
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw in allowed:
        return raw
    mapped = aliases.get(raw.lower())
    if isinstance(mapped, list):
        return mapped
    if mapped in allowed:
        return mapped
    for candidate in allowed:
        if candidate.lower() == raw.lower():
            return candidate
    return None


def _clean_field(field, value, allowed, aliases, is_list, dropped):
    if is_list:
        raw_values = value if isinstance(value, list) else ([value] if value else [])
        kept = []
        for one in raw_values:
            mapped = _map_value(one, allowed, aliases)
            if mapped is None:
                dropped.append(f"{field}={one}")
            elif isinstance(mapped, list):
                kept.extend(m for m in mapped if m not in kept)
            elif mapped not in kept:
                kept.append(mapped)
        return kept
    mapped = _map_value(value, allowed, aliases)
    if isinstance(mapped, list):
        mapped = mapped[0] if mapped else None
    if mapped is None:
        if str(value or "").strip():
            dropped.append(f"{field}={value}")
        return ""
    return mapped


def price_band_from_detail(detail):
    """Band a free-text price ("€5 per child, under 2s free") when it names an
    amount. No amount, no band — a guess here is worse than "unknown"."""
    text = str(detail or "")
    amounts = [float(m.replace(",", ".")) for m in _EURO_RE.findall(text)]
    if not amounts:
        if re.search(r"\bfree\b|\bno charge\b", text, re.I):
            return "free"
        return ""
    top = max(amounts)
    if top == 0:
        return "free"
    if top < 10:
        return "under-10"
    if top <= 25:
        return "10-25"
    return "25-plus"


def gate_meta(taxonomy):
    """`(clean_taxonomy, dropped)` — the taxonomy with every value folded onto
    the vocabulary and everything unrecognised removed and listed."""
    dropped = []
    clean = {}
    for field, (allowed, aliases, is_list) in _META_FIELDS.items():
        clean[field] = _clean_field(field, (taxonomy or {}).get(field), allowed, aliases, is_list, dropped)
    clean["price_detail"] = str((taxonomy or {}).get("price_detail") or "").strip()
    if clean["price_band"] in ("", "unknown"):
        derived = price_band_from_detail(clean["price_detail"])
        if derived:
            clean["price_band"] = derived
    rainy = (taxonomy or {}).get("rainy_ok")
    clean["rainy_ok"] = rainy if isinstance(rainy, bool) else None
    return clean, dropped


def gate_holiday(holiday):
    """The same fold for the holiday-only facets. `best_months` and `includes`
    are free text and pass through."""
    dropped = []
    clean = {}
    for field, (allowed, aliases, is_list) in _HOLIDAY_FIELDS.items():
        clean[field] = _clean_field(field, (holiday or {}).get(field), allowed, aliases, is_list, dropped)
    for passthrough in ("best_months", "includes"):
        value = (holiday or {}).get(passthrough) or []
        clean[passthrough] = [str(x) for x in value] if isinstance(value, list) else []
    for flag in ("direct_flight", "with_baby_toddler"):
        value = (holiday or {}).get(flag)
        clean[flag] = value if isinstance(value, bool) else None
    return clean, dropped


def record_drops(dropped):
    """Accumulate facet drops into `factory_state.json["facet_drops"]` (the
    Production view reads them). Imported lazily: factory_worker imports this
    module, so a module-level import would be circular."""
    if not dropped:
        return
    import factory_worker

    with factory_worker.output_lock():
        state = factory_worker.load_state()
        counts = state.setdefault("facet_drops", {})
        for item in dropped:
            field = item.split("=", 1)[0]
            counts[field] = counts.get(field, 0) + 1
        factory_worker.save_state(state)


def facet_gloss(kind="event"):
    """The vocabulary rendered for a prompt: the exact allowed values per
    field, so the write step picks from them instead of inventing labels."""
    lines = [f"{field}: {allowed}" + (" (choose any that apply)" if is_list else " (choose one)")
             for field, (allowed, _aliases, is_list) in _META_FIELDS.items()]
    lines.append('price_detail: free text exactly as the source states it, e.g. "€5 per child, under 2s free"')
    lines.append("rainy_ok: true if it works in the rain, false if it does not, null if the source does not say")
    if kind == "holiday":
        lines += [f"{field}: {allowed}" + (" (choose any that apply)" if is_list else " (choose one)")
                  for field, (allowed, _aliases, is_list) in _HOLIDAY_FIELDS.items()]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The on-air decision
# ---------------------------------------------------------------------------

def _fold(text):
    return re.sub(r"[^a-z0-9]+", "", str(text or "").lower())


def _words(text):
    return len(str(text or "").split())


def _domains(sources):
    return {urlparse(s.get("url", "")).netloc.lower().removeprefix("www.")
            for s in sources if s.get("url")}


def _needs(field, sentence):
    return False, sentence, field


def _reject(sentence):
    return False, sentence, ""


def qa(record, sources_text="", on_air_titles=()):
    """May this record go on air? `(ok, reason, missing_field)`.

    `sources_text` is the page/caption text the record was built from — the
    only thing `event.date_evidence` is allowed to be quoting. `on_air_titles`
    are the titles already published, for the duplicate fold.

    A truthy `missing_field` means needs-input; an empty one means rejected.
    """
    kind = record.get("kind")
    if kind not in contract.KINDS:
        return _reject(f"kind is {kind!r}, not one of {list(contract.KINDS)}")
    location = record.get("location") or {}
    taxonomy = record.get("taxonomy") or {}
    provenance = record.get("provenance") or {}
    links = record.get("links") or {}
    title = (record.get("title") or "").strip()

    if record.get("family_relevant") is False:
        return _reject("not family-relevant")
    if not links.get("source_url") and not provenance.get("sources"):
        return _reject("no source page and no caption to ground this in (no-content)")
    if not links.get("source_url") or not provenance.get("sources"):
        return _reject("the source it came from was not recorded")
    if len(title) > contract.MAX_TITLE:
        return _reject(f"title is {len(title)} characters (max {contract.MAX_TITLE})")
    if _title_is_caption(title, sources_text):
        return _reject("title is the raw caption, not a synthesised title")
    over = _already_over(record) if kind == "event" else ""
    if over:
        return _reject(over)
    if kind in ("event", "place") and not location.get("ireland"):
        return _reject(f"country is {location.get('country') or 'unknown'}, not IE")
    if _outside_ireland(location):
        return _reject(f"says Ireland but the coordinates are at {location.get('lat')},{location.get('lon')}")
    if _is_nav_list(record.get("description")):
        return _reject("description is a list of links or tags, not prose")
    folded = _fold(title)
    if folded and any(_fold(other) == folded for other in on_air_titles):
        return _reject("duplicate of an item already on air")

    if not title:
        return _needs("title", "no title could be read from the source")
    if not (record.get("summary") or "").strip():
        return _needs("summary", "no one-line summary")
    minimum = contract.MIN_DESCRIPTION_WORDS[kind]
    if _words(record.get("description")) < minimum:
        return _needs("description",
                      f"description is {_words(record.get('description'))} words, under the {minimum} this kind needs")
    if location.get("country") == "IE" and not location.get("county"):
        return _needs("county", "no Irish county could be identified")
    if not provenance.get("facts"):
        return _needs("facts", "nothing in the source is quoted as a fact, so nothing is grounded")

    if kind == "event":
        return _qa_event(record, sources_text)
    if kind == "place":
        return _qa_place(record, location, taxonomy)
    return _qa_holiday(record, location, provenance)


def _already_over(record):
    """An event whose last day has passed is rejected before anything else is
    weighed: no note a curator types brings it back."""
    event = record.get("event") or {}
    start = (event.get("start_date") or "")[:10]
    if not start:
        return ""
    try:
        ends = date.fromisoformat((event.get("end_date") or start)[:10])
    except ValueError:
        return ""
    if ends < datetime.now(timezone.utc).date():
        return f"the event is already over (ended {ends.isoformat()})"
    return ""


def _qa_event(record, sources_text):
    event = record.get("event") or {}
    taxonomy = record.get("taxonomy") or {}
    start = (event.get("start_date") or "")[:10]
    if not start:
        return _needs("start_date", "no start date")
    try:
        date.fromisoformat(start)
        date.fromisoformat((event.get("end_date") or start)[:10])
    except ValueError:
        return _needs("start_date", f"date {start!r} is not a usable YYYY-MM-DD date")
    evidence = (event.get("date_evidence") or "").strip()
    if not evidence:
        return _needs("date_evidence", "the date is not quoted from the source")
    if not _mentions_a_date(evidence):
        return _needs("date_evidence", "the quoted date evidence names no day or month")
    if sources_text and _fold(evidence) not in _fold(sources_text):
        return _needs("date_evidence", "the quoted date evidence is not in the source text")
    if not taxonomy.get("price_band"):
        return _needs("price_band", "no price band")
    if not taxonomy.get("age_bands"):
        return _needs("age_bands", "no age band")
    return True, "", ""


def _qa_place(record, location, taxonomy):
    if not taxonomy.get("activity_types"):
        return _needs("activity_types", "no activity type — a place with no activity type is unsearchable")
    has_coords = location.get("lat") not in (None, "") and location.get("lon") not in (None, "")
    if not has_coords and not location.get("address"):
        return _needs("address", "no address and no coordinates")
    return True, "", ""


def _qa_holiday(record, location, provenance):
    holiday = record.get("holiday") or {}
    if not location.get("country"):
        return _needs("country", "no destination country")
    if len(_domains(provenance.get("sources") or [])) < 2:
        return _needs("sources", "a holiday needs two independent sources")
    if not holiday.get("holiday_types"):
        return _needs("holiday_types", "no holiday type")
    if not holiday.get("best_seasons"):
        return _needs("best_seasons", "no season")
    return True, "", ""


def _title_is_caption(title, caption):
    """True when the "title" is just the source caption, or the front of it.

    The 2026-09-12 auto-approve published an adult vintage pop-up with the raw
    truncated caption as its title — `caption[:120]` is a literal prefix of
    the caption, which is exactly what this catches.
    """
    folded_title, folded_caption = _fold(title), _fold(caption)
    if not folded_title or not folded_caption:
        return False
    return folded_title == folded_caption or (len(folded_title) >= 20 and folded_caption.startswith(folded_title))


def _outside_ireland(location):
    if not location.get("ireland"):
        return False
    try:
        lat, lon = float(location.get("lat")), float(location.get("lon"))
    except (TypeError, ValueError):
        return False
    low_lat, high_lat, low_lon, high_lon = _IE_BOX
    return not (low_lat <= lat <= high_lat and low_lon <= lon <= high_lon)


def _is_nav_list(description):
    """A crawled nav column reads as many short fragments and no sentence."""
    text = str(description or "")
    return _words(text) > 40 and not re.search(r"[.!?]", text)


def _mentions_a_date(evidence):
    lowered = evidence.lower()
    return bool(re.search(r"\b\d{1,2}\b", lowered) or any(m in lowered for m in _MONTHS))
