#!/usr/bin/env bash
# Stop the Honest Ear dev stack.
#   ./stop.sh           stop and remove containers (data is kept)
#   ./stop.sh --clean   also delete the Postgres/Redis volumes (asks first)
set -euo pipefail
cd "$(dirname "$0")"

case "${1:-}" in
  "")
    docker compose down --remove-orphans
    ;;
  --clean)
    printf 'Delete all local data (Postgres, Redis volumes)? [y/N] '
    read -r answer
    if [ "$answer" = "y" ] || [ "$answer" = "Y" ]; then
      docker compose down --remove-orphans --volumes
    else
      echo "Aborted."
      exit 1
    fi
    ;;
  -h|--help)
    sed -n '2,4p' "$0" | sed 's/^# \{0,1\}//'
    ;;
  *)
    echo "unknown option: $1 (try --help)" >&2
    exit 1
    ;;
esac
