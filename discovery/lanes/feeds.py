#!/usr/bin/env python3
"""Lane 2 -- RSS/Atom and ICS feeds.

Feeds are the cheapest lane there is: no crawl, no search, no model call to
find anything. Each entry becomes a candidate whose `caption` is the feed
entry itself, which `research_fetch` then merges with a live fetch of the
entry's own page -- so an ICS `DTSTART` is quoted evidence that is provably in
the text the gate checks, while the description still comes off the real page.

Parsing is stdlib on purpose: `xml.etree` for RSS and Atom, and a hand-rolled
VEVENT reader for ICS, which is a line format (unfold continuation lines, split
on the first colon after the parameters) and not worth a dependency.

The four council calendar exports plan §11.2 named were probed on 2026-09-13:
only Monaghan's serves `text/calendar`. Cork County, South Dublin and Kildare
answer their `?ical=1` with the ordinary HTML page, so they are not in the
config -- add them here when they start publishing one.
"""

import re
import xml.etree.ElementTree as ET

from discovery import common

_ICS_UNFOLD = re.compile(r"\r?\n[ \t]")
_ITEM_TAGS = ("item", "{http://www.w3.org/2005/Atom}entry")


def parse_ics(text, limit=50):
    """VEVENT blocks as `{summary, description, location, url, dtstart, dtend}`."""
    events = []
    for block in _ICS_UNFOLD.sub("", text).split("BEGIN:VEVENT")[1:]:
        block = block.split("END:VEVENT")[0]
        fields = {}
        for line in block.splitlines():
            if ":" not in line:
                continue
            name, value = line.split(":", 1)
            fields.setdefault(name.split(";")[0].strip().upper(), value.strip())
        if not fields.get("SUMMARY"):
            continue
        events.append({
            "summary": _ics_unescape(fields.get("SUMMARY", "")),
            "description": _ics_unescape(fields.get("DESCRIPTION", "")),
            "location": _ics_unescape(fields.get("LOCATION", "")),
            "url": fields.get("URL", ""),
            "dtstart": fields.get("DTSTART", ""),
            "dtend": fields.get("DTEND", ""),
        })
        if len(events) >= limit:
            break
    return events


def _ics_unescape(value):
    return (value.replace("\\n", "\n").replace("\\,", ",")
            .replace("\\;", ";").replace("\\\\", "\\"))


def ics_date(value):
    """`20261026T100000` or `20261026` -> `2026-10-26`; "" when unreadable."""
    digits = re.match(r"(\d{4})(\d{2})(\d{2})", str(value or "").strip())
    return "-".join(digits.groups()) if digits else ""


def parse_rss(text, limit=50):
    """RSS `<item>` and Atom `<entry>` as `{title, link, description}`."""
    root = ET.fromstring(text.strip())
    out = []
    for tag in _ITEM_TAGS:
        for node in root.iter(tag):
            title = _node_text(node, ("title", "{http://www.w3.org/2005/Atom}title"))
            link = _node_text(node, ("link", "guid"))
            if not link:
                atom = node.find("{http://www.w3.org/2005/Atom}link")
                link = atom.get("href", "") if atom is not None else ""
            body = _node_text(node, ("description", "summary",
                                     "{http://purl.org/rss/1.0/modules/content/}encoded",
                                     "{http://www.w3.org/2005/Atom}summary"))
            if title and link.startswith("http"):
                out.append({"title": title, "link": link, "description": _strip_tags(body)})
            if len(out) >= limit:
                return out
    return out


def _node_text(node, names):
    for name in names:
        child = node.find(name)
        if child is not None and (child.text or "").strip():
            return child.text.strip()
    return ""


def _strip_tags(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip()


def run(state):
    config = state["config"]
    limit = int(config.get("items_per_feed", 20))
    out = []
    # Every feed is read every cycle: six HTTP GETs cost nothing, and stopping
    # at the budget would mean the feeds at the end of the list were only ever
    # read on a quiet cycle. The ledger and `run_all` decide what is researched.
    for feed in config.get("urls") or []:
        url, key = feed.get("url", ""), feed.get("key") or feed.get("url", "")
        if not url:
            continue
        try:
            text = common.http_text(url, timeout=45)
        except Exception as error:
            state["errors"].append((key, f"fetch failed: {error}"))
            continue
        try:
            if "BEGIN:VEVENT" in text:
                out.extend(_from_ics(text, feed, key, limit))
            else:
                out.extend(_from_rss(text, feed, key, limit))
        except Exception as error:
            state["errors"].append((key, f"parse failed: {error}"))
    return out


def _from_ics(text, feed, key, limit):
    out = []
    for event in parse_ics(text, limit):
        start = ics_date(event["dtstart"])
        url = event["url"] or feed.get("url", "")
        caption = "\n".join(part for part in (
            event["summary"], event["description"],
            f"Location: {event['location']}" if event["location"] else "",
            f"DTSTART:{event['dtstart']}" if event["dtstart"] else "") if part)
        prefill = {"start_date": start, "end_date": ics_date(event["dtend"]) or start,
                   "name": event["location"], "county": feed.get("county", ""),
                   "date_evidence": f"DTSTART:{event['dtstart']}" if event["dtstart"] else ""}
        out.append(common.candidate("feeds", key, url, title=event["summary"],
                                    caption=caption, county=feed.get("county", ""),
                                    kind_hint="event",
                                    prefill=prefill if start else None))
    return out


def _from_rss(text, feed, key, limit):
    return [common.candidate("feeds", key, item["link"], title=item["title"],
                             caption="\n".join(p for p in (item["title"],
                                                           item["description"]) if p),
                             county=feed.get("county", ""), kind_hint="event")
            for item in parse_rss(text, limit)]
