import { invokeAgent } from '../../../agent';
import {
  buildSystemPrompt,
  buildInboundSystemPrompt,
  buildMemoryContext,
} from '../../../prompts';
import { AgentCallbacks } from '../../../tac/server';
import { lookupProfileId, retrieveMemory } from './memory';
import {
  systemPromptCache,
  greetingCache,
  memoryContextCache,
  memberPhoneCache,
  outboundConversationMap,
  pendingOutboundContext,
  outboundCallerToCallSid,
  lastOutboundContext,
  setLastOutboundContext,
} from './state';

export function createHealthcareCallbacks(): AgentCallbacks {
  return {
    // onConversationSetup fires on the WS 'setup' message — before any prompt.
    // For outbound calls: from=Twilio number, to=member phone.
    // We store the outbound context keyed by callSid, and map from→callSid so
    // onMessageReady (which only sees session.authorInfo.address = from) can look it up.
    onConversationSetup({ callSid, from }) {
      const ctx = lastOutboundContext;
      setLastOutboundContext(null);
      if (ctx) {
        pendingOutboundContext.set(callSid, ctx);
        outboundCallerToCallSid.set(from, callSid);
        console.log(`[healthcare] outbound context stashed callSid=${callSid} from=${from} member=${ctx.name}`);
      }
    },

    async onMessageReady({ conversationId, message, session }) {
      // First turn: resolve context and member phone
      if (!systemPromptCache.has(conversationId)) {
        const callerAddress = session.authorInfo?.address ?? '';

        // For outbound: callerAddress = Twilio's from-number; look up by the from→callSid map
        const callSid = outboundCallerToCallSid.get(callerAddress);
        const ctx = callSid ? (pendingOutboundContext.get(callSid) ?? null) : null;

        if (ctx && callSid) {
          systemPromptCache.set(conversationId, buildSystemPrompt(ctx.name, ctx.goal, ctx.goalDesc));
          greetingCache.set(conversationId, ctx.greeting);
          outboundConversationMap.set(conversationId, ctx.phone);
          memberPhoneCache.set(conversationId, ctx.phone);
          pendingOutboundContext.delete(callSid);
          outboundCallerToCallSid.delete(callerAddress);
          console.log(`[healthcare] outbound context applied for ${ctx.name} convId=${conversationId}`);
        } else {
          // Inbound: member phone is the caller's number
          memberPhoneCache.set(conversationId, callerAddress);
          systemPromptCache.set(conversationId, buildInboundSystemPrompt(null));
          console.log(`[healthcare] inbound session convId=${conversationId} from=${callerAddress}`);
        }
      }

      // Turn 1: fetch TAC Memory and build enriched context; Turn 2+: STM carries it
      let enrichedContext: string;
      if (memoryContextCache.has(conversationId)) {
        enrichedContext = '';
        console.log(`[healthcare] memory cache hit — skipping TAC fetch convId=${conversationId}`);
      } else {
        const memberPhone = memberPhoneCache.get(conversationId) ?? '';
        const profileId = memberPhone ? await lookupProfileId(memberPhone) : null;
        const memory = profileId ? await retrieveMemory(profileId) : null;
        console.log(`[healthcare] memory for ${memberPhone}: profileId=${profileId ?? 'none'} obs=${memory?.observations.length ?? 0} summaries=${memory?.summaries.length ?? 0}`);

        const memCtx = buildMemoryContext(memory) ?? '';
        const greeting = greetingCache.get(conversationId) ?? null;
        enrichedContext = (greeting && process.env.BEDROCK_AGENT_MODE === 'agentcore')
          ? `[Greeting already spoken to member]\n${greeting}${memCtx ? '\n\n' + memCtx : ''}`
          : memCtx;
        memoryContextCache.set(conversationId, enrichedContext);
        console.log(`[healthcare] memory fetched and cached for convId=${conversationId}`);
        console.log(`[healthcare] ── enrichedContext (turn 1) ──\n${enrichedContext || '(empty)'}\n── end enrichedContext ──`);
      }

      // Send system_prompt only on turn 1 (when enrichedContext is non-empty).
      // Python saves it to STM under actor_id="system" and reloads it on turn 2+.
      const isTurn1 = enrichedContext !== '';
      const systemPrompt = isTurn1 ? (systemPromptCache.get(conversationId) ?? '') : '';
      if (isTurn1) console.log(`[healthcare] ── systemPrompt (turn 1) ──\n${systemPrompt}\n── end systemPrompt ──`);
      console.log(`[healthcare] invoking agent convId=${conversationId} turn1=${isTurn1} message="${message.slice(0, 60)}"`);
      return invokeAgent(conversationId, message, systemPrompt, enrichedContext);
    },

    onConversationEnded({ conversationId }) {
      systemPromptCache.delete(conversationId);
      greetingCache.delete(conversationId);
      memoryContextCache.delete(conversationId);
      memberPhoneCache.delete(conversationId);
      console.log(`[healthcare] cleaned up caches for convId=${conversationId}`);
    },
  };
}
