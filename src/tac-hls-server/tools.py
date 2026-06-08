"""OpenAI tool definitions and implementations for healthcare agent."""

import asyncio
import json
import logging
import os
import uuid
from base64 import b64encode
from typing import Any, Optional
from xml.sax.saxutils import escape

import requests

logger = logging.getLogger(__name__)

TWILIO_ACCOUNT_SID        = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN         = os.environ.get("TWILIO_AUTH_TOKEN", "")
FLEX_WORKSPACE_SID        = os.environ.get("TWILIO_FLEX_WORKSPACE_SID", "")
FLEX_WORKFLOW_SID         = os.environ.get("TWILIO_FLEX_WORKFLOW_SID", "")
FLEX_HANDOFF_APP_SID      = os.environ.get("TWILIO_FLEX_HANDOFF_APPLICATION_SID", "")
ESCALATION_ENABLED        = os.environ.get("ESCALATION_ENABLED", "false").lower() in ("1", "true", "yes")
APP_PORT                  = int(os.environ.get("APP_PORT", "8001"))
KNOWLEDGE_BASE_ID         = os.environ.get("HEALTHCARE_KB_ID", "")
PUBLIC_DOMAIN             = (os.environ.get("VOICE_PUBLIC_DOMAIN") or "").lstrip("https://").lstrip("http://")


# ── OpenAI tool schemas ───────────────────────────────────────────────────────

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "escalate_to_human",
            "description": (
                "Transfer the member to a human care specialist. "
                "Call this when the member asks to speak to a person or when a safety risk is detected."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "enum": ["member_requested_human", "safety_risk"],
                        "description": "Reason for escalation.",
                    },
                    "urgency": {
                        "type": "string",
                        "enum": ["normal", "high"],
                        "description": "Use 'high' for safety risks.",
                    },
                },
                "required": ["reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "schedule_call",
            "description": (
                "Schedule an outbound AI agent call when the member asks to receive a phone call."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "phone": {"type": "string", "description": "Member's phone number from their profile."},
                    "reason": {"type": "string", "description": "Brief reason for the call."},
                },
                "required": ["phone"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_knowledge_base",
            "description": (
                "Search the Owl Health knowledge base for policies, clinical guidelines, OTC card info, and FAQs. "
                "Use when the member asks about specific health policies, procedures, medications, or eligibility."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Clear question or topic to search for."},
                },
                "required": ["query"],
            },
        },
    },
]


# ── Tool implementations ───────────────────────────────────────────────────────

def _basic_auth() -> dict:
    creds = f"{TWILIO_ACCOUNT_SID}:{TWILIO_AUTH_TOKEN}".encode("utf-8")
    return {"Authorization": f"Basic {b64encode(creds).decode('ascii')}"}


def execute_tool(name: str, args: dict, conv_id: str = "",
                 member_phone: str = "", member_name: str = "",
                 member_profile_id: str = "", cfg=None,
                 call_sid: str = "", direction: str = "inbound") -> str:
    """Execute a tool call and return the result string."""
    if name == "escalate_to_human":
        cfg_phone = cfg.phone_number if cfg else ""
        return _handle_escalate(args, conv_id, member_phone, member_name, member_profile_id,
                                 call_sid=call_sid, cfg_phone=cfg_phone, direction=direction)
    elif name == "schedule_call":
        return _handle_schedule_call(args, member_phone, member_name, member_profile_id, cfg)
    elif name == "search_knowledge_base":
        return _handle_kb_search(args)
    return f"Unknown tool: {name}"


