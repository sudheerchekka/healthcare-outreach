import { OutboundContext } from '../../../types';

export const systemPromptCache      = new Map<string, string>();  // convId → system prompt
export const greetingCache          = new Map<string, string>();  // convId → TwiML greeting
export const memoryContextCache     = new Map<string, string>();  // convId → enriched context (turn 1 only)
export const memberPhoneCache       = new Map<string, string>();  // convId → member phone (for memory fetch)
export const outboundConversationMap = new Map<string, string>(); // convId → member phone (for CI routing)
export const pendingOutboundContext   = new Map<string, OutboundContext>(); // callSid → ctx
export const outboundCallerToCallSid = new Map<string, string>();           // twilioFrom → callSid

export let lastOutboundContext: OutboundContext | null = null;
export function setLastOutboundContext(ctx: OutboundContext | null): void {
  lastOutboundContext = ctx;
}
