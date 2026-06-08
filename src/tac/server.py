"""
Multi-app Python TAC Server.

Supports multiple apps (healthcare, pubsec, etc.) on one server.
Each app is configured via src/tac/apps/<id>.json and gets its own
URL-prefixed routes: /{app}/twiml, /{app}/ws, /{app}/sms, etc.

Shared routes (no prefix): /health, /get-outbound-phone/{conv_id}, /ci-webhook
"""

import asyncio
import json
import logging
import os
import re as _re
import pathlib
import time
from collections.abc import AsyncGenerator
from typing import Optional

import httpx
import requests
import websockets
from base64 import b64encode
from xml.sax.saxutils import escape
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import Response, JSONResponse, StreamingResponse

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

MEMORY_BASE = "https://memory.twilio.com"
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
PUBLIC_DOMAIN = (os.environ.get("VOICE_PUBLIC_DOMAIN") or "").lstrip("https://").lstrip("http://")
TAC_PORT = int(os.environ.get("TAC_PORT", "8000"))
APP_PORT = int(os.environ.get("APP_PORT", "8001"))

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN  = os.environ.get("TWILIO_AUTH_TOKEN", "")
ESCALATION_ENABLED = os.environ.get("ESCALATION_ENABLED", "false").lower() in ("1", "true", "yes")
FLEX_HANDOFF_APPLICATION_SID = os.environ.get("TWILIO_FLEX_HANDOFF_APPLICATION_SID", "")
FLEX_WORKFLOW_SID = os.environ.get("TWILIO_FLEX_WORKFLOW_SID", "")
FLEX_WORKSPACE_SID = os.environ.get("TWILIO_FLEX_WORKSPACE_SID", "")

from twilio.rest import Client as TwilioClient
twilio_client: Optional[TwilioClient] = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if TWILIO_ACCOUNT_SID else None

# TAC (memory retrieval disabled; we fetch it ourselves)
tac = TAC(config=TACConfig.from_env())


@dataclass
class AppConfig:
    id: str
    display_name: str
    route_prefix: str
    phone_number: str
    agent_backend_name: str       # "agentcore" | "vertexai" | "elevenlabs"
    agent_local_ws_url: str
    agentcore_arn: str
    vertexai_project: str
    vertexai_location: str
    vertexai_agent_id: str
    el_port: int
    memory_store_id: str
    memory_api_key: str
    memory_api_token: str
    flex_queue: str
    flex_escalation_announcement: str
    inbound_greeting: str
    default_outbound_greeting: str
    outbound_system_prompt_prefix: str
    system_prompt_inbound_file: str
    system_prompt_sms_file: str
    default_inbound_prompt: str
    sms_write_observation: bool
    outbound_call_to: str
    sms_simulate_member_phone: str


def _load_app_configs() -> dict[str, "AppConfig"]:
    """Load all app configs from src/tac/apps/*.json, keyed by route_prefix."""
    apps: dict[str, AppConfig] = {}
    apps_dir = pathlib.Path(__file__).parent / "apps"
    for cfg_file in sorted(apps_dir.glob("*.json")):
        try:
            raw = json.loads(cfg_file.read_text())
            ev = os.environ.get  # shorthand

            agent_local_url = ev(raw.get("agent_local_url_env", ""), "").rstrip("/")
            agent_local_ws_url = (
                agent_local_url.replace("http://", "ws://").replace("https://", "wss://") + "/ws"
                if agent_local_url else ""
            )

            cfg = AppConfig(
                id=raw["id"],
                display_name=raw["display_name"],
                route_prefix=raw["route_prefix"].rstrip("/"),
                phone_number=ev(raw.get("phone_number_env", ""), ""),
                agent_backend_name=ev(raw.get("agent_backend_env", ""), "agentcore"),
                agent_local_ws_url=agent_local_ws_url,
                agentcore_arn=ev(raw.get("agentcore_arn_env", ""), ""),
                vertexai_project=ev(raw.get("vertexai_project_env", ""), ""),
                vertexai_location=ev(raw.get("vertexai_location_env", ""), "us-central1"),
                vertexai_agent_id=ev(raw.get("vertexai_agent_id_env", ""), ""),
                el_port=int(ev(raw.get("elevenlabs_port_env", ""), "8002") or "8002"),
                memory_store_id=ev(raw.get("memory_store_env", ""), ""),
                memory_api_key=ev(raw.get("memory_api_key_env", ""), ""),
                memory_api_token=ev(raw.get("memory_api_token_env", ""), ""),
                flex_queue=raw.get("flex_queue", raw["id"]),
                flex_escalation_announcement=raw.get("flex_escalation_announcement", "Please hold while I connect you."),
                inbound_greeting=raw.get("inbound_greeting", "Hello! How can I assist you today?"),
                default_outbound_greeting=raw.get("default_outbound_greeting", "Hello!"),
                outbound_system_prompt_prefix=raw.get("outbound_system_prompt_prefix", "You are calling {name}."),
                system_prompt_inbound_file=raw.get("system_prompt_inbound_file", "system_prompt_inbound.txt"),
                system_prompt_sms_file=raw.get("system_prompt_sms_file", "system_prompt_sms.txt"),
                default_inbound_prompt=raw.get("default_inbound_prompt", "You are a helpful assistant."),
                sms_write_observation=ev(raw.get("sms_write_observation_env", ""), "false").lower() == "true",
                outbound_call_to=ev(raw.get("outbound_call_to_env", ""), ""),
                sms_simulate_member_phone=ev(raw.get("sms_simulate_member_phone_env", ""), ""),
            )
            apps[cfg.route_prefix] = cfg
            logger.info(f"[config] loaded app id={cfg.id} prefix={cfg.route_prefix} backend={cfg.agent_backend_name}")
        except Exception as e:
            logger.error(f"[config] failed to load {cfg_file.name}: {e}")
    if not apps:
        logger.error("[config] no app configs found in src/tac/apps/ — server will have no routes")
    return apps


ALL_APPS = _load_app_configs()
# Reverse lookup: phone_number → AppConfig (for inbound call routing)
_PHONE_TO_APP: dict[str, AppConfig] = {
    cfg.phone_number: cfg for cfg in ALL_APPS.values() if cfg.phone_number
}

# ---------------------------------------------------------------------------
# Agent backend abstraction
# ---------------------------------------------------------------------------

class AgentCoreBackend:
    """AWS AgentCore backend — persistent presigned WebSocket per session."""

    _WS_TTL = 270  # presigned URLs expire at 300s; reconnect before that

    def __init__(self, cfg: "AppConfig") -> None:
        self._cfg = cfg
        self._connections: dict[str, websockets.ClientConnection] = {}
        self._connection_times: dict[str, float] = {}
        self._client: Optional[AgentCoreRuntimeClient] = None
        if cfg.agentcore_arn:
            self._client = AgentCoreRuntimeClient(region=AWS_REGION)
        else:
            logger.warning(f"[{cfg.id}] agentcore_arn not set — AgentCore calls will fail")

    async def get_or_create_ws(self, session_id: str) -> Optional[websockets.ClientConnection]:
        if session_id in self._connections:
            ws = self._connections[session_id]
            age = time.time() - self._connection_times.get(session_id, 0)
            if ws.state.name == "OPEN" and age < self._WS_TTL:
                return ws
            logger.info(f"[{self._cfg.id}][agentcore] evicting session_id={session_id} age={age:.0f}s")
            del self._connections[session_id]
            self._connection_times.pop(session_id, None)
            try:
                await ws.close()
            except Exception:
                pass

        try:
            t0 = time.time()
            if self._cfg.agent_local_ws_url:
                url = self._cfg.agent_local_ws_url
                logger.info(f"[{self._cfg.id}][agentcore] local dev connecting to {url}")
            else:
                if not self._client:
                    logger.error(f"[{self._cfg.id}][agentcore] no client and agent_local_url not set")
                    return None
                url = self._client.generate_presigned_url(
                    runtime_arn=self._cfg.agentcore_arn,
                    session_id=session_id,
                )
            import ssl as _ssl
            _ssl_ctx = _ssl.create_default_context()
            _ssl_ctx.check_hostname = False
            _ssl_ctx.verify_mode = _ssl.CERT_NONE
            ws = await websockets.connect(url, ssl=_ssl_ctx)
            self._connections[session_id] = ws
            self._connection_times[session_id] = time.time()
            logger.info(f"[{self._cfg.id}][agentcore] connected session_id={session_id} in {(time.time()-t0)*1000:.0f}ms")
            return ws
        except Exception as e:
            logger.error(f"[{self._cfg.id}][agentcore] connection failed: {e}")
            return None

    async def prewarm(self, session_id: str) -> bool:
        ws = await self.get_or_create_ws(session_id)
        return ws is not None

    async def invoke(self, session_id: str, prompt: str, system_prompt: str, context: str,
                     member_phone: str = "", profile_id: str = "",
                     member_traits: Optional[dict] = None) -> AsyncGenerator[dict, None]:
        ws = await self.get_or_create_ws(session_id)
        if not ws:
            raise RuntimeError(f"[{self._cfg.id}] Could not connect to AgentCore")
        await ws.send(json.dumps({
            "type": "prompt",
            "voicePrompt": prompt,
            "systemPrompt": system_prompt,
            "memoryContext": context,
        }))
        async for raw in ws:
            data = json.loads(raw)
            msg_t = data.get("type") if isinstance(data, dict) else type(data).__name__
            if msg_t != "text":
                logger.info(f"[{self._cfg.id}][agentcore-ws] non-text msg type={msg_t} data={str(data)[:200]}")
            yield data
            if isinstance(data, dict) and data.get("type") == "text" and data.get("last"):
                break

    async def interrupt(self, session_id: str, utterance: str) -> None:
        ws = self._connections.get(session_id)
        if ws:
            try:
                await ws.send(json.dumps({"type": "interrupt", "utterance_until_interrupt": utterance}))
            except Exception:
                pass

    async def close_session(self, session_id: str) -> None:
        ws = self._connections.pop(session_id, None)
        self._connection_times.pop(session_id, None)
        if ws:
            try:
                await ws.close()
            except Exception:
                pass


class VertexAIBackend:
    """Google Vertex AI Agent Engine backend — REST streaming per turn, server-side sessions."""

    def __init__(self, cfg: "AppConfig") -> None:
        self._cfg = cfg
        self._agent = None
        self._initialized = False

    def _get_agent(self):
        if self._initialized:
            return self._agent
        self._initialized = True
        if not self._cfg.vertexai_project or not self._cfg.vertexai_agent_id:
            logger.error(f"[{self._cfg.id}][vertexai] vertexai_project and vertexai_agent_id must be set")
            return None
        try:
            import vertexai
            from vertexai import agent_engines
            vertexai.init(project=self._cfg.vertexai_project, location=self._cfg.vertexai_location)
            self._agent = agent_engines.get(self._cfg.vertexai_agent_id)
            logger.info(f"[{self._cfg.id}][vertexai] initialized agent={self._cfg.vertexai_agent_id}")
        except Exception as e:
            logger.error(f"[{self._cfg.id}][vertexai] init failed: {e}")
            self._agent = None
        return self._agent

    async def prewarm(self, session_id: str) -> bool:
        loop = asyncio.get_event_loop()
        agent = await loop.run_in_executor(None, self._get_agent)
        return agent is not None

    async def invoke(self, session_id: str, prompt: str, system_prompt: str, context: str,
                     member_phone: str = "", profile_id: str = "",
                     member_traits: Optional[dict] = None) -> AsyncGenerator[dict, None]:
        loop = asyncio.get_event_loop()
        agent = await loop.run_in_executor(None, self._get_agent)
        if not agent:
            raise RuntimeError(f"[{self._cfg.id}] Could not connect to Vertex AI Agent Engine")

        full_prompt = prompt
        if system_prompt or context:
            parts = [p for p in [system_prompt, context, prompt] if p]
            full_prompt = "\n\n".join(parts)

        def _stream():
            return list(agent.stream_query(
                user_id=session_id,
                message=full_prompt,
                session_id=session_id,
            ))

        t0 = time.time()
        events = await loop.run_in_executor(None, _stream)
        logger.info(f"[{self._cfg.id}][vertexai] stream complete session_id={session_id} events={len(events)} in {(time.time()-t0)*1000:.0f}ms")

        full_text = ""
        for event in events:
            if hasattr(event, "content") and hasattr(event.content, "parts"):
                for part in event.content.parts:
                    if hasattr(part, "text") and part.text:
                        full_text += part.text
            elif hasattr(event, "text") and event.text:
                full_text += event.text

        clean_lines = []
        for line in full_text.splitlines():
            if line.startswith("__SIGNAL__"):
                try:
                    yield json.loads(line[len("__SIGNAL__"):])
                except Exception:
                    logger.warning(f"[{self._cfg.id}][vertexai] malformed signal line: {line[:200]}")
            else:
                clean_lines.append(line)
        clean_text = "\n".join(clean_lines).strip()

        if clean_text:
            for word in clean_text.split(" "):
                yield {"type": "text", "token": word + " ", "last": False}
        yield {"type": "text", "token": "", "last": True}

    async def interrupt(self, session_id: str, utterance: str) -> None:
        pass

    async def close_session(self, session_id: str) -> None:
        pass


