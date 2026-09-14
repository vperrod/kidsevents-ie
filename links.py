#!/usr/bin/env python3
"""Resolve the outbound links of a record: official site, Instagram, TikTok.

A listing that only links back to the aggregator it was scraped from is worth
very little to a parent -- the venue's own site is where the opening hours and
the booking page live, and its Instagram is where this week's "closed for
maintenance" is posted. Nothing here is ever guessed: a URL is stored only
when it came from the source page's own markup, from Wikidata, or from a
handle that folds onto the venue's name, and only after it answered a request.

Links are optional. Every function here returns "" rather than raising, and
`resolve()` never blocks a record from going on air -- a venue with no website
is a real venue.

Run `python3 links.py refresh [limit]` for the nightly pass over on-air
records that are still missing one.
"""

import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import contract
import factory_worker

WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
# Wikidata and the search gateway both rate-limit anonymous clients harder than
# named ones; this is the same identity the discovery lanes use.
UA = {"User-Agent": "SmallDays/1.0 (+https://smalldays.ie; data@smalldays.ie)"}

# Hosts that are never anybody's "official site": the social networks have
# their own fields, and an aggregator is what we are trying to link away from.
# Matched as a stem with the dot, because the same network answers on a dozen
# TLDs -- `pinterest.co.uk` slipped through a "pinterest.com" check and was
# stored as a venue's website.
SOCIAL_HOSTS = ("instagram.", "tiktok.", "facebook.", "fb.", "twitter.", "x.com",
                "youtube.", "youtu.be", "linkedin.", "pinterest.", "whatsapp.",
                "threads.net")
OFF_LIMITS_HOSTS = SOCIAL_HOSTS + (
    "eventbrite.", "allevents.in", "google.", "goo.gl", "wikipedia.org",
    "wikidata.org", "wikimedia.org", "tripadvisor.", "yelp.", "booking.com",
    "ticketmaster.", "meetup.com", "gov.ie", "apple.com", "wordpress.org",
)
# Instagram paths that are content, not accounts.
IG_RESERVED = {"p", "reel", "reels", "tv", "explore", "accounts", "about",
               "developer", "legal", "directory", "stories", "s", "web", "help"}

# A name shorter than this folds onto far too much ("the", "park", "zoo"): a
# two-letter handle is not evidence of anything.
MIN_FOLD_CHARS = 5
# How long a resolved record is left alone by the nightly refresh.
RECHECK_DAYS = 14


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Name folding -- the only thing that licenses "this handle is that venue"
# ---------------------------------------------------------------------------

def fold(text):
    """A name with everything a domain or a handle would have dropped: case,
    spaces and punctuation ("Lough Key Forest & Activity Park" ->
    "loughkeyforestactivitypark")."""
    return re.sub(r"[^a-z0-9]+", "", str(text or "").lower())


def core(text):
    """The fold without the words a name carries and a handle usually does
    not: articles, and the company suffixes nobody types ("The Ark, A Cultural
    Centre" -> "arkculturalcentre")."""
    words = re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).split()
    return "".join(w for w in words if w not in _NOISE_WORDS)


_NOISE_WORDS = {"the", "a", "an", "of", "and", "at", "in", "ie", "com", "www",
                "official", "ltd", "limited", "cic", "clg"}


def name_folds(candidate, name):
    """True when `candidate` (a domain, a handle, a profile name) is the same
    name as `name` once case, spacing and punctuation are gone. One has to
    contain the other: "loughkey" is "Lough Key Forest & Activity Park", but
    "visitwicklow" is not "Beyond the Trees Avondale".

    Compared both ways, with and without the noise words: a domain keeps the
    article a venue name spells out ("beyondthetrees.ie" for "Beyond the Trees
    Avondale") and drops it just as often.
    """
    for left, right in ((fold(candidate), fold(name)), (core(candidate), core(name))):
        if len(left) < MIN_FOLD_CHARS or len(right) < MIN_FOLD_CHARS:
            continue
        if left in right or right in left:
            return True
    return False


