#!/bin/sh
# Claude-Code-Hook: leitet das Ereignis (JSON auf stdin) an den Werkstadt-Collector weiter.
# Blockiert nie: kurze Zeitgrenze, Fehler werden verschluckt.
DIR=$(cd "$(dirname "$0")/.." && pwd)
PORT=$(sed -n 's/.*"port": *\([0-9][0-9]*\).*/\1/p' "$DIR/werkstadt.local.json" 2>/dev/null | head -1)
curl -s -m 1 -X POST -H 'Content-Type: application/json' --data-binary @- \
  "http://127.0.0.1:${WERKSTADT_PORT:-${PORT:-7777}}/event" >/dev/null 2>&1 || true
exit 0