def _make_backend(cfg: "AppConfig") -> AgentCoreBackend | VertexAIBackend:
    if cfg.agent_backend_name == "vertexai":
        logger.info(f"[{cfg.id}] agent provider: Vertex AI (Gemini)")
        return VertexAIBackend(cfg)
    logger.info(f"[{cfg.id}] agent provider: AWS AgentCore")
    return AgentCoreBackend(cfg)


# Per-app backend instances
_APP_BACKENDS: dict[str, AgentCoreBackend | VertexAIBackend] = {
    prefix: _make_backend(cfg) for prefix, cfg in ALL_APPS.items()
}

# ---------------------------------------------------------------------------
# State  (all global dicts; conv_id keys are unique across apps)
# ---------------------------------------------------------------------------
pending_outbound_context: dict[str, dict] = {}
outbound_conversation_map: dict[str, dict] = {}
conversation_call_sid_map: dict[str, str] = {}
pending_call_sid_map: dict[str, str] = {}
system_prompt_cache: dict[str, str] = {}
greeting_cache: dict[str, str] = {}
_greeting_by_phone: dict[str, str] = {}   # phone → greeting text, set at /twiml time
memory_context_cache: dict[str, str] = {}
# Maps conv_id → route_prefix so shared endpoints can find the right app
conv_app_map: dict[str, str] = {}
# Maps profileId → route_prefix — survives conversation cleanup so CI webhook can still resolve app
profile_app_map: dict[str, str] = {}

# Chat channel: Classic Conversations SID (CH...) ↔ CO conversation ID
chat_classic_to_co: dict[str, str] = {}
chat_co_to_classic: dict[str, str] = {}
chat_escalated_convs: set[str] = set()   # conv_sids handed off to Flex — reject further messages

# Tracks CO conv_ids that originated from browser/WebRTC calls (device.connect)
# Used to pick the right Flex escalation path (Enqueue vs Application TwiML)
browser_call_conv_ids: set[str] = set()
flex_task_sid_map: dict[str, str] = {}          # conv_id → TaskRouter task SID for browser escalations
flex_task_profile_map: dict[str, str] = {}      # profileId → task SID

# ---------------------------------------------------------------------------
# Escalation helpers
# ---------------------------------------------------------------------------

def _app_for_conv(conv_id: str) -> Optional["AppConfig"]:
    prefix = conv_app_map.get(conv_id)
    return ALL_APPS.get(prefix) if prefix else None


def _twilio_basic_auth_header() -> dict[str, str]:
    creds = f"{TWILIO_ACCOUNT_SID}:{TWILIO_AUTH_TOKEN}".encode("utf-8")
    return {"Authorization": f"Basic {b64encode(creds).decode('ascii')}"}


def _build_flex_transfer_twiml(conv_id: str, reason: str, urgency: str, target_queue: str,
                               member_phone: str = "", member_name: str = "", member_profile_id: str = "",
                               is_browser_call: bool = False, cfg_phone: str = "",
                               browser_caller_id: str = "", direction: str = "inbound") -> str:
    if is_browser_call and FLEX_WORKFLOW_SID:
        import json as _json
        # Browser/WebRTC: Studio always reads From from the live call (= "client:care-team-agent"),
        # so the Studio flow path always produces invalid task attributes. Bypass Studio entirely
        # with <Enqueue> which lets us set explicit task attributes including a real E.164 "from".
        # Flex Conference Instruction uses task.from as caller ID — must be an E.164 number.
        task_attrs = {
            "taskType": "voice",
            "customerAddress": member_phone or "",
            "from": browser_caller_id or cfg_phone or "",
            "callerId": browser_caller_id or cfg_phone or "",
            "caller": member_phone or "",
            "called": cfg_phone or "",
            "name": member_name or "",
            "memberProfileId": member_profile_id or "",
            "reason": reason,
            "direction": direction,
        }
        attrs_json = _json.dumps(task_attrs).replace('"', '&quot;')
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            "<Response>"
            f'<Enqueue workflowSid="{escape(FLEX_WORKFLOW_SID)}">'
            f'<Task attributes="{attrs_json}"/>'
            "</Enqueue>"
            "</Response>"
        )
    # PSTN calls: copyParentTo copies the real E.164 From automatically
    params = f'<Parameter name="reason" value="{escape(reason)}" />'
    if member_phone:
        params += f'<Parameter name="memberPhone" value="{escape(member_phone)}" />'
    if member_name:
        params += f'<Parameter name="memberName" value="{escape(member_name)}" />'
    if member_profile_id:
        params += f'<Parameter name="memberProfileId" value="{escape(member_profile_id)}" />'
    params += f'<Parameter name="direction" value="{escape(direction)}" />'
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


async def _escalate_chat_to_flex(
    conv_sid: str, co_conv_id: str, reason: str, urgency: str,
    cfg: "AppConfig", visitor_phone: str, profile_id: str,
) -> bool:
    """Create a TaskRouter chat task so a Flex agent can pick up the Conversations thread."""
    logger.info(f"[{cfg.id}][chat-escalation] attempt conv_sid={conv_sid} reason={reason} enabled={ESCALATION_ENABLED}")
    if not ESCALATION_ENABLED:
        logger.warning(f"[{cfg.id}][chat-escalation] ESCALATION_ENABLED=false — set to true in .env and restart")
        return False
    if not FLEX_WORKSPACE_SID or not FLEX_WORKFLOW_SID:
        logger.error(f"[{cfg.id}][chat-escalation] missing FLEX_WORKSPACE_SID or FLEX_WORKFLOW_SID")
        return False
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        logger.error(f"[{cfg.id}][chat-escalation] missing Twilio credentials")
        return False

    map_entry = outbound_conversation_map.get(co_conv_id) or {}
    member_name       = map_entry.get("name", "")             if isinstance(map_entry, dict) else ""
    member_phone      = map_entry.get("phone", visitor_phone) if isinstance(map_entry, dict) else visitor_phone
    member_profile_id = map_entry.get("profileId", profile_id) if isinstance(map_entry, dict) else profile_id

    flex_flow_sid        = os.environ.get("FLEX_CHAT_FLOW_SID", "")
    chat_service_sid_env = os.environ.get("TWILIO_CHAT_CONVERSATION_SERVICE_SID", "")
    task_attrs = {
        # Flex reserved fields for display name and channel rendering
        "customer_name": member_name or member_phone,
        "customerName":  member_name or member_phone,
        "name":          member_name or member_phone,
        "from":          member_phone,
        # Channel fields Flex needs to render the conversation thread in the task panel
        "channelType":        "web",
        "conversationSid":    conv_sid,
        "conversationServiceSid": chat_service_sid_env,
        # Custom fields
        "taskType":        "chat",
        "customerAddress": member_phone,
        "memberProfileId": member_profile_id,
        "reason":          reason,
        "urgency":         urgency,
        "direction":       "inbound",
    }
    if flex_flow_sid:
        task_attrs["flexFlowSid"] = flex_flow_sid
    logger.info(f"[{cfg.id}][chat-escalation] task_attrs={task_attrs}")

    task_url = f"https://taskrouter.twilio.com/v1/Workspaces/{FLEX_WORKSPACE_SID}/Tasks"
    auth_headers = {"Content-Type": "application/x-www-form-urlencoded", **_twilio_basic_auth_header()}

    def _create_task() -> requests.Response:
        return requests.post(task_url, data={
            "WorkflowSid": FLEX_WORKFLOW_SID,
            "TaskChannel": "chat",
            "Attributes": json.dumps(task_attrs),
        }, headers=auth_headers, timeout=10)

    try:
        res = await asyncio.get_event_loop().run_in_executor(None, _create_task)
        logger.info(f"[{cfg.id}][chat-escalation] TaskRouter response status={res.status_code} body={res.text[:400]}")
        if res.status_code < 300:
            task_data = res.json()
            task_sid = task_data.get("sid", "")
            flex_task_sid_map[co_conv_id] = task_sid
            if member_profile_id:
                flex_task_profile_map[member_profile_id] = task_sid
            logger.info(f"[{cfg.id}][chat-escalation] task created task_sid={task_sid} name={member_name!r} profileId={member_profile_id}")

            # Add Flex agent as a Conversations participant so they can send messages
            flex_agent_identity = os.environ.get("FLEX_AGENT_IDENTITY", "")
            chat_service_sid = os.environ.get("TWILIO_CHAT_CONVERSATION_SERVICE_SID", "")
            if flex_agent_identity and twilio_client and chat_service_sid:
                def _add_agent_participant() -> None:
                    try:
                        twilio_client.conversations.v1 \
                            .services(chat_service_sid) \
                            .conversations(conv_sid) \
                            .participants.create(identity=flex_agent_identity)
                        logger.info(f"[{cfg.id}][chat-escalation] added flex agent participant identity={flex_agent_identity!r}")
                    except Exception as e:
                        logger.warning(f"[{cfg.id}][chat-escalation] add participant failed (may already exist): {e}")
                await asyncio.get_event_loop().run_in_executor(None, _add_agent_participant)
            else:
                logger.warning(f"[{cfg.id}][chat-escalation] FLEX_AGENT_IDENTITY not set — Flex agent won't be able to type")

            return True
        logger.error(f"[{cfg.id}][chat-escalation] task creation failed status={res.status_code}")
        return False
    except Exception as e:
        logger.error(f"[{cfg.id}][chat-escalation] exception: {e}", exc_info=True)
        return False


