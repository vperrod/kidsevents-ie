#!/usr/bin/env python3
"""The picture on a listing: a licensed hero photo, and social posts as embeds.

Two rules decide everything in this module.

**We only host what we are licensed to host.** A hero is downloaded from
Wikimedia Commons or Openverse, and only under CC0 / CC BY / CC BY-SA / public
domain, with the photographer and the licence kept next to the file so the
credit line can be rendered. A site's own `og:image` is used only when neither
found anything, is flagged `placeholder` and is replaced the moment a licensed
one turns up.

**We never rehost Instagram or TikTok.** Their photos stay on their servers:
a post becomes `media.embeds[]` and is rendered as an iframe by the browser
that is looking at it. Nothing under `web/media/` ever comes from either.

Between the download and the record sits the vision gate: one model call per
candidate that answers hero / pass / reject and writes the alt text. It fails
open -- a gate that cannot answer marks the photo `gate="skip"` and keeps it,
because an outage is not a verdict.

Run `python3 media.py refresh [limit]` for the nightly pass.
"""

import base64
import hashlib
import io
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import factory_worker
import links
import llm

BASE = Path(__file__).resolve().parent
MEDIA_DIR = BASE / "web" / "media"
MEDIA_INDEX = BASE / "media_index.json"

COMMONS_API = "https://commons.wikimedia.org/w/api.php"
OPENVERSE_API = "https://api.openverse.org/v1/images/"

# Per record: at most this many images are downloaded, and each gets at most
# one gate call. A venue with no usable photo must cost a bounded amount.
MAX_CANDIDATES = 3
MAX_IMAGE_BYTES = 12 * 1024 * 1024
# Without Pillow a file is stored exactly as fetched, so it has to be small
# enough to serve as-is.
MAX_STORED_BYTES = 2 * 1024 * 1024
MAX_WIDTH = 1600
# The gate reads a downscaled copy: the verdict does not need 1600 px and the
# local lane's image encoder is several times slower on a full-size file.
GATE_MAX_PX = 1024
REFRESH_LIMIT = 200

# Licences we may host under. Anything else -- "all rights reserved", the
# non-commercial and no-derivatives variants -- is not ours to copy.
OK_LICENCES = ("cc0", "cc-zero", "public domain", "publicdomain", "pd-", "cc by",
               "cc-by", "cc by-sa", "cc-by-sa", "attribution")
BAD_LICENCE_MARKERS = ("-nc", " nc", "noncommercial", "-nd", " nd", "noderiv",
                       "all rights reserved", "fair use")

# Lifted from WanderTold's photo-gate (scripts/photo-gate.py RULES): the same
# wrong heroes come back from every image search -- a portrait, a logo, a
# poster, or a photo of a different business entirely.
RULES = """PASS when the image is a real photograph whose SUBJECT is the place itself:
its building or exterior, its grounds or interior, the view from it, its animals,
play equipment, exhibits or attractions.
REJECT when a person or people are the SUBJECT - a portrait, a selfie, someone posing or
talking to camera, or a crowd shot with no place visible. People who merely appear in a
photograph of the place - children on the playground, visitors on the trail - are fine:
judge what the photograph is OF, not whether anyone is in it.
REJECT a map, a floor plan or a site plan; a logo or wordmark; a menu; a poster or flyer;
a screenshot or any image that is mostly text; an AI-generated or 3D-rendered image; and
any photograph of a clearly DIFFERENT place - in particular, if a sign or wordmark in the
photograph shows a business name that is not this place's name, it is a different place."""

GRADE_LINE = """HERO: the image passes AND the place itself fills the frame, with no person
as the subject.
PASS: the image passes but is a close-up detail or people are prominent in it.
REJECT: otherwise."""


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Embeds -- the only thing Instagram and TikTok are ever used for
# ---------------------------------------------------------------------------

_IG_POST_RE = re.compile(r"instagram\.com/(?:[A-Za-z0-9_.]+/)?(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)", re.I)
_TT_VIDEO_RE = re.compile(r"tiktok\.com/@[A-Za-z0-9_.]+/video/(\d+)", re.I)


