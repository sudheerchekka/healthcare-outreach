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

SYSTEM_PROMPT = """You are an Owl Health care coordination agent. \
You handle both outbound and inbound member calls on behalf of the care team. \
Be warm, professional, and concise. \
The member has already been greeted — do not re-introduce yourself or ask if they have time to talk again. \
When a call context is provided in the prompt, use it to personalize your responses. \
Avoid asking for information you already have from the customer's profile or prior call summaries."""


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

    # Prepend TAC memory context if provided (mirrors agent-persistent.ts behaviour)
    input_text = f"[Context]\n{memory_context}\n\n[User]\n{user_prompt}" if memory_context else user_prompt
    log.info(f"input (first 120 chars): {input_text[:120]}")

    # Load prior turns from AgentCore short-term memory (if configured)
    prior_messages: Messages = []
    mem_manager = None
    if MEMORY_ID:
        try:
            mem_manager = MemorySessionManager(memory_id=MEMORY_ID, region_name=REGION)
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
        except Exception as e:
            log.warning(f"[STM] load failed (continuing without history): {e}")

    # Bedrock requires messages to alternate user/assistant and the pre-loaded history must
    # end with assistant so the current user turn can follow. Drop if violated.
    if prior_messages and prior_messages[-1]["role"] != "assistant":
        log.warning(f"[STM] prior_messages last role={prior_messages[-1]['role']} — must end with assistant, dropping history to avoid Bedrock error")
        prior_messages = []

    agent = Agent(
        model=load_model(),
        system_prompt=SYSTEM_PROMPT,
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
            log.info(f"[STM] saved turn to session_id={session_id} user=\"{user_prompt[:60]}\" reply=\"{full_reply[:60]}\"")
        except Exception as e:
            log.warning(f"[STM] save failed: {e}")
    elif not full_reply:
        log.warning("[STM] skipping save — full_reply is empty")
    elif not mem_manager:
        log.warning("[STM] skipping save — mem_manager is None")


if __name__ == "__main__":
    app.run()