async def _escalate_call_to_flex(conv_id: str, reason: str, urgency: str, target_queue: str) -> bool:
    app_cfg = _app_for_conv(conv_id)
    effective_queue = target_queue or (app_cfg.flex_queue if app_cfg else "default")
    logger.info(f"[escalation] attempt conv_id={conv_id} reason={reason} urgency={urgency} queue={effective_queue} enabled={ESCALATION_ENABLED} app_sid={FLEX_HANDOFF_APPLICATION_SID or '(not set)'}")
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
    browser_caller_id = ""
    if isinstance(map_entry, dict):
        member_phone = map_entry.get("phone", "")
        member_profile_id = map_entry.get("profileId", "")
        member_name = map_entry.get("name", "")
        browser_caller_id = map_entry.get("browserCallerId", "")
    elif isinstance(map_entry, str):
        member_phone = map_entry
    # Fallback: pending_outbound_context may still have context if turn 1 hasn't run
    ctx = pending_outbound_context.get(conv_id)
    if ctx:
        if not member_name:
            member_name = ctx.get("name", "")
        if not member_phone:
            member_phone = ctx.get("phone", "")
    # If profile_id still missing, look it up from phone
    if not member_profile_id and member_phone and app_cfg:
        try:
            member_profile_id = await asyncio.get_event_loop().run_in_executor(
                None, _lookup_profile_id, member_phone, app_cfg
            )
            logger.info(f"[escalation] profile lookup result profileId={member_profile_id or '(not found)'}")
        except Exception as _e:
            logger.warning(f"[escalation] profile lookup failed: {_e}")
    logger.info(f"[escalation] resolved member_phone={member_phone or '(unknown)'} member_name={member_name or '(unknown)'} member_profile_id={member_profile_id or '(unknown)'}")

    # Detect browser/WebRTC call: outbound_conversation_map entry has no "from_pstn" marker,
    # and the conv originated from chat widget (phone stored but original From was client:xxx).
    # Reliable signal: check if conv_id is in chat_classic_to_co values (chat-originated voice call)
    # or if the map entry was set by the browser-call path (is_browser_caller flag stored at setup).
    is_browser = conv_id in browser_call_conv_ids  # populated in _handle_setup for WebRTC calls
    logger.info(f"[escalation] is_browser_call={is_browser} workflow_sid={FLEX_WORKFLOW_SID or '(not set)'}")

    loop = asyncio.get_event_loop()

    if is_browser and FLEX_WORKFLOW_SID:
        # Browser/WebRTC escalation: <Enqueue> doesn't work on WebRTC calls.
        # Instead: redirect the call into a named Twilio Conference (puts customer on hold),
        # then create a TaskRouter task via REST API with conference.sid in task attributes.
        # Flex accepts by joining the existing conference — no outbound dial needed, no From required.
        import json as _json
        import uuid as _uuid
        conference_name = f"flex-escalation-{conv_id[-12:]}-{_uuid.uuid4().hex[:8]}"
        cfg_phone = app_cfg.phone_number if app_cfg else ""

        # Step 1: redirect the call into a conference
        conf_status_callback = f"https://{PUBLIC_DOMAIN}/flex-conference-status?conv_id={conv_id}"
        conf_twiml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            "<Response>"
            "<Dial>"
            f'<Conference startConferenceOnEnter="true" endConferenceOnExit="true" '
            f'statusCallback="{conf_status_callback}" statusCallbackEvent="end" '
            f'waitUrl="https://twimlets.com/holdmusic?Bucket=com.twilio.music.classical" '
            f'beep="false">{escape(conference_name)}</Conference>'
            "</Dial>"
            "</Response>"
        )
        logger.info(f"[escalation] browser path: redirecting call_sid={call_sid} to conference={conference_name}")
        update_url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Calls/{call_sid}.json"
        auth_headers = {"Content-Type": "application/x-www-form-urlencoded", **_twilio_basic_auth_header()}

        def _redirect_call() -> requests.Response:
            return requests.post(update_url, data={"Twiml": conf_twiml}, headers=auth_headers, timeout=10)

        try:
            redirect_res = await loop.run_in_executor(None, _redirect_call)
            logger.info(f"[escalation] call redirect status={redirect_res.status_code} body={redirect_res.text[:300]}")
            if redirect_res.status_code >= 300:
                logger.error(f"[escalation] call redirect failed — falling back to PSTN path")
                is_browser = False  # fall through to PSTN path below
        except Exception as e:
            logger.error(f"[escalation] call redirect exception: {e}", exc_info=True)
            is_browser = False

        if is_browser:
            # Step 2: wait briefly for conference to be created, then fetch its SID
            await asyncio.sleep(1.5)
            conf_list_url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Conferences.json?FriendlyName={conference_name}&Status=in-progress"

            def _fetch_conference() -> requests.Response:
                return requests.get(conf_list_url, headers=auth_headers, timeout=10)

            conference_sid = ""
            try:
                conf_res = await loop.run_in_executor(None, _fetch_conference)
                conf_data = conf_res.json()
                conferences = conf_data.get("conferences", [])
                if conferences:
                    conference_sid = conferences[0].get("sid", "")
                    logger.info(f"[escalation] conference_sid={conference_sid}")
                else:
                    logger.warning(f"[escalation] conference not found yet — creating task without conference SID")
            except Exception as e:
                logger.warning(f"[escalation] could not fetch conference SID: {e}")

            # Step 3: create TaskRouter task via REST API with conference attribute
            task_attrs = {
                "taskType": "voice",
                "customerAddress": member_phone or "",
                "from": member_phone or "",
                "caller": member_phone or "",
                "called": cfg_phone or "",
                "twilioNumber": cfg_phone or "",
                "name": member_name or "",
                "memberProfileId": member_profile_id or "",
                "reason": reason,
                "direction": map_entry.get("direction", "inbound") if isinstance(map_entry, dict) else "inbound",
            }
            logger.info(f"[escalation] task direction={task_attrs['direction']!r} map_entry={map_entry!r}")
            if conference_sid:
                task_attrs["conference"] = {
                    "sid": conference_sid,
                    "participants": {"customer": call_sid},
                }

            task_url = f"https://taskrouter.twilio.com/v1/Workspaces/{FLEX_WORKSPACE_SID}/Tasks"

            def _create_task() -> requests.Response:
                return requests.post(task_url, data={
                    "WorkflowSid": FLEX_WORKFLOW_SID,
                    "TaskChannel": "voice",
                    "Attributes": _json.dumps(task_attrs),
                }, headers=auth_headers, timeout=10)

            try:
                task_res = await loop.run_in_executor(None, _create_task)
                logger.info(f"[escalation] TaskRouter task create status={task_res.status_code} body={task_res.text[:300]}")
                if task_res.status_code < 300:
                    task_data = task_res.json()
                    task_sid = task_data.get("sid", "")
                    if task_sid:
                        flex_task_sid_map[conv_id] = task_sid
                        if member_profile_id:
                            flex_task_profile_map[member_profile_id] = task_sid
                        logger.info(f"[escalation] stored task_sid={task_sid} conv_id={conv_id} profileId={member_profile_id}")
                    logger.info(f"[escalation] ✓ browser escalation complete conv_id={conv_id}")
                    try:
                        await tac.conversation_orchestrator_client.update_conversation(conv_id, status="CLOSED")
                    except Exception as e:
                        logger.error(f"[escalation] failed to close conversation: {e}")
                    return True
                else:
                    logger.error(f"[escalation] task creation failed — status={task_res.status_code}")
                    return False
            except Exception as e:
                logger.error(f"[escalation] task creation exception: {e}", exc_info=True)
                return False

    pstn_direction = map_entry.get("direction", "inbound") if isinstance(map_entry, dict) else "inbound"
    logger.info(f"[escalation] PSTN path direction={pstn_direction!r} map_entry={map_entry!r} map_entry_type={type(map_entry).__name__}")
    twiml = _build_flex_transfer_twiml(conv_id, reason, urgency, effective_queue,
                                       member_phone=member_phone,
                                       member_name=member_name,
                                       member_profile_id=member_profile_id,
                                       is_browser_call=False,
                                       cfg_phone=app_cfg.phone_number if app_cfg else "",
                                       browser_caller_id=browser_caller_id,
                                       direction=pstn_direction)
    logger.info(f"[escalation] TwiML: {twiml}")
    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Calls/{call_sid}.json"
    headers = {"Content-Type": "application/x-www-form-urlencoded", **_twilio_basic_auth_header()}
    payload = {"Twiml": twiml}

    def _do_request() -> requests.Response:
        return requests.post(url, data=payload, headers=headers, timeout=10)

    try:
        res = await loop.run_in_executor(None, _do_request)
        logger.info(f"[escalation] Twilio API response status={res.status_code} body={res.text[:500]}")
        if res.status_code >= 300:
            logger.error(f"[escalation] Twilio update failed status={res.status_code} conv_id={conv_id}")
            return False
        logger.info(f"[escalation] ✓ transferred conv_id={conv_id} call_sid={call_sid} reason={reason} urgency={urgency} queue={effective_queue}")

        try:
            await tac.conversation_orchestrator_client.update_conversation(conv_id, status="CLOSED")
            logger.info(f"[escalation] closed Maestro conversation convId={conv_id}")
        except Exception as e:
            logger.error(f"[escalation] failed to close conversation convId={conv_id}: {e}")
        return True
    except Exception as e:
        logger.error(f"[escalation] transfer exception conv_id={conv_id}: {e}", exc_info=True)
        return False


# ---------------------------------------------------------------------------
# TAC Memory helpers
# ---------------------------------------------------------------------------

def _lookup_profile_id(phone: str, cfg: "AppConfig") -> Optional[str]:
    try:
        res = requests.post(
            f"{MEMORY_BASE}/v1/Stores/{cfg.memory_store_id}/Profiles/Lookup",
            json={"idType": "phone", "value": phone},
            auth=(cfg.memory_api_key, cfg.memory_api_token),
            timeout=5,
        )
        profiles = res.json().get("profiles", [])
        return profiles[0] if profiles else None
    except Exception as e:
        logger.warning(f"[{cfg.id}][memory] lookupProfileId failed: {e}")
        return None


def _fetch_profile_traits(profile_id: str, cfg: "AppConfig") -> dict:
    """Fetch contact + outreach traits for a profile."""
    try:
        res = requests.get(
            f"{MEMORY_BASE}/v1/Stores/{cfg.memory_store_id}/Profiles/{profile_id}",
            auth=(cfg.memory_api_key, cfg.memory_api_token),
            timeout=5,
        )
        data = res.json()
        traits: dict = {}
        raw_traits = data.get("traits") or {}
        # API returns traits as {"Contact": {...}, "outreach": {...}}
        if isinstance(raw_traits, dict):
            for group_val in raw_traits.values():
                if isinstance(group_val, dict):
                    traits.update(group_val)
        elif isinstance(raw_traits, list):
            for trait_group in raw_traits:
                if not isinstance(trait_group, dict):
                    continue
                attrs = trait_group.get("attributes") or {}
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


def _fetch_memory(profile_id: str, cfg: "AppConfig") -> Optional[TACMemoryResponse]:
    try:
        base = f"{MEMORY_BASE}/v1/Stores/{cfg.memory_store_id}/Profiles/{profile_id}"
        auth = (cfg.memory_api_key, cfg.memory_api_token)
        obs_res = requests.get(f"{base}/Observations", auth=auth, timeout=5)
        sum_res = requests.get(f"{base}/ConversationSummaries", auth=auth, timeout=5)
        observations = [type("Obs", (), {"content": o["content"]})() for o in (obs_res.json() or {}).get("observations") or []]
        summaries = [type("Sum", (), {"content": s["content"]})() for s in (sum_res.json() or {}).get("summaries") or []]
        return TACMemoryResponse(observations=observations, summaries=summaries)
    except Exception as e:
        logger.warning(f"[memory] fetchMemory failed: {e}")
        return None



async def _prefetch_memory(phone: str, cfg: "AppConfig", profile_id: Optional[str] = None) -> tuple[Optional[TACMemoryResponse], dict]:
    """Run blocking memory + traits fetch in thread pool. Returns (memory, traits)."""
    loop = asyncio.get_event_loop()
    t0 = time.time()
    try:
        if not profile_id:
            profile_id = await loop.run_in_executor(None, _lookup_profile_id, phone, cfg)
        if profile_id:
            memory, traits = await asyncio.gather(
                loop.run_in_executor(None, _fetch_memory, profile_id, cfg),
                loop.run_in_executor(None, _fetch_profile_traits, profile_id, cfg),
            )
        else:
            memory, traits = None, {}
        obs = len(memory.observations) if memory else 0
        sums = len(memory.summaries) if memory else 0
        logger.info(f"[{cfg.id}][memory] prefetch phone={phone} profileId={profile_id or 'none'} obs={obs} summaries={sums} traits={len(traits)} in {(time.time()-t0)*1000:.0f}ms")
        return memory, traits
    except Exception as e:
        logger.warning(f"[{cfg.id}][memory] prefetch error: {e}")
        return None, {}


_TRAIT_SKIP = {"firstName", "lastName", "name"}  # handled separately as full name

def _build_memory_context(memory: Optional[TACMemoryResponse], traits: Optional[dict] = None) -> str:
    sections = []
    if traits:
        trait_lines = []
        full_name = (f"{traits.get('firstName', '')} {traits.get('lastName', '')}".strip()
                     or traits.get("name", ""))
        if full_name:
            trait_lines.append(f"- Name: {full_name}")
        # Include all remaining traits dynamically
        for key, val in traits.items():
            if key in _TRAIT_SKIP or not val:
                continue
            label = key.replace("_", " ").replace("-", " ")
            label = _re.sub(r'([A-Z])', r' \1', label).strip().capitalize()
            trait_lines.append(f"- {label}: {val}")
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


async def _invoke_agent(session_id: str, prompt: str, system_prompt: str, context: str,
                        cfg: "AppConfig", member_phone: str = "", profile_id: str = "",
                        member_traits: Optional[dict] = None,
                        on_schedule_call=None,
                        on_escalate=None) -> str:
    """Invoke the app's agent backend and return the full reply text.

    on_schedule_call: optional async callable(phone, reason) — chat channel shows call button
    on_escalate: optional async callable(reason, urgency) — chat channel routes to Flex
    """
    backend = _APP_BACKENDS[cfg.route_prefix]
    tokens: list[str] = []
    async for data in backend.invoke(
        session_id=session_id,
        prompt=prompt,
        system_prompt=system_prompt,
        context=context,
        member_phone=member_phone,
        profile_id=profile_id,
        member_traits=member_traits,
    ):
        if not isinstance(data, dict):
            logger.warning(f"[agent] unexpected non-dict from backend: {str(data)[:200]}")
            continue
        msg_type = data.get("type")
        logger.debug(f"[agent] msg type={msg_type}")
        if msg_type == "text":
            token = data.get("token", "")
            if token:
                tokens.append(token)
            if data.get("last", False):
                break
        elif msg_type == "schedule_call":
            sc_phone = str(data.get("phone", "")) or member_phone
            sc_reason = str(data.get("reason", ""))
            logger.info(f"[{cfg.id}][schedule_call] agent requested call profile_id={profile_id} phone={sc_phone} reason={sc_reason}")
            if on_schedule_call:
                await on_schedule_call(sc_phone, sc_reason)
            else:
                asyncio.create_task(_trigger_outbound_call_by_profile(profile_id, sc_phone, sc_reason, cfg=cfg, traits=member_traits or {}))
        elif msg_type == "escalate":
            esc_reason = str(data.get("reason", "member_requested_human"))
            esc_urgency = str(data.get("urgency", "normal"))
            logger.info(f"[{cfg.id}][escalate] agent requested escalation reason={esc_reason} urgency={esc_urgency}")
            if on_escalate:
                await on_escalate(esc_reason, esc_urgency)
            else:
                # SMS/voice context: no Flex chat channel — fall back to outbound call
                logger.info(f"[{cfg.id}][escalate] no on_escalate handler — falling back to outbound call")
                asyncio.create_task(_trigger_outbound_call_by_profile(profile_id, member_phone, esc_reason, cfg=cfg, traits=member_traits or {}))
        elif msg_type == "tool_start":
            logger.info(f"[agent] tool_start tool={data.get('tool')}")
        elif msg_type == "tool_result":
            logger.info(f"[agent] tool_result status={data.get('status')}")
        else:
            logger.info(f"[agent] msg type={msg_type} data={str(data)[:200]}")
    return "".join(tokens).strip()


async def _trigger_outbound_call(conv_id: str, phone: str, reason: str) -> None:
    """Trigger outbound call from voice context (conv_id → outbound_conversation_map)."""
    map_entry = outbound_conversation_map.get(conv_id)
    name = map_entry.get("name", "Member") if isinstance(map_entry, dict) else "Member"
    profile_id = map_entry.get("profileId", "") if isinstance(map_entry, dict) else ""
    cfg = _app_for_conv(conv_id)
    if cfg:
        await _trigger_outbound_call_by_profile(profile_id, phone, reason, cfg=cfg, name=name)


