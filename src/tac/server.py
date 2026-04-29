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
  POST /escalate-call               — IPC: Transfer active call to Flex human agent queue
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
import pathlib
import time
from collections.abc import AsyncGenerator
from typing import Optional

import requests
import websockets
from base64 import b64encode
from xml.sax.saxutils import escape
from dotenv import load_dotenv
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import Response

from bedrock_agentcore.runtime import AgentCoreRuntimeClient
from tac import TAC, TACConfig
from tac.channels.voice import VoiceChannel
from tac.models.session import AuthorInfo, ConversationSession
from dataclasses import dataclass, field as _field
from typing import Any as _Any

@dataclass
class _MemoryContainer:
    observations: list = _field(default_factory=list)
    summaries: list = _field(default_factory=list)

TACMemoryResponse = _MemoryContainer  # type: ignore
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

AGENT_BACKEND = os.environ.get("AGENT_BACKEND", "agentcore")  # "agentcore" | "elevenlabs"
EL_PORT       = int(os.environ.get("ELEVENLABS_PORT", "8002"))
EL_BASE       = f"http://localhost:{EL_PORT}"

MEMORY_BASE = "https://memory.twilio.com"
MEMORY_STORE_ID = os.environ.get("MEMORY_STORE_ID", "")
MEMORY_API_KEY = os.environ.get("TWILIO_API_KEY", "")
MEMORY_API_TOKEN = os.environ.get("TWILIO_API_TOKEN", "")

PHONE_NUMBER = os.environ.get("TWILIO_TAC_PHONE_NUMBER", "")
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_TAC_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN  = os.environ.get("TWILIO_TAC_AUTH_TOKEN", "")
SMS_WRITE_OBSERVATION = os.environ.get("SMS_WRITE_OBSERVATION", "false").lower() == "true"
OUTBOUND_CALL_TO = os.environ.get("OUTBOUND_CALL_TO", "")
SMS_SIMULATE_MEMBER_PHONE = os.environ.get("SMS_SIMULATE_MEMBER_PHONE", "")

ESCALATION_ENABLED = os.environ.get("ESCALATION_ENABLED", "false").lower() in ("1", "true", "yes")
FLEX_HANDOFF_APPLICATION_SID = os.environ.get("TWILIO_FLEX_HANDOFF_APPLICATION_SID", "")
FLEX_DEFAULT_QUEUE = os.environ.get("TWILIO_FLEX_DEFAULT_QUEUE", "healthcare")
FLEX_ESCALATION_ANNOUNCEMENT = os.environ.get(
    "TWILIO_FLEX_ESCALATION_ANNOUNCEMENT",
    "Of course! Let me connect you with one of our care specialists right away. Please hold for just a moment.",
)

from twilio.rest import Client as TwilioClient
twilio_client: Optional[TwilioClient] = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if TWILIO_ACCOUNT_SID else None

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
conversation_call_sid_map: dict[str, str] = {}
pending_call_sid_map: dict[str, str] = {}
system_prompt_cache: dict[str, str] = {}
greeting_cache: dict[str, str] = {}
memory_context_cache: dict[str, str] = {}

# ---------------------------------------------------------------------------
# Escalation helpers
# ---------------------------------------------------------------------------

def _twilio_basic_auth_header() -> dict[str, str]:
    creds = f"{TWILIO_ACCOUNT_SID}:{TWILIO_AUTH_TOKEN}".encode("utf-8")
    return {"Authorization": f"Basic {b64encode(creds).decode('ascii')}"}


def _build_flex_transfer_twiml(conv_id: str, reason: str, urgency: str, target_queue: str,
                               member_phone: str = "", member_name: str = "", member_profile_id: str = "") -> str:
    params = f'<Parameter name="reason" value="{escape(reason)}" />'
    if member_phone:
        params += f'<Parameter name="memberPhone" value="{escape(member_phone)}" />'
    if member_name:
        params += f'<Parameter name="memberName" value="{escape(member_name)}" />'
    if member_profile_id:
        params += f'<Parameter name="memberProfileId" value="{escape(member_profile_id)}" />'
    if urgency == "high":
        params += '<Parameter name="urgency" value="high" />'
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        '<Dial answerOnBridge="true">'
        '<Application copyParentTo="true">'
        f"<ApplicationSid>{escape(FLEX_HANDOFF_APPLICATION_SID)}</ApplicationSid>"
        + params +
        "</Application>"
        "</Dial>"
        "</Response>"
    )


