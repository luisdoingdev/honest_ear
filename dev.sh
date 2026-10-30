#!/bin/sh
# Local entrypoint: builds and starts the Docker Compose stack in the background and waits until it's healthy.
set -e
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  echo "Missing .env. Create it with: cp .env.example .env" >&2
  exit 1
fi

docker compose up -d --build --wait
echo "Honest Ear is running at http://localhost:8000"
echo "Logs: docker compose logs -f    Stop: ./stop.sh    Fresh start: ./stop.sh --reset && ./dev.sh"
