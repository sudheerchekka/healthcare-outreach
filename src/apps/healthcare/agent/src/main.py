"""
Owl Health outreach agent — AgentCore Runtime entry point.

Deployed via: agentcore deploy (from this directory)
Invoked by:   Python TAC server via presigned AgentCore WebSocket (primary path)
              or locally with agentcore invoke

WebSocket protocol (used by src/tac-old/server.py):
  Receive: {"type": "prompt", "voicePrompt": "...", "systemPrompt": "...", "memoryContext": "..."}
           {"type": "interrupt", "utterance_until_interrupt": "..."}
  Send:    {"type": "text", "token": "...", "last": false}
           {"type": "text", "token": "", "last": true}   ← end-of-response sentinel

HTTP entrypoint (legacy / agentcore invoke):
  Payload: { "prompt": "...", "context": "...", "system_prompt": "..." }
"""

import asyncio
import json
import os
import pathlib
import httpx
from strands import Agent, tool
from strands.types.content import Messages
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.memory import MemorySessionManager
from bedrock_agentcore.memory.constants import ConversationalMessage, MessageRole
from model.load import load_model

app = BedrockAgentCoreApp()
log = app.logger

REGION = os.getenv("AWS_REGION", "us-east-1")
MEMORY_ID = os.getenv("BEDROCK_AGENTCORE_MEMORY_ID", "")  # injected by AgentCore when STM_ONLY is set

log.info(f"[startup] BEDROCK_AGENTCORE_MEMORY_ID={MEMORY_ID or '(NOT SET — STM disabled)'} REGION={REGION}")

_DEFAULT_SYSTEM_PROMPT = (
    "You are an Owl Health care coordination agent handling member calls on behalf of the care team. "
    "Be warm and conversational. Speak in plain, natural sentences — no bullet points, no bold or italic text, no special formatting of any kind. "
    "Keep each response brief and easy to follow on a phone call. "
    "The member has already been greeted — do not re-introduce yourself or ask if they have time to talk. "
    "When call context is provided, use it to personalize your responses and guide follow-up questions naturally. "
    "Avoid asking for information you already have from the member's profile or prior call summaries. "
    "Ask no more than 3 questions total across the entire call. Once you have collected answers to those questions, "
    "thank the member warmly by name, let them know the care team will follow up if needed, and wrap up the conversation. "
    "If at any point the member says they cannot talk, are busy, or says goodbye, immediately acknowledge and wrap up "
    "warmly — do not continue asking questions."
)

_PROMPT_FILE = pathlib.Path(__file__).parent / "system_prompt.txt"

def _load_system_prompt() -> str:
    if _PROMPT_FILE.exists():
        text = _PROMPT_FILE.read_text().strip()
        if text:
            log.info(f"[startup] system prompt loaded from {_PROMPT_FILE} ({len(text)} chars)")
            return text
    log.info("[startup] system prompt using hardcoded default")
    return _DEFAULT_SYSTEM_PROMPT

SYSTEM_PROMPT = _load_system_prompt()

KNOWLEDGE_BASE_ID  = os.environ.get("HEALTHCARE_KB_ID", "")
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_TAC_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN  = os.environ.get("TWILIO_TAC_AUTH_TOKEN", "")

escalation_state: dict = {}
schedule_call_state: dict = {}


@tool
def schedule_call(phone: str = "", reason: str = "") -> str:
    """Schedule an outbound AI agent call to the member when they ask to be called.

    Call this when the member asks to receive a phone call (e.g. 'Can you call me?',
    'I prefer to talk on the phone', 'Please call me').
    phone: member's phone number — use the phone from their profile in your memory context.
    reason: brief reason for the call (optional).
    """
    schedule_call_state["triggered"] = True
    schedule_call_state["phone"] = phone
    schedule_call_state["reason"] = reason
    log.info(f"[schedule_call] triggered phone={phone} reason={reason}")
    return json.dumps({"schedule_call": True, "phone": phone, "reason": reason})


@tool
def escalate_to_human(reason: str = "member_requested_human", urgency: str = "normal") -> str:
    """Transfer the member to a human care specialist.

    Call this after acknowledging the member's request in your response.
    reason: 'member_requested_human' when they ask to speak to a person,
            'safety_risk' for distress/emergency situations.
    urgency: 'normal' or 'high' (use high for safety risks).
    """
    escalation_state["triggered"] = True
    escalation_state["reason"] = reason
    escalation_state["urgency"] = urgency
    log.info(f"[escalation] escalate_to_human called reason={reason} urgency={urgency}")
    return json.dumps({"escalate": True, "reason": reason, "urgency": urgency})


