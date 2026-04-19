/**
 * AWS Bedrock AgentCore Runtime integration.
 * Used when BEDROCK_AGENT_MODE=agentcore.
 *
 * The agent is a Python process deployed via `agentcore deploy` (Strands + BedrockAgentCoreApp).
 * Uses @aws-sdk/client-bedrock-agentcore with InvokeAgentRuntimeCommand.
 *
 * Required env vars:
 *   AGENTCORE_AGENT_ARN  - Agent Runtime ARN (from `agentcore status`)
 *                          e.g. arn:aws:bedrock-agentcore:us-east-1:123456789:runtime/outreachAgent_Agent-XXXXXXXX
 *
 * Payload sent to the agent (matches main.py's expected shape):
 *   { "prompt": "<user message>", "context": "<TAC memory block or empty string>" }
 */

import {
  BedrockAgentCoreClient,
  InvokeAgentRuntimeCommand,
} from '@aws-sdk/client-bedrock-agentcore';

const REGION = process.env.AWS_REGION ?? 'us-east-1';

const client = new BedrockAgentCoreClient({ region: REGION });

export async function invokeAgent(
  sessionId: string,
  userMessage: string,
  systemPrompt: string,
  memoryContext: string | null,
): Promise<string> {
  const agentRuntimeArn = process.env.AGENTCORE_AGENT_ARN ?? '';

  const payload = JSON.stringify({
    prompt: userMessage,
    context: memoryContext ?? '',
    system_prompt: systemPrompt,
  });

  console.log(`[agent-agentcore-runtime] invoking agentRuntimeArn=${agentRuntimeArn} sessionId=${sessionId} prompt="${userMessage.slice(0, 80)}"`);
  const t0 = Date.now();

  const command = new InvokeAgentRuntimeCommand({
    agentRuntimeArn,
    runtimeSessionId: sessionId,
    payload,
    contentType: 'application/json',
    qualifier: 'DEFAULT',
  });

  const response = await client.send(command);
  console.log(`[agent-agentcore-runtime] HTTP response complete in ${Date.now() - t0}ms`);

  const rawText = await response.response?.transformToString() ?? '';

  // Response may be plain text or SSE (data: "chunk" lines)
  let text: string;
  if (rawText.includes('\ndata:') || rawText.startsWith('data:')) {
    text = rawText
      .split('\n')
      .filter(line => line.startsWith('data:'))
      .map(line => {
        const value = line.slice('data:'.length).trim();
        try {
          const parsed = JSON.parse(value);
          if (parsed?.error) throw new Error(`AgentCore agent error: ${parsed.error}`);
          if (typeof parsed === 'string') return parsed;
          return parsed?.text ?? parsed?.output ?? parsed?.content ?? parsed?.delta ?? '';
        } catch (e) {
          if (e instanceof Error && e.message.startsWith('AgentCore agent error:')) throw e;
          return value;
        }
      })
      .join('');
  } else {
    text = rawText;
  }

  console.log(`[agent-agentcore-runtime] total latency=${Date.now() - t0}ms parsed response (${text.length} chars): "${text.slice(0, 80)}"`);
  return text.trim();
}