def _host(url):
    host = urllib.parse.urlparse(str(url or "")).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _domain_name(url):
    """The registrable-looking part of a host, which is what folds onto a venue
    name: "www.loughkey.ie" -> "loughkey"."""
    parts = [p for p in _host(url).split(".") if p]
    return parts[0] if parts else ""


def _is_off_limits(url):
    host = _host(url)
    return any(bad in host for bad in OFF_LIMITS_HOSTS)


# ---------------------------------------------------------------------------
# Verification -- a link nobody can open is worse than no link
# ---------------------------------------------------------------------------

def verify_url(url, timeout=12):
    """True when the URL answers 200 (redirects followed, so a 301 to a live
    page counts). HEAD first -- a listing page can be a megabyte and nothing
    here reads the body -- with a GET fallback for the sites that refuse it."""
    if not str(url or "").startswith(("http://", "https://")):
        return False
    for method in ("HEAD", "GET"):
        request = urllib.request.Request(url, method=method,
                                         headers=factory_worker.RESEARCH_UA)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status == 200
        except urllib.error.HTTPError as error:
            if error.code in (403, 405, 501) and method == "HEAD":
                continue
            return False
        except Exception:
            return False
    return False


def fetch_html(url, timeout=20):
    """The page's own markup (not the stripped text `research_fetch` returns --
    JSON-LD and hrefs are exactly what the strip throws away)."""
    try:
        return factory_worker._http_text(url, timeout=timeout)
    except Exception as error:
        factory_worker.log(f"links fetch {str(url)[:70]}: {error}")
        return ""


# ---------------------------------------------------------------------------
# Source A -- the source page's own markup
# ---------------------------------------------------------------------------

_JSONLD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.S | re.I)


def jsonld_objects(html):
    """Every object in every JSON-LD block on the page, @graph and arrays
    flattened, so a caller can just look for the key it wants."""
    for match in _JSONLD_RE.finditer(html or ""):
        try:
            data = json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            continue
        stack, seen = [data], 0
        while stack and seen < 200:
            node = stack.pop()
            seen += 1
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                yield node
                graph = node.get("@graph")
                if graph:
                    stack.append(graph)


def links_from_jsonld(html, source_url=""):
    """`url`, `sameAs` and `offers.url` out of the page's structured data.

    A `url` on the same host as the page itself is the page's own address, not
    an official site -- storing it would just duplicate `source_url`.
    """
    found = {"official_url": "", "instagram_url": "", "tiktok_url": "", "booking_url": ""}
    here = _host(source_url)
    for node in jsonld_objects(html):
        urls = [node.get("url")]
        same_as = node.get("sameAs")
        urls += same_as if isinstance(same_as, list) else [same_as]
        offers = node.get("offers")
        for offer in (offers if isinstance(offers, list) else [offers]):
            if isinstance(offer, dict) and not found["booking_url"]:
                found["booking_url"] = _clean(offer.get("url"))
        for url in urls:
            url = _clean(url)
            if not url:
                continue
            host = _host(url)
            if "instagram.com" in host and not found["instagram_url"]:
                found["instagram_url"] = profile_url(url) or ""
            elif "tiktok.com" in host and not found["tiktok_url"]:
                found["tiktok_url"] = profile_url(url) or ""
            elif not found["official_url"] and host and host != here and not _is_off_limits(url):
                found["official_url"] = url
    return found


def _clean(value):
    url = str(value or "").strip()
    return url if url.startswith(("http://", "https://")) else ""


_HREF_RE = re.compile(r'href=["\'](https?://[^"\'\s>]+)["\']', re.I)


def official_from_outbound(html, source_url, name):
    """The first outbound link on the source page whose domain folds onto the
    venue's name -- how a "Beyond the Trees Avondale" listing on visitwicklow.ie
    points at beyondthetrees.ie."""
    here = _host(source_url)
    for url in _HREF_RE.findall(html or ""):
        host = _host(url)
        if not host or host == here or _is_off_limits(url):
            continue
        if name_folds(_domain_name(url), name):
            return urllib.parse.urlunparse(urllib.parse.urlparse(url)._replace(
                path="/", params="", query="", fragment=""))
    return ""


