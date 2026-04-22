"""
Owl Health ElevenLabs Voice Server.

Bridges Twilio <Stream> (raw µ-law audio) to ElevenLabs Conversational AI.
Runs independently of the TAC server — no ConversationRelay, no AgentCore.

Endpoints:
  POST /twiml-outbound       — TwiML with <Stream> for outbound calls
  WS   /ws                   — Twilio Stream WebSocket bridge
  POST /set-outbound-context — IPC: Node.js app server stores pending ctx before dial
  GET  /get-outbound-phone/{conv_id} — IPC: CI webhook phone lookup (matches TAC server interface)
  GET  /health               — Health check
"""

import asyncio
import base64
import json
import logging
import os
import ssl
import time
from typing import Optional

import numpy as np

import requests
import websockets
from dotenv import load_dotenv
from elevenlabs.client import ElevenLabs
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import Response

load_dotenv()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Audio transcoding: µ-law 8kHz (Twilio) ↔ PCM 16kHz (ElevenLabs)
# ---------------------------------------------------------------------------

# µ-law decode table (256 entries → int16)
_ULAW_DECODE = np.array([
    -32124,-31100,-30076,-29052,-28028,-27004,-25980,-24956,
    -23932,-22908,-21884,-20860,-19836,-18812,-17788,-16764,
    -15996,-15484,-14972,-14460,-13948,-13436,-12924,-12412,
    -11900,-11388,-10876,-10364, -9852, -9340, -8828, -8316,
     -7932, -7676, -7420, -7164, -6908, -6652, -6396, -6140,
     -5884, -5628, -5372, -5116, -4860, -4604, -4348, -4092,
     -3900, -3772, -3644, -3516, -3388, -3260, -3132, -3004,
     -2876, -2748, -2620, -2492, -2364, -2236, -2108, -1980,
     -1884, -1820, -1756, -1692, -1628, -1564, -1500, -1436,
     -1372, -1308, -1244, -1180, -1116, -1052,  -988,  -924,
      -876,  -844,  -812,  -780,  -748,  -716,  -684,  -652,
      -620,  -588,  -556,  -524,  -492,  -460,  -428,  -396,
      -372,  -356,  -340,  -324,  -308,  -292,  -276,  -260,
      -244,  -228,  -212,  -196,  -180,  -164,  -148,  -132,
      -120,  -112,  -104,   -96,   -88,   -80,   -72,   -64,
       -56,   -48,   -40,   -32,   -24,   -16,    -8,     0,
     32124, 31100, 30076, 29052, 28028, 27004, 25980, 24956,
     23932, 22908, 21884, 20860, 19836, 18812, 17788, 16764,
     15996, 15484, 14972, 14460, 13948, 13436, 12924, 12412,
     11900, 11388, 10876, 10364,  9852,  9340,  8828,  8316,
      7932,  7676,  7420,  7164,  6908,  6652,  6396,  6140,
      5884,  5628,  5372,  5116,  4860,  4604,  4348,  4092,
      3900,  3772,  3644,  3516,  3388,  3260,  3132,  3004,
      2876,  2748,  2620,  2492,  2364,  2236,  2108,  1980,
      1884,  1820,  1756,  1692,  1628,  1564,  1500,  1436,
      1372,  1308,  1244,  1180,  1116,  1052,   988,   924,
       876,   844,   812,   780,   748,   716,   684,   652,
       620,   588,   556,   524,   492,   460,   428,   396,
       372,   356,   340,   324,   308,   292,   276,   260,
       244,   228,   212,   196,   180,   164,   148,   132,
       120,   112,   104,    96,    88,    80,    72,    64,
        56,    48,    40,    32,    24,    16,     8,     0,
], dtype=np.int16)


def _ulaw_to_pcm16_8k(ulaw_bytes: bytes) -> np.ndarray:
    """Decode µ-law bytes → int16 PCM at 8kHz."""
    return _ULAW_DECODE[np.frombuffer(ulaw_bytes, dtype=np.uint8)]


