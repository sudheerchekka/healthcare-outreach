# Detailed Flow: TAC + AgentCore Voice Call

Step-by-step walkthrough of how the Python TAC server collects context, pre-warms the AgentCore connection, and streams replies back through ConversationRelay on every voice call turn.

---

## Architecture

```mermaid
flowchart TD
    subgraph Browser["Browser — Care Team Portal"]
        UI[members.html]
    end

    subgraph AppServer["Node.js App Server · port 8001"]
        API[REST API\n/api/members\n/api/outbound-call\n/api/send-sms]
        CI[CI Webhook\n/ci-webhook]
    end

    subgraph TACServer["Python TAC Server · port 8000"]
        TWIML[TwiML Routes\n/twiml · /twiml-outbound]
        WS[ConversationRelay WebSocket\n/ws]
        PREWARM[_prewarm\nAgentCore WS + memory\nin parallel at setup]
        POOL[AgentCore WS Pool\nsession_id → WebSocket]
        RELCB[/conversation-relay-callback]
        IPC[IPC Routes\n/set-outbound-context\n/get-outbound-phone]
    end

    subgraph Twilio["Twilio"]
        VOICE[Voice API]
        RELAY[ConversationRelay\nTTS + STT]
        ORCH[Conversation Orchestrator\nconv_conversation_*]
        MEM[TAC Memory Store\nProfiles · Observations · Summaries]
        CIOP[Conversation Intelligence\npost-call summary]
    end

    subgraph AgentCore["AWS AgentCore Runtime"]
        MAIN[Strands Agent\nmain.py @app.websocket\nIn-memory conversation history]
    end

    subgraph Bedrock["AWS Bedrock"]
        BMODEL[Claude Opus 4]
    end

    UI -->|load members| API
    UI -->|initiate call / send SMS| API
    API -->|read profiles| MEM
    API -->|place call| VOICE
    API -->|POST outbound ctx| IPC

    VOICE -->|fetch TwiML| TWIML
    TWIML -->|welcomeGreeting + WebSocket URL| RELAY

    RELAY <-->|"real-time audio (STT + TTS)"| WS

    WS -->|"setup event → map outboundConvId\npopulate author_info"| PREWARM
    PREWARM -->|"open presigned WebSocket\n(parallel)"| POOL
    PREWARM -->|"profile lookup + observations\n+ summaries (parallel)"| MEM

    WS -->|"prompt/interrupt"| POOL
    POOL <-->|"persistent WebSocket\ntoken stream"| MAIN
    MAIN -->|LLM inference| BMODEL
    BMODEL -->|streaming reply| MAIN
    MAIN -->|token stream| POOL
    POOL -->|tokens| WS
    WS -->|text tokens| RELAY

    RELAY -->|call ends| RELCB
    RELCB -->|close conversation| ORCH
    ORCH -->|trigger CI| CIOP
    CIOP -->|summary webhook| CI
    CI -->|GET /get-outbound-phone| IPC
    CI -->|write lastCallSummary| MEM
```

---

## Step-by-Step Flow

### Phase 1 — Before the call (dashboard)

**Step 1 — Care team clicks "Call Maria"**

```
POST /api/outbound-call  (App Server port 8001)
{ name: "Maria", phone: "+1408...", goal: "PCP_PREP", goalDesc: "Prepare for PCP visit" }
```

**Step 2 — App Server sends outbound context to Python TAC server**

```
POST http://localhost:8000/set-outbound-context
{
  conv_id:  "outbound-+1408...-1713456789000",   ← generated key
  name:     "Maria",
  goal:     "PCP_PREP",
  goalDesc: "Prepare for PCP visit",
  phone:    "+1408...",
  greeting: "Hi Maria, this is Owl Health calling about your upcoming PCP visit..."
}
```

**Step 3 — App Server places the outbound call via Twilio REST API**

```
twilioClient.calls.create({
  to:  "+1408...",
  from: "+1888...",
  url: "https://<ngrok>/twiml-outbound?conv_id=outbound-+1408...-1713456789000"
})
```

