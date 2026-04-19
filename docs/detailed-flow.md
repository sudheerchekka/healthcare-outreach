# Detailed Flow: TAC Context Collection → AgentCore Agent

Step-by-step walkthrough of how Twilio Sierra (TAC) collects context and passes it to the AgentCore Runtime agent on every voice call turn.

---

## Architecture

```mermaid
flowchart TD
    subgraph Browser["Browser — Care Team Portal"]
        UI[members.html]
    end

    subgraph AppServer["App Server · port 8001"]
        API[REST API\n/api/members\n/api/outbound-call\n/api/send-sms]
        CI[CI Webhook\n/ci-webhook]
    end

    subgraph TACServer["TAC Server · port 8000"]
        TWIML[TwiML Routes\n/twiml · /twiml-outbound]
        WS[ConversationRelay WebSocket\n/ws]
        RELCB[/conversation-relay-callback]
    end

    subgraph State["In-process State (Node.js)"]
        BRIDGE[outboundCallerToCallSid\ntwiloFrom → callSid]
        PENDING[pendingOutboundContext\ncallSid → OutboundContext]
        CACHES[systemPromptCache\nmemberPhoneCache · memoryContextCache\ngreetingCache · outboundConversationMap]
    end

    subgraph TAC["Twilio Sierra Platform"]
        VOICE[Voice API]
        SMS[SMS API]
        RELAY[ConversationRelay\nTTS + STT]
        ORCH[Conversation Orchestrator\nconv_conversation_*]
        MEM[Memory Store\nProfiles · Observations · Summaries]
        CIOP[Conversation Intelligence\npost-call summary]
    end

    subgraph AgentCore["AWS AgentCore Runtime"]
        MAIN[Strands Agent\nmain.py]
        STM[Short-Term Memory\nwithin-call turn history]
    end

    subgraph Bedrock["AWS Bedrock"]
        BMODEL[Claude Opus 4.6]
    end

    UI -->|load members| API
    UI -->|initiate call / send SMS| API
    API -->|read profiles| MEM
    API -->|place call| VOICE
    API -->|send message| SMS
    VOICE -->|fetch TwiML| TWIML
    TWIML -->|welcomeGreeting + WebSocket URL| RELAY

    RELAY <-->|"real-time audio\n(STT: speech→text, TTS: text→speech)"| WS

    WS -->|"setup: from→callSid bridge"| BRIDGE
    WS -->|"setup: stash OutboundContext"| PENDING
    WS -->|"turn 1: resolve ctx via bridge\ncache prompt + phone"| CACHES
    WS -->|"turn 1 only: profile lookup\n+ observations + summaries"| MEM
    WS -->|"turn 1: enriched context + utterance\nturn 2+: utterance only"| MAIN

    MAIN <-->|load / save turn history| STM
    MAIN -->|Converse| BMODEL
    BMODEL -->|reply| MAIN
    MAIN -->|streaming reply| WS
    WS -->|text token| RELAY

    RELAY -->|call ends| RELCB
    RELCB -->|close conversation| ORCH
    ORCH -->|trigger CI| CIOP
    CIOP -->|summary webhook| CI
    CI -->|"PATCH lastCallSummary\n(next call picks this up)"| MEM
```

---

## Step-by-Step Flow

### Phase 1 — Before the call (dashboard)

**Step 1 — Care team clicks "Call Maria"**

```
POST /api/outbound-call  (App Server port 8001)
{ name: "Maria", phone: "+1408...", goal: "PCP_PREP", goalDesc: "Prepare for PCP visit" }
```

**Step 2 — App Server stashes call context**

```typescript
lastOutboundContext = { name: "Maria", goal, goalDesc, phone: "+1408...", greeting: "Hi Maria..." }
```

The greeting text is built here and stashed — it will be injected into the agent context on turn 1.

**Step 3 — Twilio places the outbound call**

```
twilioClient.calls.create({ to: "+1408...", from: "+1888...", url: /twiml-outbound?member=Maria&... })
```

---