@tool
def search_knowledge_base(query: str) -> str:
    """Search the Owl Health knowledge base for clinical guidelines, OTC card FAQs, policies, and FAQs.

    Use this when the member asks about specific health policies, procedures,
    medications, eligibility, or anything requiring accurate reference information.
    Do not guess at clinical or policy details — search first.
    query: a clear question or topic to search for.
    """
    if not KNOWLEDGE_BASE_ID:
        log.warning("[kb] HEALTHCARE_KB_ID not set — knowledge base unavailable")
        return "Knowledge base not configured."
    try:
        res = httpx.post(
            f"https://knowledge.twilio.com/v1/KnowledgeBases/{KNOWLEDGE_BASE_ID}/Search",
            auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
            json={"query": query, "top": 3},
            timeout=5,
        )
        chunks = res.json().get("chunks", [])
        log.info(f"[kb] search query=\"{query[:60]}\" status={res.status_code} chunks={len(chunks)}")
        if not chunks:
            return "No relevant information found in the knowledge base."
        return "\n\n".join(c["content"] for c in chunks if c.get("content"))
    except Exception as e:
        log.warning(f"[kb] search failed: {e}")
        return "Knowledge base search is currently unavailable."


def turns_to_messages(turns: list) -> Messages:
    """Convert MemorySessionManager turn events to Strands message format.

    STM stores: {"content": {"text": "..."}, "role": "USER"|"ASSISTANT"}
    EventMessage wraps this dict, so event_msg.content is {"text": "..."}.
    """
    messages: Messages = []
    for turn in turns:
        for event_msg in turn:
            content_field = event_msg.content  # {"text": "..."}
            if isinstance(content_field, dict):
                text = content_field.get("text", "")
            elif isinstance(content_field, str):
                text = content_field
            else:
                text = event_msg.text or ""
            if not text:
                log.warning(f"[STM] skipping event_msg with no text role={event_msg.role}")
                continue
            role = "user" if event_msg.role == MessageRole.USER else "assistant"
            messages.append({"role": role, "content": [{"text": text}]})
    return messages


