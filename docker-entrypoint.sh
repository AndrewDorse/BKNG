#!/bin/sh
set -eu

mkdir -p /app/data /app/logs /models /state

if [ "$(id -u)" = "0" ]; then
    chown -R appuser:appuser /app/data /app/logs /models /state 2>/dev/null || true
    exec gosu appuser "$@"
fi

exec "$@"
