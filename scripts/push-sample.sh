#!/usr/bin/env bash
set -euo pipefail

: "${VERIFAIR_API_TOKEN:?set VERIFAIR_API_TOKEN}"
url="${VERIFAIR_URL:-http://localhost:8000}"
template="$(dirname "$0")/../docs/report-template.md"

case "$url" in
  http://localhost|http://localhost:*|http://127.0.0.1|http://127.0.0.1:*) ;;
  *)
    if [ "${VERIFAIR_ALLOW_REMOTE:-}" != "1" ]; then
      echo "refusing to push the sample to $url; set VERIFAIR_ALLOW_REMOTE=1 to allow it" >&2
      exit 1
    fi
    ;;
esac

payload=$(python3 -c '
import json, sys
body = open(sys.argv[1]).read()
print(json.dumps({
    "id": "sample-prorate-mid-cycle",
    "project": "billing-service",
    "title": "Prorate mid-cycle plan changes",
    "risk": "high",
    "body": body,
    "ticket_url": "https://example.com/tickets/BILL-412",
    "author": "agent",
}))
' "$template")

curl -sS --fail-with-body -X POST "$url/api/reports" \
  -H "Authorization: Bearer $VERIFAIR_API_TOKEN" \
  -H "Content-Type: application/json" \
  -w '\nHTTP %{http_code}\n' \
  -d "$payload"