async def _trigger_outbound_call_by_profile(profile_id: str, phone: str, reason: str,
                                             cfg: Optional["AppConfig"] = None,
                                             name: str = "", traits: Optional[dict] = None) -> None:
    """Trigger outbound call using profile_id. Fetches profile traits if not provided."""
    t = traits or {}
    if not t and profile_id and cfg:
        loop = asyncio.get_event_loop()
        t = await loop.run_in_executor(None, _fetch_profile_traits, profile_id, cfg)
        logger.info(f"[schedule_call] fetched traits for profile_id={profile_id} keys={list(t.keys())}")
    if not name:
        # _fetch_profile_traits returns flat keys: firstName, lastName (from Contact group)
        name = (f"{t.get('firstName', '')} {t.get('lastName', '')}".strip()
                or t.get("name", "") or "Member")
    if not phone:
        phone = t.get("phone", "")
    goal = t.get("nextFollowUp", "") or t.get("next_follow_up", "")
    goal_desc = t.get("nextFollowUpReason", "") or t.get("next_follow_up_reason", "") or reason
    logger.info(f"[schedule_call] resolved name={name} phone={phone} goal={goal!r} traits_keys={list(t.keys())}")
    logger.info(f"[schedule_call] triggering call name={name} phone={phone} goal={goal!r} profileId={profile_id}")
    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(
                f"http://localhost:{APP_PORT}/api/outbound-call",
                json={"name": name, "phone": phone, "goal": goal, "goalDesc": goal_desc, "profileId": profile_id},
                timeout=10,
            )
            logger.info(f"[schedule_call] outbound call triggered phone={phone} status={res.status_code} body={res.text[:200]}")
    except Exception as e:
        logger.error(f"[schedule_call] failed to trigger call: {e}", exc_info=True)


async def _push_transcript_event(profile_id: str, role: str, text: str, route_prefix: str = "", ts: str = "") -> None:
    try:
        payload: dict = {"profileId": profile_id, "role": role, "text": text}
        if ts:
            payload["ts"] = ts
        async with httpx.AsyncClient() as client:
            await client.post(
                f"http://localhost:{APP_PORT}/transcript-event",
                json=payload,
                timeout=3,
            )
    except Exception as e:
        logger.warning(f"[transcript] push failed role={role}: {e}")


def _write_sms_observation(profile_id: str, role: str, content: str, cfg: "AppConfig") -> None:
    from datetime import datetime
    obs_content = f"[SMS {role}] {content}"
    try:
        requests.post(
            f"{MEMORY_BASE}/v1/Stores/{cfg.memory_store_id}/Profiles/{profile_id}/Observations",
            json={"observations": [{"content": obs_content, "occurredAt": datetime.utcnow().isoformat() + "Z", "source": "sms-conversation"}]},
            auth=(cfg.memory_api_key, cfg.memory_api_token),
            timeout=5,
        )
    except Exception as e:
        logger.warning(f"[{cfg.id}][sms] write observation failed: {e}")


def _build_system_prompt(name: str, goal: str, goal_desc: str, cfg: "AppConfig") -> str:
    prefix = cfg.outbound_system_prompt_prefix.format(name=name)
    lines = [
        prefix,
        f"The purpose of this call is: {goal}." if goal else "",
        f"Follow-up guidance: {goal_desc}" if goal_desc else "",
    ]
    return "\n".join(l for l in lines if l)


def _build_inbound_system_prompt(cfg: "AppConfig") -> str:
    prompt_file = pathlib.Path(__file__).parent / cfg.system_prompt_inbound_file
    if prompt_file.exists():
        text = prompt_file.read_text().strip()
        if text:
            return text
    return cfg.default_inbound_prompt


def _build_sms_system_prompt(cfg: "AppConfig") -> str:
    sms_file = pathlib.Path(__file__).parent / cfg.system_prompt_sms_file
    if sms_file.exists():
        text = sms_file.read_text().strip()
        if text:
            return text
    return _build_inbound_system_prompt(cfg)


def _build_chat_system_prompt(cfg: "AppConfig") -> str:
    chat_file = pathlib.Path(__file__).parent / "system_prompt_chat.txt"
    if chat_file.exists():
        text = chat_file.read_text().strip()
        if text:
            return text
    return _build_sms_system_prompt(cfg)


async def _prewarm(conv_id: str, session_id: str, phone: str, cfg: "AppConfig") -> None:
    """Pre-warm agent backend and fetch memory before the member speaks."""
    t0 = time.time()
    backend = _APP_BACKENDS[cfg.route_prefix]
    prewarm_task = asyncio.create_task(backend.prewarm(session_id))
    mem_task = asyncio.create_task(_prefetch_memory(phone, cfg)) if phone else None

    ok = await prewarm_task
    memory_result = await mem_task if mem_task else (None, {})
    memory, traits = memory_result if isinstance(memory_result, tuple) else (memory_result, {})

    if memory or traits:
        memory_context_cache[conv_id] = _build_memory_context(memory, traits)

    logger.info(f"[{cfg.id}][prewarm] done conv_id={conv_id} backend={'ok' if ok else 'FAILED'} memory={'ok' if memory else 'none'} in {(time.time()-t0)*1000:.0f}ms")


class OwlVoiceChannel(VoiceChannel):
    """VoiceChannel subclass that populates author_info, maps outbound conv_ids,
    and pre-warms the agent backend + memory before the member speaks."""

    async def _cleanup_connection(self, conv_id: str) -> None:
        """Override to always close CO conversation on WebSocket disconnect."""
        await super()._cleanup_connection(conv_id)
        try:
            await tac.conversation_orchestrator_client.update_conversation(conv_id, status="CLOSED")
            logger.info(f"[cleanup] CO conversation closed conv_id={conv_id}")
        except Exception as e:
            if "400" not in str(e) and "already" not in str(e).lower():
                logger.warning(f"[cleanup] close CO conv failed: {e}")

    async def _initialize_conversation(self, call_sid: str, setup_msg, websocket):
        """Override to capture call_sid → conv_id mapping for Flex escalation."""
        conv_id, session_state = await super()._initialize_conversation(call_sid, setup_msg, websocket)
        if conv_id and call_sid:
            conversation_call_sid_map[conv_id] = call_sid
            logger.info(f"[setup] mapped conv_id={conv_id} call_sid={call_sid}")
            # Resolve app config
            if conv_id not in conv_app_map:
                to_num = setup_msg.to_number or ""
                app_cfg = _PHONE_TO_APP.get(to_num)
                if app_cfg:
                    conv_app_map[conv_id] = app_cfg.route_prefix
                elif ALL_APPS:
                    conv_app_map[conv_id] = next(iter(ALL_APPS))
            # Map outbound conv_id if present
            cp = setup_msg.custom_parameters
            if cp is None:
                extra = {}
            elif isinstance(cp, dict):
                extra = cp
            else:
                extra = cp.model_extra or {}
            outbound_conv_id = extra.get("outboundConvId", "")
            if outbound_conv_id and outbound_conv_id in pending_outbound_context:
                pending_outbound_context[conv_id] = pending_outbound_context.pop(outbound_conv_id)
                logger.info(f"[setup] mapped outboundConvId={outbound_conv_id} → {conv_id}")
                if outbound_conv_id in conv_app_map:
                    conv_app_map[conv_id] = conv_app_map.pop(outbound_conv_id)
            # Map author info + populate outbound_conversation_map for escalation
            from_num = setup_msg.from_number or ""
            if from_num and conv_id in self._conversations:
                self._conversations[conv_id].author_info = AuthorInfo(address=from_num)
            existing_entry = outbound_conversation_map.get(conv_id)
            if not existing_entry or not (existing_entry.get("profileId") if isinstance(existing_entry, dict) else existing_entry):
                app_cfg = _app_for_conv(conv_id)
                ctx = pending_outbound_context.get(conv_id)
                member_phone_resolved = (ctx.get("phone", "") if ctx else "") or from_num
                member_name_resolved = ctx.get("name", "") if ctx else ""
                profile_id = ""
                if app_cfg and member_phone_resolved:
                    try:
                        loop = asyncio.get_event_loop()
                        profile_id = await loop.run_in_executor(None, _lookup_profile_id, member_phone_resolved, app_cfg)
                        if profile_id and not member_name_resolved:
                            traits = await loop.run_in_executor(None, _fetch_profile_traits, profile_id, app_cfg)
                            member_name_resolved = (f"{traits.get('firstName','')} {traits.get('lastName','')}".strip()
                                                    or traits.get("name", ""))
                    except Exception as _e:
                        logger.warning(f"[setup] profile lookup failed: {_e}")
                direction = "outbound" if ctx else "inbound"
                outbound_conversation_map[conv_id] = {
                    "phone": member_phone_resolved, "profileId": profile_id,
                    "name": member_name_resolved, "direction": direction,
                }
                logger.info(f"[setup] outbound_conversation_map[{conv_id}] phone={member_phone_resolved} profileId={profile_id}")
                # Store greeting in cache — already pushed to transcript at /twiml time
                greeting_text = _greeting_by_phone.pop(member_phone_resolved, "") or (ctx.get("greeting", "") if ctx else "")
                if greeting_text:
                    greeting_cache[conv_id] = greeting_text
        return conv_id, session_state

    def _handle_setup(self, message: SetupMessage) -> None:
        logger.info(f"[setup] _handle_setup called conv_id={getattr(message.custom_parameters, 'conversation_id', '?')} to={message.to_number!r} from={message.from_number!r}")
        super()._handle_setup(message)
        conv_id = message.custom_parameters.conversation_id

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

        extra = message.custom_parameters.model_extra or {}
        outbound_conv_id = extra.get("outboundConvId", "")
        if outbound_conv_id and outbound_conv_id in pending_outbound_context:
            pending_outbound_context[conv_id] = pending_outbound_context.pop(outbound_conv_id)
            logger.info(f"[setup] mapped outboundConvId={outbound_conv_id} → maestroConvId={conv_id}")
            # Inherit app mapping from the outbound conv_id
            if outbound_conv_id in conv_app_map:
                conv_app_map[conv_id] = conv_app_map.pop(outbound_conv_id)

        # Resolve app config from the called number (message.to_number = our Twilio number)
        if conv_id not in conv_app_map:
            to_num = message.to_number or ""
            app_cfg = _PHONE_TO_APP.get(to_num)
            if app_cfg:
                conv_app_map[conv_id] = app_cfg.route_prefix
            elif ALL_APPS:
                # Fallback: first app (single-app setups)
                conv_app_map[conv_id] = next(iter(ALL_APPS))
        cfg = _app_for_conv(conv_id)

        call_sid = extra.get("callSid", "")
        logger.info(f"[setup] extra keys={list(extra.keys())} callSid={call_sid!r} parentCallSid={extra.get('parentCallSid')!r}")
        if call_sid:
            conversation_call_sid_map[conv_id] = call_sid
        elif outbound_conv_id and outbound_conv_id in pending_call_sid_map:
            conversation_call_sid_map[conv_id] = pending_call_sid_map.pop(outbound_conv_id)
        if conv_id in conversation_call_sid_map:
            logger.info(f"[setup] call_sid mapped conv_id={conv_id} call_sid={conversation_call_sid_map[conv_id]}")

        ctx = pending_outbound_context.get(conv_id)
        # For browser-originated calls, the real member phone comes via custom params
        # (device.connect params → TwiML custom_parameters), not from_number
        extra_phone = extra.get("phone") or extra.get("member_phone") or ""
        raw_from_num = message.from_number or ""
        is_browser_caller = raw_from_num.startswith("client:") or not raw_from_num
        if is_browser_caller:
            browser_call_conv_ids.add(conv_id)
        phone = (ctx.get("phone", "") if ctx
                 else (extra_phone if is_browser_caller else raw_from_num))
        logger.info(f"[setup] phone resolved={phone!r} from_number={raw_from_num!r} extra_phone={extra_phone!r} is_browser={is_browser_caller}")

        member_profile_id: Optional[str] = None
        if phone and cfg:
            member_profile_id = _lookup_profile_id(phone, cfg)
            session_id = member_profile_id or conv_id
            if member_profile_id:
                logger.info(f"[setup] session resolved to member profile {member_profile_id} for phone={phone}")
        else:
            session_id = conv_id

        if phone:
            call_direction = "outbound" if ctx else "inbound"
            logger.info(f"[setup] direction={call_direction!r} ctx={bool(ctx)} raw_from_num={raw_from_num!r} is_browser_caller={is_browser_caller} phone={phone!r}")
            outbound_conversation_map[conv_id] = {"phone": phone, "profileId": member_profile_id or "", "browserCallerId": raw_from_num if is_browser_caller else "", "direction": call_direction}
            logger.info(f"[setup] outbound_conversation_map[{conv_id}] phone={phone} direction={call_direction} profileId={member_profile_id or '(pending)'}")
            if member_profile_id and cfg:
                profile_app_map[member_profile_id] = cfg.route_prefix
            greeting = ctx.get("greeting", "") if ctx else ""
            if greeting and member_profile_id:
                asyncio.get_event_loop().create_task(
                    _push_transcript_event(member_profile_id, "agent", greeting),
                    name=f"transcript-greeting-{conv_id}",
                )

        if cfg:
            asyncio.get_event_loop().create_task(
                _prewarm(conv_id, session_id, phone, cfg),
                name=f"prewarm-{conv_id}",
            )