def embed_for(url):
    """`{platform, url}` for a post, reel or video, or None.

    An account page is not an embed: both embed endpoints address one piece of
    content by id, so a profile URL has nothing to put in an iframe.
    """
    match = _IG_POST_RE.search(str(url or ""))
    if match:
        return {"platform": "instagram", "url": f"https://www.instagram.com/p/{match.group(1)}/embed/"}
    match = _TT_VIDEO_RE.search(str(url or ""))
    if match:
        return {"platform": "tiktok", "url": f"https://www.tiktok.com/embed/v2/{match.group(1)}"}
    return None


def build_embeds(record):
    """Every embeddable post this record knows about: its own source, when that
    source was a post, plus whatever the links step resolved."""
    record_links = record.get("links") or {}
    embeds, seen = [], set()
    for field in ("source_url", "instagram_url", "tiktok_url"):
        embed = embed_for(record_links.get(field, ""))
        if embed and embed["url"] not in seen:
            seen.add(embed["url"])
            embeds.append(embed)
    return embeds


# ---------------------------------------------------------------------------
# Candidate photos (a) Wikimedia Commons
# ---------------------------------------------------------------------------

def _api_json(url, timeout=30):
    request = urllib.request.Request(url, headers={**links.UA, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", "ignore"))


def licence_ok(licence):
    """True for the licences we may copy a file under."""
    text = str(licence or "").strip().lower()
    if not text or any(bad in text for bad in BAD_LICENCE_MARKERS):
        return False
    return any(ok in text for ok in OK_LICENCES)


def _strip_tags(value):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(value or ""))).strip()


_QUERY_NOISE = {"the", "and", "a", "an", "of", "at", "in"}


def query_variants(name, county=""):
    """How a photo archive has to be asked for this place: the full name, then
    a shorter one.

    Both archives AND the words together and then fall back to anything that
    shares one, so an exact, long name is the worst possible query: "Lough Key
    Forest & Activity Park Roscommon" returns two scanned books, while "Lough
    Key Forest Roscommon" returns the 34 photographs that exist.
    """
    words = [w for w in re.sub(r"[^\w\s]", " ", str(name or "")).split()
             if w.lower() not in _QUERY_NOISE]
    variants = []
    for part in (" ".join(words), " ".join(words[:3])):
        query = " ".join(x for x in (part, county) if x).strip()
        if query and query not in variants:
            variants.append(query)
    return variants


def commons_candidates(name, county="", limit=4):
    """Files Commons holds for this place, already filtered to the licences we
    may host. `extmetadata` carries the photographer and the licence, which the
    credit line needs and which nothing else in the response provides."""
    for query in query_variants(name, county):
        found = _commons_search(query, limit)
        if found:
            return found
    return []


def _commons_search(query, limit):
    search_url = COMMONS_API + "?" + urllib.parse.urlencode({
        "action": "query", "list": "search", "srsearch": query,
        "srnamespace": 6, "srlimit": limit, "format": "json"})
    try:
        hits = _api_json(search_url).get("query", {}).get("search", [])
    except Exception as error:
        factory_worker.log(f"commons search {query[:40]!r}: {error}")
        return []
    titles = [hit["title"] for hit in hits if hit.get("title")]
    if not titles:
        return []
    info_url = COMMONS_API + "?" + urllib.parse.urlencode({
        "action": "query", "titles": "|".join(titles), "prop": "imageinfo",
        "iiprop": "url|extmetadata|mime", "iiurlwidth": MAX_WIDTH, "format": "json"})
    try:
        pages = _api_json(info_url).get("query", {}).get("pages", {})
    except Exception as error:
        factory_worker.log(f"commons imageinfo {query[:40]!r}: {error}")
        return []
    return [c for c in (_commons_candidate(page) for page in pages.values()) if c]


