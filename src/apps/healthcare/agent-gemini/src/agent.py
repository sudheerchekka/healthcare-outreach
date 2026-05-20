"""
Owl Health outreach agent — Google Vertex AI Agent Engine (ADK) entry point.

Deployed to Vertex AI Agent Engine via the ADK CLI or Google Cloud Console.
Invoked by the Python TAC server via VertexAIBackend.invoke() using
vertexai.agent_engines.get(...).stream_query().

The agent emits tool signals in the response text using a JSON sentinel line
that the TAC server detects and strips before forwarding tokens to the caller.
Sentinel format (last line of response when a tool fires):
  __SIGNAL__{"type": "schedule_call", "phone": "...", "reason": "..."}
  __SIGNAL__{"type": "escalate", "reason": "...", "urgency": "..."}

The TAC server's VertexAIBackend strips these lines and yields the
corresponding signal dicts as agent protocol messages.

Short-term memory: Vertex AI Agent Engine manages session history automatically
via the session_id parameter on stream_query(). No manual STM handling needed.
"""

import json
import os
import pathlib

from google.adk.agents import Agent
from google.adk.tools import FunctionTool

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
            return text
    return _DEFAULT_SYSTEM_PROMPT


SYSTEM_PROMPT = _load_system_prompt()


def schedule_call(phone: str = "", reason: str = "") -> dict:
    """Schedule an outbound AI agent call to the member when they ask to be called.

    Call this when the member asks to receive a phone call (e.g. 'Can you call me?',
    'I prefer to talk on the phone', 'Please call me').

    Args:
        phone: member's phone number — use the phone from their profile in your memory context.
        reason: brief reason for the call.

    Returns:
        Confirmation dict.
    """
    signal = json.dumps({"type": "schedule_call", "phone": phone, "reason": reason})
    return {"signal": f"__SIGNAL__{signal}", "message": "Call scheduled. I'll let them know to expect your call shortly."}


def escalate_to_human(reason: str = "member_requested_human", urgency: str = "normal") -> dict:
    """Transfer the member to a human care specialist.

    Call this after acknowledging the member's request in your response.

    Args:
        reason: 'member_requested_human' when they ask to speak to a person,
                'safety_risk' for distress/emergency situations.
        urgency: 'normal' or 'high' (use high for safety risks).

    Returns:
        Confirmation dict.
    """
    signal = json.dumps({"type": "escalate", "reason": reason, "urgency": urgency, "targetQueue": "healthcare"})
    return {"signal": f"__SIGNAL__{signal}", "message": "Transferring now. Please hold."}


def create_agent() -> Agent:
    return Agent(
        model="gemini-2.5-flash",
        name="owl_health_agent",
        instruction=SYSTEM_PROMPT,
        tools=[
            FunctionTool(schedule_call),
            FunctionTool(escalate_to_human),
        ],
    )


root_agent = create_agent()


if __name__ == "__main__":
    # Local dev: run via `adk web` or `adk run` from this directory
    print(f"Owl Health Gemini agent loaded. System prompt: {len(SYSTEM_PROMPT)} chars")
