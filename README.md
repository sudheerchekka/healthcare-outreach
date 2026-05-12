# Owl Health Member Outreach — Node.js / TypeScript

A care outreach demo supporting three voice agent backends — **AWS AgentCore** (Strands Agent), **Google Vertex AI Agent Engine** (Gemini), and **ElevenLabs Conversational AI** — with **Twilio Agent Connect (TAC)** for voice, memory, and conversation intelligence.

---

## Overview

This app powers a care team portal where agents can trigger outbound voice calls and SMS messages to members. Key capabilities:

- **Outbound voice calls** — care team dials a member, agent greets them by name with their follow-up topic, and conducts a personalized care conversation
- **Inbound voice calls** — caller is identified by phone; agent has full context from past interactions
- **Outreach SMS** — send a personalized follow-up message from the dashboard
- **Live member dashboard** — browser UI that loads member profiles, statuses, and last call summaries from TAC Memory Store
- **Post-call summary** — Conversation Intelligence extracts a summary and writes it back to the member's profile automatically

For a detailed step-by-step walkthrough, see [docs/detailed-flow.md](docs/detailed-flow.md).

### How it works

Two servers run in parallel — a **Python TAC server** for all Twilio-facing traffic, and a **Node.js app server** for the dashboard and APIs. The `AGENT_BACKEND` env var switches between two voice agent backends without changing ports or ngrok.

```
Browser Dashboard (port 8001)
  │  GET  /api/members            → load profiles from TAC Memory Store
  │  POST /api/outbound-call      → POST context to Python TAC server, place call via Twilio
  │  POST /api/send-sms           → send SMS via Twilio SMS API
  │  POST /ci-webhook             → receive CI post-call summary → write to member profile
  │
  └── src/apps/healthcare/server/api.ts  (Fastify, port 8001)


Python TAC Server — single Twilio-facing gateway (port 8000, ngrok always exposes this)
  │
  ├── POST /twiml                       → TwiML for inbound calls
  ├── POST /twiml-outbound              → TwiML for outbound calls
  │     └── AGENT_BACKEND=agentcore     → ConversationRelay TwiML → /ws
  │     └── AGENT_BACKEND=elevenlabs    → Stream TwiML → /ws-el  (proxied to ElevenLabs server)
  │
  ├── WS   /ws                          → ConversationRelay WebSocket (agentcore backend)
  │     │
  │     ├── setup event                 → map outbound conv_id, populate caller phone,
  │     │     └── _prewarm()            → open AgentCore WS + fetch TAC memory IN PARALLEL
  │     │                                  (runs during greeting — turn 1 latency eliminated)
  │     ├── prompt turn 1               → enriched context already cached → send to agent instantly
  │     ├── prompt turn 2+              → reuse pooled AgentCore WebSocket, Strands Agent
  │     │                                  in-memory history — no API calls mid-call
  │     └── interrupt                   → cancel in-flight stream, send sentinel
  │
  ├── WS   /ws-el                       → WebSocket proxy to ElevenLabs server (elevenlabs backend)
  │
  ├── POST /conversation-relay-callback → close Maestro conversation
  ├── POST /set-outbound-context        → IPC: stores outbound ctx; forwards to ElevenLabs if needed
  ├── GET  /get-outbound-phone/{id}     → IPC: Node.js CI webhook resolves member phone
  ├── POST /ci-webhook                  → proxy to Node.js app server (port 8001)
  └── GET  /health                      → health check
  │
  └── src/tac/server.py  (FastAPI + uvicorn)


AgentCore Runtime — Python agent (deployed to AWS)   [AGENT_BACKEND=agentcore]
  │  Receives WebSocket messages: {"type":"prompt","voicePrompt":"...","systemPrompt":"...","memoryContext":"..."}
  │  Sends token stream:          {"type":"text","token":"...","last":false/true}
  └── src/apps/healthcare/agent/src/main.py


ElevenLabs Server — raw audio bridge (port 8002)        [AGENT_BACKEND=elevenlabs]
  │  Receives proxied Twilio Stream audio from TAC /ws-el
  │  Fetches TAC Memory Store → passes context to ElevenLabs via conversation_initiation_client_data
  │  Transcodes µ-law 8kHz ↔ PCM 16kHz (numpy)
  └── elevenlabs/server.py  (FastAPI + uvicorn)
```

### Memory strategy

Two memory layers work together across and within calls:

