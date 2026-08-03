#!/bin/bash
# Runner script for Kids Events Ireland scraper pipeline
set -e

cd "$(dirname "$0")"
source venv/bin/activate

# Run scraper with Tier 1 + Instagram (when sessionid is available)
# Tier 2 (Facebook) requires Playwright/chromium — can add with --tiers "1,2,3"
# Instagram will be skipped automatically if INSTAGRAM_SESSIONID is not set
python3 main.py --tiers "1,3" --limit 20 --output events_output.json 2>&1

echo "Pipeline complete at $(date)"