from tac.channels.voice import VoiceChannelConfig as _VoiceChannelConfig
voice_channel = OwlVoiceChannel(tac=tac, config=_VoiceChannelConfig(memory_mode="never"))

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
    cfg = _app_for_conv(conv_id)
    if not cfg and ALL_APPS:
        # New TAC SDK doesn't call _handle_setup — populate conv_app_map with fallback
        cfg_fallback = next(iter(ALL_APPS.values()))
        conv_app_map[conv_id] = cfg_fallback.route_prefix
        cfg = cfg_fallback
        logger.info(f"[handle_message_ready] auto-mapped conv_id={conv_id} → app={cfg.id}")
    if not cfg:
        logger.error(f"[handle_message_ready] no app config for conv_id={conv_id} — dropping")
        return
    backend = _APP_BACKENDS[cfg.route_prefix]

    _map_entry = outbound_conversation_map.get(conv_id)
    _transcript_profile_id = _map_entry.get("profileId", "") if isinstance(_map_entry, dict) else ""
    # If profileId missing but phone is available, look it up and update the map
    if not _transcript_profile_id:
        _phone = _map_entry.get("phone", "") if isinstance(_map_entry, dict) else (_map_entry if isinstance(_map_entry, str) else "")
        if _phone and cfg:
            try:
                _transcript_profile_id = await asyncio.get_event_loop().run_in_executor(None, _lookup_profile_id, _phone, cfg)
                if _transcript_profile_id and isinstance(_map_entry, dict):
                    _map_entry["profileId"] = _transcript_profile_id
                elif _transcript_profile_id:
                    outbound_conversation_map[conv_id] = {"phone": _phone, "profileId": _transcript_profile_id, "name": ""}
                logger.info(f"[{cfg.id}] transcript profile lookup phone={_phone} → profileId={_transcript_profile_id!r}")
            except Exception as _e:
                logger.warning(f"[{cfg.id}] transcript profile lookup failed: {_e}")
    logger.info(f"[{cfg.id if cfg else '?'}] transcript profile_id={_transcript_profile_id!r}")
    # Push member message now that profile_id is resolved
    if _transcript_profile_id and user_message:
        asyncio.create_task(_push_transcript_event(_transcript_profile_id, "member", user_message))
    elif user_message:
        logger.warning(f"[{cfg.id}] transcript skipped — no profile_id for conv_id={conv_id}")

    is_turn1 = conv_id not in system_prompt_cache

    if is_turn1:
        ctx = pending_outbound_context.pop(conv_id, None)
        if ctx:
            system_prompt_cache[conv_id] = _build_system_prompt(ctx["name"], ctx["goal"], ctx["goalDesc"], cfg)
            greeting_cache[conv_id] = ctx.get("greeting", "")
            phone = ctx["phone"]
            existing = outbound_conversation_map.get(conv_id)
            if isinstance(existing, dict):
                existing["name"] = ctx.get("name", "")
            else:
                outbound_conversation_map[conv_id] = {"phone": phone, "profileId": "", "name": ctx.get("name", "")}
            logger.info(f"[{cfg.id}] outbound ctx applied conv_id={conv_id} member={ctx['name']}")
        else:
            system_prompt_cache[conv_id] = _build_inbound_system_prompt(cfg)
            author_addr = (context.author_info.address if context.author_info else "") or ""
            # Browser calls have author address like "client:xxx" — use the phone stored during setup
            map_entry = outbound_conversation_map.get(conv_id)
            stored_phone = map_entry.get("phone", "") if isinstance(map_entry, dict) else (map_entry or "")
            phone = stored_phone if (not author_addr or author_addr.startswith("client:")) else author_addr
            logger.info(f"[{cfg.id}] inbound session conv_id={conv_id} phone={phone!r} author_addr={author_addr!r}")

        if conv_id in memory_context_cache:
            mem_ctx = memory_context_cache[conv_id]
            logger.info(f"[{cfg.id}] using pre-warmed memory conv_id={conv_id}")
        else:
            if phone and not memory_response:
                memory_response, traits = await _prefetch_memory(phone, cfg)
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
        logger.info(f"[{cfg.id}] memory context for agent (turn 1):\n{enriched or '(empty)'}")

        # Greeting already pushed to transcript at /twiml time — no need to repeat here
    else:
        enriched = ""

    system_prompt = system_prompt_cache.get(conv_id, "") if is_turn1 else ""
    logger.info(f"[{cfg.id}] invoking agent session_id={session_id} conv_id={conv_id} turn1={is_turn1} message=\"{user_message[:60]}\"")

    try:
        async def stream_from_agent() -> AsyncGenerator[str, None]:
            first_token = False
            collected: list[str] = []
            try:
                async for data in backend.invoke(
                    session_id=session_id,
                    prompt=user_message,
                    system_prompt=system_prompt,
                    context=enriched,
                ):
                    msg_type = data.get("type", "")
                    if msg_type not in ("text",):
                        logger.info(f"[agent] voice msg type={msg_type} data={str(data)[:200]}")
                    if msg_type == "text":
                        token = data.get("token", "")
                        if token:
                            if not first_token:
                                logger.info(f"[{cfg.id}] TTFT {(time.time()-t0)*1000:.0f}ms")
                                first_token = True
                            collected.append(token)
                            yield token
                        if data.get("last", False):
                            if _transcript_profile_id and collected:
                                asyncio.create_task(_push_transcript_event(
                                    _transcript_profile_id, "agent", "".join(collected)
                                ))
                            break
                    elif msg_type == "schedule_call":
                        sc_phone = str(data.get("phone", ""))
                        sc_reason = str(data.get("reason", ""))
                        if not sc_phone:
                            map_entry = outbound_conversation_map.get(conv_id)
                            sc_phone = map_entry.get("phone", "") if isinstance(map_entry, dict) else ""
                        logger.info(f"[{cfg.id}][schedule_call] agent requested call conv_id={conv_id} phone={sc_phone}")
                        asyncio.create_task(_trigger_outbound_call(conv_id, sc_phone, sc_reason))
                        break
                    elif msg_type == "escalate":
                        reason = str(data.get("reason", "member_requested_human"))
                        urgency = str(data.get("urgency", "normal"))
                        target_queue = str(data.get("targetQueue", cfg.flex_queue))
                        logger.info(f"[{cfg.id}][escalation] agent requested transfer conv_id={conv_id} reason={reason}")
                        escalated = await _escalate_call_to_flex(conv_id=conv_id, reason=reason, urgency=urgency, target_queue=target_queue)
                        if not escalated:
                            yield " Unfortunately I wasn't able to complete the transfer. Please try again."
                        else:
                            try:
                                await tac.conversation_orchestrator_client.update_conversation(conv_id, status="CLOSED")
                                logger.info(f"[{cfg.id}][escalation] CO conversation closed after handoff conv_id={conv_id}")
                            except Exception as _ce:
                                logger.warning(f"[{cfg.id}][escalation] close CO conv failed: {_ce}")
                        break
                    elif msg_type == "tool_start":
                        logger.info(f"[agent] tool_start tool={data.get('tool')}")
                    elif msg_type == "tool_result":
                        logger.info(f"[agent] tool_result status={data.get('status')}")
            except Exception as e:
                logger.error(f"[agent] stream error: {e}")
                yield "I'm sorry, something went wrong."

        await voice_channel.send_response(conv_id, stream_from_agent(), role="assistant")

    except Exception as e:
        logger.error(f"[{cfg.id}] message error: {e}", exc_info=True)
        await voice_channel.send_response(conv_id, "I'm sorry, something went wrong.", role="assistant")

    logger.info(f"[{cfg.id}] end-to-end {(time.time()-t0)*1000:.0f}ms session_id={session_id}")


async def handle_conversation_ended(context: ConversationSession) -> None:
    conv_id = context.conversation_id
    session_id = context.profile_id or conv_id
    cfg = _app_for_conv(conv_id)
    if cfg:
        await _APP_BACKENDS[cfg.route_prefix].close_session(session_id)

    system_prompt_cache.pop(conv_id, None)
    greeting_cache.pop(conv_id, None)
    memory_context_cache.pop(conv_id, None)
    conversation_call_sid_map.pop(conv_id, None)
    conv_app_map.pop(conv_id, None)
    browser_call_conv_ids.discard(conv_id)
    logger.info(f"[{cfg.id if cfg else '?'}] cleaned up conv_id={conv_id}")

    try:
        await tac.conversation_orchestrator_client.update_conversation(conv_id, status="CLOSED")
    except Exception as e:
        if "already" not in str(e).lower() and "400" not in str(e):
            logger.warning(f"[{cfg.id if cfg else '?'}] maestro close failed conv_id={conv_id}: {e}")


async def handle_interrupt(context: ConversationSession, interrupt_data) -> None:
    conv_id = context.conversation_id
    session_id = context.profile_id or conv_id
    cfg = _app_for_conv(conv_id)
    utterance = getattr(interrupt_data, "utterance_until_interrupt", "") or ""
    if cfg:
        await _APP_BACKENDS[cfg.route_prefix].interrupt(session_id, utterance)


tac.on_message_ready(handle_message_ready)
tac.on_conversation_ended(handle_conversation_ended)
tac.on_interrupt(handle_interrupt)

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI()

from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

from _routes import register_app_routes

for _app_cfg in ALL_APPS.values():
    register_app_routes(
        app=app, cfg=_app_cfg, voice_channel=voice_channel,
        pending_outbound_context=pending_outbound_context,
        pending_call_sid_map=pending_call_sid_map,
        conv_app_map=conv_app_map,
        outbound_conversation_map=outbound_conversation_map,
        twilio_client=twilio_client,
        _APP_BACKENDS=_APP_BACKENDS,
        _push_transcript_event=_push_transcript_event,
        _lookup_profile_id=_lookup_profile_id,
        _prefetch_memory=_prefetch_memory,
        _build_memory_context=_build_memory_context,
        _build_sms_system_prompt=_build_sms_system_prompt,
        _invoke_agent=_invoke_agent,
        _write_sms_observation=_write_sms_observation,
        _escalate_call_to_flex=_escalate_call_to_flex,
        FLEX_WORKFLOW_SID=FLEX_WORKFLOW_SID,
        PUBLIC_DOMAIN=PUBLIC_DOMAIN,
        APP_PORT=APP_PORT,
        logger=logger,
        FastAPIWebSocketAdapter=FastAPIWebSocketAdapter,
        websockets=websockets,
    )


@app.get("/get-outbound-phone/{conv_id}")
async def get_outbound_phone(conv_id: str) -> dict:
    """Shared — Node.js CI webhook resolves member phone + profileId from any app."""
    entry = outbound_conversation_map.get(conv_id)
    app_prefix = conv_app_map.get(conv_id, "")
    profile_id_from_entry = entry.get("profileId", "") if isinstance(entry, dict) else ""
    if not app_prefix and profile_id_from_entry:
        app_prefix = profile_app_map.get(profile_id_from_entry, "")
    app_cfg = ALL_APPS.get(app_prefix)
    app_id = app_cfg.id if app_cfg else ""
    if isinstance(entry, dict):
        return {"phone": entry.get("phone", ""), "profileId": entry.get("profileId", ""), "appId": app_id}
    if isinstance(entry, str):
        return {"phone": entry, "profileId": "", "appId": app_id}
    # Fallback: look up participants via Conversations API (elevenlabs path)
    if conv_id.startswith("conv_conversation_"):
        cfg = next(iter(ALL_APPS.values())) if ALL_APPS else None
        if cfg and TWILIO_ACCOUNT_SID:
            try:
                async with httpx.AsyncClient() as client:
                    res = await client.get(
                        f"https://conversations.twilio.com/v2/Conversations/{conv_id}/Participants",
                        auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
                        timeout=5,
                    )
                    for p in res.json().get("participants", []):
                        addresses = p.get("addresses") or []
                        address = addresses[0].get("address", "") if addresses else ""
                        if address and address != cfg.phone_number and address.startswith("+"):
                            outbound_conversation_map[conv_id] = address
                            return {"phone": address, "profileId": ""}
            except Exception as e:
                logger.warning(f"[get-outbound-phone] API fallback failed: {e}")
    return {"phone": "", "profileId": ""}


@app.post("/ci-webhook")
async def ci_webhook_proxy(request: Request) -> dict:
    """Forward CI webhook to the shared Node.js /ci-webhook route."""
    body = await request.json()
    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(f"http://localhost:{APP_PORT}/ci-webhook", json=body, timeout=10)
        return res.json()
    except Exception as e:
        logger.error(f"[ci-webhook] proxy failed: {e}")
        return {"success": False}