async def _escalate_call_to_flex(conv_id: str, reason: str, urgency: str, target_queue: str) -> bool:
    logger.info(f"[escalation] attempt conv_id={conv_id} reason={reason} urgency={urgency} queue={target_queue} enabled={ESCALATION_ENABLED} app_sid={FLEX_HANDOFF_APPLICATION_SID or '(not set)'}")
    if not ESCALATION_ENABLED:
        logger.warning(f"[escalation] ESCALATION_ENABLED=false — set to true in .env and restart")
        return False
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        logger.error(f"[escalation] missing TWILIO_TAC_ACCOUNT_SID or TWILIO_TAC_AUTH_TOKEN")
        return False
    if not FLEX_HANDOFF_APPLICATION_SID:
        logger.error(f"[escalation] missing TWILIO_FLEX_HANDOFF_APPLICATION_SID")
        return False

    call_sid = conversation_call_sid_map.get(conv_id)
    logger.info(f"[escalation] call_sid={call_sid or '(not mapped)'} conversation_call_sid_map keys={list(conversation_call_sid_map.keys())}")
    if not call_sid:
        logger.error(f"[escalation] no call_sid for conv_id={conv_id} — call may not have populated pending_call_sid_map")
        return False

    # Resolve actual member identity — needed when OUTBOUND_CALL_TO redirects to a demo phone
    map_entry = outbound_conversation_map.get(conv_id)
    logger.info(f"[escalation] outbound_conversation_map[{conv_id}] = {map_entry!r}")
    logger.info(f"[escalation] outbound_conversation_map full = {dict(outbound_conversation_map)!r}")
    member_phone = ""
    member_profile_id = ""
    member_name = ""
    if isinstance(map_entry, dict):
        member_phone = map_entry.get("phone", "")
        member_profile_id = map_entry.get("profileId", "")
        member_name = map_entry.get("name", "")
    elif isinstance(map_entry, str):
        member_phone = map_entry
    # Fallback: pending_outbound_context may still have context if turn 1 hasn't run
    ctx = pending_outbound_context.get(conv_id)
    if ctx:
        if not member_name:
            member_name = ctx.get("name", "")
        if not member_phone:
            member_phone = ctx.get("phone", "")
    logger.info(f"[escalation] resolved member_phone={member_phone or '(unknown)'} member_name={member_name or '(unknown)'} member_profile_id={member_profile_id or '(unknown)'}")

    twiml = _build_flex_transfer_twiml(conv_id, reason, urgency, target_queue,
                                       member_phone=member_phone,
                                       member_name=member_name,
                                       member_profile_id=member_profile_id)
    logger.info(f"[escalation] TwiML: {twiml}")
    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Calls/{call_sid}.json"
    headers = {"Content-Type": "application/x-www-form-urlencoded", **_twilio_basic_auth_header()}
    payload = {"Twiml": twiml}

    loop = asyncio.get_event_loop()

    def _do_request() -> requests.Response:
        return requests.post(url, data=payload, headers=headers, timeout=10)

    try:
        res = await loop.run_in_executor(None, _do_request)
        logger.info(f"[escalation] Twilio API response status={res.status_code} body={res.text[:500]}")
        if res.status_code >= 300:
            logger.error(f"[escalation] Twilio update failed status={res.status_code} conv_id={conv_id}")
            return False
        logger.info(f"[escalation] ✓ transferred conv_id={conv_id} call_sid={call_sid} reason={reason} urgency={urgency} queue={target_queue or FLEX_DEFAULT_QUEUE}")
        return True
    except Exception as e:
        logger.error(f"[escalation] transfer exception conv_id={conv_id}: {e}", exc_info=True)
        return False


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


def _fetch_profile_traits(profile_id: str) -> dict:
    """Fetch contact + outreach traits for a profile."""
    try:
        res = requests.get(
            f"{MEMORY_BASE}/v1/Stores/{MEMORY_STORE_ID}/Profiles/{profile_id}",
            auth=(MEMORY_API_KEY, MEMORY_API_TOKEN),
            timeout=5,
        )
        data = res.json()
        traits: dict = {}
        for trait_group in (data.get("traits") or []):
            if not isinstance(trait_group, dict):
                continue
            attrs = trait_group.get("attributes") or {}
            # attributes may be a dict of {key: value} or a list of {name, value} objects
            if isinstance(attrs, dict):
                traits.update(attrs)
            elif isinstance(attrs, list):
                for attr in attrs:
                    if isinstance(attr, dict) and "name" in attr:
                        traits[attr["name"]] = attr.get("value", "")
        return traits
    except Exception as e:
        logger.warning(f"[memory] fetchProfileTraits failed: {e}")
        return {}