---

### Phase 2 — Call connects (TwiML → ConversationRelay)

**Step 4 — Twilio fetches TwiML**

`POST /twiml-outbound?conv_id=...` returns:
```xml
<ConversationRelay
  url="wss://<ngrok>/ws"
  welcomeGreeting="Hi Maria, this is Owl Health calling..."
/>
```

The `outboundConvId` is embedded as a `<Parameter>` in the TwiML so it appears in the ConversationRelay `setup` message.

**Step 5 — Conversation Orchestrator creates a Sierra conversation**

Because the outbound capture rule (`from=+1888...`, `to=*`) is configured, Twilio automatically creates `conv_conversation_*` and associates it with the call. This enables Conversation Intelligence to write the post-call summary to the Memory Store.

**Step 6 — ConversationRelay opens the WebSocket — `setup` event**

```
setup: {
  from: "+1888...",   to: "+1408...",
  customParameters: {
    conversationId: "conv_conversation_01kph...",   ← Maestro ID
    profileId:      "mem_profile_abc...",
    outboundConvId: "outbound-+1408...-1713456789000"   ← our key
  }
}
```

`OwlVoiceChannel._handle_setup` fires and does three things:
1. Maps `outboundConvId → conv_conversation_01kph...` so `handle_message_ready` finds the pending context
2. Sets `author_info.address = "+1408..."` (member's phone, from `to` on outbound)
3. Fires `_prewarm(conv_id, session_id, phone)` as a background task

**Step 7 — Pre-warm: AgentCore WebSocket + memory fetch run in parallel**

While Twilio is speaking the greeting to Maria:
```
Task A: generate_presigned_url(runtime_arn, session_id)
        → websockets.connect(presigned_url)       ← AgentCore WS open

Task B: POST /Profiles/Lookup { phone: "+1408..." }  → profileId
        GET  /Profiles/{id}/Observations
        GET  /Profiles/{id}/ConversationSummaries
        → memory_context_cache[conv_id] = built context string
```

Both complete during the greeting — by the time Maria says her first word, the WebSocket is open and memory is cached.

---

### Phase 3 — First member utterance (turn 1)

**Step 8 — Maria speaks → Twilio transcribes**

```
prompt event: { voicePrompt: "Yes, I have a question about my appointment.", last: true }
```

**Step 9 — `handle_message_ready` fires (turn 1)**

```python
ctx = pending_outbound_context.pop("conv_conversation_01kph...")
# → { name: "Maria", goal: "PCP_PREP", phone: "+1408...", greeting: "Hi Maria..." }

system_prompt = "You are calling Maria on behalf of the Owl Health care team.
                 The purpose of this call is: PCP_PREP. ..."

enriched = memory_context_cache["conv_conversation_01kph..."]
# → "[Greeting already spoken]\nHi Maria...\n\n### Previous Observations\n- ..."
```

Memory is already in cache from Step 7 — no API call on the critical path.

**Step 10 — Send prompt to AgentCore via pooled WebSocket**

```json
{
  "type":         "prompt",
  "voicePrompt":  "Yes, I have a question about my appointment.",
  "systemPrompt": "You are calling Maria on behalf of the Owl Health care team...",
  "memoryContext": "[Greeting already spoken]\nHi Maria...\n\n### Previous Observations\n..."
}
```

The WebSocket was opened in Step 7 — no TLS handshake on the critical path.

**Step 11 — Strands Agent processes the prompt**

```python
# Agent is created once per WebSocket connection — in-memory history for the whole call
agent = Agent(model=load_model(), system_prompt=effective_system_prompt)
input_text = "[Context]\n<enrichedContext>\n\n[User]\nYes, I have a question..."
```

Bedrock receives:
- **System prompt** — agent persona + per-call context (member name, goal)
- **In-memory history** — all prior turns in this call (turn 2+)
- **Current message** — `input_text`

**Step 12 — Reply streams back token by token**

```
AgentCore → TAC server → ConversationRelay → Twilio TTS → Maria's ear
```

Token-by-token streaming means Twilio begins speaking before the full reply is generated.

---

### Phase 4 — Turn 2+ (same call)

The Strands `Agent` object stays alive in memory for the lifetime of the WebSocket connection. No STM API calls, no memory fetches.

```python
# handle_message_ready turn 2+:
is_turn1 = False
enriched = ""          # no context re-injection
system_prompt = ""     # not re-sent

# Agent already has full history in-memory from all prior turns
```

The same pooled WebSocket is reused. Latency is just model inference + streaming.

---

### Phase 5 — Interruption

If Maria speaks while the agent is mid-reply:

```json
{ "type": "interrupt", "utterance_until_interrupt": "Actually I wanted to..." }
```

The TAC server forwards this to the AgentCore WebSocket. The `handle_voice_websocket` handler in `main.py` cancels the in-flight `stream_async` task and sends the end-of-stream sentinel immediately, unblocking the TAC server to handle the new prompt.

---

### Phase 6 — Call ends (post-call)

**Step 13 — Maria hangs up**

```
POST /conversation-relay-callback { Status: "completed" }
```

`handle_conversation_ended` fires: closes the AgentCore WebSocket, clears all caches for this `conv_id`.

**Step 14 — Conversation Intelligence processes the recording**

```
POST /ci-webhook  (TAC server port 8000 → proxied to App Server port 8001)
{ operatorResults: [{ result: { payload: '{"summary":"..."}' } }] }
```

**Step 15 — App Server resolves member phone and writes summary**

```
GET http://localhost:8000/get-outbound-phone/conv_conversation_01kph...
→ { phone: "+1408..." }

POST /Profiles/Lookup { phone: "+1408..." } → profileId
PATCH /Profiles/{profileId}
  { traits: { outreach: { lastCallSummary: "[voice, Apr 20] ..." } } }
```

On Maria's **next** call this summary is fetched in Step 7 and injected into the agent's context. The loop is complete.

---

## Memory Layers

| Layer | Provider | Scope | How it's used |
|---|---|---|---|
| Long-term | TAC Memory Store | Across calls | Fetched once at call setup (Step 7) — observations, summaries, profile traits |
| In-memory | Strands Agent object | Within a single call | Full turn history; agent stays alive for WebSocket lifetime — no STM API calls |

## Server Responsibilities

| Server | Port | Routes | Role |
|---|---|---|---|
| **Python TAC Server** | 8000 | `/twiml`, `/twiml-outbound`, `/ws`, `/conversation-relay-callback`, `/set-outbound-context`, `/get-outbound-phone`, `/ci-webhook` | Twilio-facing: TwiML, ConversationRelay WebSocket, pre-warm, AgentCore pool, IPC |
| **Node.js App Server** | 8001 | `/api/members`, `/api/outbound-call`, `/api/send-sms`, `/ci-webhook` | Dashboard API, CI webhook, TAC Memory read/write |

## TAC Component Roles

| TAC Component | Steps | What it provides |
|---|---|---|
| **Conversation Orchestrator** | 5 | Links call → `conv_conversation_*`; enables CI |
| **Memory Store — Profiles** | 7, 15 | Resolves phone number → `profileId` |
| **Memory Store — Observations** | 7 | Long-term facts about the member |
| **Memory Store — Summaries** | 7 | Past call summaries |
| **Conversation Intelligence** | 14, 15 | Auto-generates post-call summary → written back to Memory Store |
| **ConversationRelay** | 4, 6, 8, 12 | Real-time STT (speech → text) and TTS (text → speech) |

## Latency Profile

| Event | What's happening | Added latency |
|---|---|---|
| Call setup (greeting playing) | AgentCore WS open + memory fetch — parallel | ~0ms on turn 1 critical path |
| Turn 1 first token | Prompt sent on pre-opened WS → Bedrock inference → first token | Model TTFT only |
| Turn 2+ first token | Same WS reused, in-memory history, no API calls | Model TTFT only |
| Interrupt | In-flight stream cancelled, sentinel sent immediately | <10ms |