def _commons_candidate(page):
    info = (page.get("imageinfo") or [{}])[0]
    if not str(info.get("mime", "")).startswith("image/"):
        return None
    meta = info.get("extmetadata") or {}
    licence = _strip_tags((meta.get("LicenseShortName") or {}).get("value"))
    if not licence_ok(licence):
        return None
    return {
        "source": "wikimedia-commons",
        "image_url": info.get("thumburl") or info.get("url", ""),
        "source_url": info.get("descriptionurl", ""),
        "licence": licence,
        "licence_url": (meta.get("LicenseUrl") or {}).get("value", ""),
        "attribution": _strip_tags((meta.get("Artist") or {}).get("value"))[:120],
    }


# ---------------------------------------------------------------------------
# Candidate photos (b) Openverse
# ---------------------------------------------------------------------------

def openverse_candidates(name, county="", limit=4):
    for query in query_variants(name, county):
        found = _openverse_search(query, limit)
        if found:
            return found
    return []


def _openverse_search(query, limit):
    url = OPENVERSE_API + "?" + urllib.parse.urlencode({
        "q": query, "license": "cc0,by,by-sa", "license_type": "commercial",
        "page_size": limit})
    try:
        # Short: anonymous Openverse regularly takes longer than a minute to
        # answer, and it is the second lane -- Commons has usually answered.
        results = _api_json(url, timeout=20).get("results", [])
    except Exception as error:
        factory_worker.log(f"openverse {query[:40]!r}: {error}")
        return []
    return [c for c in (_openverse_candidate(r) for r in results) if c]


def _openverse_candidate(result):
    licence = " ".join(x for x in ("CC", str(result.get("license", "")).upper(),
                                   str(result.get("license_version", ""))) if x).strip()
    if not licence_ok(licence) or not result.get("url"):
        return None
    return {
        "source": "openverse",
        "image_url": result["url"],
        "source_url": result.get("foreign_landing_url", ""),
        "licence": licence,
        "licence_url": result.get("license_url", ""),
        "attribution": str(result.get("creator") or "")[:120],
    }


# ---------------------------------------------------------------------------
# Candidate photo (c) the official site's own og:image -- a placeholder
# ---------------------------------------------------------------------------

_OG_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\']og:image(?::url)?["\'][^>]+content=["\']([^"\']+)["\']', re.I)


def og_image_candidate(page_url):
    """The site's own social-card image. We have no licence to it, so it is
    stored flagged as a placeholder and credited to the domain it came from."""
    if not page_url:
        return None
    html = links.fetch_html(page_url)
    match = _OG_RE.search(html or "")
    if not match:
        return None
    url = urllib.parse.urljoin(page_url, match.group(1).strip())
    if not url.startswith(("http://", "https://")):
        return None
    return {
        "source": "official-site", "image_url": url, "source_url": page_url,
        "licence": "", "licence_url": "",
        "attribution": urllib.parse.urlparse(page_url).netloc.lower(),
        "placeholder": True,
    }


# ---------------------------------------------------------------------------
# Fetch and store
# ---------------------------------------------------------------------------

def fetch_image(url, timeout=30):
    """The image's bytes, or None. Never fetched from Instagram or TikTok --
    that is the one thing this module must not do."""
    host = urllib.parse.urlparse(str(url or "")).netloc.lower()
    if any(social in host for social in ("instagram.com", "tiktok.com", "cdninstagram", "fbcdn")):
        raise ValueError(f"refusing to download from {host}")
    request = urllib.request.Request(url, headers=links.UA)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if not str(response.headers.get("Content-Type", "")).startswith("image/"):
            raise ValueError("not an image")
        data = response.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("image is larger than the fetch cap")
    return data


def to_jpeg(data, max_width=MAX_WIDTH):
    """JPEG bytes no wider than `max_width`. Pillow is in the venv; without it
    the bytes are stored exactly as fetched, which is why the caller still has
    to refuse anything over MAX_STORED_BYTES."""
    try:
        from PIL import Image
    except ImportError:
        return data
    try:
        image = Image.open(io.BytesIO(data))
        image = image.convert("RGB")
        if image.width > max_width:
            image = image.resize((max_width, round(image.height * max_width / image.width)),
                                 Image.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=85, optimize=True)
        return buffer.getvalue()
    except Exception as error:
        factory_worker.log(f"media resize: {error}")
        return data