@app.api_route("/healthcare/api/{path:path}", methods=["GET", "POST", "PATCH", "DELETE"])
async def node_api_proxy(path: str, request: Request) -> Response:
    """Proxy /healthcare/api/* requests to Node.js so the Flex plugin can use the TAC ngrok URL."""
    url = f"http://localhost:{APP_PORT}/healthcare/api/{path}"
    if request.query_params:
        url += f"?{request.query_params}"
    headers = {k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")}
    body = await request.body()
    try:
        if "stream" in path:
            # SSE endpoint — stream without buffering
            async def _sse_gen():
                async with httpx.AsyncClient(timeout=None) as client:
                    async with client.stream(request.method, url, headers=headers, content=body) as res:
                        async for chunk in res.aiter_bytes():
                            yield chunk
            return StreamingResponse(_sse_gen(), media_type="text/event-stream", headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "Access-Control-Allow-Origin": "*",
            })
        async with httpx.AsyncClient() as client:
            res = await client.request(method=request.method, url=url, headers=headers, content=body, timeout=15)
        return Response(content=res.content, status_code=res.status_code, media_type=res.headers.get("content-type"))
    except Exception as e:
        logger.error(f"[node-proxy] /healthcare/api/{path} failed: {e}")
        return Response(content=b'{"error":"proxy failed"}', status_code=502, media_type="application/json")


@app.post("/browser-answer-twiml")
async def browser_answer_twiml(request: Request) -> Response:
    from datetime import datetime, timezone
    from tac.models import ParticipantAddress
    form = {k: str(v) for k, v in (await request.form()).items()}
    params = dict(request.query_params)
    call_sid = form.get("CallSid", "")
    member_phone = params.get("member_phone", form.get("To", ""))
    profile_id = params.get("profile_id", "")
    member_name = params.get("member_name", "member")
    # Use first app's phone number for AI_AGENT participant
    first_cfg = next(iter(ALL_APPS.values())) if ALL_APPS else None
    our_number = first_cfg.phone_number if first_cfg else ""
    try:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conversation = await tac.conversation_orchestrator_client.create_conversation(name=f"human-agent-call-{call_sid or ts}")
        conv_id = conversation.id
        member_resp = await tac.conversation_orchestrator_client.add_participant(
            conversation_id=conv_id,
            addresses=[ParticipantAddress(channel="VOICE", address=member_phone, channelId=call_sid)],
            participant_type="CUSTOMER",
        )
        resolved_profile_id = (member_resp.profile_id if member_resp else None) or profile_id
        await tac.conversation_orchestrator_client.add_participant(
            conversation_id=conv_id,
            addresses=[ParticipantAddress(channel="VOICE", address=our_number, channelId=call_sid)],
            participant_type="AI_AGENT",
        )
        outbound_conversation_map[conv_id] = {"phone": member_phone, "profileId": resolved_profile_id, "name": member_name}
        conversation_call_sid_map[conv_id] = call_sid
    except Exception as e:
        logger.error(f"[browser-call] failed to create CO conversation: {e}")
    twiml = ('<?xml version="1.0" encoding="UTF-8"?><Response><Dial>'
             '<Client>care-team-agent</Client></Dial></Response>')
    return Response(content=twiml, media_type="application/xml")


@app.post("/browser-call-status")
async def browser_call_status(request: Request) -> Response:
    form = {k: str(v) for k, v in (await request.form()).items()}
    call_sid = form.get("CallSid", "")
    if form.get("CallStatus") == "completed":
        conv_id = next((c for c, s in conversation_call_sid_map.items() if s == call_sid), None)
        if conv_id:
            try:
                await tac.conversation_orchestrator_client.update_conversation(conv_id, status="CLOSED")
            except Exception as e:
                logger.error(f"[browser-call] failed to close conversation: {e}")
    return Response(content="<?xml version='1.0'?><Response/>", media_type="application/xml")


@app.post("/twiml")
async def twiml_shared(request: Request) -> Response:
    """Shared inbound voice webhook — dispatches to the right app by To number."""
    form = {k: str(v) for k, v in (await request.form()).items()}
    to_phone = form.get("To", "")
    from_phone_log = form.get("From", "")
    logger.info(f"[twiml] inbound call To={to_phone} From={from_phone_log}")
    cfg = _PHONE_TO_APP.get(to_phone)
    if not cfg and ALL_APPS:
        cfg = next(iter(ALL_APPS.values()))
        logger.warning(f"[twiml] no app matched To={to_phone} — falling back to {cfg.id}")
    if not cfg:
        return Response(content="<?xml version='1.0'?><Response/>", media_type="application/xml")

    proto = request.headers.get("x-forwarded-proto", "https")
    host = request.headers.get("host", PUBLIC_DOMAIN)
    ws_proto = "wss" if proto == "https" else "ws"
    ws_url = f"{ws_proto}://{host}{cfg.route_prefix}/ws"
    callback_url = f"{proto}://{host}{cfg.route_prefix}/conversation-relay-callback"

    # Build personalized greeting using caller's profile if available
    from_phone = form.get("From", "")
    greeting = cfg.inbound_greeting  # default (may be empty string)
    logger.info(f"[twiml] from_phone={from_phone!r} cfg.inbound_greeting={greeting!r}")
    if from_phone and not greeting:
        try:
            loop = asyncio.get_event_loop()
            profile_id = await loop.run_in_executor(None, _lookup_profile_id, from_phone, cfg)
            logger.info(f"[twiml] profile_id={profile_id!r} for from_phone={from_phone!r}")
            if profile_id:
                traits = await loop.run_in_executor(None, _fetch_profile_traits, profile_id, cfg)
                logger.info(f"[twiml] traits keys={list(traits.keys())}")
                first_name = traits.get("firstName", "") or (traits.get("name", "").split()[0] if traits.get("name") else "")
                logger.info(f"[twiml] first_name={first_name!r}")
                if first_name:
                    greeting = f"Hi {first_name}, this is your Owl Health care coordinator. How can I help you today?"
                    logger.info(f"[twiml] personalized greeting set: {greeting!r}")
        except Exception as e:
            logger.warning(f"[twiml] greeting lookup failed: {e}")
    else:
        logger.info(f"[twiml] skipping lookup — from_phone empty or greeting already set")

    if greeting and from_phone:
        _greeting_by_phone[from_phone] = greeting
    # Push greeting to transcript immediately if we resolved a profile_id above
    if greeting and 'profile_id' in dir() and profile_id:
        from datetime import datetime, timezone, timedelta
        _greeting_ts = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        asyncio.create_task(_push_transcript_event(profile_id, "agent", greeting, ts=_greeting_ts))
        logger.info(f"[twiml] pushed greeting to transcript profileId={profile_id}")

    twiml = await voice_channel.handle_incoming_call(
        options={"websocket_url": ws_url, "action_url": callback_url,
                 "welcome_greeting": greeting},
    )
    # Allow member to interrupt the agent by speaking.
    twiml = twiml.replace("<ConversationRelay ", '<ConversationRelay interruptible="true" ', 1)
    return Response(content=twiml, media_type="application/xml")


@app.post("/sms")
async def sms_shared(request: Request) -> Response:
    """Shared SMS webhook — dispatches to the right app by To number."""
    form = dict(await request.form())
    to_phone = form.get("To", "")
    from_phone = form.get("From", "")
    body_text = form.get("Body", "").strip()
    logger.info(f"[sms] received To={to_phone} From={from_phone} Body=\"{body_text[:80]}\"")
    logger.info(f"[sms] _PHONE_TO_APP keys={list(_PHONE_TO_APP.keys())}")
    empty_twiml = Response(content="<?xml version='1.0'?><Response/>", media_type="application/xml")

    cfg = _PHONE_TO_APP.get(to_phone)
    if not cfg and ALL_APPS:
        cfg = next(iter(ALL_APPS.values()))
        logger.warning(f"[sms] no app matched To={to_phone} — falling back to {cfg.id}")
    if not cfg:
        logger.error(f"[sms] no app config available for To={to_phone}")
        return empty_twiml
    logger.info(f"[sms] dispatching to app={cfg.id} phone_number={cfg.phone_number}")

    if not body_text or not from_phone:
        logger.warning(f"[{cfg.id}][sms] empty body or from_phone — ignoring")
        return empty_twiml

    lookup_phone = from_phone
    logger.info(f"[{cfg.id}][sms] sms_simulate_member_phone={cfg.sms_simulate_member_phone!r} outbound_call_to={cfg.outbound_call_to!r}")
    if cfg.sms_simulate_member_phone and cfg.outbound_call_to and from_phone == cfg.outbound_call_to:
        lookup_phone = cfg.sms_simulate_member_phone
        logger.info(f"[{cfg.id}][sms] simulating member {lookup_phone} (actual sender={from_phone})")

    logger.info(f"[{cfg.id}][sms] looking up profile for phone={lookup_phone} store={cfg.memory_store_id}")
    loop = asyncio.get_event_loop()
    profile_id = await loop.run_in_executor(None, _lookup_profile_id, lookup_phone, cfg)
    if not profile_id:
        logger.warning(f"[{cfg.id}][sms] no profile found for phone={lookup_phone} — cannot proceed")
        return empty_twiml
    logger.info(f"[{cfg.id}][sms] profile_id={profile_id}")

    logger.info(f"[{cfg.id}][sms] inbound from={from_phone} profile_id={profile_id} body=\"{body_text[:80]}\"")

    t0 = time.time()
    backend = _APP_BACKENDS[cfg.route_prefix]
    logger.info(f"[{cfg.id}][sms] backend={cfg.agent_backend_name} prefetching memory + prewarming agent")
    memory_task = asyncio.create_task(_prefetch_memory(lookup_phone, cfg, profile_id=profile_id))
    prewarm_task = asyncio.create_task(backend.prewarm(profile_id))
    (memory, traits), prewarm_ok = await asyncio.gather(memory_task, prewarm_task)
    logger.info(f"[{cfg.id}][sms] memory+backend ready in {(time.time()-t0)*1000:.0f}ms prewarm_ok={prewarm_ok}")

    context = _build_memory_context(memory, traits)
    system_prompt = _build_sms_system_prompt(cfg)
    logger.info(f"[{cfg.id}][sms] system_prompt={system_prompt[:120]!r} context_len={len(context)}")

    try:
        logger.info(f"[{cfg.id}][sms] invoking agent session_id={profile_id}")
        reply = await _invoke_agent(
            session_id=profile_id, prompt=body_text, system_prompt=system_prompt,
            context=context, cfg=cfg, member_phone=lookup_phone,
            profile_id=profile_id, member_traits=traits,
        )
        logger.info(f"[{cfg.id}][sms] agent reply ({len(reply)} chars): \"{reply[:120]}\"")
    except Exception as e:
        logger.error(f"[{cfg.id}][sms] agent invocation failed: {e}", exc_info=True)
        return empty_twiml

    if not reply:
        logger.warning(f"[{cfg.id}][sms] agent returned empty reply — not sending SMS")
        return empty_twiml

    logger.info(f"[{cfg.id}][sms] sending SMS to={from_phone} from={cfg.phone_number} twilio_client={'set' if twilio_client else 'MISSING'}")
    if twilio_client and cfg.phone_number:
        try:
            msg = twilio_client.messages.create(to=from_phone, from_=cfg.phone_number, body=reply)
            logger.info(f"[{cfg.id}][sms] sent sid={msg.sid}")
        except Exception as e:
            logger.error(f"[{cfg.id}][sms] Twilio send failed: {e}", exc_info=True)
    else:
        logger.error(f"[{cfg.id}][sms] cannot send — twilio_client={bool(twilio_client)} phone_number={cfg.phone_number!r}")

    if cfg.sms_write_observation:
        loop.run_in_executor(None, _write_sms_observation, profile_id, "member", body_text, cfg)
        loop.run_in_executor(None, _write_sms_observation, profile_id, "agent", reply, cfg)

    return empty_twiml


# Maps conversation SID → app route_prefix (populated when chat session starts)
_CONV_SID_TO_APP: dict[str, str] = {}


@app.post("/register-conversation")
async def register_conversation(request: Request) -> dict:
    """Node.js calls this after creating a Twilio Conversation so TAC knows which app to use."""
    body = await request.json()
    conv_sid = body.get("conversationSid", "")
    app_id   = body.get("appId", "")
    if not conv_sid:
        return {"success": False, "error": "conversationSid required"}
    cfg = next((c for c in ALL_APPS.values() if c.id == app_id), None)
    if not cfg and ALL_APPS:
        cfg = next(iter(ALL_APPS.values()))
    if cfg:
        _CONV_SID_TO_APP[conv_sid] = cfg.route_prefix
        logger.info(f"[chat] registered conv_sid={conv_sid} app={cfg.id}")
    return {"success": bool(cfg)}


