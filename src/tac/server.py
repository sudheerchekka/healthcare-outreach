"""
Owl Health Python TAC Server.

Bridges Twilio ConversationRelay to AgentCore via persistent WebSocket.
Uses AgentCoreRuntimeClient.generate_presigned_url() for per-session WebSocket
pooling — eliminating per-turn HTTP overhead from the Node.js implementation.

Endpoints:
  POST /twiml                       — TwiML for inbound calls
  POST /twiml-outbound              — TwiML for outbound calls (personalized greeting)
  WS   /ws                          — ConversationRelay WebSocket
  POST /conversation-relay-callback — Call-end webhook
  POST /set-outbound-context        — IPC: Node.js app server stores pending ctx before dial
  GET  /get-outbound-phone/{conv_id} — IPC: Node.js CI webhook looks up member phone
  GET  /health                      — Health check
"""

# Patch importlib.metadata.version before any tac imports.
# The TAC SDK calls version("tac") but the package is named "twilio-agent-connect".
import importlib.metadata as _meta
_orig_version = _meta.version
def _patched_version(name: str) -> str:
    try:
        return _orig_version(name)
    except _meta.PackageNotFoundError:
        if name == "tac":
            return _orig_version("twilio-agent-connect")
        raise
_meta.version = _patched_version  # type: ignore[assignment]

import asyncio
import json
import logging
import os
import time
from collections.abc import AsyncGenerator
from typing import Optional

import requests
import websockets
from dotenv import load_dotenv
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import Response

from bedrock_agentcore.runtime import AgentCoreRuntimeClient
from tac import TAC, TACConfig
from tac.channels.voice import VoiceChannel
from tac.models.session import AuthorInfo, ConversationSession
from tac.models.tac import TACMemoryResponse
from tac.models.voice import SetupMessage
from tac.server import FastAPIWebSocketAdapter

load_dotenv()

logger = logging.getLogger(__name__)
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

AGENTCORE_RUNTIME_ARN = os.environ.get("VOICE_CONCIERGE_RUNTIME_ARN") or ""
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
PUBLIC_DOMAIN = (os.environ.get("VOICE_PUBLIC_DOMAIN") or "").lstrip("https://").lstrip("http://")
TAC_PORT = int(os.environ.get("TAC_PORT", "8000"))
APP_PORT = int(os.environ.get("APP_PORT", "8001"))

# Local dev: set AGENT_LOCAL_URL=http://localhost:8080 to bypass presigned URL and
# connect directly to agentcore dev server. Unset (or empty) = use deployed AgentCore.
AGENT_LOCAL_URL = os.environ.get("AGENT_LOCAL_URL", "").rstrip("/")
AGENT_LOCAL_WS_URL = AGENT_LOCAL_URL.replace("http://", "ws://").replace("https://", "wss://") + "/ws" if AGENT_LOCAL_URL else ""

MEMORY_BASE = "https://memory.twilio.com"
MEMORY_STORE_ID = os.environ.get("MEMORY_STORE_ID", "")
MEMORY_API_KEY = os.environ.get("TWILIO_API_KEY", "")
MEMORY_API_TOKEN = os.environ.get("TWILIO_API_TOKEN", "")

# AgentCore presigned-URL WebSocket client
agentcore_client: Optional[AgentCoreRuntimeClient] = None
if AGENTCORE_RUNTIME_ARN:
    agentcore_client = AgentCoreRuntimeClient(region=AWS_REGION)
else:
    logger.warning("VOICE_CONCIERGE_RUNTIME_ARN not set — AgentCore calls will fail")

# TAC + VoiceChannel (memory retrieval disabled; we fetch it ourselves)
tac = TAC(config=TACConfig.from_env())

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

agent_connections: dict[str, websockets.ClientConnection] = {}
pending_outbound_context: dict[str, dict] = {}
outbound_conversation_map: dict[str, str] = {}
system_prompt_cache: dict[str, str] = {}
greeting_cache: dict[str, str] = {}
memory_context_cache: dict[str, str] = {}

# ---------------------------------------------------------------------------
# TAC Memory helpers
# ---------------------------------------------------------------------------

def _lookup_profile_id(phone: str) -> Optional[str]:
    try:
        res = requests.post(
            f"{MEMORY_BASE}/v1/Stores/{MEMORY_STORE_ID}/Profiles/Lookup",
            json={"idType": "phone", "value": phone},
            auth=(MEMORY_API_KEY, MEMORY_API_TOKEN),
            timeout=5,
        )
        profiles = res.json().get("profiles", [])
        return profiles[0] if profiles else None
    except Exception as e:
        logger.warning(f"[memory] lookupProfileId failed: {e}")
        return None