def _fetch_memory(profile_id: str) -> Optional[TACMemoryResponse]:
    try:
        base = f"{MEMORY_BASE}/v1/Stores/{MEMORY_STORE_ID}/Profiles/{profile_id}"
        auth = (MEMORY_API_KEY, MEMORY_API_TOKEN)
        obs_res = requests.get(f"{base}/Observations", auth=auth, timeout=5)
        sum_res = requests.get(f"{base}/ConversationSummaries", auth=auth, timeout=5)
        observations = [type("Obs", (), {"content": o["content"]})() for o in (obs_res.json() or {}).get("observations") or []]
        summaries = [type("Sum", (), {"content": s["content"]})() for s in (sum_res.json() or {}).get("summaries") or []]
        return TACMemoryResponse(observations=observations, summaries=summaries)
    except Exception as e:
        logger.warning(f"[memory] fetchMemory failed: {e}")
        return None


async def _prefetch_memory(phone: str) -> tuple[Optional[TACMemoryResponse], dict]:
    """Run blocking memory + traits fetch in thread pool. Returns (memory, traits)."""
    loop = asyncio.get_event_loop()
    t0 = time.time()
    try:
        profile_id = await loop.run_in_executor(None, _lookup_profile_id, phone)
        if profile_id:
            memory, traits = await asyncio.gather(
                loop.run_in_executor(None, _fetch_memory, profile_id),
                loop.run_in_executor(None, _fetch_profile_traits, profile_id),
            )
        else:
            memory, traits = None, {}
        obs = len(memory.observations) if memory else 0
        sums = len(memory.summaries) if memory else 0
        logger.info(f"[memory] prefetch phone={phone} profileId={profile_id or 'none'} obs={obs} summaries={sums} traits={len(traits)} in {(time.time()-t0)*1000:.0f}ms")
        return memory, traits
    except Exception as e:
        logger.warning(f"[memory] prefetch error: {e}")
        return None, {}


def _build_memory_context(memory: Optional[TACMemoryResponse], traits: Optional[dict] = None) -> str:
    sections = []
    if traits:
        trait_lines = []
        if traits.get("name"):        trait_lines.append(f"- Name: {traits['name']}")
        if traits.get("phone"):       trait_lines.append(f"- Phone: {traits['phone']}")
        if traits.get("nextFollowUp"): trait_lines.append(f"- Next follow-up goal: {traits['nextFollowUp']}")
        if traits.get("nextFollowUpReason"): trait_lines.append(f"- Follow-up details: {traits['nextFollowUpReason']}")
        if traits.get("status"):      trait_lines.append(f"- Outreach status: {traits['status']}")
        if traits.get("lastCallSummary"): trait_lines.append(f"- Last call summary: {traits['lastCallSummary']}")
        if trait_lines:
            sections.append("### Member Profile\n" + "\n".join(trait_lines))
    if memory and memory.observations:
        lines = [f"- {o.content}" for o in memory.observations]
        sections.append("### Previous Observations\n" + "\n".join(lines))
    if memory and memory.summaries:
        lines = [f"- {s.content}" for s in memory.summaries]
        sections.append("### Previous Summaries\n" + "\n".join(lines))
    if not sections:
        return ""
    return "The following is from previous interactions with this member.\n\n" + "\n\n".join(sections)


async def _invoke_agentcore_ws(session_id: str, prompt: str, system_prompt: str, context: str) -> str:
    """Invoke AgentCore via WebSocket (reuses the pooled connection like voice calls)."""
    agent_ws = await get_or_create_agent_ws(session_id)
    if not agent_ws:
        raise RuntimeError("Could not connect to AgentCore")

    await agent_ws.send(json.dumps({
        "type": "prompt",
        "voicePrompt": prompt,
        "systemPrompt": system_prompt,
        "memoryContext": context,
    }))

    tokens: list[str] = []
    async for raw in agent_ws:
        data = json.loads(raw)
        if data.get("type") == "text":
            token = data.get("token", "")
            if token:
                tokens.append(token)
            if data.get("last", False):
                break
        elif data.get("type") == "tool_start":
            logger.info(f"[agentcore] sms tool_start tool={data.get('tool')}")
        elif data.get("type") == "tool_result":
            logger.info(f"[agentcore] sms tool_result status={data.get('status')}")

    return "".join(tokens).strip()