@app.post("/conversations-webhook")
async def conversations_webhook(request: Request) -> dict:
    """Twilio Conversations webhook — fires on onMessageAdded for Classic Conversations service."""
    form = dict(await request.form())
    conv_sid   = str(form.get("ConversationSid", ""))
    author     = str(form.get("Author", ""))
    body_text  = str(form.get("Body", "")).strip()
    event_type = str(form.get("EventType", ""))

    logger.info(f"[chat] webhook event={event_type} conv_sid={conv_sid} author={author} body=\"{body_text[:80]}\"")

    if event_type != "onMessageAdded":
        return {"success": True}
    if not body_text:
        return {"success": True}

    # Resolve app config
    prefix = _CONV_SID_TO_APP.get(conv_sid)
    cfg = ALL_APPS.get(prefix) if prefix else None
    if not cfg and ALL_APPS:
        cfg = next(iter(ALL_APPS.values()))
        logger.warning(f"[chat] conv_sid={conv_sid} not registered — falling back to app={cfg.id}")
    if not cfg:
        logger.error("[chat] no app config available")
        return {"success": False}

    # Conversation handed off to Flex — fan out human agent messages to browser, suppress AI
    if conv_sid in chat_escalated_convs:
        ai_authors = {"agent", "", "visitor"}
        if author in ai_authors or author.startswith("agent_"):
            return {"success": True}  # skip AI echo
        logger.info(f"[{cfg.id}][chat] escalated — fanning out human agent msg author={author!r} body=\"{body_text[:80]}\"")
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"http://localhost:{APP_PORT}{cfg.route_prefix}/chat-message-event",
                    json={"conversationSid": conv_sid, "body": body_text, "author": author},
                    timeout=5,
                )
        except Exception as e:
            logger.warning(f"[{cfg.id}][chat] SSE fanout for human agent message failed: {e}")
        return {"success": True}

    # Skip AI agent's own echo before invoking AI
    if author in ("agent", "") or author.startswith("agent_"):
        return {"success": True}

    visitor_phone = author  # identity was set to phone number at participant creation

    loop = asyncio.get_event_loop()
    profile_id = await loop.run_in_executor(None, _lookup_profile_id, visitor_phone, cfg)
    if not profile_id:
        logger.warning(f"[chat] no profile for phone={visitor_phone} — using conv_sid as session key")
        profile_id = conv_sid

    # ── Bridge into Conversation Orchestrator ────────────────────────────────
    co_conv_id = chat_classic_to_co.get(conv_sid)
    if not co_conv_id:
        logger.info(f"[{cfg.id}][chat] creating CO conversation for conv_sid={conv_sid}")
        try:
            from tac.models import ParticipantAddress
            co_conv = await tac.conversation_orchestrator_client.create_conversation(
                name=f"webchat-{visitor_phone}-{conv_sid[-8:]}"
            )
            co_conv_id = co_conv.id
            chat_classic_to_co[conv_sid] = co_conv_id
            chat_co_to_classic[co_conv_id] = conv_sid

            await tac.conversation_orchestrator_client.add_participant(
                conversation_id=co_conv_id,
                addresses=[ParticipantAddress(channel="CHAT", address=visitor_phone, channelId=conv_sid)],
                participant_type="CUSTOMER",
            )
            await tac.conversation_orchestrator_client.add_participant(
                conversation_id=co_conv_id,
                addresses=[ParticipantAddress(channel="CHAT", address=cfg.phone_number or visitor_phone, channelId=conv_sid)],
                participant_type="AI_AGENT",
            )
            conv_app_map[co_conv_id] = cfg.route_prefix
            outbound_conversation_map[co_conv_id] = {
                "phone": visitor_phone, "profileId": profile_id, "name": ""
            }
            logger.info(f"[{cfg.id}][chat] CO conversation created co_conv_id={co_conv_id}")
        except Exception as e:
            logger.error(f"[{cfg.id}][chat] CO conversation creation failed: {e}", exc_info=True)
            co_conv_id = conv_sid  # fall back to classic SID as session key
    else:
        logger.info(f"[{cfg.id}][chat] using existing CO conversation co_conv_id={co_conv_id}")

    # ── Invoke agent via CO session ──────────────────────────────────────────
    backend = _APP_BACKENDS[cfg.route_prefix]
    memory_task  = asyncio.create_task(_prefetch_memory(visitor_phone, cfg, profile_id=profile_id))
    prewarm_task = asyncio.create_task(backend.prewarm(co_conv_id))
    (memory, traits), _ = await asyncio.gather(memory_task, prewarm_task)

    context       = _build_memory_context(memory, traits)
    system_prompt = _build_sms_system_prompt(cfg)

    try:
        reply_text = await _invoke_agent(
            session_id=co_conv_id,  # CO conv ID as session — gives CO continuity
            prompt=body_text,
            system_prompt=system_prompt,
            context=context,
            cfg=cfg,
            member_phone=visitor_phone,
            profile_id=profile_id,
            member_traits=traits,
        )
    except Exception as e:
        logger.error(f"[{cfg.id}][chat] agent invocation failed: {e}", exc_info=True)
        return {"success": False}

    logger.info(f"[{cfg.id}][chat] reply co_conv_id={co_conv_id} \"{reply_text[:120]}\"")

    # ── Send reply via Classic Conversations (browser receives it) ───────────
    chat_service_sid = os.environ.get("TWILIO_CHAT_CONVERSATION_SERVICE_SID", "")
    if not chat_service_sid:
        logger.warning(f"[{cfg.id}][chat] TWILIO_CHAT_CONVERSATION_SERVICE_SID not set — cannot send reply")
    elif twilio_client:
        try:
            twilio_client.conversations.v1 \
                .services(chat_service_sid) \
                .conversations(conv_sid) \
                .messages.create(author="agent", body=reply_text)
            logger.info(f"[{cfg.id}][chat] reply sent to Classic Conversations conv_sid={conv_sid}")
        except Exception as e:
            logger.error(f"[{cfg.id}][chat] Classic Conversations send failed: {e}")

    # ── Push reply to browser via Node.js SSE fanout ─────────────────────────
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"http://localhost:{APP_PORT}{cfg.route_prefix}/chat-message-event",
                json={"conversationSid": conv_sid, "body": reply_text, "author": "agent"},
                timeout=5,
            )
    except Exception as e:
        logger.warning(f"[{cfg.id}][chat] SSE fanout failed: {e}")

    return {"success": True}


@app.post("/chat-message")
async def chat_message(request: Request) -> dict:
    """Direct entry point from Node.js for web chat messages — bypasses Twilio webhook roundtrip."""
    body = await request.json()
    conv_sid  = body.get("conversationSid", "")
    body_text = body.get("body", "").strip()
    author    = body.get("author", "")  # visitor phone number

    if not conv_sid or not body_text:
        return {"success": False, "error": "conversationSid and body required"}

    if conv_sid in chat_escalated_convs:
        # Conversation is with a human Flex agent — write visitor message to Conversations and return.
        # The Flex agent sees it in their UI; the conversations-webhook fans it out if needed.
        logger.info(f"[chat] escalated conv — writing visitor message directly conv_sid={conv_sid} author={author}")
        chat_service_sid = os.environ.get("TWILIO_CHAT_CONVERSATION_SERVICE_SID", "")
        if twilio_client and chat_service_sid:
            try:
                twilio_client.conversations.v1 \
                    .services(chat_service_sid) \
                    .conversations(conv_sid) \
                    .messages.create(author=author or "visitor", body=body_text)
            except Exception as e:
                logger.warning(f"[chat] write visitor message to escalated conv failed: {e}")
        return {"success": True}

    # Resolve app config
    prefix = _CONV_SID_TO_APP.get(conv_sid)
    cfg = ALL_APPS.get(prefix) if prefix else None
    if not cfg and ALL_APPS:
        cfg = next(iter(ALL_APPS.values()))
    if not cfg:
        return {"success": False, "error": "no app config"}

    visitor_phone = author or ""
    logger.info(f"[{cfg.id}][chat] direct message conv_sid={conv_sid} phone={visitor_phone} body=\"{body_text[:80]}\"")

    # Write visitor message to Classic Conversations
    chat_service_sid = os.environ.get("TWILIO_CHAT_CONVERSATION_SERVICE_SID", "")
    if twilio_client and chat_service_sid:
        try:
            twilio_client.conversations.v1 \
                .services(chat_service_sid) \
                .conversations(conv_sid) \
                .messages.create(author=visitor_phone or "visitor", body=body_text)
            logger.info(f"[{cfg.id}][chat] visitor message written to Classic Conversations")
        except Exception as e:
            logger.warning(f"[{cfg.id}][chat] write visitor message failed (non-fatal): {e}")

    # Look up profile
    loop = asyncio.get_event_loop()
    logger.info(f"[{cfg.id}][chat] looking up profile for phone={visitor_phone}")
    profile_id = await loop.run_in_executor(None, _lookup_profile_id, visitor_phone, cfg) if visitor_phone else None
    if profile_id:
        logger.info(f"[{cfg.id}][chat] profile matched phone={visitor_phone} profileId={profile_id}")
    else:
        profile_id = conv_sid
        logger.warning(f"[{cfg.id}][chat] no profile matched phone={visitor_phone} — falling back to conv_sid as session key")

    traits: dict = {}  # populated after memory fetch below; initialised here for CO creation reference

    # Bridge into CO (create on first message, reuse on subsequent)
    co_conv_id = chat_classic_to_co.get(conv_sid)
    if not co_conv_id:
        logger.info(f"[{cfg.id}][chat] creating CO conversation conv_sid={conv_sid}")
        try:
            from tac.models import ParticipantAddress
            co_conv = await tac.conversation_orchestrator_client.create_conversation(
                name=f"webchat-{visitor_phone}-{conv_sid[-8:]}"
            )
            co_conv_id = co_conv.id
            chat_classic_to_co[conv_sid] = co_conv_id
            chat_co_to_classic[co_conv_id] = conv_sid
            await tac.conversation_orchestrator_client.add_participant(
                conversation_id=co_conv_id,
                addresses=[ParticipantAddress(channel="CHAT", address=visitor_phone, channelId=conv_sid)],
                participant_type="CUSTOMER",
            )
            await tac.conversation_orchestrator_client.add_participant(
                conversation_id=co_conv_id,
                addresses=[ParticipantAddress(channel="CHAT", address=cfg.phone_number or visitor_phone, channelId=conv_sid)],
                participant_type="AI_AGENT",
            )
            conv_app_map[co_conv_id] = cfg.route_prefix
            member_name_for_map = (
                f"{traits.get('firstName', '')} {traits.get('lastName', '')}".strip()
                or traits.get("name", "")
            ) if traits else ""
            outbound_conversation_map[co_conv_id] = {"phone": visitor_phone, "profileId": profile_id, "name": member_name_for_map}
            logger.info(f"[{cfg.id}][chat] CO conversation created co_conv_id={co_conv_id} name={member_name_for_map!r}")
        except Exception as e:
            logger.error(f"[{cfg.id}][chat] CO creation failed: {e}", exc_info=True)
            co_conv_id = conv_sid  # fall back to classic SID
    else:
        logger.info(f"[{cfg.id}][chat] reusing CO conversation co_conv_id={co_conv_id}")

    # Invoke agent
    backend = _APP_BACKENDS[cfg.route_prefix]
    memory_task  = asyncio.create_task(_prefetch_memory(visitor_phone, cfg, profile_id=profile_id))
    prewarm_task = asyncio.create_task(backend.prewarm(co_conv_id))
    (memory, traits), _ = await asyncio.gather(memory_task, prewarm_task)

    # Update outbound_conversation_map with name now that traits are resolved
    if co_conv_id in outbound_conversation_map and traits:
        entry = outbound_conversation_map[co_conv_id]
        if isinstance(entry, dict) and not entry.get("name"):
            resolved_name = (
                f"{traits.get('firstName', '')} {traits.get('lastName', '')}".strip()
                or traits.get("name", "")
            )
            if resolved_name:
                entry["name"] = resolved_name
                logger.info(f"[{cfg.id}][chat] updated member name in map name={resolved_name!r}")

    obs_count  = len(memory.observations) if memory else 0
    sum_count  = len(memory.summaries)    if memory else 0
    trait_keys = list(traits.keys()) if traits else []
    logger.info(f"[{cfg.id}][chat] memory loaded profileId={profile_id} observations={obs_count} summaries={sum_count} trait_keys={trait_keys}")

    context       = _build_memory_context(memory, traits)
    if context:
        logger.info(f"[{cfg.id}][chat] injecting memory context into AgentCore ({len(context)} chars)")
    else:
        logger.warning(f"[{cfg.id}][chat] memory context is EMPTY — AgentCore will have no member history")
    system_prompt = _build_chat_system_prompt(cfg)

    # on_schedule_call: push a show_call_button event to the browser instead of auto-dialing
    call_button_signal: dict = {}

    async def _on_schedule_call(phone: str, reason: str) -> None:
        call_button_signal["phone"] = phone
        call_button_signal["reason"] = reason
        call_button_signal["profileId"] = profile_id
        logger.info(f"[{cfg.id}][chat] schedule_call signal — will show call button phone={phone}")

    # on_escalate: create a Flex chat task and signal the browser to show handoff UI
    escalation_signal: dict = {}

    async def _on_escalate(reason: str, urgency: str) -> None:
        escalation_signal["triggered"] = True
        escalation_signal["reason"] = reason
        escalation_signal["urgency"] = urgency
        logger.info(f"[{cfg.id}][chat] escalation signal captured reason={reason} urgency={urgency}")

    try:
        reply_text = await _invoke_agent(
            session_id=co_conv_id,
            prompt=body_text,
            system_prompt=system_prompt,
            context=context,
            cfg=cfg,
            member_phone=visitor_phone,
            profile_id=profile_id,
            member_traits=traits,
            on_schedule_call=_on_schedule_call,
            on_escalate=_on_escalate,
        )
    except Exception as e:
        logger.error(f"[{cfg.id}][chat] agent invocation failed: {e}", exc_info=True)
        return {"success": False, "error": str(e)}

    logger.info(f"[{cfg.id}][chat] reply co_conv_id={co_conv_id} \"{reply_text[:120]}\"")

    # Write agent reply to Classic Conversations
    if twilio_client and chat_service_sid:
        try:
            twilio_client.conversations.v1 \
                .services(chat_service_sid) \
                .conversations(conv_sid) \
                .messages.create(author="agent", body=reply_text)
            logger.info(f"[{cfg.id}][chat] agent reply written to Classic Conversations")
        except Exception as e:
            logger.error(f"[{cfg.id}][chat] write agent reply failed: {e}")

    # Handle Flex chat escalation if agent signalled it
    handoff_to_flex: dict = {}
    if escalation_signal.get("triggered"):
        esc_reason  = escalation_signal.get("reason", "member_requested_human")
        esc_urgency = escalation_signal.get("urgency", "normal")
        escalated = await _escalate_chat_to_flex(
            conv_sid=conv_sid,
            co_conv_id=co_conv_id,
            reason=esc_reason,
            urgency=esc_urgency,
            cfg=cfg,
            visitor_phone=visitor_phone,
            profile_id=profile_id,
        )
        if escalated:
            chat_escalated_convs.add(conv_sid)
            handoff_to_flex = {"reason": esc_reason}
            logger.info(f"[{cfg.id}][chat] conv_sid={conv_sid} added to chat_escalated_convs")

    # Push reply + optional signals to browser via Node.js SSE fanout
    try:
        async with httpx.AsyncClient() as client:
            payload: dict = {"conversationSid": conv_sid, "body": reply_text, "author": "agent"}
            if call_button_signal:
                payload["showCallButton"] = call_button_signal
            if handoff_to_flex:
                payload["handoffToFlex"] = handoff_to_flex
            await client.post(
                f"http://localhost:{APP_PORT}{cfg.route_prefix}/chat-message-event",
                json=payload,
                timeout=5,
            )
    except Exception as e:
        logger.warning(f"[{cfg.id}][chat] SSE fanout failed: {e}")

    return {"success": True}