def store_image(kind, record_id, image_url, data):
    """Write the photo under `web/media/<kind>/` and return its public path.
    The hash is of the source URL, so re-running never leaves two copies.

    Relative, no leading slash: the site is served under a path prefix in
    production (Caddy `handle_path /kidsevents*` strips it before forwarding
    to this Flask app, which itself answers at "/"), same as the page's own
    favicon link. A leading "/media/..." resolves against the *site* root
    from the browser's point of view, which is a different app in
    production -- found 2026-09-14 when every hero photo 401'd on the public
    URL despite loading fine when the app was hit directly."""
    digest = hashlib.sha1(str(image_url).encode("utf-8")).hexdigest()[:10]
    folder = MEDIA_DIR / kind
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{record_id}-{digest}.jpg"
    path.write_bytes(data)
    return f"media/{kind}/{path.name}"


def public_path_to_file(url):
    return MEDIA_DIR / str(url or "").lstrip("/").removeprefix("media/")


# ---------------------------------------------------------------------------
# The vision gate
# ---------------------------------------------------------------------------

def _gate_prompt(name, county):
    where = ", ".join(x for x in (name, county) if x) or "this place"
    return (
        RULES + f"\n\nIs this image a photograph of: {where}, Ireland?\n" + GRADE_LINE
        + '\n\nReply ONLY a JSON object:\n'
        + '{"verdict": "hero" or "pass" or "reject", "why": at most 100 characters, '
        + f'"alt": alt text for this photo of {where}, at most 120 characters}}'
    )


def _vision_payload(model, prompt, image_b64):
    return {
        "model": model, "max_tokens": 400, "temperature": 0,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
        ]}],
    }