def _fetch_memory(profile_id: str) -> Optional[TACMemoryResponse]:
    try:
        base = f"{MEMORY_BASE}/v1/Stores/{MEMORY_STORE_ID}/Profiles/{profile_id}"
        auth = (MEMORY_API_KEY, MEMORY_API_TOKEN)
        obs_res = requests.get(f"{base}/Observations", auth=auth, timeout=5)
        sum_res = requests.get(f"{base}/ConversationSummaries", auth=auth, timeout=5)
        observations = [type("Obs", (), {"content": o["content"]})() for o in obs_res.json().get("observations", [])]
        summaries = [type("Sum", (), {"content": s["content"]})() for s in sum_res.json().get("summaries", [])]
        return TACMemoryResponse(observations=observations, summaries=summaries)
    except Exception as e:
        logger.warning(f"[memory] fetchMemory failed: {e}")
        return None


async def _prefetch_memory(phone: str) -> Optional[TACMemoryResponse]:
    """Run blocking memory fetch in thread pool so it doesn't block the event loop."""
    loop = asyncio.get_event_loop()
    t0 = time.time()
    try:
        profile_id = await loop.run_in_executor(None, _lookup_profile_id, phone)
        memory = await loop.run_in_executor(None, _fetch_memory, profile_id) if profile_id else None
        obs = len(memory.observations) if memory else 0
        sums = len(memory.summaries) if memory else 0
        logger.info(f"[memory] prefetch phone={phone} profileId={profile_id or 'none'} obs={obs} summaries={sums} in {(time.time()-t0)*1000:.0f}ms")
        return memory
    except Exception as e:
        logger.warning(f"[memory] prefetch error: {e}")
        return None


def _build_memory_context(memory: Optional[TACMemoryResponse]) -> str:
    if not memory:
        return ""
    sections = []
    if memory.observations:
        lines = [f"- {o.content}" for o in memory.observations]
        sections.append("### Previous Observations\n" + "\n".join(lines))
    if memory.summaries:
        lines = [f"- {s.content}" for s in memory.summaries]
        sections.append("### Previous Summaries\n" + "\n".join(lines))
    if not sections:
        return ""
    return "The following is from previous interactions with this member.\n\n" + "\n\n".join(sections)


def _build_system_prompt(name: str, goal: str, goal_desc: str) -> str:
    lines = [
        f"You are calling {name} on behalf of the Owl Health care team.",
        f"The purpose of this call is: {goal}." if goal else "",
        f"Follow-up guidance: {goal_desc}" if goal_desc else "",
        "Be warm, concise, and natural — no bullet points, no bold text.",
        "The member has already been greeted. Do not re-introduce yourself.",
        "Steer toward a clear next step.",
    ]
    return "\n".join(l for l in lines if l)


def _build_inbound_system_prompt() -> str:
    return (
        "You are an Owl Health care coordination agent handling an inbound member call. "
        "Be warm and conversational. Speak in plain, natural sentences — no bullet points, no bold or italic text. "
        "Keep responses brief and easy to follow on a phone call. "
        "Identify how you can help and guide the member toward a clear next step."
    )

# ---------------------------------------------------------------------------
# AgentCore WebSocket pool
# ---------------------------------------------------------------------------

async def get_or_create_agent_ws(session_id: str) -> Optional[websockets.ClientConnection]:
    if session_id in agent_connections:
        ws = agent_connections[session_id]
        if ws.state.name == "OPEN":
            return ws
        del agent_connections[session_id]

    try:
        t0 = time.time()
        if AGENT_LOCAL_WS_URL:
            url = AGENT_LOCAL_WS_URL
            logger.info(f"[agentcore] local dev connecting to {url}")
        else:
            if not agentcore_client:
                logger.error("[agentcore] no client and AGENT_LOCAL_URL not set")
                return None
            url = agentcore_client.generate_presigned_url(
                runtime_arn=AGENTCORE_RUNTIME_ARN,
                session_id=session_id,
            )
        ws = await websockets.connect(url)
        agent_connections[session_id] = ws
        logger.info(f"[agentcore] connected session_id={session_id} in {(time.time()-t0)*1000:.0f}ms")
        return ws
    except Exception as e:
        logger.error(f"[agentcore] connection failed: {e}")
        return None


