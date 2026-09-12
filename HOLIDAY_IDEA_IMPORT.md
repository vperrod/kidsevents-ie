# Holiday ideas: editorial import contract

Holiday ideas are evergreen places and routes, not dated events. Each idea
must retain its original source link and a place that can open in Google Maps.

## TikTok collection handoff

TikTok collections are private to the account that saved them, so they are read
on the mini PC by the OpenCLI collector that owns the authenticated Chrome
profile. It pipes discovered posts into `staging.py append` on this VM, which
only stages them for review — see `SOCIAL_COLLECTION_ACTIVATION.md`. Do not
share account cookies or passwords. A curator should verify practical details
on the destination's own website before publishing an entry.

```json
[
  {
    "source_url": "https://www.tiktok.com/@creator/video/123",
    "title": "Optional working title",
    "notes": "Why it could suit a family"
  }
]
```

## Publishable fields

- title, description and category
- region, county and full place name
- latitude/longitude when verified, otherwise a map-search label
- price range and age guidance, explicitly marked `check` when the original
  source does not establish them
- original source URL and, where different, the organiser booking URL

Never turn a social caption into a factual listing without checking the
destination or organiser page.