async def _invoke_agentcore_http(session_id: str, prompt: str, system_prompt: str, context: str) -> str:
    """Invoke AgentCore via WebSocket (AgentCore only speaks WS, both local and deployed)."""
    return await _invoke_agentcore_ws(session_id, prompt, system_prompt, context)


def _write_sms_observation(profile_id: str, role: str, content: str) -> None:
    from datetime import datetime
    obs_content = f"[SMS {role}] {content}"
    try:
        requests.post(
            f"{MEMORY_BASE}/v1/Stores/{MEMORY_STORE_ID}/Profiles/{profile_id}/Observations",
            json={"observations": [{"content": obs_content, "occurredAt": datetime.utcnow().isoformat() + "Z", "source": "sms-conversation"}]},
            auth=(MEMORY_API_KEY, MEMORY_API_TOKEN),
            timeout=5,
        )
    except Exception as e:
        logger.warning(f"[sms] write observation failed: {e}")


def _build_system_prompt(name: str, goal: str, goal_desc: str) -> str:
    lines = [
        f"You are calling {name} on behalf of the Owl Health care team.",
        f"The purpose of this call is: {goal}." if goal else "",
        f"Follow-up guidance: {goal_desc}" if goal_desc else "",
    ]
    return "\n".join(l for l in lines if l)


_INBOUND_PROMPT_FILE = pathlib.Path(__file__).parent / "system_prompt_inbound.txt"
_DEFAULT_INBOUND_PROMPT = (
    "You are an Owl Health care coordination agent handling an inbound member call. "
    "Be warm and conversational. Speak in plain, natural sentences — no bullet points, no bold or italic text. "
    "Keep responses brief and easy to follow on a phone call. "
    "Identify how you can help and guide the member toward a clear next step."
)

