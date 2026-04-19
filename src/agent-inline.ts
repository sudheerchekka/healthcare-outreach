/**
 * AWS AgentCore InlineAgent integration.
 * Used when BEDROCK_AGENT_MODE=inline (default).
 *
 * Key benefit over direct ConverseCommand:
 *   - sessionId lets AgentCore maintain multi-turn conversation history server-side.
 *   - No conversationHistory Map needed in app code.
 *   - TAC Conversation Memory is injected into the `instruction` field each turn.
 *   - enableTrace provides CloudWatch observability out of the box.
 */

import {
  BedrockAgentRuntimeClient,
  InvokeInlineAgentCommand,
} from '@aws-sdk/client-bedrock-agent-runtime';

const agentClient = new BedrockAgentRuntimeClient({
  region: process.env.AWS_REGION ?? 'us-east-1',
});

const MODEL_ID =
  process.env.BEDROCK_MODEL_ID ?? 'anthropic.claude-opus-4-6-v1:0';

export async function invokeAgent(
  sessionId: string,
  userMessage: string,
  systemPrompt: string,
  memoryContext: string | null,
): Promise<string> {
  // Combine TAC memory + system prompt into AgentCore's instruction field.
  // This mirrors Python's MemoryPromptBuilder prepending a SystemMessage at index 0.
  const base = memoryContext ? `${memoryContext}\n\n${systemPrompt}` : systemPrompt;
  // AgentCore requires instruction to be at least 40 characters.
  const instruction = base.length >= 40 ? base : base.padEnd(40, ' ');

  console.log(`[agent-inline] invoking model=${MODEL_ID} sessionId=${sessionId} inputText="${userMessage.slice(0, 80)}"`);
  console.log(`[agent-inline] instruction (first 120 chars): ${instruction.slice(0, 120)}`);

  const command = new InvokeInlineAgentCommand({
    foundationModel: MODEL_ID,
    instruction,
    sessionId,
    inputText: userMessage,
    enableTrace: true,
  });

  const response = await agentClient.send(command);
  console.log(`[agent-inline] response received, streaming chunks…`);

  let fullResponse = '';
  let chunkCount = 0;
  for await (const event of response.completion ?? []) {
    console.log(`[agent-inline] event keys:`, Object.keys(event));
    if (event.chunk?.bytes) {
      const text = new TextDecoder().decode(event.chunk.bytes);
      console.log(`[agent-inline] chunk text: "${text.slice(0, 80)}"`);
      fullResponse += text;
      chunkCount++;
    } else if (event.trace) {
      console.log(`[agent-inline] trace event:`, JSON.stringify(event.trace).slice(0, 200));
    } else {
      console.log(`[agent-inline] unhandled event:`, JSON.stringify(event).slice(0, 300));
    }
  }
  console.log(`[agent-inline] done — ${chunkCount} chunks, ${fullResponse.length} chars total`);

  return fullResponse.trim();
}