| Layer | Provider | Scope | Content |
|---|---|---|---|
| Long-term | TAC Memory Store | Across calls | Member observations, past call summaries |
| In-memory | Strands Agent object | Within a single call | Full conversation history — no STM API calls |

TAC Memory is fetched during call setup (before the member speaks) via `_prewarm()`, which runs the AgentCore WebSocket open and the memory fetch in parallel while the greeting plays. The Strands `Agent` object stays alive for the lifetime of the WebSocket connection — all turns share in-memory history.

### Agent backend switching

Set `AGENT_BACKEND` in `.env` to switch between backends. **ngrok always points to port 8000** — no URL changes needed.

| `AGENT_BACKEND` | Voice AI | TwiML type | TAC role |
|---|---|---|---|
| `agentcore` (default) | AWS AgentCore (Strands + Claude) | ConversationRelay | Full voice stack — STT, agent, TTS |
| `vertexai` | Google Vertex AI Agent Engine (Gemini) | ConversationRelay | Full voice stack — STT, agent, TTS |
| `elevenlabs` | ElevenLabs Conversational AI | `<Stream>` | Gateway — proxies raw audio via `/ws-el` to ElevenLabs server on port 8002 |

**How it works when `AGENT_BACKEND=elevenlabs`:**

1. Dashboard POSTs outbound call → TAC server stores context and forwards it to ElevenLabs server on port 8002
2. TAC's `/twiml-outbound` returns `<Stream>` TwiML pointing to `wss://<ngrok-domain>/ws-el`
3. Twilio connects to TAC's `/ws-el` WebSocket proxy
4. TAC proxies the raw audio stream bidirectionally to `ws://localhost:8002/ws`
5. ElevenLabs server handles STT + LLM + TTS; fetches TAC Memory Store for member context
6. Conversation Intelligence and Memory Store still work — CI webhooks route through TAC and Node.js as usual

**What differs between backends:**

| Feature | `agentcore` | `vertexai` | `elevenlabs` |
|---|---|---|---|
| STT / TTS | Twilio (ConversationRelay) | Twilio (ConversationRelay) | ElevenLabs |
| LLM | AWS Bedrock (Claude Opus 4) | Google Gemini (Vertex AI) | ElevenLabs agent |
| TAC Memory Store | Yes | Yes | Yes (fetched at call start) |
| Conversation Intelligence | Yes | Yes | Yes (CI webhooks unchanged) |
| Inbound call routing | Yes (`/twiml`) | Yes (`/twiml`) | Partial — inbound TwiML not wired to ElevenLabs |

---

## Project Structure

```
healthcare-outreach-node/
├── src/
│   ├── index.ts                         # Entry point — starts Node.js app server (port 8001)
│   │
│   ├── tac/
│   │   ├── server.py                    # Python TAC server (FastAPI + uvicorn, port 8000)
│   │   └── requirements.txt             # Python deps (twilio-agent-connect, bedrock-agentcore, …)
│   │
│   ├── apps/
│   │   └── healthcare/
│   │       ├── client/                  # Browser dashboard
│   │       │   ├── members.html         # Member list + actions
│   │       │   ├── member-detail.html   # Per-member observations, summaries, live CI results
│   │       │   └── owl-health-logo-*.svg
│   │       ├── server/                  # Node.js app logic
│   │       │   ├── api.ts               # Fastify routes — /api/*, /ci-webhook, dashboard
│   │       │   └── memory.ts            # TAC Memory Store helpers
│   │       └── agent/                   # Python AgentCore Runtime agent
│   │           ├── src/
│   │           │   ├── main.py          # @app.entrypoint (HTTP) + @app.websocket (voice WS)
│   │           │   └── model/load.py    # Bedrock model config (claude-opus-4-6)
│   │           ├── pyproject.toml       # Python dependencies (managed by uv)
│   │           └── .bedrock_agentcore.yaml
│   │
│   ├── prompts.ts                       # Greeting + system prompt builders
│   └── types.ts                         # Shared TypeScript interfaces
│
├── elevenlabs/                          # ElevenLabs voice backend (AGENT_BACKEND=elevenlabs)
│   ├── server.py                        # FastAPI server — Twilio Stream bridge to ElevenLabs WS
│   ├── requirements.txt                 # elevenlabs, fastapi, uvicorn, websockets, numpy
│   └── .env.example                     # ELEVENLABS_API_KEY, ELEVENLABS_AGENT_ID
│
├── scripts/
│   ├── add-outbound-capture-rule.ts     # Configure Conversation Orchestrator for outbound calls
│   └── grant-agentcore-memory-permissions.ts  # Grant STM access to AgentCore execution role
│
├── blog/
│   └── agentic-care-coordination.md    # Twilio blog post draft
│
├── docs/
│   └── detailed-flow.md                 # Step-by-step TAC + AgentCore call flow
│
├── .env.example             # All required environment variables
├── package.json
├── tsconfig.json
├── Dockerfile
└── README.md
```

