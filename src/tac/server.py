"""
Multi-app Python TAC Server.

Supports multiple apps (healthcare, pubsec, etc.) on one server.
Each app is configured via src/tac/apps/<id>.json and gets its own
URL-prefixed routes: /{app}/twiml, /{app}/ws, /{app}/sms, etc.

Shared routes (no prefix): /health, /get-outbound-phone/{conv_id}, /ci-webhook
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

import httpx
import requests
import websockets
from base64 import b64encode
from xml.sax.saxutils import escape
from dotenv import load_dotenv
from fastapi import FastAPI, Request
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

MEMORY_BASE = "https://memory.twilio.com"
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
PUBLIC_DOMAIN = (os.environ.get("VOICE_PUBLIC_DOMAIN") or "").lstrip("https://").lstrip("http://")
TAC_PORT = int(os.environ.get("TAC_PORT", "8000"))
APP_PORT = int(os.environ.get("APP_PORT", "8001"))

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_TAC_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN  = os.environ.get("TWILIO_TAC_AUTH_TOKEN", "")
ESCALATION_ENABLED = os.environ.get("ESCALATION_ENABLED", "false").lower() in ("1", "true", "yes")
FLEX_HANDOFF_APPLICATION_SID = os.environ.get("TWILIO_FLEX_HANDOFF_APPLICATION_SID", "")

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
            yield data
            if data.get("type") == "text" and data.get("last"):
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
memory_context_cache: dict[str, str] = {}
# Maps conv_id → route_prefix so shared endpoints can find the right app
conv_app_map: dict[str, str] = {}

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

    twiml = _build_flex_transfer_twiml(conv_id, reason, urgency, effective_queue,
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
        logger.info(f"[escalation] ✓ transferred conv_id={conv_id} call_sid={call_sid} reason={reason} urgency={urgency} queue={effective_queue}")
        try:
            await tac.maestro_client.update_conversation(conv_id, status="CLOSED")
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


def _build_memory_context(memory: Optional[TACMemoryResponse], traits: Optional[dict] = None) -> str:
    sections = []
    if traits:
        trait_lines = []
        full_name = (f"{traits.get('firstName', '')} {traits.get('lastName', '')}".strip()
                     or traits.get("name", ""))
        if full_name:          trait_lines.append(f"- Name: {full_name}")
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


async def _invoke_agent(session_id: str, prompt: str, system_prompt: str, context: str,
                        cfg: "AppConfig", member_phone: str = "", profile_id: str = "",
                        member_traits: Optional[dict] = None) -> str:
    """Invoke the app's agent backend and return the full reply text."""
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
        msg_type = data.get("type")
        if msg_type == "text":
            token = data.get("token", "")
            if token:
                tokens.append(token)
            if data.get("last", False):
                break
        elif msg_type == "schedule_call":
            sc_phone = str(data.get("phone", "")) or member_phone
            sc_reason = str(data.get("reason", ""))
            logger.info(f"[{cfg.id}][schedule_call] sms: agent requested call profile_id={profile_id} phone={sc_phone} reason={sc_reason}")
            asyncio.create_task(_trigger_outbound_call_by_profile(profile_id, sc_phone, sc_reason, cfg=cfg, traits=member_traits or {}))
        elif msg_type == "tool_start":
            logger.info(f"[agent] sms tool_start tool={data.get('tool')}")
        elif msg_type == "tool_result":
            logger.info(f"[agent] sms tool_result status={data.get('status')}")
        else:
            logger.info(f"[agent] sms msg type={msg_type} data={str(data)[:200]}")
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