def _pcm16_8k_to_ulaw(pcm: np.ndarray) -> bytes:
    """Encode int16 PCM at 8kHz → µ-law bytes."""
    # ITU-T G.711 µ-law encode
    s = pcm.astype(np.int32)
    sign = (s < 0).astype(np.uint8)
    s = np.abs(s)
    s = np.clip(s + 33, 33, 32767)
    exp = (np.floor(np.log2(s)).astype(np.int32) - 5).clip(0, 7)
    mantissa = ((s >> (exp + 1)) & 0x0F).astype(np.uint8)
    ulaw = (~((sign << 7) | (exp.astype(np.uint8) << 4) | mantissa)).astype(np.uint8)
    return ulaw.tobytes()


def _resample_2x(pcm: np.ndarray) -> np.ndarray:
    """Upsample 8kHz → 16kHz by linear interpolation."""
    out = np.empty(len(pcm) * 2, dtype=np.int16)
    out[0::2] = pcm
    out[1::2] = ((pcm.astype(np.int32) + np.roll(pcm, -1).astype(np.int32)) // 2).astype(np.int16)
    return out


def _resample_half(pcm: np.ndarray) -> np.ndarray:
    """Downsample 16kHz → 8kHz by taking every other sample."""
    return pcm[::2]


def twilio_to_el(b64_ulaw: str) -> str:
    """Convert Twilio base64 µ-law 8kHz → base64 PCM 16kHz for ElevenLabs."""
    ulaw = base64.b64decode(b64_ulaw)
    pcm8 = _ulaw_to_pcm16_8k(ulaw)
    pcm16 = _resample_2x(pcm8)
    return base64.b64encode(pcm16.tobytes()).decode()


def el_to_twilio(b64_pcm16: str) -> str:
    """Convert ElevenLabs base64 PCM 16kHz → base64 µ-law 8kHz for Twilio."""
    pcm16 = np.frombuffer(base64.b64decode(b64_pcm16), dtype=np.int16)
    pcm8 = _resample_half(pcm16)
    return base64.b64encode(_pcm16_8k_to_ulaw(pcm8)).decode()
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ELEVENLABS_API_KEY  = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVENLABS_AGENT_ID = os.environ.get("ELEVENLABS_AGENT_ID", "")
EL_PORT             = int(os.environ.get("ELEVENLABS_PORT", "8002"))
PUBLIC_DOMAIN       = (os.environ.get("VOICE_PUBLIC_DOMAIN") or "").lstrip("https://").lstrip("http://")

MEMORY_BASE       = "https://memory.twilio.com"
MEMORY_STORE_ID   = os.environ.get("MEMORY_STORE_ID", "")
MEMORY_API_KEY    = os.environ.get("TWILIO_API_KEY", "")
MEMORY_API_TOKEN  = os.environ.get("TWILIO_API_TOKEN", "")

el_client = ElevenLabs(api_key=ELEVENLABS_API_KEY) if ELEVENLABS_API_KEY else None

SYSTEM_PROMPT_TEMPLATE = """\
You are an Owl Health care coordination agent calling {{member_name}} on behalf of the care team.
The purpose of this call is: {{goal}}.
Be warm, concise, and natural — no bullet points, no bold text.
The member has already been greeted. Do not re-introduce yourself.
Ask no more than 3 questions total. Once you have answers, thank the member warmly by name, \
let them know the care team will follow up, and wrap up the conversation.
If at any point the member says they cannot talk, are busy, or says goodbye, \
immediately acknowledge and wrap up warmly — do not continue asking questions.

{{memory_context}}"""

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

pending_outbound_context: dict[str, dict] = {}
# conv_id → member phone (kept for CI webhook lookup)
outbound_conversation_map: dict[str, str] = {}

# ---------------------------------------------------------------------------
# Memory helpers (same REST API as TAC server)
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
        logger.warning(f"[memory] lookup failed: {e}")
        return None


def _fetch_memory_context(profile_id: str) -> str:
    try:
        base = f"{MEMORY_BASE}/v1/Stores/{MEMORY_STORE_ID}/Profiles/{profile_id}"
        auth = (MEMORY_API_KEY, MEMORY_API_TOKEN)
        obs_res = requests.get(f"{base}/Observations", auth=auth, timeout=5)
        sum_res = requests.get(f"{base}/ConversationSummaries", auth=auth, timeout=5)
        sections = []
        observations = obs_res.json().get("observations", [])
        summaries    = sum_res.json().get("summaries", [])
        if observations:
            lines = [f"- {o['content']}" for o in observations]
            sections.append("### Previous Observations\n" + "\n".join(lines))
        if summaries:
            lines = [f"- {s['content']}" for s in summaries]
            sections.append("### Previous Summaries\n" + "\n".join(lines))
        if not sections:
            return ""
        return "The following is from previous interactions with this member.\n\n" + "\n\n".join(sections)
    except Exception as e:
        logger.warning(f"[memory] fetch failed: {e}")
        return ""


async def _build_context_for_call(ctx: dict) -> tuple[str, str]:
    """Return (system_prompt, memory_context) for the given outbound context dict."""
    loop = asyncio.get_event_loop()
    phone = ctx.get("phone", "")
    memory_context = ""
    if phone:
        profile_id = await loop.run_in_executor(None, _lookup_profile_id, phone)
        if profile_id:
            memory_context = await loop.run_in_executor(None, _fetch_memory_context, profile_id)

    system_prompt = (
        SYSTEM_PROMPT_TEMPLATE
        .replace("{{member_name}}", ctx.get("name", "the member"))
        .replace("{{goal}}", ctx.get("goal", "") or ctx.get("goalDesc", "") or "a care follow-up")
        .replace("{{memory_context}}", memory_context)
        .strip()
    )
    return system_prompt, memory_context

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI()


@app.post("/twiml-outbound")
async def post_twiml_outbound(request: Request) -> Response:
    """Return TwiML that opens a bidirectional <Stream> to our /ws endpoint."""
    params   = dict(request.query_params)
    conv_id  = params.get("conv_id", "")
    proto = request.headers.get("x-forwarded-proto", "https")
    host  = request.headers.get("host", PUBLIC_DOMAIN)
    ws_proto = "wss" if proto == "https" else "ws"
    ws_url   = f"{ws_proto}://{host}/ws"

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="{ws_url}">
      <Parameter name="conv_id" value="{conv_id}" />
    </Stream>
  </Connect>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    """Bridge Twilio <Stream> (µ-law audio) ↔ ElevenLabs Conversational AI WebSocket."""
    await websocket.accept()
    conv_id = ""  # resolved from start.customParameters
    call_sid = ""
    el_ws: Optional[websockets.ClientConnection] = None

    async def relay_twilio_to_el() -> None:
        """Forward Twilio media events to ElevenLabs."""
        nonlocal call_sid, el_ws, conv_id
        try:
            while True:
                raw = await websocket.receive_text()
                msg = json.loads(raw)
                event = msg.get("event", "")

                if event == "start":
                    start = msg.get("start", {})
                    call_sid = start.get("callSid", "")
                    conv_id = start.get("customParameters", {}).get("conv_id", "")
                    logger.info(f"[el] stream start callSid={call_sid} conv_id={conv_id}")

                    # Retrieve stored context and build ElevenLabs session
                    ctx = pending_outbound_context.get(conv_id, {})
                    logger.info(f"[el] ctx keys={list(ctx.keys())} name={ctx.get('name')} conv_id={conv_id}")
                    logger.info(f"[el] pending_outbound_context keys={list(pending_outbound_context.keys())}")
                    if ctx and ctx.get("phone"):
                        outbound_conversation_map[conv_id] = ctx["phone"]

                    system_prompt, memory_context = await _build_context_for_call(ctx)
                    logger.info(f"[el] ── system_prompt ──\n{system_prompt}\n── end system_prompt ──")
                    logger.info(f"[el] ── memory_context ({len(memory_context)} chars) ──\n{memory_context}\n── end memory_context ──")

                    if not el_client:
                        logger.error("[el] ElevenLabs client not configured (ELEVENLABS_API_KEY missing)")
                        await websocket.close()
                        return

                    # Open ElevenLabs Conversational AI WebSocket (public agent, no auth required)
                    t0 = time.time()
                    el_ws_url = f"wss://api.elevenlabs.io/v1/convai/conversation?agent_id={ELEVENLABS_AGENT_ID}"
                    ssl_ctx = ssl.create_default_context()
                    ssl_ctx.check_hostname = False
                    ssl_ctx.verify_mode = ssl.CERT_NONE
                    el_ws = await websockets.connect(el_ws_url, ssl=ssl_ctx)
                    # Send initiation data as first message (per ElevenLabs WS protocol)
                    initiation = {
                        "type": "conversation_initiation_client_data",
                        "dynamic_variables": {
                            "member_name": ctx.get("name", ""),
                            "goal": ctx.get("goal", ""),
                            "memory_context": memory_context,
                        },
                        "conversation_config_override": {
                            "agent": {
                                "prompt": {"prompt": system_prompt},
                                "first_message": ctx.get("greeting", f"Hi {ctx.get('name', '')}, this is the Owl Health care team."),
                            },
                            "tts": {"optimize_streaming_latency": 3},
                            "audio": {
                                "input": {"encoding": "ulaw", "sample_rate": 8000},
                                "output": {"encoding": "ulaw", "sample_rate": 8000},
                            },
                        },
                    }
                    await el_ws.send(json.dumps(initiation))
                    logger.info(f"[el] connected to ElevenLabs in {(time.time()-t0)*1000:.0f}ms")

                    # Start background task to forward ElevenLabs audio → Twilio
                    asyncio.create_task(_relay_el_to_twilio(el_ws, websocket, start.get("streamSid", "")))

                elif event == "media" and el_ws:
                    payload = msg.get("media", {}).get("payload", "")
                    if payload:
                        await el_ws.send(json.dumps({
                            "user_audio_chunk": twilio_to_el(payload),
                        }))

                elif event == "stop":
                    logger.info(f"[el] stream stop callSid={call_sid}")
                    if el_ws:
                        try:
                            await el_ws.close()
                        except Exception:
                            pass
                    break

        except Exception as e:
            logger.error(f"[el] relay_twilio_to_el error: {e}", exc_info=True)
        finally:
            if el_ws:
                try:
                    await el_ws.close()
                except Exception:
                    pass

    await relay_twilio_to_el()


async def _relay_el_to_twilio(el_ws: websockets.ClientConnection, twilio_ws: WebSocket, stream_sid: str) -> None:
    """Forward ElevenLabs audio back to Twilio as <Stream> media events."""
    try:
        async for raw in el_ws:
            try:
                msg = json.loads(raw)
            except Exception:
                continue

            msg_type = msg.get("type", "")

            if msg_type == "audio":
                audio_b64 = msg.get("audio_event", {}).get("audio_base_64", "")
                if audio_b64 and stream_sid:
                    await twilio_ws.send_text(json.dumps({
                        "event": "media",
                        "streamSid": stream_sid,
                        "media": {"payload": el_to_twilio(audio_b64)},
                    }))

            elif msg_type == "interruption":
                # Clear Twilio's audio buffer
                if stream_sid:
                    await twilio_ws.send_text(json.dumps({
                        "event": "clear",
                        "streamSid": stream_sid,
                    }))

            elif msg_type == "ping":
                event_id = msg.get("ping_event", {}).get("event_id")
                await el_ws.send(json.dumps({"type": "pong", "event_id": event_id}))

            elif msg_type == "conversation_initiation_metadata":
                logger.info(f"[el←] initiation_metadata: {json.dumps(msg)}")
            else:
                logger.info(f"[el←] type={msg_type} keys={list(msg.keys())} raw={json.dumps(msg)[:200]}")

    except websockets.exceptions.ConnectionClosed:
        logger.info("[el] ElevenLabs WebSocket closed")
    except Exception as e:
        logger.error(f"[el] relay_el_to_twilio error: {e}", exc_info=True)


@app.post("/set-outbound-context")
async def set_outbound_context(request: Request) -> dict:
    """IPC endpoint — Node.js app server POSTs outbound ctx before dialling."""
    body = await request.json()
    conv_id = body.get("conv_id", "")
    if not conv_id:
        return {"success": False, "error": "conv_id required"}
    pending_outbound_context[conv_id] = body
    logger.info(f"[ipc] stored conv_id={conv_id} member={body.get('name')}")
    return {"success": True}


@app.get("/get-outbound-phone/{conv_id}")
async def get_outbound_phone(conv_id: str) -> dict:
    """IPC endpoint — CI webhook resolves member phone for profile lookup."""
    phone = outbound_conversation_map.get(conv_id, "")
    return {"phone": phone}


@app.get("/health")
async def health() -> dict:
    return {"status": "healthy", "agent_id": ELEVENLABS_AGENT_ID or "(not configured)"}


if __name__ == "__main__":
    import uvicorn
    logger.info(f"ElevenLabs server starting on port {EL_PORT}")
    uvicorn.run(app, host="0.0.0.0", port=EL_PORT, log_config=None, log_level="info")
