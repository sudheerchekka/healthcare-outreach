#!/bin/sh
set -e

cd /app/src/tac && uvicorn server:app --host 0.0.0.0 --port 8000 &

exec node /app/dist/index.js
