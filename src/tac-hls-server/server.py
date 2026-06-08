"""
Owl Health TAC-SDK server — OpenAI Chat Completions + Twilio Agent Connect SDK.

Drop-in replacement for src/tac/server.py using the TAC SDK's TACFastAPIServer
instead of a custom FastAPI app.

Run:
    cd src/tac-hls-server
    uvicorn server:app --port 8000 --reload
"""

import asyncio
import json
import logging
import os
import pathlib
import time

import requests
from dotenv import load_dotenv
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam

from tac import TAC, TACConfig
from tac.channels.voice import VoiceChannel, VoiceChannelConfig
from tac.channels.sms import SMSChannel, SMSChannelConfig
from tac.server import TACFastAPIServer
from tac.server.config import TACServerConfig
from tac.models.session import ConversationSession
from tac.models.tac import TACMemoryResponse

from memory import prefetch_memory, build_memory_context, write_sms_observation, lookup_profile_id, fetch_profile_traits
from prompts import get_inbound_system_prompt, get_sms_system_prompt, get_outbound_system_prompt
from tools import TOOLS, execute_tool

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("tac-sdk.server")

# ── Config ────────────────────────────────────────────────────────────────────

APP_PORT           = int(os.environ.get("APP_PORT", "8001"))
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN  = os.environ.get("TWILIO_AUTH_TOKEN", "")
PUBLIC_DOMAIN      = (os.environ.get("VOICE_PUBLIC_DOMAIN") or "").lstrip("https://").lstrip("http://")
OPENAI_MODEL       = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
SMS_WRITE_OBS      = os.environ.get("SMS_WRITE_OBSERVATION", "false").lower() in ("1", "true", "yes")

# Load app config from apps/healthcare.json
_CFG_FILE = pathlib.Path(__file__).parent / "apps" / "healthcare.json"
_raw = json.loads(_CFG_FILE.read_text())
ev = os.environ.get

class AppConfig:
    id                = _raw["id"]
    display_name      = _raw["display_name"]
    phone_number      = ev(_raw.get("phone_number_env", ""), "")
    memory_store_id   = ev(_raw.get("memory_store_env", ""), "")
    memory_api_key    = ev(_raw.get("memory_api_key_env", ""), "")
    memory_api_token  = ev(_raw.get("memory_api_token_env", ""), "")
    flex_queue        = _raw.get("flex_queue", "healthcare")
    inbound_greeting  = _raw.get("inbound_greeting", "")
    outbound_call_to  = ev(_raw.get("outbound_call_to_env", ""), "")
    sms_simulate_phone = ev(_raw.get("sms_simulate_member_phone_env", ""), "")

cfg = AppConfig()

# ── TAC + channels ────────────────────────────────────────────────────────────

tac = TAC(config=TACConfig.from_env())
# memory_mode="never" — we do manual memory fetch for full personalization
voice_channel = VoiceChannel(tac, config=VoiceChannelConfig(memory_mode="never"))
sms_channel   = SMSChannel(tac, config=SMSChannelConfig(memory_mode="never"))

# ── OpenAI ────────────────────────────────────────────────────────────────────

openai_client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))

# Per-conversation: {"history": [...messages], "profile_id": str, "phone": str, "name": str}
_sessions: dict[str, dict] = {}
# Maps conv_id → call_sid (populated from custom_parameters in twiml-outbound)
_conv_call_sid_map: dict[str, str] = {}


# ── Message handler ───────────────────────────────────────────────────────────

