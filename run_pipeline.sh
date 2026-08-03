#!/bin/bash
# Runner script for Kids Events Ireland scraper pipeline
set -e

cd /home/azureuser/kidsevents-ie
source venv/bin/activate

# Run the scraper with Tier 1 + 3 (Facebook requires Playwright, Instagram needs INSTAGRAM_SESSIONID)
# To enable Facebook: ensure chromium is installed via playwright
# To enable Instagram: export INSTAGRAM_SESSIONID="your_cookie_here"
python3 main.py --tiers "1" --limit 15 2>&1

echo "Pipeline complete at $(date)"