---

## Prerequisites

- Node.js 20+
- Python 3.10+ and `pip` (for the TAC server); [uv](https://github.com/astral-sh/uv) is used by the AgentCore agent (`agentcore dev` manages it automatically)
- An active Twilio account with:
  - TAC Memory Store
  - Conversation Orchestrator configuration (see [Orchestrator Setup](#conversation-orchestrator-setup) below)
  - Conversation Intelligence configuration with a summary operator
  - A Twilio phone number
- AWS account with Bedrock access (Claude Opus 4 enabled in your region)
- [ngrok](https://ngrok.com) (for local development — exposes the TAC server to Twilio)
- `bedrock-agentcore-starter-toolkit` CLI (`pip install bedrock-agentcore-starter-toolkit`)

---

## Environment Variables

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

**Shared — used by both servers:**

| Variable | Description |
|---|---|
| `TWILIO_ACCOUNT_SID` | Twilio Account SID (`ACxxx`) |
| `TWILIO_AUTH_TOKEN` | Twilio Auth Token |
| `TWILIO_PHONE_NUMBER` | Twilio phone number for calls/SMS |
| `TWILIO_API_KEY` | Twilio API Key (`SKxxx`) — for Memory Store access |
| `TWILIO_API_TOKEN` | Twilio API Key Secret |
| `MEMORY_STORE_ID` | TAC Memory Store ID |

**Node.js app server** (read by `api.ts`):

| Variable | Description |
|---|---|
| `VOICE_PUBLIC_DOMAIN` | ngrok domain with `https://` (e.g. `https://abc.ngrok.io`) |
| `OUTBOUND_CALL_TO` | Override number for all outbound calls (testing) |
| `TWILIO_TAC_CI_SUMMARY_OPERATOR_SID` | Conversation Intelligence summary operator SID |
| `TAC_PORT` | Python TAC server port (default: `8000`) |
| `APP_PORT` | Node.js app server port (default: `8001`) |

**Python TAC server** (read by `server.py`):

| Variable | Description |
|---|---|
| `TWILIO_TAC_ACCOUNT_SID` | Same value as `TWILIO_ACCOUNT_SID` |
| `TWILIO_TAC_AUTH_TOKEN` | Same value as `TWILIO_AUTH_TOKEN` |
| `TWILIO_TAC_PHONE_NUMBER` | Same value as `TWILIO_PHONE_NUMBER` |
| `TWILIO_TAC_API_KEY` | Same value as `TWILIO_API_KEY` |
| `TWILIO_TAC_API_TOKEN` | Same value as `TWILIO_API_TOKEN` |
| `TWILIO_TAC_ENVIRONMENT` | TAC environment — use `prod` |
| `TWILIO_TAC_CONVERSATION_SERVICE_SID` | Conversation Configuration ID (`conv_configuration_*`) |
| `TWILIO_TAC_MEMORY_STORE_ID` | Same value as `MEMORY_STORE_ID` |
| `VOICE_CONCIERGE_RUNTIME_ARN` | AgentCore Runtime ARN (from `agentcore status`) |
| `AWS_REGION` | AWS region (e.g. `us-east-1`) |
| `AWS_PROFILE` | AWS CLI profile name (alternative to access key/secret) |
| `AWS_ACCESS_KEY_ID` | AWS access key (not needed when using `AWS_PROFILE` or an IAM role) |
| `AWS_SECRET_ACCESS_KEY` | AWS secret key (not needed when using `AWS_PROFILE` or an IAM role) |
| `AGENT_LOCAL_URL` | Local dev only — set to `http://localhost:<port>` to bypass presigned URL and connect directly to `agentcore dev` |

**Agent backend + ElevenLabs** (read by both TAC server and ElevenLabs server):

| Variable | Description |
|---|---|
| `AGENT_BACKEND` | `agentcore` (default), `vertexai`, or `elevenlabs` — controls which voice backend TAC routes to |
| `ELEVENLABS_API_KEY` | ElevenLabs API key (`elevenlabs` backend only) |
| `ELEVENLABS_AGENT_ID` | ElevenLabs Agent ID (`elevenlabs` backend only) |
| `ELEVENLABS_PORT` | ElevenLabs server port (default: `8002`) |

**Vertex AI / Gemini** (read by TAC server when `AGENT_BACKEND=vertexai`):

| Variable | Description |
|---|---|
| `VERTEXAI_PROJECT` | Google Cloud project ID |
| `VERTEXAI_LOCATION` | Vertex AI region (e.g. `us-central1`) |
| `VERTEXAI_AGENT_ID` | Deployed Vertex AI Agent Engine resource name |
| `GOOGLE_APPLICATION_CREDENTIALS` | Path to GCP service account JSON key file (or use ADC) |

**Conversation Intelligence live results** (read by `api.ts`):

| Variable | Description |
|---|---|
| `CI_OPERATOR_1_SID` … `CI_OPERATOR_4_SID` | Operator SIDs to display in the live results grid on member detail page |
| `CI_OPERATOR_1_LABEL` … `CI_OPERATOR_4_LABEL` | Display labels for each operator slot (e.g. `Sentiment`, `Next Best Response`) |

---

## Running Locally

**ngrok always exposes port 8000 regardless of which backend you use.** The TAC server is the single Twilio-facing gateway.

### Quick start (3 terminals)

```bash
# Terminal 1 — ngrok
ngrok http 8000

# Terminal 2 — Python TAC server (port 8000)
cd src/tac && source .venv/bin/activate && uvicorn server:app --port 8000 --reload

# Terminal 3 — Node.js app server (port 8001)
npm run dev
```

Open the dashboard at **http://localhost:8001/members.html**

---

### 1. Expose the TAC server with ngrok

Twilio needs a public HTTPS URL for TwiML and the WebSocket. In a dedicated terminal:

```bash
ngrok http 8000
```

Copy the forwarding URL (e.g. `https://abc123.ngrok-free.app`) and set it in `.env`:

```
VOICE_PUBLIC_DOMAIN=https://abc123.ngrok-free.app
```

Configure your Twilio phone number's **Voice webhook** to:
```
https://abc123.ngrok-free.app/twiml
```

### 2. Start the Python TAC server (port 8000)

```bash
cd src/tac
python3 -m venv .venv          # first time only
source .venv/bin/activate
pip install -r requirements.txt  # first time only
AWS_PROFILE=<profile_name> uvicorn server:app --port 8000 --reload
```

### 3. Start the Node.js app server (port 8001)

In a third terminal:

```bash
npm install        # first time only
npm run dev
```

### 4. (ElevenLabs only) Start the ElevenLabs server (port 8002)

Only needed when `AGENT_BACKEND=elevenlabs`. In a fourth terminal:

```bash
cd elevenlabs
python3 -m venv .venv          # first time only
source .venv/bin/activate
pip install -r requirements.txt  # first time only
uvicorn server:app --port 8002 --reload
```

Make sure `ELEVENLABS_API_KEY` and `ELEVENLABS_AGENT_ID` are set in `.env`.

| Server | Port | Purpose |
|---|---|---|
| Python TAC server | 8000 | Always running — Twilio webhooks, routing gateway |
| Node.js app server | 8001 | Dashboard, member APIs, CI webhook |
| ElevenLabs server | 8002 | ElevenLabs voice bridge (only when `AGENT_BACKEND=elevenlabs`) |

Open the care team dashboard at:
```
http://localhost:8001/members.html
```

### 4. Production build (Node.js only)

```bash
npm run build        # compiles TypeScript → dist/
npm start            # runs node dist/index.js
```

### Docker

The Docker container runs both servers — Python TAC server (port 8000) and Node.js app server (port 8001) — from a single image.

#### 1. Vendor the TAC Python SDK (one-time per machine)

The TAC SDK is not on PyPI. Copy it into `vendor/` so Docker can find it:

```bash
cp -r ~/.claude/cache/twilio-agent-connect-python vendor/twilio-agent-connect
```

> `vendor/` is gitignored. Repeat this after cloning on a new machine.

#### 2. Build the image

```bash
docker build -t healthcare-outreach-node .
```

#### 3. Run

**With AWS profile** (local dev — mounts your `~/.aws` credentials read-only):

```bash
docker run -p 8000:8000 -p 8001:8001 \
  --env-file .env \
  -v ~/.aws:/root/.aws:ro \
  healthcare-outreach-node
```

**On EC2 with an IAM role** (no credential files needed):

```bash
docker run -p 8000:8000 -p 8001:8001 \
  --env-file .env \
  healthcare-outreach-node
```

Open the dashboard at `http://localhost:8001/members.html`.

| Port | Server |
|---|---|
| 8000 | Python TAC server (Twilio webhooks, ConversationRelay) |
| 8001 | Node.js app server (dashboard, member APIs) |

---

## Conversation Orchestrator Setup

The Conversation Orchestrator must have an outbound VOICE capture rule so that outbound calls create a `conv_conversation_*` in Sierra (required for Conversation Intelligence to write post-call summaries to the Memory Store).

By default, new Conversation Configurations only capture **inbound** calls (`from=*, to=<your-twilio-number>`). You need to add the reverse rule for outbound calls.

### Option A — Script (recommended)

```bash
npx ts-node scripts/add-outbound-capture-rule.ts
```

This script:
- Fetches the current configuration so no existing settings are lost
- Checks if the outbound rule already exists (idempotent)
- Adds `from=<TWILIO_TAC_PHONE_NUMBER>, to=*` to the VOICE capture rules

### Option B — Twilio Console

1. Go to **Conversations → Conversation Orchestrator → Configurations → your config → Channels → Voice**
2. Add a capture rule: `from: <your-twilio-number>`, `to: *`

### Option C — curl

```bash
source .env && \
CONFIG=$(curl -s "https://conversations.twilio.com/v2/ControlPlane/Configurations/${TWILIO_TAC_CONVERSATION_SERVICE_SID}" \
  -u "${TWILIO_TAC_API_KEY}:${TWILIO_TAC_API_TOKEN}") && \
curl -s -X PUT "https://conversations.twilio.com/v2/ControlPlane/Configurations/${TWILIO_TAC_CONVERSATION_SERVICE_SID}" \
  -u "${TWILIO_TAC_API_KEY}:${TWILIO_TAC_API_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "$(echo $CONFIG | python3 -c "
import sys, json
c = json.load(sys.stdin)
rules = c['channelSettings']['VOICE']['captureRules']
new_rule = {'from': '${TWILIO_TAC_PHONE_NUMBER}', 'to': '*', 'metadata': {'callType': 'PSTN'}}
if not any(r['from'] == new_rule['from'] and r['to'] == '*' for r in rules):
    rules.append(new_rule)
c['channelSettings']['VOICE']['captureRules'] = rules
print(json.dumps(c))
")"
```

> **Note:** The Conversation Configuration `PUT` endpoint replaces the entire config. Always fetch first and merge — never construct the body from scratch.

---

## API Endpoints

**Python TAC server (port 8000) — Twilio-facing gateway (always running)**

| Method | Path | Description |
|---|---|---|
| `POST` | `/twiml` | TwiML for inbound calls |
| `POST` | `/twiml-outbound` | TwiML for outbound calls — ConversationRelay or `<Stream>` based on `AGENT_BACKEND` |
| `WS` | `/ws` | ConversationRelay WebSocket (`agentcore` backend) |
| `WS` | `/ws-el` | WebSocket proxy to ElevenLabs server (`elevenlabs` backend) |
| `POST` | `/conversation-relay-callback` | Call-end webhook — closes Maestro conversation |
| `POST` | `/set-outbound-context` | IPC: stores outbound call context; forwards to ElevenLabs if needed |
| `GET` | `/get-outbound-phone/{conv_id}` | IPC: Node.js CI webhook resolves member phone number |
| `POST` | `/ci-webhook` | Proxy to Node.js app server (port 8001) |
| `GET` | `/health` | Health check |

**Node.js app server (port 8001) — dashboard and application APIs**

| Method | Path | Description |
|---|---|---|
| `GET` | `/members.html` | Care team member list dashboard (static) |
| `GET` | `/member-detail.html` | Per-member detail page — observations, summaries, live CI results (static) |
| `GET` | `/api/members` | All member profiles from TAC Memory Store |
| `POST` | `/api/outbound-call` | Initiate outbound voice call |
| `POST` | `/api/send-sms` | Send outreach SMS |
| `POST` | `/ci-webhook` | CI operator result → store + push to live SSE subscribers |
| `GET` | `/api/member-detail/:profileId` | Observations + conversation summaries for a member |
| `DELETE` | `/api/member-detail/:profileId/observations/:obsId` | Delete a single observation |
| `DELETE` | `/api/member-detail/:profileId/summaries/:sumId` | Delete a single conversation summary |
| `GET` | `/api/ci-results/:profileId` | Snapshot of latest CI operator results |
| `GET` | `/api/ci-results/:profileId/stream` | SSE stream — pushes CI results in real time as webhooks arrive |

**ElevenLabs server (port 8002) — voice bridge (only when `AGENT_BACKEND=elevenlabs`)**

| Method | Path | Description |
|---|---|---|
| `WS` | `/ws` | Receives proxied Twilio Stream from TAC; connects to ElevenLabs Conversational AI |
| `POST` | `/set-outbound-context` | IPC from TAC server — stores outbound call context |
| `GET` | `/health` | Health check |

---

## AgentCore Runtime Agent

The Python agent under `src/apps/healthcare/agent/` is built with [Strands Agents](https://github.com/strands-agents/sdk-python) and deployed via the `agentcore` CLI. It exposes two entrypoints:

- `@app.entrypoint` — HTTP invocation (used by `agentcore invoke` for testing)
- `@app.websocket` — persistent WebSocket per call, used by the TAC server for streaming voice turns

The Strands `Agent` object is created once per WebSocket connection and kept alive for the full call — no STM round-trips between turns.

### Prerequisites

```bash
pip install bedrock-agentcore-starter-toolkit
```

### Project structure

```
src/apps/healthcare/agent/
├── src/
│   ├── main.py              # @app.entrypoint + @app.websocket handlers
│   ├── model/load.py        # Bedrock model config (claude-opus-4-6)
│   └── mcp_client/client.py # MCP client stub (unused)
├── pyproject.toml           # Python dependencies (managed by uv)
└── .bedrock_agentcore.yaml  # CLI config — agent name, ARN, memory ID
```

### Local development

`agentcore dev` uses `uv` to manage the virtual environment — no manual `venv` needed.

```bash
cd src/apps/healthcare/agent

# Start local dev server (defaults to port 8080; shifts to 8081 if 8080 is taken)
AWS_PROFILE=<your-profile> AWS_ACCESS_KEY_ID= AWS_SECRET_ACCESS_KEY= agentcore dev
```

> **Note:** Clear `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` explicitly if your shell has them set for a different AWS account — they override `AWS_PROFILE`.

To connect the TAC server to the local agent instead of the deployed one, set in `.env`:
```
AGENT_LOCAL_URL=http://localhost:8081   # match the port agentcore dev is using
```

Remove or comment out `AGENT_LOCAL_URL` to switch back to the deployed AgentCore agent.

Test the HTTP entrypoint directly:
```bash
# In a second terminal
AWS_PROFILE=<your-profile> agentcore invoke --dev --port 8081 '{"prompt": "Hi, how are you?"}'
```

### Deploy to AWS

```bash
cd src/apps/healthcare/agent
AWS_PROFILE=<your-profile> AWS_ACCESS_KEY_ID= AWS_SECRET_ACCESS_KEY= agentcore deploy
```

The CLI builds and deploys the Python code directly to AgentCore Runtime (no Docker required). After deploy:

```bash
AWS_PROFILE=<your-profile> agentcore status   # confirms ARN and memory_id
```

Set `VOICE_CONCIERGE_RUNTIME_ARN` in `.env` to the ARN shown in `agentcore status`, then restart the TAC server.

### Post-deploy: grant memory permissions to the execution role

The execution role does **not** have STM memory access by default. Run this once after each deploy into a new environment:

```bash
npx ts-node scripts/grant-agentcore-memory-permissions.ts
```

Set these in `.env` first (values from `agentcore status`):
```
AGENTCORE_EXECUTION_ROLE_NAME=AmazonBedrockAgentCoreSDKRuntime-<region>-<suffix>
AGENTCORE_MEMORY_ARN=arn:aws:bedrock-agentcore:<region>:<account>:memory/<memory-id>
```

Without this step, the `@app.entrypoint` HTTP handler will silently fail STM reads/writes. The `@app.websocket` voice handler is unaffected (it uses Strands in-memory history, not STM).

### Tear down

```bash
AWS_PROFILE=<your-profile> agentcore destroy
```

---

## Latency Profile

| Event | What's happening | Latency impact |
|---|---|---|
| Call setup (greeting playing) | AgentCore WS open + TAC memory fetch run in parallel | Zero — hidden behind greeting |
| Turn 1 first token | Prompt sent on pre-opened WS → Bedrock inference → first token | Model TTFT only |
| Turn 2+ first token | Same WS reused, Strands in-memory history, no API calls | Model TTFT only |
| Interrupt | In-flight stream cancelled, sentinel sent immediately | <10ms |
