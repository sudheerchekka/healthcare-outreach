/**
 * Prompt builders — ported from Python getting_started/examples/langchain/app.py.
 *
 * buildGreeting        → ConversationRelay welcome_greeting for outbound calls
 * buildSystemPrompt    → LLM system prompt for outbound calls
 * buildInboundSystemPrompt → LLM system prompt for inbound calls (uses profile traits)
 * buildMemoryContext   → formats TACMemoryResponse as a text block for AgentCore instruction
 */

import { MemberProfile, TACMemoryResponse } from './types';

/** Spoken greeting injected into TwiML for outbound calls. */
export function buildGreeting(name: string, goal: string, _goalDesc: string): string {
  const topic = goal || 'your care plan';
  return (
    `Hi ${name}, this is the Owl Health Care Team calling. ` +
    `I'm reaching out about ${topic}. ` +
    `Do you have a moment to chat?`
  );
}

/** System prompt for an outbound call — gives the agent purpose and member context. */
export function buildSystemPrompt(name: string, goal: string, goalDesc: string): string {
  const topic = goal || 'their care plan';
  const followUpDetail = goalDesc
    ? `Once they respond, use the following to guide your follow-up questions: ${goalDesc}`
    : '';
  return (
    `You are an Owl Health care coordination agent making an outbound call to ${name}. ` +
    `The member has already been greeted and told this call is about ${topic} — do not re-introduce yourself or repeat that. ` +
    `Wait for their response, then naturally guide the conversation using what you know. ` +
    `${followUpDetail} ` +
    `Be warm and conversational — speak in plain sentences, no bullet points, no bold text, no special formatting. ` +
    `Keep responses brief and easy to follow on a phone call. ` +
    `Guide the conversation toward a clear next step or action.`
  ).trim();
}

/** System prompt for an inbound call — personalised from profile traits. */
export function buildInboundSystemPrompt(profile: MemberProfile | null): string {
  let name: string | null = null;
  let nextFollowUp: string | null = null;
  let nextFollowUpReason: string | null = null;

  if (profile?.traits) {
    const contact = profile.traits.Contact;
    const outreach = profile.traits.outreach;
    if (contact?.firstName) {
      name = [contact.firstName, contact.lastName].filter(Boolean).join(' ');
    }
    nextFollowUp = outreach?.nextFollowUp ?? null;
    nextFollowUpReason = outreach?.nextFollowUpReason ?? null;
  }

  let prompt =
    'You are an Owl Health care coordination agent handling an inbound call. ' +
    'Be warm and conversational — speak in plain sentences, no bullet points, no bold text, no special formatting. ' +
    'Keep responses brief and easy to follow on a phone call. ' +
    'You have been provided with the member\'s profile and past call summaries — ' +
    'use this to personalize your responses and avoid asking for information you already have.';

  if (name) prompt += ` The member's name is ${name}.`;
  if (nextFollowUp) prompt += ` Their next scheduled follow-up is about: ${nextFollowUp}.`;
  if (nextFollowUpReason) prompt += ` Use this to guide your questions: ${nextFollowUpReason}.`;

  return prompt;
}

/**
 * Format TAC Conversation Memory (observations + summaries) as a text block.
 * This is injected into AgentCore's `instruction` field, mirroring Python's
 * MemoryPromptBuilder which prepends a SystemMessage with "# Customer Context".
 */
export function buildMemoryContext(memory: TACMemoryResponse | null): string | null {
  if (!memory) return null;

  const sections: string[] = [];

  if (memory.observations.length > 0) {
    sections.push(
      '## Key Observations\n' +
      'Important notes about the customer from previous interactions:\n' +
      memory.observations.map((o) => `- ${o}`).join('\n'),
    );
  }

  if (memory.summaries.length > 0) {
    sections.push(
      '## Previous Call Summaries\n' +
      memory.summaries.map((s) => `- ${s}`).join('\n'),
    );
  }

  if (sections.length === 0) return null;

  return (
    '# Customer Context\n' +
    'You have access to the following information about this customer from previous interactions:\n\n' +
    sections.join('\n\n')
  );
}