@app.post("/chat-close")
async def chat_close(request: Request) -> dict:
    """Browser calls this when the chat widget is closed — closes the CO conversation."""
    body = await request.json()
    classic_sid = body.get("conversationSid", "")
    co_conv_id = chat_classic_to_co.pop(classic_sid, None)
    if co_conv_id:
        chat_co_to_classic.pop(co_conv_id, None)
        conv_app_map.pop(co_conv_id, None)
        outbound_conversation_map.pop(co_conv_id, None)
        try:
            await tac.conversation_orchestrator_client.update_conversation(co_conv_id, status="CLOSED")
            logger.info(f"[chat] CO conversation closed co_conv_id={co_conv_id}")
        except Exception as e:
            logger.warning(f"[chat] close CO conv failed: {e}")
    return {"success": True}


@app.get("/health")
async def health() -> dict:
    return {"status": "healthy", "apps": list(ALL_APPS.keys())}


@app.post("/flex-assignment-callback")
async def flex_assignment_callback(request: Request) -> Response:
    """TaskRouter assignment callback. Handles both voice (dequeue) and chat tasks."""
    form = dict(await request.form())
    task_attrs_raw = form.get("TaskAttributes", "{}")
    worker_attrs_raw = form.get("WorkerAttributes", "{}")
    worker_sid = form.get("WorkerSid", "")
    try:
        import json as _json
        task_attrs = _json.loads(task_attrs_raw)
        worker_attrs = _json.loads(worker_attrs_raw)
    except Exception:
        task_attrs = {}
        worker_attrs = {}

    task_type = task_attrs.get("taskType", "voice")
    logger.info(f"[flex-assignment] taskType={task_type} worker_sid={worker_sid} task_attrs={task_attrs}")

    # Chat task: add the Flex agent as a Conversations participant so they can send messages
    if task_type == "chat":
        conv_sid = task_attrs.get("conversationSid", "")
        worker_identity = worker_attrs.get("full_name", "") or worker_attrs.get("email", "") or worker_sid
        chat_service_sid = os.environ.get("TWILIO_CHAT_CONVERSATION_SERVICE_SID", "")
        logger.info(f"[flex-assignment][chat] conv_sid={conv_sid} worker_identity={worker_identity!r}")
        if twilio_client and conv_sid and worker_identity and chat_service_sid:
            try:
                twilio_client.conversations.v1 \
                    .services(chat_service_sid) \
                    .conversations(conv_sid) \
                    .participants.create(identity=worker_identity)
                logger.info(f"[flex-assignment][chat] added worker {worker_identity!r} as participant to conv {conv_sid}")
            except Exception as e:
                logger.warning(f"[flex-assignment][chat] add participant failed (may already exist): {e}")
        return Response(content=_json.dumps({"instruction": "accept"}), media_type="application/json")

    # Voice task: dequeue into conference
    called = task_attrs.get("called", "")
    contact_uri = worker_attrs.get("contact_uri", "")
    logger.info(f"[flex-assignment][voice] called={called!r} contact_uri={contact_uri!r}")

    instruction = {
        "instruction": "dequeue",
        "from": called,
        "post_work_activity_sid": "",
    }
    if contact_uri:
        instruction["to"] = contact_uri

    return Response(content=_json.dumps(instruction), media_type="application/json")


@app.post("/flex-dequeue-reservation")
async def flex_dequeue_reservation(request: Request) -> Response:
    """Proxy the TaskRouter dequeue instruction from the Flex plugin.
    Browser JS can't call taskrouter.twilio.com directly due to CORS."""
    import json as _json
    body = await request.json()
    workspace_sid = body.get("workspaceSid", "")
    task_sid = body.get("taskSid", "")
    reservation_sid = body.get("reservationSid", "")
    dequeue_from = body.get("dequeueFrom", "")
    dequeue_to = body.get("dequeueTo", "")

    if not all([workspace_sid, task_sid, reservation_sid, dequeue_from]):
        return Response(content=_json.dumps({"error": "missing required fields"}), status_code=400, media_type="application/json")

    url = f"https://taskrouter.twilio.com/v1/Workspaces/{workspace_sid}/Tasks/{task_sid}/Reservations/{reservation_sid}"
    data: dict = {"Instruction": "dequeue", "DequeueFrom": dequeue_from}
    if dequeue_to:
        data["DequeueTo"] = dequeue_to

    auth_headers = {"Content-Type": "application/x-www-form-urlencoded", **_twilio_basic_auth_header()}

    def _do() -> requests.Response:
        return requests.post(url, data=data, headers=auth_headers, timeout=10)

    try:
        loop = asyncio.get_event_loop()
        res = await loop.run_in_executor(None, _do)
        logger.info(f"[flex-dequeue] status={res.status_code} task={task_sid} reservation={reservation_sid}")
        return Response(content=res.text, status_code=res.status_code, media_type="application/json")
    except Exception as e:
        logger.error(f"[flex-dequeue] error: {e}")
        return Response(content=_json.dumps({"error": str(e)}), status_code=500, media_type="application/json")


@app.post("/flex-cancel-task")
async def flex_cancel_task(request: Request) -> dict:
    """Cancel a pending Flex task by profileId — called when browser call hangs up."""
    body = await request.json()
    profile_id = body.get("profileId", "")
    task_sid = flex_task_profile_map.pop(profile_id, "") if profile_id else ""
    # Also clean up conv_id keyed entry
    stale = [k for k, v in flex_task_sid_map.items() if v == task_sid]
    for k in stale:
        flex_task_sid_map.pop(k, None)

    logger.info(f"[flex-cancel-task] profileId={profile_id} task_sid={task_sid or '(not found)'}")
    if not task_sid or not FLEX_WORKSPACE_SID:
        return {"success": False, "reason": "no task found"}

    task_url = f"https://taskrouter.twilio.com/v1/Workspaces/{FLEX_WORKSPACE_SID}/Tasks/{task_sid}"
    auth_headers = {"Content-Type": "application/x-www-form-urlencoded", **_twilio_basic_auth_header()}

    def _cancel() -> requests.Response:
        return requests.post(task_url, data={"AssignmentStatus": "canceled", "Reason": "customer_hangup"}, headers=auth_headers, timeout=10)

    try:
        loop = asyncio.get_event_loop()
        res = await loop.run_in_executor(None, _cancel)
        logger.info(f"[flex-cancel-task] status={res.status_code} task_sid={task_sid}")
        return {"success": res.status_code < 300}
    except Exception as e:
        logger.error(f"[flex-cancel-task] error: {e}")
        return {"success": False}


@app.post("/flex-conference-status")
async def flex_conference_status(request: Request) -> dict:
    """Called by Twilio when the escalation conference ends (customer hung up).
    Cancels the pending TaskRouter task so it doesn't linger in Flex."""
    params = dict(await request.form())
    conv_id = request.query_params.get("conv_id", "")
    status_event = params.get("StatusCallbackEvent", params.get("ReasonConferenceEnded", ""))
    logger.info(f"[flex-conference-status] conv_id={conv_id} event={status_event}")

    task_sid = flex_task_sid_map.pop(conv_id, "") if conv_id else ""
    if task_sid and FLEX_WORKSPACE_SID:
        task_url = f"https://taskrouter.twilio.com/v1/Workspaces/{FLEX_WORKSPACE_SID}/Tasks/{task_sid}"
        auth_headers = {"Content-Type": "application/x-www-form-urlencoded", **_twilio_basic_auth_header()}

        def _cancel_task() -> requests.Response:
            return requests.post(task_url, data={
                "AssignmentStatus": "canceled",
                "Reason": "customer_hangup",
            }, headers=auth_headers, timeout=10)

        try:
            loop = asyncio.get_event_loop()
            res = await loop.run_in_executor(None, _cancel_task)
            logger.info(f"[flex-conference-status] task cancel status={res.status_code} task_sid={task_sid}")
        except Exception as e:
            logger.error(f"[flex-conference-status] task cancel failed: {e}")

    return {"success": True}


@app.post("/flex-accept-reservation")
async def flex_accept_reservation(request: Request) -> dict:
    """Accept a TaskRouter reservation on behalf of the Flex agent (backend has real auth)."""
    body = await request.json()
    workspace_sid: str = body.get("workspaceSid", "")
    task_sid: str = body.get("taskSid", "")
    reservation_sid: str = body.get("reservationSid", "")
    if not all([workspace_sid, task_sid, reservation_sid]):
        return JSONResponse({"error": "workspaceSid, taskSid, reservationSid required"}, status_code=400)

    url = f"https://taskrouter.twilio.com/v1/Workspaces/{workspace_sid}/Tasks/{task_sid}/Reservations/{reservation_sid}"
    auth_headers = {"Content-Type": "application/x-www-form-urlencoded", **_twilio_basic_auth_header()}

    def _accept() -> requests.Response:
        return requests.post(url, data={"ReservationStatus": "accepted"}, headers=auth_headers, timeout=10)

    try:
        loop = asyncio.get_event_loop()
        res = await loop.run_in_executor(None, _accept)
        logger.info(f"[flex-accept-reservation] status={res.status_code} reservation={reservation_sid}")
        return {"success": res.status_code < 300}
    except Exception as e:
        logger.error(f"[flex-accept-reservation] error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/flex-handoff")
async def flex_handoff(request: Request) -> dict:
    """Bridge a browser WebRTC call and a Flex agent into a shared conference."""
    body = await request.json()
    call_sid: str = body.get("callSid", "")
    worker_identity: str = body.get("workerIdentity", "")
    if not call_sid or not worker_identity:
        return JSONResponse({"error": "callSid and workerIdentity required"}, status_code=400)
    if not twilio_client:
        return JSONResponse({"error": "Twilio not configured"}, status_code=500)

    # Pick the cfg phone number from any loaded app config
    from_number = ""
    for px, cfg in _APP_BACKENDS.items():
        if hasattr(cfg, "phone_number") and cfg.phone_number:
            from_number = cfg.phone_number
            break

    conference_name = f"Handoff_{call_sid}"
    conf_twiml = f'<Response><Dial><Conference waitUrl="" beep="false">{conference_name}</Conference></Dial></Response>'

    loop = asyncio.get_event_loop()

    def _redirect_customer() -> None:
        twilio_client.calls(call_sid).update(twiml=conf_twiml)

    def _dial_agent() -> None:
        twilio_client.calls.create(
            to=f"client:{worker_identity}",
            from_=from_number,
            twiml=conf_twiml,
        )

    try:
        await loop.run_in_executor(None, _redirect_customer)
        logger.info(f"[flex-handoff] redirected customer call_sid={call_sid} to conference={conference_name}")
        await loop.run_in_executor(None, _dial_agent)
        logger.info(f"[flex-handoff] dialed agent worker={worker_identity}")
        return {"success": True, "conferenceName": conference_name}
    except Exception as e:
        logger.error(f"[flex-handoff] error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    app_list = ", ".join(ALL_APPS.keys())
    logger.info(f"TAC Server starting — apps=[{app_list}] domain={PUBLIC_DOMAIN} port={TAC_PORT}")
    uvicorn.run(app, host="0.0.0.0", port=TAC_PORT, log_config=None, log_level="info")