### Phase 2 — Call connects (TwiML → ConversationRelay)

**Step 4 — Twilio fetches TwiML**

`POST /twiml-outbound` (TAC Server port 8000) returns:
```xml
<ConversationRelay url="wss://.../ws" welcomeGreeting="Hi Maria..."/>
```
Twilio speaks the greeting via TTS to Maria before the WebSocket opens.

**Step 5 — Conversation Orchestrator creates a Sierra conversation**

Because the outbound capture rule (`from=+1888...`, `to=*`) is configured, Sierra automatically creates `conv_conversation_*` and associates it with the call. This enables Conversation Intelligence to write the post-call summary back to TAC Memory.

**Step 6 — ConversationRelay opens the WebSocket — `setup` event**

```
setup event: { callSid: "CA...", from: "+1888..." (Twilio), to: "+1408..." (Maria) }
```

`onConversationSetup` fires and stores two entries:
```typescript
pendingOutboundContext.set("CA...", ctx)       // callSid → OutboundContext
outboundCallerToCallSid.set("+1888...", "CA...") // twilioFrom → callSid
```

The Twilio `from` number (+1888...) is used as the bridge key because `session.authorInfo.address` in `onMessageReady` is set to the `from` number by the TAC SDK.

---

### Phase 3 — First member utterance (turn 1)

**Step 7 — Member speaks → Twilio transcribes**

```
prompt event: { transcript: "Yes.", last: true }
```

