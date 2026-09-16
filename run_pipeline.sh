#!/bin/bash
# Runner script for the Kids Events Ireland scraper pipeline (Small Days factory).
# The pipeline entry point is `python3 factory_worker.py` -> run_discovery_cycle(),
# scheduled hourly by the kidsevents-factory systemd timer. One cycle per run; a
# second concurrent run skips automatically via daemon.lock (flock LOCK_EX|LOCK_NB).
set -e

cd "$(dirname "$0")"
source venv/bin/activate

python3 factory_worker.py "$@"

echo "Pipeline complete at $(date)"