def _build_inbound_system_prompt() -> str:
    if _INBOUND_PROMPT_FILE.exists():
        text = _INBOUND_PROMPT_FILE.read_text().strip()
        if text:
            return text
    return _DEFAULT_INBOUND_PROMPT

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
        import ssl as _ssl
        _ssl_ctx = _ssl.create_default_context()
        _ssl_ctx.check_hostname = False
        _ssl_ctx.verify_mode = _ssl.CERT_NONE
        ws = await websockets.connect(url, ssl=_ssl_ctx)
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
    memory_result = await mem_task if mem_task else (None, {})
    memory, traits = memory_result if isinstance(memory_result, tuple) else (memory_result, {})

    if memory or traits:
        memory_context_cache[conv_id] = _build_memory_context(memory, traits)

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

        # Track call_sid for escalation — Twilio passes it via customParameters or pending map
        call_sid = extra.get("callSid", "")
        if call_sid:
            conversation_call_sid_map[conv_id] = call_sid
        elif outbound_conv_id and outbound_conv_id in pending_call_sid_map:
            conversation_call_sid_map[conv_id] = pending_call_sid_map.pop(outbound_conv_id)
        if conv_id in conversation_call_sid_map:
            logger.info(f"[setup] call_sid mapped conv_id={conv_id} call_sid={conversation_call_sid_map[conv_id]}")

        # Populate outbound_conversation_map immediately at call setup so the CI
        # webhook can find the member phone even if the caller hangs up before speaking.
        ctx = pending_outbound_context.get(conv_id)
        phone = ""
        if ctx:
            phone = ctx.get("phone", "")
        elif message.from_number:
            phone = message.from_number

        # For outbound calls the Orchestrator labels our Twilio number as CUSTOMER and
        # gives it a profile_id — but we want the member's existing profile as session_id.
        # Look up the member profile by phone; fall back to the Maestro-assigned profile_id.
        member_profile_id: Optional[str] = None
        if ctx and phone:
            member_profile_id = _lookup_profile_id(phone)
            session_id = member_profile_id or message.custom_parameters.profile_id or conv_id
            if member_profile_id:
                logger.info(f"[setup] outbound session resolved to member profile {member_profile_id} for phone={phone}")
        else:
            session_id = message.custom_parameters.profile_id or conv_id

        # Store phone + resolved profile in outbound_conversation_map so the CI webhook
        # can write summaries to the correct member profile without re-running lookupProfileId.
        if ctx and phone:
            outbound_conversation_map[conv_id] = {"phone": phone, "profileId": member_profile_id or ""}
            logger.info(f"[setup] outbound_conversation_map[{conv_id}] phone={phone} profileId={member_profile_id or '(pending)'}")

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
            phone = ctx["phone"]
            # Preserve/update the dict entry set by _handle_setup (keeps profileId and real member phone).
            # If _handle_setup already wrote a dict, merge name in; otherwise write fresh.
            existing = outbound_conversation_map.get(conv_id)
            if isinstance(existing, dict):
                existing["name"] = ctx.get("name", "")
            else:
                outbound_conversation_map[conv_id] = {"phone": phone, "profileId": "", "name": ctx.get("name", "")}
            logger.info(f"[healthcare] outbound ctx applied conv_id={conv_id} member={ctx['name']} map={outbound_conversation_map.get(conv_id)}")
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
                memory_response, traits = await _prefetch_memory(phone)
            else:
                traits = {}
            mem_ctx = _build_memory_context(memory_response, traits)
        greeting = greeting_cache.get(conv_id)
        bedrock_mode = os.environ.get("BEDROCK_AGENT_MODE") == "agentcore"
        if greeting and bedrock_mode:
            enriched = f"[Greeting already spoken to member]\n{greeting}" + (f"\n\n{mem_ctx}" if mem_ctx else "")
        else:
            enriched = mem_ctx
        memory_context_cache[conv_id] = enriched
    else:
        enriched = ""

    system_prompt = system_prompt_cache.get(conv_id, "") if is_turn1 else ""

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

    if system_prompt:
        logger.info(f"[agent] turn1 systemPrompt:\n{system_prompt}")
    if enriched:
        logger.info(f"[agent] turn1 memoryContext:\n{enriched}")

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
                    elif msg_type == "escalate":
                        reason = str(data.get("reason", "member_requested_human"))
                        urgency = str(data.get("urgency", "normal"))
                        target_queue = str(data.get("targetQueue", FLEX_DEFAULT_QUEUE))
                        logger.info(f"[escalation] agent requested transfer conv_id={conv_id} reason={reason} urgency={urgency}")
                        escalated = await _escalate_call_to_flex(
                            conv_id=conv_id,
                            reason=reason,
                            urgency=urgency,
                            target_queue=target_queue,
                        )
                        if not escalated:
                            yield " Unfortunately I wasn't able to complete the transfer. Please try again."
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
    conversation_call_sid_map.pop(conv_id, None)
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
    """TwiML for outbound calls. Routes to ConversationRelay (agentcore) or Stream (elevenlabs)."""
    params = dict(request.query_params)
    conv_id = params.get("conv_id", "")
    ctx = pending_outbound_context.get(conv_id) if conv_id else None

    if AGENT_BACKEND == "elevenlabs":
        proto    = request.headers.get("x-forwarded-proto", "https")
        host     = request.headers.get("host", PUBLIC_DOMAIN)
        ws_proto = "wss" if proto == "https" else "ws"
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="{ws_proto}://{host}/ws-el">
      <Parameter name="conv_id" value="{conv_id}" />
    </Stream>
  </Connect>