async def _prewarm(conv_id: str, session_id: str, phone: str) -> None:
    """Pre-warm AgentCore WebSocket and fetch memory before the member speaks."""
    t0 = time.time()
    ws_task = asyncio.create_task(get_or_create_agent_ws(session_id))
    mem_task = asyncio.create_task(_prefetch_memory(phone)) if phone else None

    ws = await ws_task
    memory = await mem_task if mem_task else None

    if memory:
        memory_context_cache[conv_id] = _build_memory_context(memory)

    logger.info(f"[prewarm] done conv_id={conv_id} ws={'ok' if ws else 'FAILED'} memory={'ok' if memory else 'none'} in {(time.time()-t0)*1000:.0f}ms")


class OwlVoiceChannel(VoiceChannel):
    """VoiceChannel subclass that always populates author_info from setup message,
    maps our outbound conv_id to the Maestro conversation_id, and pre-warms the
    AgentCore WebSocket + memory fetch before the member speaks (turn-1 latency fix)."""

    def _handle_setup(self, message: SetupMessage) -> None:
        super()._handle_setup(message)
        conv_id = message.custom_parameters.conversation_id

        # Always populate author_info (caller phone) and ai_agent_info (our number).
        # The base class only does this when enable_voice_active_hydration=True.
        if message.from_number and conv_id in self._conversations:
            self._conversations[conv_id].author_info = AuthorInfo(
                address=message.from_number,
                participant_id=message.custom_parameters.customer_participant_id,
            )
        if message.to_number and conv_id in self._conversations:
            self._conversations[conv_id].ai_agent_info = AuthorInfo(
                address=message.to_number,
                participant_id=message.custom_parameters.ai_agent_participant_id,
            )

        # Outbound calls: our app server stores context under a key it generates
        # and embeds that key as customParameters.outboundConvId in the TwiML.
        # Map it here so handle_message_ready can pop it with the Maestro conv_id.
        extra = message.custom_parameters.model_extra or {}
        outbound_conv_id = extra.get("outboundConvId", "")
        if outbound_conv_id and outbound_conv_id in pending_outbound_context:
            pending_outbound_context[conv_id] = pending_outbound_context.pop(outbound_conv_id)
            logger.info(f"[setup] mapped outboundConvId={outbound_conv_id} → maestroConvId={conv_id}")

        # Pre-warm AgentCore WebSocket + memory in background so turn 1 is instant.
        # _handle_setup is sync, so schedule the async work onto the event loop.
        session_id = message.custom_parameters.profile_id or conv_id
        phone = ""
        ctx = pending_outbound_context.get(conv_id)
        if ctx:
            phone = ctx.get("phone", "")
        elif message.from_number:
            phone = message.from_number
        asyncio.get_event_loop().create_task(
            _prewarm(conv_id, session_id, phone),
            name=f"prewarm-{conv_id}",
        )


voice_channel = OwlVoiceChannel(tac=tac, auto_retrieve_memory=False)

# ---------------------------------------------------------------------------
# TAC callbacks
# ---------------------------------------------------------------------------

