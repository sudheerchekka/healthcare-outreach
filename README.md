# Owl Health Member Outreach — Node.js / TypeScript

A Node.js/TypeScript based Owl Health care outreach demo, using **AWS AgentCore** for LLM orchestration and **Twilio Conversation Memory** for persistent member profiles.


---

## Overview

This app powers a care team portal where agents can trigger outbound voice calls and SMS messages to members. Key capabilities:

- **Outbound voice calls** — agent dials a member, greets them by name with their next follow-up topic, and conducts a personalized care conversation
- **Inbound voice calls** — caller is greeted by name; agent has full context from past interactions
- **Outreach SMS** — send a personalized follow-up message to a member from the dashboard
- **Live member dashboard** — browser UI that loads member profiles, statuses, and last call summaries from Twilio Conversation Memory
- **Post-call summary** — Twilio Conversation Intelligence extracts a call summary and writes it back to the member's profile automatically

For a detailed step-by-step walkthrough of the TAC + AgentCore integration, see [docs/detailed-flow.md](docs/detailed-flow.md).

### How it works

Two servers run in parallel — one owned by the TAC SDK for all Twilio communication, one for app-specific APIs and the dashboard.

```
Browser Dashboard (port 8001)
  │  GET  /api/members          → load profiles from Twilio Memory Store
  │  POST /api/outbound-call    → place call via Twilio Voice API
  │  POST /api/send-sms         → send SMS via Twilio SMS API
  │  POST /ci-webhook           → receive CI post-call summary → write to member profile
  │
  └── src/apps/healthcare/api.ts  (Fastify, port 8001)


TACServer — Twilio-facing webhooks (port 8000, ngrok exposes this)
  │
  ├── POST /twiml                 → SDK generates TwiML with greeting + ConversationRelay URL
  ├── WS   /ws                    → SDK manages ConversationRelay WebSocket lifecycle
  │     │
  │     ├── setup (callSid)       → stash outbound context keyed by callSid
  │     ├── prompt turn 1         → SDK calls onMessageReady
  │     │     └── fetch TAC Memory + build enriched context → invoke AgentCore Runtime
  │     │         AgentCore STM saves full context; Node.js caches it for call lifetime
  │     ├── prompt turn 2+        → SDK calls onMessageReady
  │     │     └── invoke AgentCore Runtime (cache hit — no TAC fetch; context in STM)
  │     └── interrupt             → SDK sends empty acknowledgement
  │
  ├── POST /conversation-relay-callback → SDK closes conversation, fires onConversationEnded
  └── POST /cintel                → Conversation Intelligence webhook (SDK route)
  │
  └── src/tac/server.ts + src/apps/healthcare/callbacks.ts
```

### Memory strategy

Two memory layers work together across and within calls:

| Layer | Provider | Scope | Content |
|---|---|---|---|
| Long-term | TAC Memory Store | Across calls | Member observations, past call summaries |
| Short-term | AgentCore STM | Within a single call | Turn-by-turn conversation history |

TAC Memory is fetched **once on turn 1** and passed to the AgentCore Runtime agent as enriched context (greeting + observations + summaries). The Python agent saves this full context into AgentCore STM alongside the first utterance. On turns 2+, Node.js skips the TAC Memory API — the agent loads the context from STM history directly.

Set `BEDROCK_AGENT_MODE` to choose the invocation mode: `agentcore` (Python AgentCore Runtime, default), `inline` (InvokeInlineAgentCommand), or `persistent` (pre-created Bedrock Agent).

---

## Project Structure

