#!/bin/bash
# Starts the MCP server the CX platform will talk to — this process is the
# drop-in replacement for agent-mcp's MCP endpoint. It is ACI's own
# `aci-mcp` client (pip/uvx package) pointed at the self-hosted backend;
# every tool call becomes POST /v1/functions/<NAME>/execute with the
# tenant's linked account.
#
# Usage: ACI_API_KEY=<key> ./scripts/run-mcp.sh [owner-id] [port]
#
# Two modes (MCP_MODE env):
#   apps    (default) — flat tools/list of all 188 thin tools across 14 apps.
#                       NOTE: >40 tools degrades model accuracy; fine for
#                       testing, consider subsets per agent in production.
#   unified           — ACI's 2 meta-tools (search + execute, progressive
#                       discovery). Same shape as the catalog facade plan.
set -euo pipefail

[ -n "${ACI_API_KEY:-}" ] || { echo "ERROR: set ACI_API_KEY (printed by seed.sh)"; exit 1; }

OWNER_ID="${1:-${OWNER_ID:-cx-tenant}}"
PORT="${2:-${PORT:-8100}}"
export ACI_SERVER_URL="${ACI_SERVER_URL:-http://localhost:8000/v1}"

APPS="CAL,CALENDLY,GITHUB,GMAIL,GOOGLE_CALENDAR,GOOGLE_DOCS,GOOGLE_SHEETS,NOTION,REDDIT,SERPAPI,SLACK,X,YOUTUBE,ZOHO_DESK"

# Prefer the local clone if present (known to honor ACI_SERVER_URL); fall back to uvx.
ACI_MCP_DIR="${ACI_MCP_DIR:-$(cd "$(dirname "$0")/.." && pwd)/../aci-pilot/aci-mcp}"
if [ -d "$ACI_MCP_DIR" ]; then
  RUN=(uv run --project "$ACI_MCP_DIR" aci-mcp)
else
  RUN=(uvx aci-mcp)
fi

if [ "${MCP_MODE:-apps}" = "unified" ]; then
  exec "${RUN[@]}" unified-server \
    --linked-account-owner-id "$OWNER_ID" --transport sse --port "$PORT"
else
  exec "${RUN[@]}" apps-server --apps "$APPS" \
    --linked-account-owner-id "$OWNER_ID" --transport sse --port "$PORT"
fi
