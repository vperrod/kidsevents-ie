"""Safe public links derived from an event record."""

from urllib.parse import urlencode, urlparse


def public_source_url(event):
    """Return the first safe original-listing URL, or an empty string."""
    candidates = [event.get("url", ""), *(event.get("all_urls") or [])]
    for candidate in candidates:
        if not isinstance(candidate, str):
            continue
        parsed = urlparse(candidate.strip())
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            return candidate.strip()
    return ""


def google_maps_url(event):
    """Return a Google Maps search/directions URL from the best known location."""
    latitude = str(event.get("latitude") or "").strip()
    longitude = str(event.get("longitude") or "").strip()
    if latitude and longitude:
        query = f"{latitude},{longitude}"
    else:
        location = [
            event.get("venue_name"),
            event.get("venue_address"),
            event.get("city"),
            event.get("county"),
            event.get("country") or "Ireland",
        ]
        query = ", ".join(str(part).strip() for part in location if str(part or "").strip())
        if not query:
            query = str(event.get("title") or "Family event in Ireland").strip()
    return "https://www.google.com/maps/search/?" + urlencode({"api": "1", "query": query})