async def handle_message_ready(
    user_message: str,
    context: ConversationSession,
    memory_response: TACMemoryResponse | None,
) -> str:
    conv_id = context.conversation_id
    channel = getattr(context, "channel", "voice")
    t0 = time.time()
    logger.info(f"[{cfg.id}] handle_message_ready conv_id={conv_id} channel={channel} msg=\"{user_message[:60]}\"")

    # ── Session init / memory fetch on turn 1 ────────────────────────────────
    if conv_id not in _sessions:
        phone = getattr(getattr(context, "author_info", None), "address", "") or ""
        if phone.startswith("client:"):
            phone = ""

        # Check for outbound context set by Node.js before the call
        outbound_ctx = _outbound_context.pop(conv_id, None)
        if outbound_ctx:
            phone = outbound_ctx.get("phone", "") or phone

        memory, traits, profile_id = await prefetch_memory(
            phone, cfg.memory_store_id, cfg.memory_api_key, cfg.memory_api_token
        )
        mem_ctx = build_memory_context(memory, traits)
        name = (outbound_ctx.get("name", "") if outbound_ctx else "") or \
               (f"{traits.get('firstName', '')} {traits.get('lastName', '')}".strip()
                or traits.get("name", "") or "")

        # Build system prompt for this channel
        if channel == "sms":
            system_prompt = get_sms_system_prompt()
        elif outbound_ctx:
            goal = outbound_ctx.get("goal", "")
            goal_desc = outbound_ctx.get("goalDesc", "")
            system_prompt = get_outbound_system_prompt(name, goal, goal_desc)
        else:
            system_prompt = get_inbound_system_prompt()

        messages: list[ChatCompletionMessageParam] = [{"role": "system", "content": system_prompt}]
        if mem_ctx:
            messages.append({"role": "system", "content": mem_ctx})

        _sessions[conv_id] = {
            "history": messages,
            "profile_id": profile_id or "",
            "phone": phone,
            "name": name,
            "direction": "outbound" if outbound_ctx else "inbound",
        }
        logger.info(f"[{cfg.id}] session init conv_id={conv_id} profile_id={profile_id} name={name!r} direction={'outbound' if outbound_ctx else 'inbound'}")

    session = _sessions[conv_id]

    # ── Append user message ───────────────────────────────────────────────────
    session["history"].append({"role": "user", "content": user_message})

    # ── OpenAI call with tool loop ────────────────────────────────────────────
    try:
        final_response = ""
        escalated = False

        while True:
            response = await openai_client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=session["history"],
                tools=TOOLS,
                tool_choice="auto",
            )
            msg = response.choices[0].message

            # Add assistant message to history
            session["history"].append(msg.model_dump(exclude_none=True))

            if msg.tool_calls:
                # Execute each tool call
                for tc in msg.tool_calls:
                    tool_name = tc.function.name
                    tool_args = json.loads(tc.function.arguments or "{}")
                    logger.info(f"[{cfg.id}] tool_call name={tool_name} args={tool_args}")

                    result = execute_tool(
                        name=tool_name,
                        args=tool_args,
                        conv_id=conv_id,
                        member_phone=session["phone"],
                        member_name=session["name"],
                        member_profile_id=session["profile_id"],
                        cfg=cfg,
                        call_sid=_conv_call_sid_map.get(conv_id, ""),
                        direction=session.get("direction", "inbound"),
                    )
                    logger.info(f"[{cfg.id}] tool_result name={tool_name} result={result[:200]}")

                    if tool_name == "escalate_to_human":
                        escalated = True

                    session["history"].append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result,
                    })
                # Loop back for agent's response after tool results
                continue

            # No tool calls — extract text response
            final_response = msg.content or ""
            break

        logger.info(f"[{cfg.id}] response ({len(final_response)} chars) in {(time.time()-t0)*1000:.0f}ms: \"{final_response[:100]}\"")

        # ── SMS observation write ─────────────────────────────────────────────
        if channel == "sms" and SMS_WRITE_OBS and session["profile_id"]:
            loop = asyncio.get_event_loop()
            loop.run_in_executor(None, write_sms_observation,
                user_message, "member", session["profile_id"],
                cfg.memory_store_id, cfg.memory_api_key, cfg.memory_api_token)
            loop.run_in_executor(None, write_sms_observation,
                final_response, "agent", session["profile_id"],
                cfg.memory_store_id, cfg.memory_api_key, cfg.memory_api_token)

        return final_response

    except Exception as e:
        logger.error(f"[{cfg.id}] handle_message_ready error: {e}", exc_info=True)
        return "I'm sorry, something went wrong. Please try again."


# ── Register handler ──────────────────────────────────────────────────────────

tac.on_message_ready(handle_message_ready)

# ── Personalized inbound greeting ─────────────────────────────────────────────
# Patch voice_channel to inject personalized welcome_greeting at TwiML time.
# We store a per-call lookup result keyed by call_sid.