</Response>"""
        return Response(content=twiml, media_type="application/xml")

    # agentcore — ConversationRelay
    greeting = ctx.get("greeting", "") if ctx else "Hello! This is the Owl Health Care Team."
    form = {k: str(v) for k, v in (await request.form()).items()}
    call_sid = form.get("CallSid", "")
    # Cache call_sid now so _handle_setup can map it to the Maestro conv_id
    if conv_id and call_sid:
        pending_call_sid_map[conv_id] = call_sid
    ws_url, callback_url = _get_urls(request)
    # For outbound calls swap From/To so TAC labels the member (To) as CUSTOMER.
    # This ensures memory extraction writes summaries to the member's profile,
    # not to the Twilio from-number's profile.
    raw_to   = form.get("To", "")
    raw_from = form.get("From", "")
    is_outbound = bool(ctx)
    twiml = await voice_channel.handle_incoming_call(
        to_number=raw_from if is_outbound else raw_to,
        from_number=raw_to if is_outbound else raw_from,
        options={
            "websocket_url": ws_url,
            "action_url": callback_url,
            "welcome_greeting": greeting,
            "custom_parameters": {
                "outboundConvId": conv_id,
                "callSid": call_sid,
            } if conv_id else {"callSid": call_sid},
        },
        call_sid=call_sid,
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
    """IPC endpoint — Node.js app server POSTs outbound ctx before dialling."""
    body = await request.json()
    conv_id = body.get("conv_id", "")
    if not conv_id:
        return {"success": False, "error": "conv_id required"}
    pending_outbound_context[conv_id] = body
    logger.info(f"[ipc] outbound context stored conv_id={conv_id} member={body.get('name')}")

    # Forward to ElevenLabs server so it has context when the Stream WebSocket connects
    if AGENT_BACKEND == "elevenlabs":
        import httpx
        try:
            async with httpx.AsyncClient() as client:
                await client.post(f"{EL_BASE}/set-outbound-context", json=body, timeout=5)
            logger.info(f"[ipc] forwarded context to ElevenLabs server conv_id={conv_id}")
        except Exception as e:
            logger.warning(f"[ipc] ElevenLabs context forward failed: {e}")

    return {"success": True}


@app.get("/get-outbound-phone/{conv_id}")
async def get_outbound_phone(conv_id: str) -> dict:
    """IPC endpoint — Node.js CI webhook calls this to resolve member phone + profileId."""
    entry = outbound_conversation_map.get(conv_id)
    if isinstance(entry, dict):
        return {"phone": entry.get("phone", ""), "profileId": entry.get("profileId", "")}
    if isinstance(entry, str):
        # Legacy string entry (elevenlabs ws-el path) — no profileId cached
        return {"phone": entry, "profileId": ""}
    phone = ""

    # Fallback for elevenlabs backend: the stream start event has no Maestro conv_id,
    # so the map is keyed by synthetic outbound-* id. Look up participants via Conversations API.
    if conv_id.startswith("conv_conversation_") and MEMORY_API_KEY:
        import httpx
        try:
            async with httpx.AsyncClient() as client:
                res = await client.get(
                    f"https://conversations.twilio.com/v2/Conversations/{conv_id}/Participants",
                    auth=(MEMORY_API_KEY, MEMORY_API_TOKEN),
                    timeout=5,
                )
                participants = res.json().get("participants", [])
                our_number = os.environ.get("TWILIO_TAC_PHONE_NUMBER", "")
                for p in participants:
                    addresses = p.get("addresses") or []
                    address = addresses[0].get("address", "") if addresses else ""
                    # Pick the participant whose address is not our Twilio number
                    if address and address != our_number and address.startswith("+"):
                        outbound_conversation_map[conv_id] = address
                        logger.info(f"[get-outbound-phone] resolved via API {conv_id} → {address}")
                        return {"phone": address}
        except Exception as e:
            logger.warning(f"[get-outbound-phone] API fallback failed: {e}")

    return {"phone": phone}


@app.websocket("/ws-el")
async def ws_el_proxy(websocket: WebSocket) -> None:
    """Proxy Twilio <Stream> WebSocket to ElevenLabs server (elevenlabs backend only)."""
    await websocket.accept()
    el_ws_url = f"ws://localhost:{EL_PORT}/ws"
    try:
        async with websockets.connect(el_ws_url) as el_ws:
            async def twilio_to_el() -> None:
                async for msg in websocket.iter_text():
                    # Intercept the start event to map Maestro conv_id → phone
                    try:
                        parsed = json.loads(msg)
                        if parsed.get("event") == "start":
                            start = parsed.get("start", {})
                            logger.info(f"[ws-el] start event keys: {list(start.keys())} customParameters={start.get('customParameters')}")
                            maestro_conv_id = start.get("conversationSid", "")
                            outbound_conv_id = start.get("customParameters", {}).get("conv_id", "")
                            if outbound_conv_id and outbound_conv_id in pending_outbound_context:
                                ctx = pending_outbound_context[outbound_conv_id]
                                phone = ctx.get("phone", "")
                                # Map the real Maestro conv_id if available, else keep synthetic id
                                map_key = maestro_conv_id or outbound_conv_id
                                member_pid = _lookup_profile_id(phone) if phone else ""
                                outbound_conversation_map[map_key] = {"phone": phone, "profileId": member_pid or ""}
                                logger.info(f"[ws-el] mapped {map_key} → phone={phone} profileId={member_pid or '(none)'}")
                    except Exception:
                        pass
                    await el_ws.send(msg)

            async def el_to_twilio() -> None:
                async for msg in el_ws:
                    await websocket.send_text(msg if isinstance(msg, str) else msg.decode())

            await asyncio.gather(twilio_to_el(), el_to_twilio())
    except Exception as e:
        logger.error(f"[ws-el] proxy error: {e}")


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


@app.post("/browser-answer-twiml")
async def browser_answer_twiml(request: Request) -> Response:
    """TwiML: connect member's answered call back to the browser via Twilio Client."""
    twiml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Dial>
    <Client>care-team-agent</Client>
  </Dial>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


