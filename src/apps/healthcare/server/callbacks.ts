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
import { TACMemoryResponse } from '../../../types';

// Prefetch cache: callSid → Promise<TACMemoryResponse | null>
// Started in onConversationSetup so memory is ready before the first utterance arrives.
const memoryPrefetchCache = new Map<string, Promise<TACMemoryResponse | null>>();

async function prefetchMemory(phone: string): Promise<TACMemoryResponse | null> {
  const t0 = Date.now();
  const profileId = await lookupProfileId(phone);
  const memory = profileId ? await retrieveMemory(profileId) : null;
  console.log(`[healthcare] prefetch complete phone=${phone} profileId=${profileId ?? 'none'} obs=${memory?.observations.length ?? 0} summaries=${memory?.summaries.length ?? 0} in ${Date.now() - t0}ms`);
  return memory;
}

export function createHealthcareCallbacks(): AgentCallbacks {
  return {
    // onConversationSetup fires on the WS 'setup' message — before any prompt.
    // For outbound calls: from=Twilio number, to=member phone.
    // We store the outbound context keyed by callSid, and map from→callSid so
    // onMessageReady (which only sees session.authorInfo.address = from) can look it up.
    // We also kick off a TAC Memory prefetch immediately so it's ready by turn 1.
    onConversationSetup({ callSid, from }) {
      const ctx = lastOutboundContext;
      setLastOutboundContext(null);
      if (ctx) {
        pendingOutboundContext.set(callSid, ctx);
        outboundCallerToCallSid.set(from, callSid);
        console.log(`[healthcare] outbound context stashed callSid=${callSid} from=${from} member=${ctx.name}`);
        // Prefetch TAC memory using the real member phone (ctx.phone = to number)
        memoryPrefetchCache.set(callSid, prefetchMemory(ctx.phone));
        console.log(`[healthcare] prefetch started for ${ctx.phone}`);
      } else {
        // Inbound: prefetch using the caller's number (from = member phone for inbound)
        memoryPrefetchCache.set(callSid, prefetchMemory(from));
        console.log(`[healthcare] prefetch started for inbound from=${from}`);
      }
    },

    async onMessageReady({ conversationId, message, session }) {
      const t0 = Date.now();

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
          // Move the prefetch promise to be keyed by conversationId
          const prefetch = memoryPrefetchCache.get(callSid);
          if (prefetch) {
            memoryPrefetchCache.set(conversationId, prefetch);
            memoryPrefetchCache.delete(callSid);
          }
          console.log(`[healthcare] outbound context applied for ${ctx.name} convId=${conversationId}`);
        } else {
          // Inbound: member phone is the caller's number
          memberPhoneCache.set(conversationId, callerAddress);
          systemPromptCache.set(conversationId, buildInboundSystemPrompt(null));
          // Move the prefetch promise (was keyed by callSid — not available here, look up by address)
          // For inbound the prefetch was stored under the callSid; find and re-key it
          for (const [key, promise] of memoryPrefetchCache) {
            if (key !== conversationId) {
              memoryPrefetchCache.set(conversationId, promise);
              memoryPrefetchCache.delete(key);
              break;
            }
          }
          console.log(`[healthcare] inbound session convId=${conversationId} from=${callerAddress}`);
        }
      }

      // Turn 1: await prefetched TAC Memory; Turn 2+: STM carries it
      let enrichedContext: string;
      if (memoryContextCache.has(conversationId)) {
        enrichedContext = '';
        console.log(`[healthcare] memory cache hit — skipping TAC fetch convId=${conversationId}`);
      } else {
        const prefetch = memoryPrefetchCache.get(conversationId);
        const memory = prefetch ? await prefetch : null;
        memoryPrefetchCache.delete(conversationId);
        console.log(`[healthcare] memory ready in ${Date.now() - t0}ms from turn start obs=${memory?.observations.length ?? 0} summaries=${memory?.summaries.length ?? 0}`);

        const memCtx = buildMemoryContext(memory) ?? '';
        const greeting = greetingCache.get(conversationId) ?? null;
        enrichedContext = (greeting && process.env.BEDROCK_AGENT_MODE === 'agentcore')
          ? `[Greeting already spoken to member]\n${greeting}${memCtx ? '\n\n' + memCtx : ''}`
          : memCtx;
        memoryContextCache.set(conversationId, enrichedContext);
        console.log(`[healthcare] ── enrichedContext (turn 1) ──\n${enrichedContext || '(empty)'}\n── end enrichedContext ──`);
      }

      // Send system_prompt only on turn 1 (when enrichedContext is non-empty).
      // Python saves it to STM under actor_id="system" and reloads it on turn 2+.
      const isTurn1 = enrichedContext !== '';
      const systemPrompt = isTurn1 ? (systemPromptCache.get(conversationId) ?? '') : '';
      if (isTurn1) console.log(`[healthcare] ── systemPrompt (turn 1) ──\n${systemPrompt}\n── end systemPrompt ──`);

      // Use profileId as AgentCore session key when available — reuses warm microVM for the same
      // member across calls (within 15-min idle timeout). Falls back to conversationId.
      const sessionId = session.profileId ?? conversationId;
      console.log(`[healthcare] invoking agent sessionId=${sessionId} convId=${conversationId} turn1=${isTurn1} message="${message.slice(0, 60)}"`);
      return invokeAgent(sessionId, message, systemPrompt, enrichedContext);
    },

    onConversationEnded({ conversationId }) {
      systemPromptCache.delete(conversationId);
      greetingCache.delete(conversationId);
      memoryContextCache.delete(conversationId);
      memberPhoneCache.delete(conversationId);
      memoryPrefetchCache.delete(conversationId);
      console.log(`[healthcare] cleaned up caches for convId=${conversationId}`);
    },
  };
}
