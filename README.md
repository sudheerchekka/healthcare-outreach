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

### How it works

```
Browser Dashboard
  │  GET /api/members          → load profiles from Twilio Conversation Memory
  │  POST /api/outbound-call   → place call via Twilio Voice API
  │  POST /api/send-sms        → send SMS via Twilio SMS API
  │
  ▼
Express Server (app.ts)
  │
  ├── POST /twiml              → returns TwiML with personalized greeting + ConversationRelay
  ├── WS   /ws                 → ConversationRelay audio stream
  │     │
  │     ├── setup   → capture callSid + fromNumber
  │     ├── prompt  → retrieve TAC Memory → invoke AgentCore → send reply
  │     └── interrupt → acknowledge
  │
  ├── POST /ci-webhook         → Conversation Intelligence post-call summary
  │     └── update lastCallSummary on mock member profile
  │
  └── POST /conversation-relay-callback → close conversation on call end
```

### AWS AgentCore vs direct Bedrock

This project uses `InvokeInlineAgentCommand` from `@aws-sdk/client-bedrock-agent-runtime` instead of calling Bedrock directly:

| Direct `ConverseCommand` | AgentCore `InvokeInlineAgentCommand` |
|---|---|
| Pass full message history every turn | Pass `sessionId` — AgentCore maintains history |
| Manage `conversationHistory` Map in app | No history map needed in app code |
| No built-in tool support | Define action groups / tools inline |
| No observability | CloudWatch tracing via `enableTrace: true` |

Twilio Conversation Memory is injected into AgentCore's `instruction` field each turn — same effect as the Python `with_tac_memory` adapter.

---

## Project Structure

```
healthcare-outreach-node/
├── src/
│   ├── app.ts               # Express server — all endpoints + ConversationRelay WebSocket
│   ├── agent.ts             # AWS AgentCore InvokeInlineAgent integration
│   ├── prompts.ts           # System prompt + greeting builders
│   └── types.ts             # Shared TypeScript interfaces
│
├── scripts/
│   └── add-outbound-capture-rule.ts  # Configure Conversation Orchestrator for outbound calls
│
├── healthcare_outreach/     # Static browser dashboard (served by Express)
│   ├── members.html         # Care team portal UI
│   └── owl-health-logo-*.svg
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
| `TWILIO_TAC_ACCOUNT_SID` | Twilio Account SID (`ACxxx`) |
| `TWILIO_TAC_AUTH_TOKEN` | Twilio Auth Token |
| `TWILIO_TAC_API_KEY` | Twilio API Key (`SKxxx`) |
| `TWILIO_TAC_API_TOKEN` | Twilio API Key Secret |
| `TWILIO_TAC_PHONE_NUMBER` | Twilio phone number for calls/SMS |
| `TWILIO_TAC_VOICE_PUBLIC_DOMAIN` | ngrok domain without `https://` (e.g. `abc.ngrok.io`) |
| `TWILIO_TAC_CONVERSATION_CONFIGURATION_ID` | Conversation Configuration ID (`conv_configuration_*`) |
| `TWILIO_TAC_OUTBOUND_CALL_TO` | Override number for outbound calls (testing) |
| `TWILIO_TAC_MEMORY_STORE_ID` | Conversation Memory Store ID |
| `TWILIO_TAC_CI_SUMMARY_OPERATOR_SID` | Conversation Intelligence summary operator SID |
| `AWS_REGION` | AWS region (e.g. `us-east-1`) |
| `BEDROCK_MODEL_ID` | Bedrock model ID (default: `anthropic.claude-opus-4-6-v1:0`) |
| `AWS_ACCESS_KEY_ID` | AWS access key (not needed when using an IAM role) |
| `AWS_SECRET_ACCESS_KEY` | AWS secret key (not needed when using an IAM role) |

---

## Build & Run

### Development (hot reload)

```bash
npm install
npm run dev
```

The server starts on `http://localhost:8000`. Open the dashboard at:
```
http://localhost:8000/members.html
```

### Production build

```bash
npm run build        # compiles TypeScript → dist/
npm start            # runs node dist/app.js
```

### Docker

```bash
# Build image
npm run build
docker build -t healthcare-outreach-node .

# Run container
docker run -p 8000:8000 \
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

## Local Development with ngrok

Twilio needs a public HTTPS URL to reach your local server for TwiML and the ConversationRelay WebSocket.

```bash
# In a separate terminal
ngrok http 8000
```

Copy the forwarding domain (e.g. `abc123.ngrok.io`) and set it in `.env`:

```
TWILIO_TAC_VOICE_PUBLIC_DOMAIN=abc123.ngrok.io
```

Configure your Twilio phone number's voice webhook to:
```
https://abc123.ngrok.io/twiml
```

---

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/members.html` | Care team dashboard (static) |
| `GET` | `/api/members` | All member profiles from Conversation Memory |
| `POST` | `/api/outbound-call` | Initiate outbound voice call |
| `POST` | `/api/send-sms` | Send outreach SMS |
| `GET` | `/api/latest-summary` | Most recent call summary for a phone number |
| `POST` | `/twiml` | TwiML for inbound calls (personalized greeting) |
| `POST` | `/twiml-outbound` | TwiML for outbound calls |
| `WS` | `/ws` | ConversationRelay audio stream |
| `POST` | `/conversation-relay-callback` | Call status webhook |
| `POST` | `/ci-webhook` | Conversation Intelligence summary webhook |

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
