#!/usr/bin/env bash
#
# Stop the running container, pull the latest changes, then rebuild and
# restart it.
#
# Run on the Pi from anywhere; it cd's into the project itself.
#
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

docker compose stop
git pull
docker compose build
docker compose up -d
