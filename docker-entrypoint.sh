#!/bin/sh
set -e

# the articles, the Studio's settings, keys, prompts, caches and the job list all live on the
# volume mounted at project/output; the image carries none of them
mkdir -p /app/project/output

# one worker: the job queue and the article locks are per-process by design
exec gunicorn ui.app:app \
  --bind "${UI_HOST:-0.0.0.0}:${UI_PORT:-5050}" \
  --workers 1 --threads 8 --timeout 600 \
  --access-logfile - --error-logfile -
