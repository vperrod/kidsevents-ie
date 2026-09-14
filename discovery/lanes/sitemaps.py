#!/usr/bin/env python3
"""Lane 3 -- sitemaps of the curated domains.

A site's own sitemap lists every page it has, including the detail pages a
listing only shows this week's slice of. Filtering it to the family/event
paths and handing the ledger only the URLs it has never seen turns each
curated domain into a slow, complete backfill instead of a weekly snapshot.

Domains rotate `domains_per_cycle` at a time: a sitemap index can point at
dozens of child sitemaps and there is no value in re-reading all of them every
hour when the ledger drops everything already researched anyway.
"""

import re
import xml.etree.ElementTree as ET

from discovery import common

_SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
_WANTED = re.compile(r"event|whats-on|what-s-on|things-to-do|family|kids", re.I)
_CHILD_LIMIT = 5


def sitemap_urls(xml_text):
    """`(page_urls, child_sitemap_urls)` from a sitemap or a sitemap index."""
    root = ET.fromstring(xml_text.strip())
    children = [loc.text.strip() for entry in root.iter(f"{_SITEMAP_NS}sitemap")
                for loc in entry.iter(f"{_SITEMAP_NS}loc") if loc.text]
    pages = [loc.text.strip() for entry in root.iter(f"{_SITEMAP_NS}url")
             for loc in entry.iter(f"{_SITEMAP_NS}loc") if loc.text]
    return pages, children


def wanted(urls):
    return [url for url in urls if _WANTED.search(url)]


def _read(url):
    text = common.http_text(url, timeout=60)
    if not text.lstrip().startswith("<"):
        raise ValueError("not XML")
    return text


def run(state):
    config = state["config"]
    domains = config.get("domains") or []
    if not domains:
        return []
    per_cycle = min(int(config.get("domains_per_cycle", 3)), len(domains))
    start = int((state["factory_state"].get("lane_cursor") or {}).get("sitemaps", 0)) % len(domains)
    picked = [domains[(start + i) % len(domains)] for i in range(per_cycle)]
    state["cursor"] = (start + per_cycle) % len(domains)

    per_domain = int(config.get("urls_per_domain", 15))
    out = []
    for domain in picked:
        key = domain.removeprefix("https://").removeprefix("http://").removeprefix("www.").rstrip("/")
        try:
            pages, children = sitemap_urls(_read(domain.rstrip("/") + "/sitemap.xml"))
        except Exception as error:
            state["errors"].append((key, f"sitemap unreadable: {error}"))
            continue
        for child in wanted(children)[:_CHILD_LIMIT] or children[:_CHILD_LIMIT]:
            try:
                more, _ = sitemap_urls(_read(child))
            except Exception as error:
                state["errors"].append((key, f"child sitemap unreadable: {error}"))
                continue
            pages.extend(more)
        fresh = common.unresearched([common.candidate("sitemaps", key, url)
                                     for url in wanted(pages)])
        if not fresh:
            state["errors"].append((key, f"all {len(wanted(pages))} matching URLs "
                                         "already researched"))
        out.extend(fresh[:per_domain])
    return out
