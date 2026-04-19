/**
 * AWS Bedrock persistent agent integration.
 * Used when BEDROCK_AGENT_MODE=persistent.
 *
 * Key differences from inline mode:
 *   - Agent is pre-created in AWS with a fixed instruction/model — no foundationModel or instruction fields here.
 *   - Requires BEDROCK_AGENT_ID + BEDROCK_AGENT_ALIAS_ID env vars.
 *   - TAC Conversation Memory is prepended to inputText (can't inject into instruction at runtime).
 *   - sessionId still enables multi-turn history managed by AgentCore.
 */

import {
  BedrockAgentRuntimeClient,
  InvokeAgentCommand,
} from '@aws-sdk/client-bedrock-agent-runtime';

const agentClient = new BedrockAgentRuntimeClient({
  region: process.env.AWS_REGION ?? 'us-east-1',
});

export async function invokeAgent(
  sessionId: string,
  userMessage: string,
  systemPrompt: string,
  memoryContext: string | null,
): Promise<string> {
  const agentId      = process.env.BEDROCK_AGENT_ID ?? '';
  const agentAliasId = process.env.BEDROCK_AGENT_ALIAS_ID ?? '';

  // systemPrompt is fixed in the AWS agent definition — log so callers know it's ignored here.
  console.log(`[agent-persistent] systemPrompt ignored (fixed in agent definition), length=${systemPrompt.length}`);

  // Prepend memory context to the user message since there's no runtime instruction field.
  const inputText = memoryContext
    ? `[Context]\n${memoryContext}\n\n[User]\n${userMessage}`
    : userMessage;

  console.log(`[agent-persistent] invoking agentId=${agentId} aliasId=${agentAliasId} sessionId=${sessionId} inputText="${inputText.slice(0, 80)}"`);

  const command = new InvokeAgentCommand({
    agentId,
    agentAliasId,
    sessionId,
    inputText,
    enableTrace: true,
  });

  const response = await agentClient.send(command);
  console.log(`[agent-persistent] response received, streaming chunks…`);

  let fullResponse = '';
  let chunkCount = 0;
  for await (const event of response.completion ?? []) {
    console.log(`[agent-persistent] event keys:`, Object.keys(event));
    if (event.chunk?.bytes) {
      const text = new TextDecoder().decode(event.chunk.bytes);
      console.log(`[agent-persistent] chunk text: "${text.slice(0, 80)}"`);
      fullResponse += text;
      chunkCount++;
    } else if (event.trace) {
      console.log(`[agent-persistent] trace event:`, JSON.stringify(event.trace).slice(0, 200));
    } else {
      console.log(`[agent-persistent] unhandled event:`, JSON.stringify(event).slice(0, 300));
    }
  }
  console.log(`[agent-persistent] done — ${chunkCount} chunks, ${fullResponse.length} chars total`);

  return fullResponse.trim();
}