@app.entrypoint
async def invoke(payload, context):
    session_id = getattr(context, 'session_id', 'default')
    payload_session = payload.get("session_id", "(not in payload)")
    log.info(f"invoke context.session_id={session_id} payload.session_id={payload_session} memory_id={MEMORY_ID or '(not configured)'}")

    user_prompt = payload.get("prompt", "")
    memory_context = payload.get("context", "")
    call_system_prompt = payload.get("system_prompt", "")  # non-empty on turn 1 only

    # Prepend TAC memory context if provided (turn 1 only; turn 2+ comes from STM)
    input_text = f"[Context]\n{memory_context}\n\n[User]\n{user_prompt}" if memory_context else user_prompt
    log.info(f"── input_text (full) ──\n{input_text}\n── end input_text ──")

    # Load prior turns and system prompt from STM
    prior_messages: Messages = []
    mem_manager = None
    stored_system_prompt = ""
    if MEMORY_ID:
        try:
            mem_manager = MemorySessionManager(memory_id=MEMORY_ID, region_name=REGION)

            # Load conversation history (actor_id="agent")
            prior_turns = mem_manager.get_last_k_turns(
                actor_id="agent",
                session_id=session_id,
                k=10,
            )
            prior_messages = turns_to_messages(prior_turns)
            log.info(f"[STM] loaded {len(prior_messages)} prior messages for session_id={session_id}")
            for i, msg in enumerate(prior_messages):
                text = msg.get("content", [{}])[0].get("text", "")
                log.info(f"[STM] history[{i}] role={msg['role']} text=\"{text[:80]}\"")

            # Load stored per-call system prompt (actor_id="system", turn 2+)
            if not call_system_prompt:
                sys_turns = mem_manager.get_last_k_turns(
                    actor_id="system",
                    session_id=session_id,
                    k=1,
                )
                if sys_turns:
                    for event_msg in sys_turns[0]:
                        content = event_msg.content
                        stored_system_prompt = (
                            content.get("text", "") if isinstance(content, dict) else str(content)
                        )
                        break
                log.info(f"[STM] loaded system prompt from STM ({len(stored_system_prompt)} chars)")

        except Exception as e:
            log.warning(f"[STM] load failed (continuing without history): {e}")

    # Turn 1: merge static + per-call prompt and save to STM.
    # Turn 2+: restore from STM, fall back to static only.
    if call_system_prompt:
        effective_system_prompt = f"{SYSTEM_PROMPT}\n\n{call_system_prompt}"
        if MEMORY_ID and mem_manager:
            try:
                mem_manager.add_turns(
                    actor_id="system",
                    session_id=session_id,
                    messages=[ConversationalMessage(text=effective_system_prompt, role=MessageRole.USER)],
                )
                log.info(f"[STM] saved system prompt to STM ({len(effective_system_prompt)} chars)")
            except Exception as e:
                log.warning(f"[STM] system prompt save failed: {e}")
    else:
        effective_system_prompt = stored_system_prompt or SYSTEM_PROMPT

    log.info(f"── effective_system_prompt ──\n{effective_system_prompt}\n── end system_prompt ──")

    # Bedrock requires messages to alternate user/assistant; history must end with assistant.
    if prior_messages and prior_messages[-1]["role"] != "assistant":
        log.warning(f"[STM] dropping history — last role={prior_messages[-1]['role']}, must end with assistant")
        prior_messages = []

    _tools = [escalate_to_human, schedule_call]
    if KNOWLEDGE_BASE_ID:
        _tools.append(search_knowledge_base)
    agent = Agent(
        model=load_model(),
        system_prompt=effective_system_prompt,
        messages=prior_messages,
        tools=_tools,
    )

    full_reply = ""
    async for event in agent.stream_async(input_text):
        if "data" in event and isinstance(event["data"], str):
            full_reply += event["data"]
            yield event["data"]
        elif "result" in event:
            # Strands final event — extract full text from result if full_reply is empty
            result_text = str(event["result"])
            if not full_reply and result_text:
                full_reply = result_text
                log.info(f"[STM] captured reply from result event ({len(full_reply)} chars)")

    log.info(f"[STM] stream complete — full_reply length={len(full_reply)} MEMORY_ID={bool(MEMORY_ID)} mem_manager={bool(mem_manager)}")

    # Emit escalation signal if the agent called escalate_to_human() during this turn
    if escalation_state.get("triggered"):
        esc_reason  = escalation_state.pop("reason", "member_requested_human")
        esc_urgency = escalation_state.pop("urgency", "normal")
        escalation_state.clear()
        log.info(f"[escalation] invoke() emitting escalate signal reason={esc_reason} urgency={esc_urgency}")
        yield {"type": "escalate", "reason": esc_reason, "urgency": esc_urgency}

    # Persist this turn to short-term memory
    if MEMORY_ID and mem_manager and full_reply:
        try:
            mem_manager.add_turns(
                actor_id="agent",
                session_id=session_id,
                messages=[
                    ConversationalMessage(text=input_text, role=MessageRole.USER),
                    ConversationalMessage(text=full_reply, role=MessageRole.ASSISTANT),
                ],
            )
            log.info(f"[STM] ✓ saved turn session_id={session_id}")
            log.info(f"[STM]   USER  : {input_text[:200]}")
            log.info(f"[STM]   ASST  : {full_reply[:200]}")
            # Verify it's readable back immediately
            verify = mem_manager.get_last_k_turns(actor_id="agent", session_id=session_id, k=1)
            log.info(f"[STM]   verify read-back: {len(verify)} turn(s) found")
        except Exception as e:
            log.warning(f"[STM] save failed: {e}")
    elif not full_reply:
        log.warning("[STM] skipping save — full_reply is empty")
    elif not MEMORY_ID:
        log.warning(f"[STM] skipping save — MEMORY_ID not set (value='{MEMORY_ID}')")
    elif not mem_manager:
        log.warning("[STM] skipping save — mem_manager is None")


