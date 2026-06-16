# OAuth2 client credentials (seed-time secrets)

Each oauth2 app's `app.json` contains `{{ PLACEHOLDER }}` template variables for
the OAuth client id/secret. To enable real linking for an app:

1. Register an OAuth app with the provider (Google Cloud Console, Slack app,
   GitHub OAuth app, ...). Redirect URI must point at the ACI server's
   callback: `<ACI_URL>/v1/linked-accounts/oauth2/callback`.
2. Copy `<app>.secrets.json.example` → `<app>.secrets.json` and fill in the
   values.
3. Re-run `scripts/seed.sh` (upsert is idempotent — it re-renders the app with
   the secrets).

`*.secrets.json` files are gitignored. Apps seeded WITHOUT a secrets file
still work for everything except the OAuth2 link flow (definitions, search,
api-key linking are unaffected).