_pending_greetings: dict[str, str] = {}  # call_sid → greeting

async def _get_personalized_greeting(from_number: str) -> str:
    """Look up member name and build a personalized greeting."""
    greeting = cfg.inbound_greeting
    if from_number and not greeting:
        try:
            loop = asyncio.get_event_loop()
            profile_id = await loop.run_in_executor(
                None, lookup_profile_id,
                from_number, cfg.memory_store_id, cfg.memory_api_key, cfg.memory_api_token
            )
            if profile_id:
                traits = await loop.run_in_executor(
                    None, fetch_profile_traits,
                    profile_id, cfg.memory_store_id, cfg.memory_api_key, cfg.memory_api_token
                )
                first_name = traits.get("firstName", "") or (traits.get("name", "").split()[0] if traits.get("name") else "")
                if first_name:
                    greeting = f"Hi {first_name}, this is your Owl Health care coordinator. How can I help you today?"
                    logger.info(f"[twiml] personalized greeting name={first_name}")
        except Exception as e:
            logger.warning(f"[twiml] greeting lookup failed: {e}")
    return greeting

# ── Server ────────────────────────────────────────────────────────────────────

_server = TACFastAPIServer(
    tac=tac,
    voice_channel=voice_channel,
    messaging_channels=[sms_channel],
    config=TACServerConfig(
        port=8000,
        public_domain=PUBLIC_DOMAIN,
    ),
)

# Expose the FastAPI app for uvicorn
app = _server.app

# Log every incoming request to help debug missing webhooks
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as _MWRequest

class LogAllRequestsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: _MWRequest, call_next):
        logger.info(f"[REQUEST] {request.method} {request.url.path} from={request.client.host if request.client else '?'} content-type={request.headers.get('content-type','')}")
        response = await call_next(request)
        logger.info(f"[RESPONSE] {request.method} {request.url.path} status={response.status_code}")
        return response

app.add_middleware(LogAllRequestsMiddleware)

# Override /twiml to inject personalized greeting
from fastapi import Request as _TwimlRequest
from fastapi.responses import Response as _TwimlResponse

@app.post("/twiml", include_in_schema=False)
async def twiml_override(request: _TwimlRequest):
    """Override TAC SDK's /twiml to add personalized greeting."""
    form = {k: str(v) for k, v in (await request.form()).items()}
    from_number = form.get("From", "")
    call_sid    = form.get("CallSid", "")
    proto    = request.headers.get("x-forwarded-proto", "https")
    host     = request.headers.get("host", PUBLIC_DOMAIN)
    ws_proto = "wss" if proto == "https" else "ws"
    greeting = await _get_personalized_greeting(from_number)
    twiml = await voice_channel.handle_incoming_call(
        options={
            "websocket_url": f"{ws_proto}://{host}/ws",
            "action_url": f"{proto}://{host}/conversation-relay-callback",
            "welcome_greeting": greeting,
            "custom_parameters": {"callSid": call_sid},
        }
    )
    twiml = twiml.replace("<ConversationRelay ", '<ConversationRelay interruptible="false" ', 1)
    return _TwimlResponse(content=twiml, media_type="application/xml")


# TODO: /ci-webhook should have its own public URL (separate ngrok tunnel or dedicated
# subdomain) pointing directly at the Node.js server (port APP_PORT) so Twilio doesn't
# need to route through TAC. For now, proxy to Node.js internally.

import httpx as _httpx_extra
from fastapi import Request as _Request
from fastapi.responses import Response as _FResponse

# Per-conversation outbound context (name, phone, goal) set by Node.js before call
_outbound_context: dict[str, dict] = {}

from fastapi import WebSocket as _WebSocket

@app.websocket("/healthcare/ws")
async def ws_healthcare(websocket: _WebSocket):
    """WebSocket alias with /healthcare prefix for ConversationRelay."""
    from tac.server.fastapi_server import FastAPIWebSocketAdapter
    await voice_channel.handle_websocket(FastAPIWebSocketAdapter(websocket))


