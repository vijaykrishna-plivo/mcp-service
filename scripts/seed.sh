#!/bin/sh
# Runs INSIDE the runner container:
#   docker compose -f <aci>/backend/compose.yml -f compose.override.yml \
#     exec runner sh /workdir/cx-scripts/seed.sh
#
# Upserts EVERY app in /workdir/cx-apps (stock ACI definitions, thin tools,
# no business logic), seeds plans + a default project/agent, and prints the
# agent API key to use as X-API-KEY.
#
# OAuth2 client credentials: app.json files contain {{ PLACEHOLDER }} template
# vars. If /workdir/cx-secrets/<app>.secrets.json exists it is passed as
# --secrets-file. Without it the app still seeds (placeholders render empty)
# but the OAuth2 link flow for that app will fail until you re-seed with real
# client credentials.
set -eu

cd /workdir

for dir in cx-apps/*/; do
  app=$(basename "$dir")
  secrets="cx-secrets/$app.secrets.json"
  echo "== upserting app: $app =="
  if [ -f "$secrets" ]; then
    python -m aci.cli upsert-app --app-file "$dir/app.json" --secrets-file "$secrets" --skip-dry-run
  else
    python -m aci.cli upsert-app --app-file "$dir/app.json" --skip-dry-run
  fi
  python -m aci.cli upsert-functions --functions-file "$dir/functions.json" --skip-dry-run
done

echo "== seeding plans + default project/agent (prints the agent API key) =="
python -m aci.cli populate-subscription-plans --skip-dry-run
python -m aci.cli create-random-api-key --visibility-access public --org-id 107e06da-e857-4864-bc1d-4adcba02ab76
