#!/bin/sh
set -e
npm ci --no-audit --no-fund --loglevel=error
exec npx playwright test "$@"
