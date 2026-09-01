#!/usr/bin/env bash
#
# Pull the latest changes and rebuild/recreate the Docker container.
#
# Run on the Pi from anywhere; it cd's into the project itself.
#
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

git pull
docker compose build
docker compose up -d
