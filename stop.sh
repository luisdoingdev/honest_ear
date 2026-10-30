#!/bin/sh
# Stops the local Docker Compose stack and removes its containers.
# --reset also deletes the volumes (Postgres data, Redis data, e2e node_modules) for a completely fresh start.
set -e
cd "$(dirname "$0")"

if [ "$1" = "--reset" ]; then
  docker compose --profile test down --volumes --remove-orphans
  echo "Stopped and wiped local data. ./dev.sh starts from an empty database."
else
  docker compose --profile test down --remove-orphans
  echo "Stopped. Data is kept; ./stop.sh --reset wipes it."
fi