@app.post("/sms")
async def post_sms(request: Request) -> Response:
    """Handle inbound SMS reply from a member and reply via AgentCore."""
    form = dict(await request.form())
    from_phone = form.get("From", "")
    body = form.get("Body", "").strip()
    empty_twiml = Response(content="<?xml version='1.0'?><Response/>", media_type="application/xml")
    if not body or not from_phone:
        return empty_twiml

    # Allow simulating a member's SMS conversation using a personal test phone.
    # When SMS_SIMULATE_MEMBER_PHONE is set and the message comes from OUTBOUND_CALL_TO,
    # use the member's phone for profile lookup/memory — reply still goes to from_phone.
    lookup_phone = from_phone
    if SMS_SIMULATE_MEMBER_PHONE and OUTBOUND_CALL_TO and from_phone == OUTBOUND_CALL_TO:
        lookup_phone = SMS_SIMULATE_MEMBER_PHONE
        logger.info(f"[sms] simulating member {lookup_phone} from personal phone {from_phone}")

    loop = asyncio.get_event_loop()
    profile_id = await loop.run_in_executor(None, _lookup_profile_id, lookup_phone)
    if not profile_id:
        logger.warning(f"[sms] no profile for {lookup_phone}")
        return empty_twiml

    logger.info(f"[sms] inbound from={from_phone} lookup_phone={lookup_phone} profile_id={profile_id} body=\"{body[:80]}\"")

    memory, traits = await _prefetch_memory(lookup_phone)
    context = _build_memory_context(memory, traits)
    system_prompt = _build_inbound_system_prompt()

    try:
        reply = await _invoke_agentcore_http(
            session_id=profile_id,
            prompt=body,
            system_prompt=system_prompt,
            context=context,
        )
    except Exception as e:
        logger.error(f"[sms] AgentCore invocation failed: {e}")
        return empty_twiml

    logger.info(f"[sms] reply profile_id={profile_id} reply=\"{reply[:80]}\"")

    if SMS_WRITE_OBSERVATION:
        await loop.run_in_executor(None, _write_sms_observation, profile_id, "member", body)
        await loop.run_in_executor(None, _write_sms_observation, profile_id, "agent", reply)

    if twilio_client and PHONE_NUMBER:
        try:
            twilio_client.messages.create(to=from_phone, from_=PHONE_NUMBER, body=reply)
            logger.info(f"[sms] reply sent to={from_phone}")
        except Exception as e:
            logger.error(f"[sms] send failed: {e}")

    return empty_twiml


@app.post("/escalate-call")
async def escalate_call_endpoint(request: Request) -> dict:
    """Care-team-initiated escalation. Accepts conv_id or profile_id."""
    body = await request.json()
    conv_id = body.get("conv_id", "")
    profile_id = body.get("profile_id", "")
    reason = body.get("reason", "care_team_requested")

    # Resolve conv_id from profile_id via reverse-lookup on outbound_conversation_map
    if not conv_id and profile_id:
        for cid, entry in outbound_conversation_map.items():
            pid = entry.get("profileId", "") if isinstance(entry, dict) else ""
            if pid == profile_id:
                conv_id = cid
                break

    # Also check system_prompt_cache (all active conversations) as fallback
    if not conv_id and profile_id:
        for cid in list(system_prompt_cache.keys()):
            if conversation_call_sid_map.get(cid):
                conv_id = cid
                break

    if not conv_id:
        return {"success": False, "error": "No active call found for this member"}

    success = await _escalate_call_to_flex(conv_id, reason, "normal", FLEX_DEFAULT_QUEUE)
    return {"success": success}


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