async def _push_transcript_event(profile_id: str, role: str, text: str, route_prefix: str = "") -> None:
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"http://localhost:{APP_PORT}/transcript-event",
                json={"profileId": profile_id, "role": role, "text": text},
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

    def _handle_setup(self, message: SetupMessage) -> None:
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
        if call_sid:
            conversation_call_sid_map[conv_id] = call_sid
        elif outbound_conv_id and outbound_conv_id in pending_call_sid_map:
            conversation_call_sid_map[conv_id] = pending_call_sid_map.pop(outbound_conv_id)
        if conv_id in conversation_call_sid_map:
            logger.info(f"[setup] call_sid mapped conv_id={conv_id} call_sid={conversation_call_sid_map[conv_id]}")

        ctx = pending_outbound_context.get(conv_id)
        phone = ctx.get("phone", "") if ctx else (message.from_number or "")

        member_profile_id: Optional[str] = None
        if ctx and phone and cfg:
            member_profile_id = _lookup_profile_id(phone, cfg)
            session_id = member_profile_id or message.custom_parameters.profile_id or conv_id
            if member_profile_id:
                logger.info(f"[setup] outbound session resolved to member profile {member_profile_id} for phone={phone}")
        else:
            session_id = message.custom_parameters.profile_id or conv_id

        if ctx and phone:
            outbound_conversation_map[conv_id] = {"phone": phone, "profileId": member_profile_id or ""}
            logger.info(f"[setup] outbound_conversation_map[{conv_id}] phone={phone} profileId={member_profile_id or '(pending)'}")
            greeting = ctx.get("greeting", "")
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
    cfg = _app_for_conv(conv_id)
    if not cfg:
        logger.error(f"[handle_message_ready] no app config for conv_id={conv_id} — dropping")
        return
    backend = _APP_BACKENDS[cfg.route_prefix]

    _map_entry = outbound_conversation_map.get(conv_id)
    _transcript_profile_id = _map_entry.get("profileId", "") if isinstance(_map_entry, dict) else ""
    if _transcript_profile_id and user_message:
        asyncio.create_task(_push_transcript_event(_transcript_profile_id, "member", user_message))

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
            phone = (context.author_info.address if context.author_info else "") or ""
            logger.info(f"[{cfg.id}] inbound session conv_id={conv_id} from={phone}")

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
    logger.info(f"[{cfg.id if cfg else '?'}] cleaned up conv_id={conv_id}")

    try:
        await tac.maestro_client.update_conversation(conv_id, status="CLOSED")
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
    if isinstance(entry, dict):
        return {"phone": entry.get("phone", ""), "profileId": entry.get("profileId", "")}
    if isinstance(entry, str):
        return {"phone": entry, "profileId": ""}
    # Fallback: look up participants via Conversations API (elevenlabs path)
    if conv_id.startswith("conv_conversation_"):
        cfg = next(iter(ALL_APPS.values())) if ALL_APPS else None
        if cfg and cfg.memory_api_key:
            try:
                async with httpx.AsyncClient() as client:
                    res = await client.get(
                        f"https://conversations.twilio.com/v2/Conversations/{conv_id}/Participants",
                        auth=(cfg.memory_api_key, cfg.memory_api_token),
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
        conversation = await tac.maestro_client.create_conversation(name=f"human-agent-call-{call_sid or ts}")
        conv_id = conversation.id
        member_resp = await tac.maestro_client.add_participant(
            conversation_id=conv_id,
            addresses=[ParticipantAddress(channel="VOICE", address=member_phone, channelId=call_sid)],
            participant_type="CUSTOMER",
        )
        resolved_profile_id = (member_resp.profile_id if member_resp else None) or profile_id
        await tac.maestro_client.add_participant(
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
                await tac.maestro_client.update_conversation(conv_id, status="CLOSED")
            except Exception as e:
                logger.error(f"[browser-call] failed to close conversation: {e}")
    return Response(content="<?xml version='1.0'?><Response/>", media_type="application/xml")


@app.post("/twiml")
async def twiml_shared(request: Request) -> Response:
    """Shared inbound voice webhook — dispatches to the right app by To number."""
    form = {k: str(v) for k, v in (await request.form()).items()}
    to_phone = form.get("To", "")
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

    twiml = await voice_channel.handle_incoming_call(
        to_number=form.get("To", ""),
        from_number=form.get("From", ""),
        options={"websocket_url": ws_url, "action_url": callback_url,
                 "welcome_greeting": cfg.inbound_greeting},
        call_sid=form.get("CallSid", ""),
    )
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


@app.get("/health")
async def health() -> dict:
    return {"status": "healthy", "apps": list(ALL_APPS.keys())}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    app_list = ", ".join(ALL_APPS.keys())
    logger.info(f"TAC Server starting — apps=[{app_list}] domain={PUBLIC_DOMAIN} port={TAC_PORT}")
    uvicorn.run(app, host="0.0.0.0", port=TAC_PORT, log_config=None, log_level="info")
