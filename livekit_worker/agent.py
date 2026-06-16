"""
Standalone LiveKit Agents worker that uses the self-hosted ACI MCP bridge as its
tool source — the end-to-end test rig for aci-mcp-server.

Flow:
    you (voice) ─▶ LiveKit Agents (this worker)
                      │  mcp_servers=[ MCPServerHTTP ]
                      ▼
                   aci-mcp bridge  (POST /<app>/<owner_id>, Bearer auth)
                      ▼
                   ACI backend  (resolves the linked account's OAuth token)
                      ▼
                   Google Calendar API

Run modes (livekit CLI subcommands, provided by cli.run_app):
    python agent.py console   # talk via your laptop mic/speaker — NO LiveKit server/tunnel needed
    python agent.py dev       # connect to LIVEKIT_URL; join via the Agents Playground
    python agent.py connect --room <name>   # join a specific room

Config comes from livekit_worker/.env (see .env.example).
"""

import logging
import os

from dotenv import load_dotenv
from livekit.agents import Agent, AgentSession, JobContext, WorkerOptions, cli, mcp
from livekit.plugins import openai, silero

load_dotenv()
# also pull voice-provider keys (Deepgram / Azure OpenAI / ElevenLabs) from the CX
# agent's own .env, so we reuse them without ever copying the secrets out.
_cx = os.environ.get("CX_ENV_PATH")
if _cx and os.path.exists(_cx):
    load_dotenv(_cx)  # override=False -> our .env still wins for overlapping keys

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("aci-livekit-worker")

ACI_BRIDGE_URL = os.environ.get("ACI_BRIDGE_URL", "http://localhost:8101")
ACI_APP = os.environ.get("ACI_APP", "google_calendar")
ACI_OWNER_ID = os.environ.get("ACI_OWNER_ID", "cx-tenant")
BRIDGE_SECRET_KEY = os.environ.get("BRIDGE_SECRET_KEY", "")
VOICE_MODE = os.environ.get("VOICE_MODE", "realtime")  # realtime | pipeline

INSTRUCTIONS = (
    "You are a helpful voice assistant used to test a Google Calendar integration. "
    "You have tools whose names start with GOOGLE_CALENDAR__ . Use them to list the "
    "user's calendars, look up upcoming events, and create or find events. "
    "When the user asks about their calendar, CALL the relevant tool rather than "
    "guessing, then summarize the result in one or two natural sentences. "
    "Always confirm the details out loud before creating or changing an event. "
    "Keep replies short and conversational — this is spoken aloud."
)


def build_mcp_server() -> mcp.MCPServerHTTP:
    """The ACI bridge endpoint for one app + tenant, with bearer auth."""
    url = f"{ACI_BRIDGE_URL.rstrip('/')}/{ACI_APP}/{ACI_OWNER_ID}"
    headers = {"Authorization": f"Bearer {BRIDGE_SECRET_KEY}"} if BRIDGE_SECRET_KEY else None
    logger.info("ACI MCP server: %s (bearer auth: %s)", url, "on" if headers else "off")
    return mcp.MCPServerHTTP(
        url=url,
        transport_type="streamable_http",
        headers=headers,
        timeout=20,
        client_session_timeout_seconds=30,
    )


def build_session() -> AgentSession:
    if VOICE_MODE == "pipeline":
        # Discrete STT → LLM → TTS, reusing the CX stack's providers.
        from livekit.plugins import deepgram, elevenlabs

        az_ep = os.environ.get("AZURE_OPENAI_ENDPOINT")
        az_key = os.environ.get("AZURE_OPENAI_API_KEY")
        az_dep = os.environ.get("AZURE_OPENAI_LLM_DEPLOYMENT") or os.environ.get("AZURE_OPENAI_SCRIBE_DEPLOYMENT") or "gpt-4o"
        az_ver = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")
        if az_ep and az_key:
            logger.info("LLM: Azure OpenAI (deployment=%s)", az_dep)
            llm = openai.LLM.with_azure(azure_endpoint=az_ep, azure_deployment=az_dep, api_key=az_key, api_version=az_ver)
        else:
            llm = openai.LLM(model=os.environ.get("OPENAI_MODEL", "gpt-4o"))

        stt_kw = {}
        if os.environ.get("DEEPGRAM_API_KEY"):
            stt_kw["api_key"] = os.environ["DEEPGRAM_API_KEY"]
        tts_kw = {}
        if os.environ.get("ELEVENLABS_API_KEY"):
            tts_kw["api_key"] = os.environ["ELEVENLABS_API_KEY"]

        logger.info("voice mode: pipeline (deepgram STT / azure-or-openai LLM / elevenlabs TTS)")
        return AgentSession(
            stt=deepgram.STT(**stt_kw),
            llm=llm,
            tts=elevenlabs.TTS(**tts_kw),
            vad=silero.VAD.load(),
        )

    # Default: OpenAI Realtime — one OPENAI_API_KEY, speech-to-speech, native tool calls.
    logger.info("voice mode: realtime (openai speech-to-speech)")
    return AgentSession(
        llm=openai.realtime.RealtimeModel(voice=os.environ.get("OPENAI_REALTIME_VOICE", "alloy")),
    )


async def entrypoint(ctx: JobContext):
    import datetime
    tz_name = os.environ.get("TZ_NAME", "Asia/Kolkata")
    try:
        from zoneinfo import ZoneInfo
        now = datetime.datetime.now(ZoneInfo(tz_name))
    except Exception:
        now = datetime.datetime.now().astimezone()
    ctx_block = (
        f"CURRENT CONTEXT — today is {now:%A, %B %d, %Y}; local time {now:%H:%M} ({tz_name}). "
        f"Resolve relative dates ('today', 'tomorrow', 'next week') against THIS date — never assume any other year. "
        f"For any calendar event ALWAYS send start and end as objects with BOTH a 'dateTime' (RFC3339) AND "
        f"'timeZone': '{tz_name}'. Only tell the user something is booked/updated if the tool result actually succeeded."
    )
    logger.info("session context: today=%s tz=%s", now.date(), tz_name)
    session = build_session()
    agent = Agent(instructions=ctx_block + "\n\n" + INSTRUCTIONS, mcp_servers=[build_mcp_server()])
    await session.start(room=ctx.room, agent=agent)
    await ctx.connect()
    await session.generate_reply(
        instructions="Greet the user in one sentence and offer to help with their Google Calendar."
    )


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))