def _ask_vision(base_url, model, prompt, image_b64, timeout):
    request = urllib.request.Request(
        f"{base_url}/v1/chat/completions",
        data=json.dumps(_vision_payload(model, prompt, image_b64)).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8", "ignore"))
    if data.get("error"):
        raise RuntimeError(str(data["error"])[:200])
    message = (data.get("choices") or [{}])[0].get("message", {})
    return message.get("content") or message.get("reasoning") or ""


def local_free():
    """True when the mini PC's llama-server has a slot going spare. WanderTold's
    factory shares those two slots and runs continuously; a gate call that
    queues behind it would stall this cycle for minutes, and a photo is never
    worth that."""
    try:
        with urllib.request.urlopen(f"{llm.LOCAL_LLM_URL}/health", timeout=2) as response:
            if json.loads(response.read().decode("utf-8", "ignore")).get("status") != "ok":
                return False
        with urllib.request.urlopen(f"{llm.LOCAL_LLM_URL}/metrics", timeout=2) as response:
            metrics = response.read().decode("utf-8", "ignore")
    except Exception:
        return False
    match = re.search(r"^llamacpp:requests_processing\s+([0-9.]+)", metrics, re.M)
    return bool(match) and float(match.group(1)) < llm.LOCAL_BUSY_AT


def gate_photo(data, name, county=""):
    """`(verdict, why, alt)` for one photo. `verdict` is "hero", "pass",
    "reject" or "skip".

    "skip" is what an outage looks like: no lane answered, or none of them
    could read an image. It is deliberately NOT a rejection -- the photo is
    kept and the next refresh asks again.
    """
    prompt = _gate_prompt(name, county)
    image_b64 = base64.b64encode(to_jpeg(data, GATE_MAX_PX)).decode()
    lanes = []
    if local_free():
        lanes.append((llm.LOCAL_LLM_URL, "local", 90))
    lanes.append((llm.OMNIROUTE_URL, llm.AUTO_LANE, 120))
    for base_url, model, timeout in lanes:
        try:
            text = _ask_vision(base_url, model, prompt, image_b64, timeout)
        except Exception as error:
            factory_worker.log(f"vision gate {model}: {str(error)[:120]}")
            continue
        verdict, why, alt = _read_verdict(text)
        if verdict:
            return verdict, why, alt
    return "skip", "no vision lane answered", ""


def _read_verdict(text):
    """The model's answer as `(verdict, why, alt)`; an unparsable reply is no
    verdict at all, so `verdict` comes back empty and the caller tries the next
    lane."""
    obj = factory_worker.extract_obj(text or "") or {}
    verdict = str(obj.get("verdict") or "").strip().lower()
    if verdict not in ("hero", "pass", "reject"):
        match = re.search(r"\b(hero|pass|reject)\b", str(text or ""), re.I)
        verdict = match.group(1).lower() if match else ""
    return verdict, str(obj.get("why") or "")[:100], str(obj.get("alt") or "")[:120]


# ---------------------------------------------------------------------------
# The hero
# ---------------------------------------------------------------------------

def credit_line(candidate):
    """What is printed under the photo. A licensed photo credits the
    photographer and the licence; a placeholder credits the site it came
    from, because we are not claiming a licence we do not have."""
    if candidate.get("placeholder"):
        return f"Photo: {candidate.get('attribution', '')}".strip()
    parts = [candidate.get("attribution") or candidate.get("source", ""), candidate.get("licence", "")]
    return "Photo: " + " · ".join(p for p in parts if p)


def candidates_for(record):
    """Licensed sources first, in the spec's order; the site's own og:image is
    only ever the last resort."""
    name = links.venue_name(record)
    county = (record.get("location") or {}).get("county", "")
    found = commons_candidates(name, county) + openverse_candidates(name, county)
    if not found:
        record_links = record.get("links") or {}
        placeholder = og_image_candidate(record_links.get("official_url")
                                         or record_links.get("source_url", ""))
        if placeholder:
            found = [placeholder]
    return found[:MAX_CANDIDATES]


def choose_hero(record):
    """Download, gate and keep the best photo for this record, or None.

    The first HERO verdict wins outright; a PASS is held as the fallback; a
    REJECT is deleted from disk. At most MAX_CANDIDATES downloads and one gate
    call each, whatever the outcome.
    """
    name = links.venue_name(record)
    county = (record.get("location") or {}).get("county", "")
    fallback = None
    for candidate in candidates_for(record):
        try:
            data = to_jpeg(fetch_image(candidate["image_url"]))
        except Exception as error:
            factory_worker.log(f"media fetch {str(candidate.get('image_url'))[:70]}: {error}")
            continue
        if len(data) > MAX_STORED_BYTES:
            factory_worker.log(f"media {str(candidate.get('image_url'))[:60]}: over the 2 MB cap")
            continue
        url = store_image(record["kind"], record["id"], candidate["image_url"], data)
        verdict, why, alt = gate_photo(data, name, county)
        if verdict == "reject":
            public_path_to_file(url).unlink(missing_ok=True)
            factory_worker.log(f"media rejected {record['id']}: {why}")
            continue
        hero = _hero(record, candidate, url, verdict, why, alt)
        _index(hero, record)
        if verdict == "hero":
            return hero
        fallback = fallback or hero
    return fallback


def _hero(record, candidate, url, verdict, why, alt):
    where = ", ".join(x for x in (links.venue_name(record),
                                  (record.get("location") or {}).get("county", "")) if x)
    return {
        "url": url,
        "source": candidate.get("source", ""),
        "source_url": candidate.get("source_url", ""),
        "licence": candidate.get("licence", ""),
        "licence_url": candidate.get("licence_url", ""),
        "attribution": candidate.get("attribution", ""),
        "credit": credit_line(candidate),
        "alt": alt or f"Photo of {where}"[:120],
        "gate": verdict,
        "why": why,
        "placeholder": bool(candidate.get("placeholder")),
        "checked": now_iso(),
    }


def _index(hero, record):
    """One line per stored file in `media_index.json`: what it is, where it came
    from and under what licence. This is the file that proves nothing under
    `web/media/` came from a social network."""
    entry = {"path": hero["url"], "kind": record["kind"], "id": record["id"],
             "source": hero["source"], "source_url": hero["source_url"],
             "licence": hero["licence"], "gate": hero["gate"], "stored_at": hero["checked"]}
    with factory_worker.output_lock():
        index = factory_worker.load_json_store(MEDIA_INDEX, [])
        index = [row for row in index if row.get("path") != entry["path"]]
        index.append(entry)
        factory_worker.write_json_atomic(MEDIA_INDEX, index)


def is_provisional(hero):
    """A hero worth looking for a better one than: none at all, an unlicensed
    placeholder, or one no vision lane ever graded."""
    return not hero or bool(hero.get("placeholder")) or hero.get("gate") == "skip"


def _rank(hero):
    """How good a hero is, in the order that decides a replacement: licensed
    beats a placeholder, graded beats ungraded, and the gate's own HERO beats
    its PASS."""
    if not hero:
        return (0, 0, 0)
    return (0 if hero.get("placeholder") else 1,
            0 if hero.get("gate") == "skip" else 1,
            1 if hero.get("gate") == "hero" else 0)


def attach(record):
    """Give one record its embeds and, when we can license one, a hero photo.
    Mutates and returns the record; never raises."""
    media = record.setdefault("media", {"hero": None, "embeds": []})
    media["embeds"] = build_embeds(record)
    hero = media.get("hero")
    if is_provisional(hero):
        try:
            found = choose_hero(record)
        except Exception as error:
            factory_worker.log(f"media {record.get('id')}: {error}")
            found = None
        # `choose_hero` deletes a photo the gate rejects — and on a retry of an
        # ungraded one, that photo is the one this record is still pointing at.
        # Letting it stand would leave a hero URL that 404s.
        if hero and not public_path_to_file(hero.get("url", "")).exists():
            hero = media["hero"] = None
        if found and _rank(found) > _rank(hero):
            media["hero"] = found
    media["checked"] = now_iso()
    return record


# ---------------------------------------------------------------------------
# Nightly refresh
# ---------------------------------------------------------------------------

def needs_media(record):
    """On-air contract records with no hero yet, only a placeholder one, or one
    no vision lane ever graded -- `gate="skip"` is an outage to retry, which is
    the whole point of failing open instead of rejecting."""
    if record.get("schema_version") != 1 or record.get("status", "on-air") != "on-air":
        return False
    return is_provisional((record.get("media") or {}).get("hero"))


def refresh(limit=REFRESH_LIMIT):
    """Give the on-air records that still have no licensed photo another go.
    Returns `{kind: (looked_at, heroes_gained)}`."""
    summary = {}
    budget = limit
    for kind, store in factory_worker.STORE_FOR_KIND.items():
        looked, gained = 0, 0
        for record in factory_worker.load_json_store(store, []):
            if budget <= 0:
                break
            if not needs_media(record):
                continue
            budget -= 1
            looked += 1
            before = (record.get("media") or {}).get("hero")
            attach(record)
            after = (record.get("media") or {}).get("hero")
            if after and after is not before:
                gained += 1
            _save_media(store, record)
        summary[kind] = (looked, gained)
    return summary


def _save_media(store, record):
    with factory_worker.output_lock():
        records = factory_worker.load_json_store(store, [])
        for stored in records:
            if stored.get("id") == record.get("id"):
                stored["media"] = record["media"]
                factory_worker.write_json_atomic(store, records)
                return True
    return False


def main():
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else REFRESH_LIMIT
    if len(sys.argv) > 1 and sys.argv[1] == "refresh":
        for kind, (looked, gained) in refresh(limit).items():
            print(f"{kind}: {looked} checked, {gained} heroes")
        return
    print(__doc__)


if __name__ == "__main__":
    main()
