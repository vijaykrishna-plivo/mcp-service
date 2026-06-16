"""
Pre-flight: connect to the ACI bridge with the SAME MCP client the worker uses
(livekit.agents.mcp.MCPServerHTTP), run the initialize handshake, and list the
tools the agent would see. No voice, no LLM key required.

    python check_mcp.py            # uses ACI_APP from .env (default google_calendar)
    python check_mcp.py serpapi    # override the app to smoke-test
"""

import asyncio
import os
import sys

from dotenv import load_dotenv
from livekit.agents import mcp

load_dotenv()

ACI_BRIDGE_URL = os.environ.get("ACI_BRIDGE_URL", "http://localhost:8101")
ACI_OWNER_ID = os.environ.get("ACI_OWNER_ID", "cx-tenant")
BRIDGE_SECRET_KEY = os.environ.get("BRIDGE_SECRET_KEY", "")
ACI_APP = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("ACI_APP", "google_calendar")


async def main() -> int:
    url = f"{ACI_BRIDGE_URL.rstrip('/')}/{ACI_APP}/{ACI_OWNER_ID}"
    headers = {"Authorization": f"Bearer {BRIDGE_SECRET_KEY}"} if BRIDGE_SECRET_KEY else None
    print(f"→ connecting MCP client to {url} (bearer: {'on' if headers else 'off'})")
    server = mcp.MCPServerHTTP(
        url=url,
        transport_type="streamable_http",
        headers=headers,
        timeout=20,
        client_session_timeout_seconds=30,
    )
    try:
        await server.initialize()
        tools = await server.list_tools()
        print(f"✓ initialize OK — {len(tools)} tool(s) exposed to the agent:")
        for t in tools:
            name = getattr(t, "name", None) or getattr(getattr(t, "tool", None), "name", t)
            print(f"   - {name}")
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"✗ FAILED: {type(e).__name__}: {e}")
        return 1
    finally:
        await server.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