```
healthcare-outreach-node/
├── src/
│   ├── index.ts                         # Entry point — starts TACServer + app server
│   │
│   ├── tac/
│   │   └── server.ts                    # Reusable TACServer wrapper + AgentCallbacks interface
│   │
│   ├── apps/
│   │   └── healthcare/                  # All healthcare app files co-located
│   │       ├── client/                  # Browser dashboard
│   │       │   ├── members.html
│   │       │   └── owl-health-logo-*.svg
│   │       ├── server/                  # Node.js app logic
│   │       │   ├── api.ts               # Fastify routes — /api/*, /ci-webhook, dashboard
│   │       │   ├── callbacks.ts         # AgentCallbacks impl — memory cache, invokeAgent
│   │       │   └── state.ts             # Shared in-memory Maps (caches, conversation routing)
│   │       └── agent/                   # Python AgentCore Runtime agent
│   │           ├── src/main.py
│   │           └── .bedrock_agentcore.yaml
│   │
│   ├── agent.ts                         # invokeAgent router (inline / persistent / agentcore)
│   ├── agent-inline.ts                  # InvokeInlineAgentCommand implementation
│   ├── agent-persistent.ts              # InvokeAgentCommand implementation
│   ├── agent-agentcore-runtime.ts       # AgentCore Runtime (Python agent) implementation
│   ├── prompts.ts                       # System prompt + greeting builders
│   └── types.ts                         # Shared TypeScript interfaces
│
├── scripts/
│   └── add-outbound-capture-rule.ts     # Configure Conversation Orchestrator for outbound calls
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
- An active Twilio account with:
  - Conversation Memory store
  - Conversation Orchestrator configuration (see [Orchestrator Setup](#conversation-orchestrator-setup) below)
  - Conversation Intelligence configuration with a summary operator
  - A Twilio phone number
- AWS account with Bedrock access (Claude model enabled in your region)
- [ngrok](https://ngrok.com) (for local development — exposes `/twiml` and `/ws` to Twilio)

---

## Environment Variables

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

| Variable | Description |
|---|---|
| `TWILIO_ACCOUNT_SID` | Twilio Account SID (`ACxxx`) |
| `TWILIO_AUTH_TOKEN` | Twilio Auth Token |
| `TWILIO_API_KEY` | Twilio API Key (`SKxxx`) |
| `TWILIO_API_TOKEN` | Twilio API Key Secret |
| `TWILIO_PHONE_NUMBER` | Twilio phone number for calls/SMS |
| `VOICE_PUBLIC_DOMAIN` | Full ngrok URL with `https://` (e.g. `https://abc.ngrok.io`) |
| `CONVERSATION_SERVICE_ID` | Conversation Configuration ID (`conv_configuration_*`) |
| `OUTBOUND_CALL_TO` | Override number for outbound calls (testing) |
| `MEMORY_STORE_ID` | Conversation Memory Store ID |
| `TWILIO_TAC_CI_SUMMARY_OPERATOR_SID` | Conversation Intelligence summary operator SID |
| `TAC_PORT` | Port for TACServer — Twilio webhooks (default: `8000`) |
| `APP_PORT` | Port for app server — dashboard + `/api/*` (default: `8001`) |
| `AWS_REGION` | AWS region (e.g. `us-east-1`) |
| `BEDROCK_MODEL_ID` | Bedrock model ID |
| `AWS_ACCESS_KEY_ID` | AWS access key (not needed when using an IAM role) |
| `AWS_SECRET_ACCESS_KEY` | AWS secret key (not needed when using an IAM role) |

---

## Running Locally

Two servers start together with a single command — **TACServer** (Twilio webhooks, port 8000) and the **app server** (dashboard + APIs, port 8001).

### 1. Expose the TAC server with ngrok

Twilio needs a public HTTPS URL to reach your local machine for TwiML and the ConversationRelay WebSocket. In a dedicated terminal:

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

### 2. Start the servers

```bash
npm install
npm run dev
```

Both servers start in a single process:

| Server | Port | Purpose |
|---|---|---|
| TACServer | 8000 | Twilio webhooks — `/twiml`, `/ws`, `/conversation-relay-callback`, `/cintel` |
| App server | 8001 | Dashboard, `/api/members`, `/api/outbound-call`, `/api/send-sms`, `/ci-webhook` |

Open the care team dashboard at:
```
http://localhost:8001/members.html
```

### 3. Production build

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

On EC2 with an IAM role attached, omit `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` — credentials are picked up automatically from the instance metadata.

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
CONFIG=$(curl -s "https://conversations.twilio.com/v2/ControlPlane/Configurations/${TWILIO_TAC_CONVERSATION_CONFIGURATION_ID}" \
  -u "${TWILIO_TAC_API_KEY}:${TWILIO_TAC_API_TOKEN}") && \
curl -s -X PUT "https://conversations.twilio.com/v2/ControlPlane/Configurations/${TWILIO_TAC_CONVERSATION_CONFIGURATION_ID}" \
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

---

## API Endpoints

**TACServer (port 8000) — Twilio-facing, managed by TAC SDK**

| Method | Path | Description |
|---|---|---|
| `POST` | `/twiml` | TwiML for inbound calls (personalized greeting) |
| `POST` | `/twiml-outbound` | TwiML for outbound calls |
| `WS` | `/ws` | ConversationRelay audio stream |
| `POST` | `/conversation-relay-callback` | Call status / call-end webhook |
| `POST` | `/cintel` | Conversation Intelligence operator results |

**App server (port 8001) — dashboard and application APIs**

| Method | Path | Description |
|---|---|---|
| `GET` | `/members.html` | Care team dashboard (static) |
| `GET` | `/api/members` | All member profiles from Memory Store |
| `POST` | `/api/outbound-call` | Initiate outbound voice call |
| `POST` | `/api/send-sms` | Send outreach SMS |
| `POST` | `/ci-webhook` | CI summary → write to member profile |