@app.post("/healthcare/twiml-outbound")
async def twiml_outbound(request: _Request):
    """Generate ConversationRelay TwiML for outbound calls initiated by Node.js."""
    params = dict(request.query_params)
    form = {k: str(v) for k, v in (await request.form()).items()}
    conv_id  = params.get("conv_id", "")
    call_sid = form.get("CallSid", "")

    ctx      = _outbound_context.get(conv_id)
    proto    = request.headers.get("x-forwarded-proto", "https")
    host     = request.headers.get("host", PUBLIC_DOMAIN)
    ws_proto = "wss" if proto == "https" else "ws"

    from_number = form.get("From", "")
    greeting = ctx.get("greeting", "") if ctx else await _get_personalized_greeting(from_number)

    custom_params: dict = {"callSid": call_sid}
    if conv_id:
        custom_params["outboundConvId"] = conv_id

    twiml = await voice_channel.handle_incoming_call(
        options={
            "websocket_url": f"{ws_proto}://{host}/healthcare/ws",
            "action_url": f"{proto}://{host}/healthcare/conversation-relay-callback",
            "welcome_greeting": greeting,
            "custom_parameters": custom_params,
        }
    )
    # Inject interruptible=false to reduce noise interruptions
    twiml = twiml.replace("<ConversationRelay ", '<ConversationRelay interruptible="false" ', 1)
    if conv_id and call_sid:
        _conv_call_sid_map[conv_id] = call_sid
    logger.info(f"[twiml-outbound] conv_id={conv_id} call_sid={call_sid} has_ctx={bool(ctx)}")
    return _FResponse(content=twiml, media_type="application/xml")


@app.post("/healthcare/conversation-relay-callback")
async def cr_callback_prefixed(request: _Request):
    """Alias for /conversation-relay-callback with /healthcare prefix (Node.js uses this path)."""
    form = {k: str(v) for k, v in (await request.form()).items()}
    # Track conv_id ↔ call_sid for escalation
    conv_id  = form.get("ConversationSid", "") or form.get("conversationId", "")
    call_sid = form.get("CallSid", "")
    if conv_id and call_sid:
        _conv_call_sid_map[conv_id] = call_sid
        logger.info(f"[cr-callback] mapped conv_id={conv_id} call_sid={call_sid}")
    try:
        result = await voice_channel.handle_conversation_relay_callback(form)
        return _FResponse(content=result or "OK", media_type="text/xml" if result else "text/plain")
    except Exception as e:
        logger.error(f"[cr-callback] error: {e}")
        return _FResponse(content="OK")


@app.post("/healthcare/set-outbound-context")
async def set_outbound_context(request: _Request):
    """Node.js calls this before placing an outbound call to set member context."""
    body = await request.json()
    conv_id = body.get("conv_id", "")
    if not conv_id:
        return {"success": False, "error": "conv_id required"}
    _outbound_context[conv_id] = body
    logger.info(f"[outbound] context stored conv_id={conv_id} name={body.get('name')} phone={body.get('phone')}")
    return {"success": True}


@app.get("/get-outbound-phone/{conv_id}")
async def get_outbound_phone(conv_id: str):
    """Return member phone + profileId for a conversation — used by Node.js CI webhook."""
    # Check in-memory session store first
    session_data = _sessions.get(conv_id)
    if session_data:
        return {
            "phone": session_data.get("phone", ""),
            "profileId": session_data.get("profile_id", ""),
            "appId": cfg.id,
        }
    # Fallback: look up via CO Participants API
    if conv_id.startswith("conv_conversation_"):
        try:
            async with _httpx_extra.AsyncClient() as client:
                res = await client.get(
                    f"https://conversations.twilio.com/v2/Conversations/{conv_id}/Participants",
                    auth=(cfg.memory_api_key, cfg.memory_api_token),
                    timeout=5,
                )
                for p in res.json().get("participants", []):
                    addresses = p.get("addresses") or []
                    address = addresses[0].get("address", "") if addresses else ""
                    if address and address != cfg.phone_number and address.startswith("+"):
                        return {"phone": address, "profileId": "", "appId": cfg.id}
        except Exception as e:
            logger.warning(f"[get-outbound-phone] fallback failed: {e}")
    return {"phone": "", "profileId": "", "appId": cfg.id}


