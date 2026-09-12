#!/usr/bin/env python3
"""Collect saved Instagram/TikTok posts through the authenticated OpenCLI browser.

This mirrors WanderTold's collection mechanism: private collection pages are
read in the browser profile that owns them, post permalinks are collected from
the rendered DOM, and the resulting leads go to staging. Nothing is published
to the public event or holiday feeds from this script.
"""

import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


BASE = Path(__file__).resolve().parent
STAGED_FILE = BASE / "staged" / "social_candidates.json"
OPENCLI = os.environ.get("OPENCLI_BIN", "opencli")
TIKTOK_COLLECTION_URL = os.environ.get("TIKTOK_COLLECTION_URL", "").strip()
INSTAGRAM_COLLECTION_URL = os.environ.get("INSTAGRAM_COLLECTION_URL", "").strip()
INSTAGRAM_COLLECTION_NAME = os.environ.get("INSTAGRAM_COLLECTION_NAME", "Kids")
MAX_POSTS = min(max(int(os.environ.get("SOCIAL_COLLECTION_LIMIT", "50")), 1), 100)

TIKTOK_LINKS = (
    "Array.from(document.querySelectorAll("
    "'[data-e2e=collection-item] a[href*=\"/video/\"]'))"
    ".map(a => a.getAttribute('href'))"
)
INSTAGRAM_LINKS = (
    "Array.from(document.querySelectorAll("
    "'a[href*=\"/p/\"], a[href*=\"/reel/\"]'))"
    ".map(a => a.getAttribute('href'))"
)


def run(*args, timeout=90):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def collection_links(session, collection_url, selector, scrolls=4):
    """Collect lazy-loaded permalink hrefs from a private collection page."""
    run(OPENCLI, "browser", session, "close", timeout=20)
    opened = run(
        OPENCLI,
        "browser",
        session,
        "open",
        collection_url,
        "--window",
        "background",
        timeout=90,
    )
    if opened.returncode:
        raise RuntimeError(
            "OpenCLI could not open the collection. Connect its browser bridge "
            "to the authenticated Chrome profile first."
        )
    links = []
    try:
        for _ in range(scrolls):
            time.sleep(2)
            result = run(OPENCLI, "browser", session, "eval", selector, timeout=30)
            if result.returncode == 0:
                try:
                    for href in json.loads(result.stdout):
                        if href and href not in links:
                            links.append(href)
                except json.JSONDecodeError:
                    pass
            run(OPENCLI, "browser", session, "scroll", "down", timeout=20)
    finally:
        run(OPENCLI, "browser", session, "close", timeout=20)
    return links[:MAX_POSTS]


def tiktok_caption(url):
    try:
        endpoint = "https://www.tiktok.com/oembed?url=" + urllib.parse.quote(url, safe="")
        request = urllib.request.Request(endpoint, headers={"User-Agent": "Mozilla/5.0"})
        data = json.loads(urllib.request.urlopen(request, timeout=15).read())
        return f"{data.get('title', '')} — by {data.get('author_name', '')}".strip(" —")
    except Exception:
        return ""


def instagram_saved():
    """Use OpenCLI's structured read first, then pair it with collection URLs."""
    if not INSTAGRAM_COLLECTION_URL:
        return []
    result = run(
        OPENCLI,
        "instagram",
        "saved",
        "--limit",
        str(MAX_POSTS),
        "--collection",
        INSTAGRAM_COLLECTION_NAME,
        "-f",
        "json",
    )
    try:
        posts = json.loads(result.stdout) if result.returncode == 0 else []
    except json.JSONDecodeError:
        posts = []
    hrefs = collection_links("kids-ig", INSTAGRAM_COLLECTION_URL, INSTAGRAM_LINKS)
    candidates = []
    for index, href in enumerate(hrefs):
        post = posts[index] if index < len(posts) else {}
        url = href if href.startswith("http") else "https://www.instagram.com" + href
        candidates.append(
            {
                "platform": "instagram",
                "source_url": url,
                "caption": post.get("caption", ""),
                "author": post.get("user", ""),
            }
        )
    return candidates


def tiktok_saved():
    if not TIKTOK_COLLECTION_URL:
        return []
    hrefs = collection_links("kids-tiktok", TIKTOK_COLLECTION_URL, TIKTOK_LINKS)
    candidates = []
    for href in hrefs:
        url = href if href.startswith("http") else "https://www.tiktok.com" + href
        candidates.append(
            {
                "platform": "tiktok",
                "source_url": url,
                "caption": tiktok_caption(url),
                "author": "",
            }
        )
    return candidates


def write_staged(candidates):
    STAGED_FILE.parent.mkdir(exist_ok=True)
    try:
        existing = json.loads(STAGED_FILE.read_text()) if STAGED_FILE.exists() else []
    except json.JSONDecodeError:
        existing = []
    seen = {item.get("source_url") for item in existing}
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    additions = []
    for candidate in candidates:
        if candidate["source_url"] in seen:
            continue
        additions.append(
            {
                **candidate,
                "captured_at": timestamp,
                "status": "needs_review",
                "review_note": "Verify destination, dates, age guidance and price on the organiser website before publishing.",
            }
        )
    STAGED_FILE.write_text(json.dumps(existing + additions, indent=2, ensure_ascii=False) + "\n")
    return len(additions)


def main():
    candidates = []
    candidates.extend(instagram_saved())
    candidates.extend(tiktok_saved())
    if not TIKTOK_COLLECTION_URL and not INSTAGRAM_COLLECTION_URL:
        print(
            "No collection URL is configured. Set TIKTOK_COLLECTION_URL and/or "
            "INSTAGRAM_COLLECTION_URL in .env.",
            file=sys.stderr,
        )
        return 2
    print(f"Staged {write_staged(candidates)} new social candidates.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