def _handle_escalate(args: dict, conv_id: str, member_phone: str,
                     member_name: str, member_profile_id: str,
                     call_sid: str = "", cfg_phone: str = "",
                     direction: str = "inbound") -> str:
    reason = args.get("reason", "member_requested_human")
    urgency = args.get("urgency", "normal")
    logger.info(f"[tools] escalate_to_human conv_id={conv_id} reason={reason} call_sid={call_sid}")

    if not ESCALATION_ENABLED:
        return "Escalation not enabled."
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        return "Missing Twilio credentials."

    auth_headers = {"Content-Type": "application/x-www-form-urlencoded", **_basic_auth()}

    # Browser/WebRTC path: redirect into conference + create TaskRouter task
    if call_sid and FLEX_WORKFLOW_SID:
        conference_name = f"flex-escalation-{conv_id[-12:]}-{uuid.uuid4().hex[:8]}"
        conf_status_cb  = f"https://{PUBLIC_DOMAIN}/flex-conference-status?conv_id={conv_id}"
        conf_twiml = (
            '<?xml version="1.0" encoding="UTF-8"?><Response><Dial>'
            f'<Conference startConferenceOnEnter="true" endConferenceOnExit="true" '
            f'statusCallback="{conf_status_cb}" statusCallbackEvent="end" '
            f'waitUrl="https://twimlets.com/holdmusic?Bucket=com.twilio.music.classical" '
            f'beep="false">{escape(conference_name)}</Conference>'
            '</Dial></Response>'
        )
        # Redirect call into conference
        try:
            res = requests.post(
                f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Calls/{call_sid}.json",
                data={"Twiml": conf_twiml}, headers=auth_headers, timeout=10,
            )
            logger.info(f"[tools] call redirect status={res.status_code}")
        except Exception as e:
            logger.error(f"[tools] call redirect failed: {e}")
            return json.dumps({"escalated": False, "error": str(e)})

        # Wait for conference, then create task
        import time; time.sleep(1.5)
        try:
            conf_res = requests.get(
                f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Conferences.json"
                f"?FriendlyName={conference_name}&Status=in-progress",
                headers=auth_headers, timeout=10,
            )
            conferences = conf_res.json().get("conferences", [])
            conference_sid = conferences[0]["sid"] if conferences else ""
            logger.info(f"[tools] conference_sid={conference_sid}")
        except Exception as e:
            logger.warning(f"[tools] conference fetch failed: {e}")
            conference_sid = ""

        task_attrs: dict = {
            "taskType": "voice", "customerAddress": member_phone,
            "from": member_phone, "caller": member_phone,
            "called": cfg_phone, "name": member_name,
            "memberProfileId": member_profile_id, "reason": reason,
            "direction": direction,
        }
        if conference_sid:
            task_attrs["conference"] = {"sid": conference_sid, "participants": {"customer": call_sid}}

        try:
            res = requests.post(
                f"https://taskrouter.twilio.com/v1/Workspaces/{FLEX_WORKSPACE_SID}/Tasks",
                data={"WorkflowSid": FLEX_WORKFLOW_SID, "TaskChannel": "voice",
                      "Attributes": json.dumps(task_attrs)},
                headers=auth_headers, timeout=10,
            )
            logger.info(f"[tools] TaskRouter task status={res.status_code}")
            if res.status_code < 300:
                return json.dumps({"escalated": True, "task_sid": res.json().get("sid", "")})
            return json.dumps({"escalated": False, "error": f"status={res.status_code}"})
        except Exception as e:
            return json.dumps({"escalated": False, "error": str(e)})

    # PSTN path: redirect call via Flex Application TwiML
    if not FLEX_HANDOFF_APP_SID:
        return "TWILIO_FLEX_HANDOFF_APPLICATION_SID not configured."
    if not call_sid:
        return "No call_sid available for escalation."

    params = f'<Parameter name="reason" value="{escape(reason)}" />'
    if member_phone: params += f'<Parameter name="memberPhone" value="{escape(member_phone)}" />'
    if member_name:  params += f'<Parameter name="memberName" value="{escape(member_name)}" />'
    if member_profile_id: params += f'<Parameter name="memberProfileId" value="{escape(member_profile_id)}" />'
    params += f'<Parameter name="direction" value="{escape(direction)}" />'
    if urgency == "high": params += '<Parameter name="urgency" value="high" />'

    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?><Response>'
        '<Dial answerOnBridge="true"><Application copyParentTo="true">'
        f'<ApplicationSid>{escape(FLEX_HANDOFF_APP_SID)}</ApplicationSid>'
        f'{params}</Application></Dial></Response>'
    )
    try:
        res = requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Calls/{call_sid}.json",
            data={"Twiml": twiml}, headers=auth_headers, timeout=10,
        )
        if res.status_code < 300:
            task_sid = res.json().get("sid", "")
            logger.info(f"[tools] Flex task created task_sid={task_sid}")
            return json.dumps({"escalated": True, "task_sid": task_sid})
        logger.error(f"[tools] Flex task failed status={res.status_code}")
        return json.dumps({"escalated": False, "error": f"status={res.status_code}"})
    except Exception as e:
        logger.error(f"[tools] escalate_to_human exception: {e}")
        return json.dumps({"escalated": False, "error": str(e)})


def _handle_schedule_call(args: dict, member_phone: str, member_name: str,
                          member_profile_id: str, cfg) -> str:
    phone = args.get("phone", "") or member_phone
    reason = args.get("reason", "")
    logger.info(f"[tools] schedule_call phone={phone} reason={reason}")
    try:
        res = requests.post(
            f"http://localhost:{APP_PORT}/api/outbound-call",
            json={"phone": phone, "name": member_name, "goal": reason,
                  "goalDesc": reason, "profileId": member_profile_id},
            timeout=10,
        )
        logger.info(f"[tools] schedule_call status={res.status_code}")
        return json.dumps({"scheduled": res.status_code < 300})
    except Exception as e:
        logger.error(f"[tools] schedule_call failed: {e}")
        return json.dumps({"scheduled": False, "error": str(e)})


def _handle_kb_search(args: dict) -> str:
    query = args.get("query", "")
    if not KNOWLEDGE_BASE_ID or not query:
        return "Knowledge base not configured."
    try:
        res = requests.post(
            f"https://knowledge.twilio.com/v1/KnowledgeBases/{KNOWLEDGE_BASE_ID}/Search",
            auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
            json={"query": query, "top": 3},
            timeout=5,
        )
        chunks = res.json().get("chunks", [])
        logger.info(f"[tools] kb search query=\"{query[:60]}\" chunks={len(chunks)}")
        if not chunks:
            return "No relevant information found."
        return "\n\n".join(c["content"] for c in chunks if c.get("content"))
    except Exception as e:
        logger.warning(f"[tools] kb search failed: {e}")
        return "Knowledge base search unavailable."