@app.post("/ci-webhook")
async def ci_webhook_proxy(request: _Request):
    """Proxy CI webhook from Twilio to Node.js for processing."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    params = str(request.url.query)
    url = f"http://localhost:{APP_PORT}/ci-webhook" + (f"?{params}" if params else "")
    try:
        async with _httpx_extra.AsyncClient() as client:
            res = await client.post(url, json=body, timeout=10)
        return res.json()
    except Exception as e:
        logger.error(f"[ci-webhook] proxy failed: {e}")
        return {"success": False}


@app.post("/sms")
async def sms_handler(request: _Request):
    """Direct inbound SMS handler — bypasses CO webhook, processes message inline."""
    form = dict(await request.form())
    from_phone = form.get("From", "").strip()
    body_text  = form.get("Body", "").strip()
    to_phone   = form.get("To", "")
    logger.info(f"[sms] inbound From={from_phone} To={to_phone} Body=\"{body_text[:80]}\"")

    empty_twiml = _FResponse(content="<?xml version='1.0'?><Response/>", media_type="text/xml")

    if not from_phone or not body_text:
        return empty_twiml

    # Use phone as conv_id for SMS (stable per member)
    conv_id = f"sms-{from_phone.replace('+', '')}"

    # Session init / memory fetch on first message
    if conv_id not in _sessions:
        memory, traits, profile_id = await prefetch_memory(
            from_phone, cfg.memory_store_id, cfg.memory_api_key, cfg.memory_api_token
        )
        mem_ctx = build_memory_context(memory, traits)
        name = (f"{traits.get('firstName', '')} {traits.get('lastName', '')}".strip()
                or traits.get("name", "") or "")

        system_prompt = get_sms_system_prompt()
        messages = [{"role": "system", "content": system_prompt}]
        if mem_ctx:
            messages.append({"role": "system", "content": mem_ctx})

        _sessions[conv_id] = {
            "history": messages,
            "profile_id": profile_id or "",
            "phone": from_phone,
            "name": name,
        }
        logger.info(f"[sms] session init conv_id={conv_id} profile_id={profile_id} name={name!r}")

    session = _sessions[conv_id]
    session["history"].append({"role": "user", "content": body_text})

    # Call OpenAI with tool loop
    try:
        final_response = ""
        while True:
            response = await openai_client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=session["history"],
                tools=TOOLS,
                tool_choice="auto",
            )
            msg = response.choices[0].message
            session["history"].append(msg.model_dump(exclude_none=True))

            if msg.tool_calls:
                for tc in msg.tool_calls:
                    tool_name = tc.function.name
                    tool_args = json.loads(tc.function.arguments or "{}")
                    logger.info(f"[sms] tool_call name={tool_name} args={tool_args}")
                    result = execute_tool(
                        name=tool_name, args=tool_args, conv_id=conv_id,
                        member_phone=session["phone"], member_name=session["name"],
                        member_profile_id=session["profile_id"], cfg=cfg,
                    )
                    session["history"].append({"role": "tool", "tool_call_id": tc.id, "content": result})
                continue
            final_response = msg.content or ""
            break

        logger.info(f"[sms] reply to {from_phone}: \"{final_response[:100]}\"")

        # Send SMS reply via Twilio
        if final_response and TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
            from twilio.rest import Client as _TwilioClient
            _tc = _TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
            send_to = cfg.sms_simulate_phone or from_phone
            _tc.messages.create(to=send_to, from_=cfg.phone_number, body=final_response)

        # Write observations
        if SMS_WRITE_OBS and session["profile_id"]:
            loop = asyncio.get_event_loop()
            loop.run_in_executor(None, write_sms_observation,
                body_text, "member", session["profile_id"],
                cfg.memory_store_id, cfg.memory_api_key, cfg.memory_api_token)
            loop.run_in_executor(None, write_sms_observation,
                final_response, "agent", session["profile_id"],
                cfg.memory_store_id, cfg.memory_api_key, cfg.memory_api_token)

    except Exception as e:
        logger.error(f"[sms] error: {e}", exc_info=True)

    return empty_twiml

if __name__ == "__main__":
    _server.start()
