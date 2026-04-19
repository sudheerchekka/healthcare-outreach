/**
 * Agent router — selects the LLM backend based on BEDROCK_AGENT_MODE (default: inline).
 *
 * Modes:
 *   inline           - InvokeInlineAgentCommand (no AWS resource needed)
 *   persistent       - InvokeAgentCommand against a pre-created Bedrock Agent
 *   agentcore        - HTTP call to a Python agent deployed via `agentcore deploy`
 *
 * Both modules are imported at startup (CommonJS static imports).
 * Env var validation lives here so it only fires for the selected mode.
 */

import { invokeAgent as invokeInline }          from './agent-inline';
import { invokeAgent as invokePersistent }       from './agent-persistent';
import { invokeAgent as invokeAgentcoreRuntime } from './agent-agentcore-runtime';

const mode = (process.env.BEDROCK_AGENT_MODE ?? 'inline').toLowerCase();

if (mode === 'persistent') {
  if (!process.env.BEDROCK_AGENT_ID || !process.env.BEDROCK_AGENT_ALIAS_ID) {
    console.error('[agent] BEDROCK_AGENT_MODE=persistent requires BEDROCK_AGENT_ID and BEDROCK_AGENT_ALIAS_ID');
    process.exit(1);
  }
}

if (mode === 'agentcore') {
  if (!process.env.AGENTCORE_AGENT_ARN) {
    console.error('[agent] BEDROCK_AGENT_MODE=agentcore requires AGENTCORE_AGENT_ARN');
    console.error('[agent] Get it from: cd src/apps/healthcare/agent && agentcore status');
    process.exit(1);
  }
}

console.log(`[agent] mode=${mode}`);

export const invokeAgent: typeof invokeInline =
  mode === 'persistent'  ? invokePersistent :
  mode === 'agentcore'   ? invokeAgentcoreRuntime :
  invokeInline;
