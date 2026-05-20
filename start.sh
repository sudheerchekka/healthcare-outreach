#!/bin/sh
set -e

# Prefix each line of a process's output with a label
prefix() {
  label="$1"
  shift
  "$@" 2>&1 | while IFS= read -r line; do
    printf '[%s] %s\n' "$label" "$line"
  done
}

cd /app/src/tac && prefix TAC uvicorn server:app --host 0.0.0.0 --port 8000 &
prefix APP node /app/dist/index.js &

# Wait for either process to exit and propagate its exit code
wait -n 2>/dev/null || wait
