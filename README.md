# Owl Health Member Outreach — Node.js / TypeScript

A care outreach demo using **AWS AgentCore** (Strands Agent) for LLM orchestration and **Twilio Agent Connect (TAC)** for voice, memory, and conversation intelligence.

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

Two servers run in parallel — a **Python TAC server** for all Twilio-facing traffic, and a **Node.js app server** for the dashboard and APIs.

```
Browser Dashboard (port 8001)
  │  GET  /api/members            → load profiles from TAC Memory Store
  │  POST /api/outbound-call      → POST context to Python TAC server, place call via Twilio
  │  POST /api/send-sms           → send SMS via Twilio SMS API
  │  POST /ci-webhook             → receive CI post-call summary → write to member profile
  │
  └── src/apps/healthcare/server/api.ts  (Fastify, port 8001)


Python TAC Server — Twilio-facing webhooks (port 8000, ngrok exposes this)
  │
  ├── POST /twiml                       → TwiML for inbound calls
  ├── POST /twiml-outbound              → TwiML for outbound calls (personalized greeting)
  ├── WS   /ws                          → ConversationRelay WebSocket
  │     │
  │     ├── setup event                 → map outbound conv_id, populate caller phone,
  │     │     └── _prewarm()            → open AgentCore WS + fetch TAC memory IN PARALLEL
  │     │                                  (runs during greeting — turn 1 latency eliminated)
  │     ├── prompt turn 1               → enriched context already cached → send to agent instantly
  │     ├── prompt turn 2+              → reuse pooled AgentCore WebSocket, Strands Agent
  │     │                                  in-memory history — no API calls mid-call
  │     └── interrupt                   → cancel in-flight stream, send sentinel
  │
  ├── POST /conversation-relay-callback → close Maestro conversation
  ├── POST /set-outbound-context        → IPC: Node.js stores pending outbound ctx before dial
  ├── GET  /get-outbound-phone/{id}     → IPC: Node.js CI webhook resolves member phone
  ├── POST /ci-webhook                  → proxy to Node.js app server (port 8001)
  └── GET  /health                      → health check
  │
  └── src/tac/server.py  (FastAPI + uvicorn)


AgentCore Runtime — Python agent (deployed to AWS)
  │  Receives WebSocket messages: {"type":"prompt","voicePrompt":"...","systemPrompt":"...","memoryContext":"..."}
  │  Sends token stream:          {"type":"text","token":"...","last":false/true}
  └── src/apps/healthcare/agent/src/main.py
```

### Memory strategy

Two memory layers work together across and within calls:

| Layer | Provider | Scope | Content |
|---|---|---|---|
| Long-term | TAC Memory Store | Across calls | Member observations, past call summaries |
| In-memory | Strands Agent object | Within a single call | Full conversation history — no STM API calls |

TAC Memory is fetched during call setup (before the member speaks) via `_prewarm()`, which runs the AgentCore WebSocket open and the memory fetch in parallel while the greeting plays. The Strands `Agent` object stays alive for the lifetime of the WebSocket connection — all turns share in-memory history.

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
│   │       │   ├── members.html
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

---

## Running Locally

Three processes run in parallel — ngrok, the Python TAC server, and the Node.js app server.

### 1. Expose the TAC server with ngrok

Twilio needs a public HTTPS URL for TwiML and the ConversationRelay WebSocket. In a dedicated terminal:

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
uvicorn server:app --port 8000 --reload
```

### 3. Start the Node.js app server (port 8001)

In a third terminal:

```bash
npm install        # first time only
npm run dev
```

| Server | Port | Purpose |
|---|---|---|
| Python TAC server | 8000 | Twilio webhooks — TwiML, ConversationRelay, pre-warm, AgentCore WS pool |
| Node.js app server | 8001 | Dashboard, member APIs, CI webhook |

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

```bash
npm run build
docker build -t healthcare-outreach-node .

docker run -p 8000:8000 -p 8001:8001 \
  --env-file .env \
  healthcare-outreach-node
```

On EC2 with an IAM role attached, omit `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, and `AWS_PROFILE` — credentials are picked up automatically from the instance metadata.

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

**Python TAC server (port 8000) — Twilio-facing**

| Method | Path | Description |
|---|---|---|
| `POST` | `/twiml` | TwiML for inbound calls |
| `POST` | `/twiml-outbound` | TwiML for outbound calls (personalized greeting) |
| `WS` | `/ws` | ConversationRelay audio stream |
| `POST` | `/conversation-relay-callback` | Call-end webhook — closes Maestro conversation |
| `POST` | `/set-outbound-context` | IPC: Node.js stores outbound call context before dialling |
| `GET` | `/get-outbound-phone/{conv_id}` | IPC: Node.js CI webhook resolves member phone number |
| `POST` | `/ci-webhook` | Proxy to Node.js app server (port 8001) |
| `GET` | `/health` | Health check |

**Node.js app server (port 8001) — dashboard and application APIs**

| Method | Path | Description |
|---|---|---|
| `GET` | `/members.html` | Care team dashboard (static) |
| `GET` | `/api/members` | All member profiles from TAC Memory Store |
| `POST` | `/api/outbound-call` | Initiate outbound voice call |
| `POST` | `/api/send-sms` | Send outreach SMS |
| `POST` | `/ci-webhook` | CI summary → write to member profile |

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