---

## AgentCore Runtime Agent (Strands + CLI)

The project includes a Python agent under `src/apps/healthcare/agent/` built with [Strands Agents](https://github.com/strands-agents/sdk-python) and deployed via the AgentCore CLI. This is an alternative to the inline and persistent Bedrock Agents modes — the agent logic runs as a managed AWS-hosted runtime instead of being driven entirely by the Node.js SDK.

### Prerequisites

```bash
pip3 install bedrock-agentcore-starter-toolkit
```

This installs the `agentcore` CLI.

### Project structure

```
src/apps/healthcare/agent/
├── src/
│   ├── main.py              # Agent entrypoint — Owl Health system prompt + payload handling
│   ├── model/load.py        # Bedrock model config (us.anthropic.claude-opus-4-6-v1)
│   └── mcp_client/client.py # MCP client stub (unused, generated by agentcore create)
├── .bedrock_agentcore.yaml  # CLI config — agent name, runtime, deployment type
├── pyproject.toml           # Python dependencies
└── .venv/                   # Virtual environment (created by agentcore create)
```

### Local development

```bash
cd src/apps/healthcare/agent
source .venv/bin/activate

# Start local dev server (default port 8080; use a free port if taken)
AWS_PROFILE=<your-profile> agentcore dev

# In a second terminal — invoke with a test payload
AWS_PROFILE=<your-profile> agentcore invoke --dev --port 8080 '{"prompt": "Hi, how are you?"}'

# Pass TAC memory context the same way the Node app does
AWS_PROFILE=<your-profile> agentcore invoke --dev --port 8080 \
  '{"prompt": "I want to reschedule", "context": "## Key Observations\n- Member prefers morning calls"}'
```

### Payload shape

The agent expects:

| Field | Type | Description |
|---|---|---|
| `prompt` | string | The user utterance (voice transcript or SMS text) |
| `context` | string | TAC Conversation Memory block (optional — omit or pass `""` if none) |

### Deploy to AWS

```bash
cd src/apps/healthcare/agent
AWS_PROFILE=<your-profile> agentcore deploy
```

The CLI builds and deploys the Python code directly to AgentCore Runtime (no Docker required). On first deploy with `mode: STM_ONLY` in `.bedrock_agentcore.yaml`, it automatically provisions an AgentCore Memory resource and writes the `memory_id` back into the yaml.

After deploy:

```bash
# Check status — confirms memory_id and agent ARN
AWS_PROFILE=<your-profile> agentcore status

# Invoke the deployed agent
AWS_PROFILE=<your-profile> agentcore invoke '{"prompt": "Hi, how are you?"}'
```

### Post-deploy: grant memory permissions to the execution role

The execution role created by the CLI does **not** have memory access by default. Run this script once after every fresh deploy into a new environment:

```bash
npx ts-node scripts/grant-agentcore-memory-permissions.ts
```

This script reads `AGENTCORE_EXECUTION_ROLE_NAME` and `AGENTCORE_MEMORY_ARN` from `.env` and attaches an inline IAM policy granting `ListEvents`, `CreateEvent`, and related memory actions. Safe to re-run (idempotent).

Without this step, STM (short-term memory) will silently fail with `AccessDeniedException` on `ListEvents` — the agent will still respond but will have no conversation history between turns.

Set these in `.env` after deploy (values from `agentcore status`):
```
AGENTCORE_EXECUTION_ROLE_NAME=AmazonBedrockAgentCoreSDKRuntime-<region>-<suffix>
AGENTCORE_MEMORY_ARN=arn:aws:bedrock-agentcore:<region>:<account>:memory/<memory-id>
```

### Connecting to the Node.js app

Set in `.env` then restart `npm run dev`:
```
BEDROCK_AGENT_MODE=agentcore
AGENTCORE_AGENT_ARN=<from agentcore status>
```

### Tear down

```bash
AWS_PROFILE=<your-profile> agentcore destroy
```

---

## Key Differences from Python Version

| Aspect | Python | Node.js |
|---|---|---|
| Framework | FastAPI + Uvicorn | Express.js + `express-ws` |
| LLM | LangChain `ChatBedrock` | AWS AgentCore `InvokeInlineAgentCommand` |
| Session history | `conversationHistory` dict (managed in app) | AgentCore `sessionId` (managed by AWS) |
| Memory injection | `with_tac_memory()` adapter | Injected into AgentCore `instruction` field |
| Twilio APIs | TAC Python SDK | Direct `axios` calls |
| Observability | Manual logging | AgentCore CloudWatch tracing |