@app.websocket
async def handle_voice_websocket(websocket, request_context=None):
    """
    Persistent WebSocket handler for voice calls from the Python TAC server.

    Strands Agent is created once per connection and kept alive, so all turns
    within a call share in-memory conversation history — no STM round-trips needed.
    The connection is closed by the TAC server when the call ends.
    """
    agent: Agent | None = None
    # asyncio.Task currently streaming tokens (cancelled on interrupt)
    stream_task: asyncio.Task | None = None

    async def _stream_and_send(agent_instance: Agent, input_text: str) -> None:
        full_reply = ""
        try:
            async for event in agent_instance.stream_async(input_text):
                event_keys = list(event.keys()) if isinstance(event, dict) else type(event).__name__
                if event_keys not in (["data"],):
                    log.info(f"[ws] non-text event keys={event_keys} preview={str(event)[:200]}")
                if "data" in event and isinstance(event["data"], str):
                    token = event["data"]
                    full_reply += token
                    await websocket.send_text(json.dumps({"type": "text", "token": token, "last": False}))
        except asyncio.CancelledError:
            log.info("[ws] stream cancelled (interrupt)")
            raise
        finally:
            log.info(f"[ws] _stream_and_send finally: escalation_state={dict(escalation_state)} schedule_call_state={dict(schedule_call_state)}")
            # Send escalate signal before sentinel if tool was triggered
            if escalation_state.get("triggered"):
                esc_reason = escalation_state.get("reason", "member_requested_human")
                esc_urgency = escalation_state.get("urgency", "normal")
                escalation_state.clear()
                try:
                    await websocket.send_text(json.dumps({
                        "type": "escalate",
                        "reason": esc_reason,
                        "urgency": esc_urgency,
                        "targetQueue": "default",
                    }))
                    log.info(f"[escalation] signal sent reason={esc_reason} urgency={esc_urgency}")
                except Exception as e:
                    log.warning(f"[escalation] signal send failed: {e}")
            if schedule_call_state.get("triggered"):
                sc_phone = schedule_call_state.get("phone", "")
                sc_reason = schedule_call_state.get("reason", "")
                schedule_call_state.clear()
                try:
                    await websocket.send_text(json.dumps({
                        "type": "schedule_call",
                        "phone": sc_phone,
                        "reason": sc_reason,
                    }))
                    log.info(f"[schedule_call] signal sent phone={sc_phone}")
                except Exception as e:
                    log.warning(f"[schedule_call] signal send failed: {e}")
            try:
                await websocket.send_text(json.dumps({"type": "text", "token": "", "last": True}))
            except Exception as e:
                log.warning(f"[ws] could not send sentinel (connection already closed): {e}")
            log.info(f"[ws] stream done reply_len={len(full_reply)}")

    await websocket.accept()
    try:
        async for raw in websocket.iter_text():
            try:
                msg = json.loads(raw)
            except Exception:
                continue

            msg_type = msg.get("type", "")

            if msg_type == "prompt":
                voice_prompt = msg.get("voicePrompt", "")
                system_prompt = msg.get("systemPrompt", "")
                memory_context = msg.get("memoryContext", "")

                # First prompt: build the agent with effective system prompt
                if agent is None:
                    base_system = SYSTEM_PROMPT
                    effective_system = f"{base_system}\n\n{system_prompt}" if system_prompt else base_system
                    _ws_tools = [escalate_to_human, schedule_call]
                    if KNOWLEDGE_BASE_ID:
                        _ws_tools.append(search_knowledge_base)
                    agent = Agent(model=load_model(), system_prompt=effective_system, tools=_ws_tools)
                    log.info(f"[ws] agent created system_prompt_len={len(effective_system)}")

                input_text = (
                    f"[Context]\n{memory_context}\n\n[User]\n{voice_prompt}"
                    if memory_context else voice_prompt
                )
                log.info(f"[ws] prompt received: {voice_prompt[:60]}")

                # Cancel any in-flight stream before starting a new one
                if stream_task and not stream_task.done():
                    stream_task.cancel()
                    try:
                        await stream_task
                    except asyncio.CancelledError:
                        pass

                stream_task = asyncio.create_task(_stream_and_send(agent, input_text))
                await stream_task

            elif msg_type == "interrupt":
                if stream_task and not stream_task.done():
                    stream_task.cancel()
                    try:
                        await stream_task
                    except asyncio.CancelledError:
                        pass
                log.info("[ws] interrupt processed")

    except Exception as e:
        log.error(f"[ws] error: {e}", exc_info=True)
    finally:
        if stream_task and not stream_task.done():
            stream_task.cancel()
        log.info("[ws] connection closed")


if __name__ == "__main__":
    app.run()