`session.authorInfo.address` = `"+1888..."` (Twilio's number — the call `from`).

**Step 8 — Resolve outbound context via the bridge**

```typescript
const callSid = outboundCallerToCallSid.get("+1888...")  // → "CA..."
const ctx     = pendingOutboundContext.get("CA...")       // → { name: "Maria", phone: "+1408...", ... }
```

Both maps are cleared after use. Context is applied:
```typescript
systemPromptCache.set(convId, buildSystemPrompt("Maria", "PCP_PREP", "..."))
greetingCache.set(convId, "Hi Maria, this is Owl Health Care Team...")
outboundConversationMap.set(convId, "+1408...")   // for CI webhook routing
memberPhoneCache.set(convId, "+1408...")           // for Memory Store lookup
```

---

### Phase 4 — TAC Memory lookup (turn 1 only)

**Step 9 — Look up the member's profile ID**

```
POST https://memory.twilio.com/v1/Stores/{store_id}/Profiles/Lookup
{ idType: "phone", value: "+1408..." }
→ profileId: "mem_profile_*"
```

**Step 10 — Fetch long-term memory (parallel)**

```
GET /v1/Stores/{id}/Profiles/{profileId}/Observations
→ ["Member prefers morning calls", "Has diabetes type 2", ...]

GET /v1/Stores/{id}/Profiles/{profileId}/ConversationSummaries
→ ["[voice, Apr 15] Confirmed PCP appointment...", ...]
```

**Step 11 — Build enriched context**

```
[Greeting already spoken to member]
Hi Maria, this is the Owl Health Care Team calling. I'm reaching out regarding...

# Customer Context
## Key Observations
- Member prefers morning calls
- Has diabetes type 2

## Previous Call Summaries
- [voice, Apr 15] Confirmed PCP appointment...
```

The greeting is prepended so the agent knows it already introduced itself.

> **Turn 2+ — cache hit**: `memoryContextCache` already has an entry for this `convId` → `enrichedContext = ''`. The full enriched context was stored in AgentCore STM on turn 1 (Step 16), so the agent loads it from history. No Memory Store API calls after the first turn.

---

### Phase 5 — Call AgentCore Runtime

**Step 12 — Invoke AgentCore Runtime**

```typescript
InvokeAgentRuntimeCommand({
  agentRuntimeArn: "arn:aws:bedrock-agentcore:...",
  runtimeSessionId: "conv_conversation_01kph...",   // Sierra convId = STM session key
  payload: {
    prompt:  "Yes.",                                 // member's words
    context: "[Greeting already spoken...]\n..."     // TAC long-term memory + greeting (turn 1 only)
  }
})
```

**Step 13 — AgentCore Runtime loads short-term memory**

```python
MemorySessionManager.get_last_k_turns(
  actor_id="agent",
  session_id="conv_conversation_01kph..."
)
# turn 1 → [] (empty — first turn)
# turn 2 → [{ role: "user", text: "[Context]\n...\n\n[User]\nYes." },
#            { role: "assistant", text: "Great, thanks Maria!..." }]
```

**Step 14 — Strands Agent builds input and calls Bedrock**

```python
input_text = "[Context]\n<enrichedContext>\n\n[User]\nYes."
```

Bedrock receives:
- **System prompt** → "You are Owl Health agent. Member already greeted..."
- **Prior turns** (turn 2+) → conversation history from AgentCore STM (includes context from turn 1)
- **Current message** → `input_text`

**Step 15 — Reply streams back**

```
main.py yields SSE chunks
  → Node.js collects and sends: socket.send({ type: 'text', token: reply, last: true })
  → Twilio TTS speaks reply to Maria
```

**Step 16 — AgentCore saves this turn to STM**

```python
MemorySessionManager.add_turns(
  session_id="conv_conversation_01kph...",
  messages=[
    ConversationalMessage(
      text="[Context]\n[Greeting...]\n# Customer Context\n...\n\n[User]\nYes.",
      role=USER
    ),
    ConversationalMessage(text="Great, thanks Maria!...", role=ASSISTANT),
  ]
)
```

The full `input_text` (not just the raw utterance) is stored so turn 2+ loads the TAC context from STM history — no repeat Memory Store fetches needed.

---

### Phase 6 — Call ends (post-call)

**Step 17 — Maria hangs up**

```
POST /conversation-relay-callback { Status: "completed" }
→ Conversation Orchestrator closes conv_conversation_01kph...
```

Closing the conversation signals Sierra to run Conversation Intelligence on the recording.

**Step 18 — Conversation Intelligence processes the recording**

```
POST /ci-webhook  (App Server port 8001)
{ operatorResults: [{ result: { payload: '{"summary":"..."}' }, executionDetails: { ... } }] }
```

**Step 19 — App Server writes summary back to TAC Memory**

```typescript
outboundConversationMap.get("conv_conversation_01kph...") → "+1408..."
POST /Profiles/Lookup → profileId
PATCH /Profiles/{profileId}
  { traits: { outreach: { lastCallSummary: "[voice, Apr 18] ..." } } }
```

On Maria's **next** call this summary is fetched in Step 10 and injected into the agent's context. The loop is complete.

---

## Memory Layers

| Layer | Provider | Scope | Used for |
|---|---|---|---|
| Long-term | TAC Memory Store | Across calls | Member profile, past summaries, observations — fetched once on turn 1 |
| Short-term | AgentCore STM | Within a single call | Turn history including enriched context; eliminates repeat TAC fetches |

## Server Responsibilities

| Server | Port | Routes | Role |
|---|---|---|---|
| **TAC Server** | 8000 | `/twiml`, `/twiml-outbound`, `/ws`, `/conversation-relay-callback` | Twilio-facing: TwiML, ConversationRelay WebSocket, call lifecycle |
| **App Server** | 8001 | `/api/members`, `/api/outbound-call`, `/api/send-sms`, `/ci-webhook` | Dashboard API, CI webhook, TAC Memory read/write |

## TAC Component Roles

| TAC Component | Steps | What it provides |
|---|---|---|
| **Conversation Orchestrator** | 5, 8 | Links `callSid` → `conv_conversation_*`; enables CI |
| **Memory Store — Profiles** | 9 | Resolves phone number → `profileId` |
| **Memory Store — Observations** | 10 | Long-term facts about the member |
| **Memory Store — Summaries** | 10 | Past call summaries |
| **Conversation Intelligence** | 18, 19 | Auto-generates post-call summary, written back to Memory Store |
| **ConversationRelay** | 4, 6, 7, 15 | Real-time STT (speech → text) and TTS (text → speech) |