async def handle_message_ready(
    user_message: str,
    context: ConversationSession,
    memory_response: Optional[TACMemoryResponse],
) -> None:
    t0 = time.time()
    conv_id = context.conversation_id
    session_id = context.profile_id or conv_id

    # Turn 1: resolve outbound context + build enriched prompt
    is_turn1 = conv_id not in system_prompt_cache

    if is_turn1:
        ctx = pending_outbound_context.pop(conv_id, None)
        if ctx:
            # Outbound call
            system_prompt_cache[conv_id] = _build_system_prompt(ctx["name"], ctx["goal"], ctx["goalDesc"])
            greeting_cache[conv_id] = ctx.get("greeting", "")
            outbound_conversation_map[conv_id] = ctx["phone"]
            phone = ctx["phone"]
            logger.info(f"[healthcare] outbound ctx applied conv_id={conv_id} member={ctx['name']}")
        else:
            # Inbound call — fetch memory using caller's address if available
            system_prompt_cache[conv_id] = _build_inbound_system_prompt()
            phone = (context.author_info.address if context.author_info else "") or ""
            logger.info(f"[healthcare] inbound session conv_id={conv_id} from={phone}")

        # Use pre-warmed memory if available; otherwise fetch now (fallback)
        if conv_id in memory_context_cache:
            mem_ctx = memory_context_cache[conv_id]
            logger.info(f"[healthcare] using pre-warmed memory conv_id={conv_id}")
        else:
            if phone and not memory_response:
                memory_response = await _prefetch_memory(phone)
            mem_ctx = _build_memory_context(memory_response)
        greeting = greeting_cache.get(conv_id)
        bedrock_mode = os.environ.get("BEDROCK_AGENT_MODE") == "agentcore"
        if greeting and bedrock_mode:
            enriched = f"[Greeting already spoken to member]\n{greeting}" + (f"\n\n{mem_ctx}" if mem_ctx else "")
        else:
            enriched = mem_ctx
        memory_context_cache[conv_id] = enriched
        logger.info(f"[healthcare] enrichedContext (turn 1):\n{enriched or '(empty)'}")
    else:
        enriched = ""

    system_prompt = system_prompt_cache.get(conv_id, "") if is_turn1 else ""
    if is_turn1:
        logger.info(f"[healthcare] systemPrompt (turn 1):\n{system_prompt}")

    logger.info(f"[healthcare] invoking agent session_id={session_id} conv_id={conv_id} turn1={is_turn1} message=\"{user_message[:60]}\"")

    agent_ws = await get_or_create_agent_ws(session_id)
    if not agent_ws:
        await voice_channel.send_response(conv_id, "I'm sorry, I can't reach the agent right now.", role="assistant")
        return

    agent_msg: dict = {
        "type": "prompt",
        "voicePrompt": user_message,
        "systemPrompt": system_prompt,
        "memoryContext": enriched,
    }

    try:
        await agent_ws.send(json.dumps(agent_msg))

        async def stream_from_agent() -> AsyncGenerator[str, None]:
            first_token = False
            try:
                async for raw in agent_ws:
                    data = json.loads(raw)
                    msg_type = data.get("type", "")
                    if msg_type == "text":
                        token = data.get("token", "")
                        if token:
                            if not first_token:
                                logger.info(f"[healthcare] TTFT {(time.time()-t0)*1000:.0f}ms")
                                first_token = True
                            yield token
                        if data.get("last", False):
                            break
                    elif msg_type == "tool_start":
                        logger.info(f"[agentcore] tool_start tool={data.get('tool')}")
                    elif msg_type == "tool_result":
                        logger.info(f"[agentcore] tool_result status={data.get('status')}")
            except websockets.exceptions.ConnectionClosed:
                logger.warning(f"[agentcore] WebSocket closed mid-stream session_id={session_id}")
                agent_connections.pop(session_id, None)
            except Exception as e:
                logger.error(f"[agentcore] stream error: {e}")
                yield "I'm sorry, something went wrong."

        await voice_channel.send_response(conv_id, stream_from_agent(), role="assistant")

    except websockets.exceptions.ConnectionClosed:
        agent_connections.pop(session_id, None)
        await voice_channel.send_response(conv_id, "I'm sorry, the connection was lost.", role="assistant")
    except Exception as e:
        logger.error(f"[healthcare] message error: {e}", exc_info=True)
        await voice_channel.send_response(conv_id, "I'm sorry, something went wrong.", role="assistant")

    logger.info(f"[healthcare] end-to-end {(time.time()-t0)*1000:.0f}ms session_id={session_id}")


async def handle_conversation_ended(context: ConversationSession) -> None:
    conv_id = context.conversation_id
    session_id = context.profile_id or conv_id

    ws = agent_connections.pop(session_id, None)
    if ws:
        try:
            await ws.close()
        except Exception:
            pass

    system_prompt_cache.pop(conv_id, None)
    greeting_cache.pop(conv_id, None)
    memory_context_cache.pop(conv_id, None)
    # outbound_conversation_map entry stays until CI webhook consumes it
    logger.info(f"[healthcare] cleaned up conv_id={conv_id}")

    try:
        await tac.maestro_client.update_conversation(conv_id, status="CLOSED")
    except Exception as e:
        # 400 "Conversation is closed" is expected — the relay callback already closed it
        if "already" not in str(e).lower() and "400" not in str(e):
            logger.warning(f"[healthcare] maestro close failed conv_id={conv_id}: {e}")


async def handle_interrupt(context: ConversationSession, interrupt_data) -> None:
    session_id = context.profile_id or context.conversation_id
    ws = agent_connections.get(session_id)
    if ws:
        try:
            utterance = getattr(interrupt_data, "utterance_until_interrupt", "") or ""
            await ws.send(json.dumps({"type": "interrupt", "utterance_until_interrupt": utterance}))
        except Exception:
            pass


