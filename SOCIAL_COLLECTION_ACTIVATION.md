# Activate saved-collection ingestion

This uses the same architecture as WanderTold: an account owner's authenticated
Chrome profile is connected to OpenCLI; the collector reads the private saved
collection through the rendered browser page and stages, rather than publishes,
the post links.

## One-time browser connection

On the VM's interactive Chrome profile:

1. Install and enable the OpenCLI Browser Bridge extension.
2. Sign in to TikTok as the owner of the `Kids` collection.
3. Open the `Kids` collection and copy its full collection URL.
4. Confirm `opencli profile list` shows a connected profile on the VM.

Do not send passwords or cookie values through chat.

## Configure and activate

Add these values to the protected `/home/azureuser/kidsevents-ie/.env`:

```ini
TIKTOK_COLLECTION_URL=https://www.tiktok.com/@account/collection/Kids-opaque-id
SOCIAL_COLLECTION_LIMIT=50
```

Then register the staged, hourly one-shot job:

```bash
systemctl --user link /home/azureuser/kidsevents-ie/systemd/kidsevents-social-ingest.service
systemctl --user link /home/azureuser/kidsevents-ie/systemd/kidsevents-social-ingest.timer
systemctl --user enable --now kidsevents-social-ingest.timer
systemctl --user start kidsevents-social-ingest.service
```

Successful runs append only new post URLs to `staged/social_candidates.json`
with `needs_review` status. A source or organiser page must verify destination,
dates, price and age information before any candidate enters `events_output.json`
or `holidays_output.json`.
