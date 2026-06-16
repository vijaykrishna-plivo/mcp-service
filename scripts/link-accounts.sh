#!/bin/bash
# Runs on the HOST after seed.sh. For every seeded app it creates the ACI
# app-configuration, then links an account for the tenant (OWNER_ID):
#
#   - api_key apps (CAL, SERPAPI): key injected directly from env vars
#     CAL_API_KEY / SERPAPI_API_KEY via POST /v1/linked-accounts/api-key.
#   - oauth2 apps: prints the browser URL from GET /v1/linked-accounts/oauth2.
#     Open it, complete consent, and ACI stores + auto-refreshes the tokens.
#     (This is the part that replaces nango-oauth.) Requires real client
#     credentials seeded via secrets/<app>.secrets.json.
#
# Usage: ACI_API_KEY=<key from seed.sh> ./scripts/link-accounts.sh
set -euo pipefail

ACI_URL="${ACI_URL:-http://localhost:8000}"
OWNER_ID="${OWNER_ID:-cx-tenant}"
REDIRECT="${AFTER_LINK_REDIRECT:-http://localhost:8000/v1/health}"

[ -n "${ACI_API_KEY:-}" ] || { echo "ERROR: set ACI_API_KEY (printed by seed.sh)"; exit 1; }

API_KEY_APPS="CAL SERPAPI"
OAUTH2_APPS="CALENDLY GITHUB GMAIL GOOGLE_CALENDAR GOOGLE_DOCS GOOGLE_SHEETS NOTION REDDIT SLACK X YOUTUBE ZOHO_DESK"

configure() {
  app="$1"; scheme="$2"
  curl -sf -X POST "$ACI_URL/v1/app-configurations" \
    -H "X-API-KEY: $ACI_API_KEY" -H "Content-Type: application/json" \
    -d "{\"app_name\":\"$app\",\"security_scheme\":\"$scheme\",\"all_functions_enabled\":true,\"enabled_functions\":[]}" \
    > /dev/null 2>&1 || true   # already exists — ok
}

link_api_key() {
  app="$1"; token="$2"
  # replace stale linked account if present
  existing=$(curl -s "$ACI_URL/v1/linked-accounts?app_name=$app&linked_account_owner_id=$OWNER_ID" \
    -H "X-API-KEY: $ACI_API_KEY" | python3 -c "import json,sys;d=json.load(sys.stdin);print(d[0]['id'] if d else '')" 2>/dev/null || echo "")
  [ -n "$existing" ] && curl -sf -X DELETE "$ACI_URL/v1/linked-accounts/$existing" -H "X-API-KEY: $ACI_API_KEY" > /dev/null
  curl -sf -X POST "$ACI_URL/v1/linked-accounts/api-key" \
    -H "X-API-KEY: $ACI_API_KEY" -H "Content-Type: application/json" \
    -d "{\"app_name\":\"$app\",\"linked_account_owner_id\":\"$OWNER_ID\",\"api_key\":\"$token\"}" > /dev/null
  echo "   linked (api_key)"
}

echo "=== api_key apps ==="
for app in $API_KEY_APPS; do
  var="${app}_API_KEY"
  token="${!var:-}"
  echo "== $app =="
  configure "$app" "api_key"
  if [ -n "$token" ]; then link_api_key "$app" "$token"; else echo "   skipped: set $var to link"; fi
done

echo
echo "=== oauth2 apps (open each URL in a browser to finish linking) ==="
for app in $OAUTH2_APPS; do
  echo "== $app =="
  configure "$app" "oauth2"
  url=$(curl -s "$ACI_URL/v1/linked-accounts/oauth2?app_name=$app&linked_account_owner_id=$OWNER_ID&after_oauth2_link_redirect_url=$REDIRECT" \
    -H "X-API-KEY: $ACI_API_KEY" | python3 -c "import json,sys;d=json.load(sys.stdin);print(d.get('url') or d)" 2>/dev/null || echo "   (failed — app seeded without client credentials?)")
  echo "   $url"
done

echo
echo "Done. Verify a link with:"
echo "  curl -s '$ACI_URL/v1/linked-accounts?linked_account_owner_id=$OWNER_ID' -H 'X-API-KEY: \$ACI_API_KEY'"