tac.on_message_ready(handle_message_ready)
tac.on_conversation_ended(handle_conversation_ended)
tac.on_interrupt(handle_interrupt)

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI()


def _get_urls(request: Request) -> tuple[str, str]:
    proto = request.headers.get("x-forwarded-proto", "https")
    host = request.headers.get("host", PUBLIC_DOMAIN)
    ws_proto = "wss" if proto == "https" else "ws"
    return f"{ws_proto}://{host}/ws", f"{proto}://{host}/conversation-relay-callback"


@app.post("/twiml")
async def post_twiml(request: Request) -> Response:
    """TwiML for inbound calls."""
    form = {k: str(v) for k, v in (await request.form()).items()}
    ws_url, callback_url = _get_urls(request)
    twiml = await voice_channel.handle_incoming_call(
        to_number=form.get("To", ""),
        from_number=form.get("From", ""),
        options={
            "websocket_url": ws_url,
            "action_url": callback_url,
            "welcome_greeting": "Hello! This is the Owl Health Care Team. How can I assist you today?",
        },
        call_sid=form.get("CallSid", ""),
    )
    return Response(content=twiml, media_type="application/xml")


@app.post("/twiml-outbound")
async def post_twiml_outbound(request: Request) -> Response:
    """TwiML for outbound calls with personalized greeting.

    The greeting is built by the Node.js app server and sent via /set-outbound-context.
    Query params (member, goal, desc) are kept for the TAC setup custom parameters so
    the Python server can look up the pending context by conversationId.
    """
    params = dict(request.query_params)
    conv_id = params.get("conv_id", "")

    ctx = pending_outbound_context.get(conv_id) if conv_id else None
    greeting = ctx.get("greeting", "") if ctx else "Hello! This is the Owl Health Care Team."

    form = {k: str(v) for k, v in (await request.form()).items()}
    ws_url, callback_url = _get_urls(request)
    twiml = await voice_channel.handle_incoming_call(
        to_number=form.get("To", ""),
        from_number=form.get("From", ""),
        options={
            "websocket_url": ws_url,
            "action_url": callback_url,
            "welcome_greeting": greeting,
            "custom_parameters": {"outboundConvId": conv_id} if conv_id else {},
        },
        call_sid=form.get("CallSid", ""),
    )
    return Response(content=twiml, media_type="application/xml")


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    await voice_channel.handle_websocket(FastAPIWebSocketAdapter(websocket))


@app.post("/conversation-relay-callback")
async def cr_callback(request: Request) -> Response:
    form = {k: str(v) for k, v in (await request.form()).items()}
    result = await voice_channel.handle_conversation_relay_callback(form)
    return Response(
        content=result or "OK",
        media_type="text/xml" if result else "text/plain",
    )


@app.post("/set-outbound-context")
async def set_outbound_context(request: Request) -> dict:
    """IPC endpoint — Node.js app server POSTs outbound ctx before dialling.

    Body: { conv_id, name, goal, goalDesc, phone, greeting }
    The conv_id is generated by the app server from the Twilio callSid or a UUID
    and passed as a query param on the twiml-outbound URL so we can correlate.
    """
    body = await request.json()
    conv_id = body.get("conv_id", "")
    if not conv_id:
        return {"success": False, "error": "conv_id required"}
    pending_outbound_context[conv_id] = body
    logger.info(f"[ipc] outbound context stored conv_id={conv_id} member={body.get('name')}")
    return {"success": True}


@app.get("/get-outbound-phone/{conv_id}")
async def get_outbound_phone(conv_id: str) -> dict:
    """IPC endpoint — Node.js CI webhook calls this to resolve member phone."""
    phone = outbound_conversation_map.get(conv_id)
    if phone:
        outbound_conversation_map.pop(conv_id, None)
    return {"phone": phone or ""}


@app.post("/ci-webhook")
async def ci_webhook_proxy(request: Request) -> dict:
    """Forward CI webhook to Node.js app server (port APP_PORT)."""
    import httpx
    body = await request.json()
    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(
                f"http://localhost:{APP_PORT}/ci-webhook",
                json=body,
                timeout=10,
            )
        return res.json()
    except Exception as e:
        logger.error(f"[ci-webhook] proxy failed: {e}")
        return {"success": False}


@app.get("/health")
async def health() -> dict:
    return {"status": "healthy"}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    logger.info(f"Owl Health TAC Server starting — {PUBLIC_DOMAIN}:{TAC_PORT}")
    uvicorn.run(app, host="0.0.0.0", port=TAC_PORT, log_config=None, log_level="info")
