# Activate saved-collection ingestion

Instagram and TikTok only serve saved collections and tag results to a browser
that is already signed in as the account owner. This VM has no such session —
its datacentre IP is blocked outright — so nothing social is collected here.

## Where each half runs

**Mini PC (`vperrod@100.105.72.86`)** — collection reading and tag search.
The collector drives the already-connected OpenCLI / `agent-reach-chrome`
daemon and its authenticated Chrome profile (snap profile
`~/snap/chromium/common/agent-reach-profile`, display `xvfb99`). It enumerates
the saved collection's post permalinks and tag-search hits, and records which
collection or tag surfaced each one as `found_via`.

**This VM (`kidsevents-ie`)** — staging, review and publishing. It never opens
a social page. New candidates arrive over SSH:

```bash
ssh azureuser@claude-dev-vperrod.westeurope.cloudapp.azure.com \
  "cd /home/azureuser/kidsevents-ie && venv/bin/python3 staging.py append"
```

with a JSON array on stdin, one object per post:

```json
[
  {
    "platform": "instagram",
    "source_url": "https://www.instagram.com/p/XXXXXXXX/",
    "caption": "Halloween trail, Marlay Park, 26 October, free",
    "author": "dlrcoco",
    "found_via": "Kids collection"
  }
]
```

`staging.py append` de-duplicates by `source_url`, stamps `captured_at` and
`status: needs_review`, writes `staged/social_candidates.json`, and prints
`Staged N new social candidates.` Nothing it writes is public.

## Review and publishing

The admin dashboard's **Social** view lists every candidate still marked
`needs_review`, with its `found_via` counts.

- **Approve** runs the caption through the same Hermes enrichment the factory
  uses, normalises the result to the public event contract, and appends it to
  `events_output.json`. If no usable date comes out, the candidate stays staged
  and the dashboard says it still needs manual information.
- **Reject** marks the candidate `rejected`. Candidates are never deleted, so
  the audit trail survives.

A curator should still verify destination, dates, age guidance and price on the
organiser's own website before approving.

## Remaining manual step

Victor needs to supply the real **Kids** saved-collection URL (and the
Instagram equivalent if that collection is used too) so the mini-PC collector
knows what to open. Configure it there, not on this VM. Do not send passwords
or cookie values through chat.
