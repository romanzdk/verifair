#!/usr/bin/env bash
set -euo pipefail

: "${VERIFAIR_API_TOKEN:?set VERIFAIR_API_TOKEN}"
url="${VERIFAIR_URL:-http://localhost:8000}"
template="$(dirname "$0")/../docs/report-template.md"

payload=$(python3 -c '
import json, sys
body = open(sys.argv[1]).read()
print(json.dumps({
    "project": "billing-service",
    "title": "Prorate mid-cycle plan changes",
    "risk": "high",
    "body": body,
    "ticket_url": "https://example.com/tickets/BILL-412",
    "author": "agent",
}))
' "$template")

curl -sS -X POST "$url/api/reports" \
  -H "Authorization: Bearer $VERIFAIR_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d "$payload"
echo
