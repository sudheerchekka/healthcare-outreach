"""
Owl Health outreach agent — AgentCore Runtime entry point.

Deployed via: agentcore deploy (from this directory)
Invoked by:   the Node.js app via InvokeAgentRuntimeCommand, or locally with agentcore invoke

Payload shape:
  { "prompt": "<user utterance>", "context": "<TAC memory block or empty string>" }

Short-term memory:
  AgentCore injects AGENTCORE_MEMORY_ID at runtime (set via STM_ONLY in .bedrock_agentcore.yaml).
  MemorySessionManager stores/retrieves conversation turns keyed by session_id (= Sierra convId),
  so each voice call has multi-turn context across ConversationRelay turns.
"""

import os
from strands import Agent
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

SYSTEM_PROMPT = """You are an Owl Health care coordination agent handling member calls on behalf of the care team. \
Be warm and conversational. Speak in plain, natural sentences — no bullet points, no bold or italic text, no special formatting of any kind. \
Keep each response brief and easy to follow on a phone call. \
The member has already been greeted — do not re-introduce yourself or ask if they have time to talk. \
When call context is provided, use it to personalize your responses and guide follow-up questions naturally. \
Avoid asking for information you already have from the member's profile or prior call summaries. \
Always steer the conversation toward a clear next step or action."""


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

    agent = Agent(
        model=load_model(),
        system_prompt=effective_system_prompt,
        messages=prior_messages,
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


if __name__ == "__main__":
    app.run()
