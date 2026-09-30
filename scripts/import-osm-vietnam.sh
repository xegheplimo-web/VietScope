#!/usr/bin/env bash
# P14 — import the OSM Vietnam extract into hub-postgres as osm_pois.
#
# Downloads the Geofabrik extract into ./.osm-data/ (skipped when the file
# already exists — delete it to refresh), then runs the profile-gated
# `osm-import` compose service: osm2pgsql --output=flex with
# deploy/osm/pois.lua. The import drops and rebuilds osm_pois
# (--create), so re-running is safe and idempotent.
#
#   scripts/import-osm-vietnam.sh                # Geofabrik vietnam-latest
#   PBF_FILE=/abs/path/custom.osm.pbf scripts/import-osm-vietnam.sh
set -euo pipefail
cd "$(dirname "$0")/.."

PBF_URL=${PBF_URL:-https://download.geofabrik.de/asia/vietnam-latest.osm.pbf}
PBF_FILE=${PBF_FILE:-vietnam-latest.osm.pbf}
mkdir -p .osm-data

if [ ! -s ".osm-data/$(basename "$PBF_FILE")" ]; then
    echo "downloading $PBF_URL -> .osm-data/$(basename "$PBF_FILE")"
    curl -fL --retry 3 -o ".osm-data/$(basename "$PBF_FILE")" "$PBF_URL"
fi

PBF_FILE="/data/$(basename "$PBF_FILE")" \
    docker compose --profile import run --rm osm-import
