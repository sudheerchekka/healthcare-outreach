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
export function buildGreeting(name: string, goal: string, goalDesc: string): string {
  return (
    `Hi ${name}, this is the Owl Health Care Team calling. ` +
    `I'm reaching out regarding ${goalDesc}. ` +
    `Do you have a moment to chat?`
  );
}

/** System prompt for an outbound call — gives the agent purpose and member context. */
export function buildSystemPrompt(name: string, goal: string, goalDesc: string): string {
  return (
    `You are an Owl Health care coordination agent making an outbound call to ${name}. ` +
    `Purpose of this call: ${goal} — ${goalDesc}. ` +
    `Be friendly, professional, and concise. ` +
    `The member has already been greeted — do not re-introduce yourself. ` +
    `Address the reason for the call directly and guide the conversation toward a clear next step.`
  );
}

/** System prompt for an inbound call — personalised from profile traits. */
export function buildInboundSystemPrompt(profile: MemberProfile | null): string {
  let name: string | null = null;
  let nextFollowUp: string | null = null;

  if (profile?.traits) {
    const contact = profile.traits.Contact;
    const outreach = profile.traits.outreach;
    if (contact?.firstName) {
      name = [contact.firstName, contact.lastName].filter(Boolean).join(' ');
    }
    nextFollowUp = outreach?.nextFollowUp ?? null;
  }

  let prompt =
    'You are an Owl Health care coordination agent handling an inbound call. ' +
    'Be warm, professional, and concise. ' +
    'You have been provided with the customer\'s profile and a summary of past conversations above — ' +
    'use this context to personalize your responses and avoid asking for information you already have.';

  if (name) prompt += ` The member's name is ${name}.`;
  if (nextFollowUp) prompt += ` Their next scheduled follow-up topic is: ${nextFollowUp}.`;

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
