# Owl Health Gemini Agent

ADK agent deployed to Google Vertex AI Agent Engine.

## Setup

```bash
cd src/apps/healthcare/agent-gemini
source .venv/bin/activate
export SSL_CERT_FILE=$(python3 -c "import certifi; print(certifi.where())")
```

> Run the `export SSL_CERT_FILE` line every time you open a new terminal session before deploying.

## New Deployment

Creates a new Agent Engine resource.

```bash
adk deploy agent_engine \
  --project=dotorg-se-project \
  --region=us-central1 \
  --display_name="Owl Health Gemini Agent" \
  --requirements_file=requirements.txt \
  src
```

On success, note the resource name printed:
```
projects/626716753065/locations/us-central1/reasoningEngines/<RESOURCE_ID>
```

Update `--agent_engine_id` in the redeploy command below with the new resource ID.

## Redeploy / Update Existing Agent

Updates the deployed agent in-place — keeps the same resource name.

```bash
adk deploy agent_engine \
  --project=dotorg-se-project \
  --region=us-central1 \
  --display_name="Owl Health Gemini Agent" \
  --requirements_file=requirements.txt \
  --agent_engine_id=projects/626716753065/locations/us-central1/reasoningEngines/3750755769495060480 \
  src
```

## Check Logs

```bash
gcloud logging read \
  "resource.type=\"aiplatform.googleapis.com/ReasoningEngine\" AND resource.labels.reasoning_engine_id=\"3750755769495060480\"" \
  --project=dotorg-se-project \
  --limit=30 \
  --order=desc \
  --format="table(timestamp,severity,textPayload)"
```

## Agent Playground

https://console.cloud.google.com/vertex-ai/agents/agent-engines/locations/us-central1/agent-engines/3750755769495060480/playground?project=626716753065