# ---------------------------------------------------------------------------
# Source B -- Wikidata
# ---------------------------------------------------------------------------

_WIKIDATA_QUERY = """SELECT ?item ?site ?ig ?tt ?adminLabel WHERE {
  ?item rdfs:label %s@en .
  OPTIONAL { ?item wdt:P856 ?site }
  OPTIONAL { ?item wdt:P2003 ?ig }
  OPTIONAL { ?item wdt:P7085 ?tt }
  OPTIONAL { ?item wdt:P131 ?admin }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en" }
} LIMIT 8"""


def wikidata_links(name, county="", timeout=30):
    """`{official_url, instagram_url, tiktok_url}` for a place Wikidata knows
    by that exact English label.

    The county is the disambiguator: there is a Lough Key in Roscommon and a
    dozen same-named things elsewhere, so a labelled administrative unit that
    is not this county disqualifies the item. An item with no P131 at all is
    accepted only when it is the only answer.
    """
    if len(fold(name)) < MIN_FOLD_CHARS:
        return {}
    query = _WIKIDATA_QUERY % json.dumps(str(name))
    url = WIKIDATA_SPARQL + "?" + urllib.parse.urlencode({"query": query, "format": "json"})
    try:
        request = urllib.request.Request(
            url, headers={**UA, "Accept": "application/sparql-results+json"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8", "ignore"))
    except Exception as error:
        factory_worker.log(f"wikidata {name[:40]!r}: {error}")
        return {}
    rows = data.get("results", {}).get("bindings", [])
    if not rows:
        return {}
    if county:
        matched = [r for r in rows
                   if county.lower() in r.get("adminLabel", {}).get("value", "").lower()]
        if not matched:
            matched = [r for r in rows if not r.get("adminLabel")] if len(rows) == 1 else []
        rows = matched
    found = {}
    for row in rows:
        site = _clean(row.get("site", {}).get("value"))
        if site and not found.get("official_url"):
            found["official_url"] = site
        handle = row.get("ig", {}).get("value", "").strip().lstrip("@")
        if handle and not found.get("instagram_url"):
            found["instagram_url"] = f"https://www.instagram.com/{handle}/"
        tiktok = row.get("tt", {}).get("value", "").strip().lstrip("@")
        if tiktok and not found.get("tiktok_url"):
            found["tiktok_url"] = f"https://www.tiktok.com/@{tiktok}"
    return found


# ---------------------------------------------------------------------------
# Source C -- handles on the official site, then the search gateway
# ---------------------------------------------------------------------------

_IG_HANDLE_RE = re.compile(r"instagram\.com/([A-Za-z0-9_.]{2,30})", re.I)
_TT_HANDLE_RE = re.compile(r"tiktok\.com/@([A-Za-z0-9_.]{2,30})", re.I)


def profile_url(url):
    """The account URL behind any Instagram or TikTok link, or "" when the URL
    names no account (a /explore/ page, a bare instagram.com)."""
    host = _host(url)
    if "instagram.com" in host:
        match = _IG_HANDLE_RE.search(str(url))
        if match and match.group(1).lower() not in IG_RESERVED:
            return f"https://www.instagram.com/{match.group(1)}/"
    if "tiktok.com" in host:
        match = _TT_HANDLE_RE.search(str(url))
        if match:
            return f"https://www.tiktok.com/@{match.group(1)}"
    return ""


def _handle_folds(profile, name):
    """True when an account URL's handle is this venue's name."""
    return name_folds(profile.rstrip("/").rsplit("/", 1)[-1].lstrip("@"), name)


def handles_in_html(html):
    """Every Instagram and TikTok account the page links to, in page order --
    a venue's own handles live in its footer or nav."""
    instagram = [h for h in _IG_HANDLE_RE.findall(html or "") if h.lower() not in IG_RESERVED]
    tiktok = _TT_HANDLE_RE.findall(html or "")
    return instagram, tiktok


def site_handle(handles, name, official_url):
    """Which of the accounts a site links to is the site's own.

    Not simply the first one on the page: a venue embeds other people's posts.
    jurassicnewpark.com embeds @wishwithaudrey, ucc.ie embeds @graduatecompass
    and arklowartscentre.ie embeds @fionnmaccumhail1 -- all three were stored
    as the venue's own account before this test existed. Its own name, its own
    domain, or the only account on the page.
    """
    unique = list(dict.fromkeys(handle.lower() for handle in handles))
    for handle in unique:
        if name_folds(handle, name) or name_folds(handle, _domain_name(official_url)):
            return handle
    return unique[0] if len(unique) == 1 else ""


def social_from_search(platform, name, county):
    """Search the gateway for the venue's account and accept a result only when
    its handle or its profile title folds onto the venue's name. A search that
    finds nothing convincing returns "" -- it never guesses."""
    site = "instagram.com" if platform == "instagram" else "tiktok.com"
    query = f'site:{site} "{name}"' + (f" {county}" if county else "")
    for result in factory_worker._src_gateway(query, 8):
        url = profile_url(result.get("url", ""))
        if not url:
            continue
        handle = url.rstrip("/").rsplit("/", 1)[-1].lstrip("@")
        title = re.sub(r"[(@|·].*$", "", result.get("title", ""))
        if name_folds(handle, name) or name_folds(title, name):
            return url
    return ""


# ---------------------------------------------------------------------------
# The resolver
# ---------------------------------------------------------------------------

def venue_name(record):
    return (record.get("location") or {}).get("name") or record.get("title") or ""


def social_candidate_profile(record, candidate=None, verdict=None):
    """For a record found ON Instagram or TikTok: the author's own profile URL,
    when the account is the venue's rather than a passer-by's.

    `classify` answers `is_venue_account`; a handle that folds onto the venue's
    name is the same evidence read out of the URL, and either is enough.
    """
    source = (record.get("links") or {}).get("source_url", "")
    profile = profile_url(source)
    if not profile:
        return "", ""
    handle = profile.rstrip("/").rsplit("/", 1)[-1].lstrip("@")
    author = str((candidate or {}).get("author") or "")
    is_venue = (verdict or {}).get("is_venue_account") is True
    if not (is_venue or name_folds(handle, venue_name(record))
            or name_folds(author, venue_name(record))):
        return "", ""
    return ("instagram" if "instagram.com" in _host(source) else "tiktok"), profile


def resolve(record, candidate=None, verdict=None, html=None, search=True):
    """Fill in `links.official_url`, `instagram_url` and `tiktok_url` for one
    record, and stamp `links_checked`. Mutates and returns the record.

    Never raises: every lane is best-effort, and a record with no links is a
    perfectly good record.
    """
    links = record.setdefault("links", {})
    name = venue_name(record)
    county = (record.get("location") or {}).get("county", "")
    source_url = links.get("source_url", "")

    platform, profile = social_candidate_profile(record, candidate, verdict)
    # `_build_record` parks the post's own URL in this field so a record found
    # on Instagram keeps the link even when this step never runs; the account
    # is the better answer, so it replaces it (and the post itself survives as
    # `source_url`, which is what `media.build_embeds` reads).
    if platform and links.get(f"{platform}_url", "") in ("", source_url):
        links[f"{platform}_url"] = profile

    if html is None:
        html = "" if _host(source_url) in ("instagram.com", "tiktok.com") else fetch_html(source_url)
    markup = links_from_jsonld(html, source_url)
    if markup["booking_url"] and not links.get("booking_url"):
        links["booking_url"] = markup["booking_url"]

    official = markup["official_url"]
    if not official:
        known = wikidata_links(name, county)
        official = known.get("official_url", "")
        for field in ("instagram_url", "tiktok_url"):
            if known.get(field) and not links.get(field):
                links[field] = known[field]
    if not official:
        official = official_from_outbound(html, source_url, name)
    if official and not links.get("official_url") and verify_url(official):
        links["official_url"] = official

    _resolve_socials(links, markup, name, county, search)
    links["links_checked"] = now_iso()
    return record


def _resolve_socials(links, markup, name, county, search):
    """Instagram and TikTok, in the spec's order: the source page's markup, the
    official site's own footer, then the search gateway.

    The markup lane has to pass the same fold test as the search lane. A
    listing page's `sameAs` describes whoever published the page, not the venue
    on it: purecork.ie's own JSON-LD put `@pure_cork` on a trad session at the
    Blue Haven, and thisisgalway.ie put `@thisisgalway` on Culture Night. A
    handle read off the venue's OWN site needs no such test.
    """
    for field in ("instagram_url", "tiktok_url"):
        if markup[field] and not links.get(field) and _handle_folds(markup[field], name):
            links[field] = markup[field]
    if links.get("instagram_url") and links.get("tiktok_url"):
        return
    if links.get("official_url"):
        instagram, tiktok = handles_in_html(fetch_html(links["official_url"]))
        for handles, field, url in ((instagram, "instagram_url", "https://www.instagram.com/{}/"),
                                    (tiktok, "tiktok_url", "https://www.tiktok.com/@{}")):
            handle = site_handle(handles, name, links["official_url"])
            if handle and not links.get(field):
                links[field] = url.format(handle)
    if not search:
        return
    for platform, field in (("instagram", "instagram_url"), ("tiktok", "tiktok_url")):
        if not links.get(field) and len(fold(name)) >= MIN_FOLD_CHARS:
            links[field] = social_from_search(platform, name, county)


# ---------------------------------------------------------------------------
# Nightly refresh
# ---------------------------------------------------------------------------

def needs_links(record, before=""):
    """On-air contract records that are still missing a link and have not been
    looked at since `before`."""
    if record.get("schema_version") != 1 or record.get("status", "on-air") != "on-air":
        return False
    links = record.get("links") or {}
    if all(links.get(f) for f in ("official_url", "instagram_url", "tiktok_url")):
        return False
    return (links.get("links_checked") or "") < before


def refresh(limit=200, search=True):
    """Re-resolve the on-air records that are still missing a link. Returns
    `{kind: (looked_at, gained)}`."""
    before = (datetime.now(timezone.utc) - timedelta(days=RECHECK_DAYS)).isoformat(timespec="seconds")
    summary = {}
    budget = limit
    for kind, store in factory_worker.STORE_FOR_KIND.items():
        looked, gained = 0, 0
        for record in factory_worker.load_json_store(store, []):
            if budget <= 0:
                break
            if not needs_links(record, before):
                continue
            budget -= 1
            looked += 1
            had = _link_count(record)
            try:
                resolve(record, search=search)
            except Exception as error:
                factory_worker.log(f"links refresh {record.get('id')}: {error}")
                continue
            gained += _link_count(record) - had
            _save_links(store, record)
        summary[kind] = (looked, gained)
    return summary


def _link_count(record):
    links = record.get("links") or {}
    return sum(1 for f in ("official_url", "instagram_url", "tiktok_url") if links.get(f))


def _save_links(store, record):
    """Write one record's links back into its store under the output lock --
    the factory cycle may be publishing into the same file."""
    with factory_worker.output_lock():
        records = factory_worker.load_json_store(store, [])
        for stored in records:
            if stored.get("id") == record.get("id"):
                stored["links"] = record["links"]
                factory_worker.write_json_atomic(store, records)
                return True
    return False


def main():
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 200
    if len(sys.argv) > 1 and sys.argv[1] == "refresh":
        for kind, (looked, gained) in refresh(limit).items():
            print(f"{kind}: {looked} checked, {gained} links gained")
        return
    print(__doc__)


if __name__ == "__main__":
    main()
