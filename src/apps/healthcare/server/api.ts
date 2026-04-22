import 'dotenv/config';
import Fastify from 'fastify';
import fastifyStatic from '@fastify/static';
import fastifyFormBody from '@fastify/formbody';
import axios from 'axios';
import { Twilio } from 'twilio';
import * as path from 'path';

import { buildGreeting } from '../../../prompts';
import { MemberRow, OutboundContext } from '../../../types';
import {
  normalizePhone,
  lookupProfileId,
  fetchProfile,
  updateProfileTraits,
  MEMORY_BASE,
  MEMORY_STORE_ID,
  memoryAuth,
} from './memory';

// ── Env ────────────────────────────────────────────────────────────────────
const ACCOUNT_SID      = process.env.TWILIO_TAC_ACCOUNT_SID ?? '';
const AUTH_TOKEN       = process.env.TWILIO_TAC_AUTH_TOKEN ?? '';
const PHONE_NUMBER     = process.env.TWILIO_TAC_PHONE_NUMBER ?? '';
const VOICE_DOMAIN     = (process.env.VOICE_PUBLIC_DOMAIN ?? 'NOT_SET').replace(/^https?:\/\//, '');
const OUTBOUND_CALL_TO = process.env.OUTBOUND_CALL_TO ?? '';
const CI_SUMMARY_OPERATOR_SID  = process.env.TWILIO_TAC_CI_SUMMARY_OPERATOR_SID ?? '';
const CI_OUTREACH_OPERATOR_SID = process.env.TWILIO_TAC_CI_OUTREACH_OPERATOR_SID ?? '';
const TAC_PORT         = parseInt(process.env.TAC_PORT ?? '8000', 10);
const AGENT_BACKEND    = process.env.AGENT_BACKEND ?? 'agentcore';
const EL_PORT          = parseInt(process.env.ELEVENLABS_PORT ?? '8002', 10);

const twilioClient = new Twilio(ACCOUNT_SID, AUTH_TOKEN);

// ── CI write debouncer ──────────────────────────────────────────────────────
// Both CI operators fire separate webhooks within milliseconds. Without debouncing,
// whichever writes second reads a stale profile (before the first write landed) and
// overwrites the first update. We buffer trait updates per profileId for 3s then flush once.
const ciPendingTraits = new Map<string, Record<string, unknown>>();
const ciFlushTimers   = new Map<string, ReturnType<typeof setTimeout>>();

function scheduleCIFlush(profileId: string): void {
  const existing = ciFlushTimers.get(profileId);
  if (existing) clearTimeout(existing);
  ciFlushTimers.set(profileId, setTimeout(async () => {
    const traits = ciPendingTraits.get(profileId);
    ciPendingTraits.delete(profileId);
    ciFlushTimers.delete(profileId);
    if (!traits) return;
    try {
      const profile = await fetchProfile(profileId);
      const existingOutreach = (profile?.traits?.outreach ?? {}) as Record<string, unknown>;
      await updateProfileTraits(profileId, 'outreach', { ...existingOutreach, ...traits });
      console.log(`[CI] flushed profileId=${profileId} traits=${Object.keys(traits).join(', ')}`);
    } catch (e) {
      console.error(`[CI] flush FAILED profileId=${profileId}:`, e);
    }
  }, 3000));
}

function formatTimestampPST(dateStr: string | undefined): string {
  const date = dateStr ? new Date(dateStr) : new Date();
  return date.toLocaleString('en-US', {
    timeZone: 'America/Los_Angeles',
    month: 'short', day: '2-digit', year: 'numeric',
    hour: '2-digit', minute: '2-digit', hour12: true,
  }).replace(',', '');
}

// ── App server ─────────────────────────────────────────────────────────────

export async function startHealthcareAppServer(): Promise<void> {
  const app = Fastify({ logger: false });
  await app.register(fastifyFormBody);
  await app.register(fastifyStatic, {
    root: path.join(__dirname, '../client'),
    prefix: '/',
  });

  // CORS
  app.addHook('onSend', async (_req, reply) => {
    reply.header('Access-Control-Allow-Origin', '*');
    reply.header('Access-Control-Allow-Methods', 'GET,POST,OPTIONS');
    reply.header('Access-Control-Allow-Headers', 'Content-Type');
  });

  // ── Dashboard: list members ──────────────────────────────────────────────
  app.get('/api/members', async (_req, reply) => {
    console.log(`[members] MEMORY_STORE_ID=${MEMORY_STORE_ID || '(empty)'} API_KEY=${(process.env.TWILIO_API_KEY ?? '').slice(0,8) || '(empty)'}...`);
    try {
      const listRes = await axios.get(
        `${MEMORY_BASE}/v1/Stores/${MEMORY_STORE_ID}/Profiles`,
        { auth: memoryAuth },
      );
      const profileIds: string[] = listRes.data.profiles ?? [];

      const results = await Promise.all(
        profileIds.map(async (pid): Promise<MemberRow | null> => {
          try {
            const profile = await fetchProfile(pid);
            const contact = profile?.traits.Contact ?? {};
            const outreach = profile?.traits.outreach ?? {};
            if (!contact.memberId) return null;
            const first = contact.firstName ?? '';
            const last  = contact.lastName ?? '';
            return {
              profile_id: pid,
              name: `${first} ${last}`.trim(),
              initials: `${first.slice(0, 1)}${last.slice(0, 1)}`.toUpperCase(),
              member_id: contact.memberId ?? '',
              phone: contact.phone ?? '',
              next_follow_up: outreach.nextFollowUp ?? '',
              next_follow_up_reason: outreach.nextFollowUpReason ?? '',
              status: outreach.status ?? 'pending',
              outreach_responses: outreach.outreachResponses ?? '',
              last_call_summary: outreach.lastCallSummary ?? '',
            };
          } catch { return null; }
        }),
      );

      reply.send({ members: results.filter((m): m is MemberRow => m !== null) });
    } catch (e: unknown) {
      const axErr = e as { response?: { status?: number; data?: unknown } };
      console.error(`[members] FAILED status=${axErr.response?.status} body=${JSON.stringify(axErr.response?.data)}`);
      reply.status(500).send({ members: [], error: String(e) });
    }
  });

  // ── Outbound call ────────────────────────────────────────────────────────
  app.post('/api/outbound-call', async (req, reply) => {
    const { name = 'Member', phone = '', goal = '', goalDesc = '' } = req.body as Record<string, string>;
    const memberPhone = normalizePhone(phone);
    const dialTo = OUTBOUND_CALL_TO || memberPhone;
    const greeting = buildGreeting(name, goal, goalDesc);

    // Use a stable conv_id based on phone + timestamp so the Python TAC server
    // can look up the context when handle_incoming_call creates the conversation.
    // The actual Maestro conversationId won't be known until after the call connects,
    // so we pass conv_id as a query param on the twiml-outbound URL and the Python
    // server reads it from the query string to look up pending context.
    const convId = `outbound-${memberPhone}-${Date.now()}`;
    const ctx: OutboundContext & { conv_id: string } = { conv_id: convId, name, goal, goalDesc, phone: memberPhone, greeting };

    const backendPort = AGENT_BACKEND === 'elevenlabs' ? EL_PORT : TAC_PORT;
    const backendName = AGENT_BACKEND === 'elevenlabs' ? 'ElevenLabs' : 'TAC';
    try {
      await fetch(`http://localhost:${backendPort}/set-outbound-context`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(ctx),
      });
    } catch (e) {
      console.error(`[outbound-call] failed to set outbound context on ${backendName} server:`, e);
      return reply.status(500).send({ success: false, error: `${backendName} server unreachable` });
    }

    const params = new URLSearchParams({ conv_id: convId });
    // Both backends expose /twiml-outbound at their respective ports but share the same
    // public domain (ngrok). The backend-specific path must be routed via VOICE_PUBLIC_DOMAIN.
    const twimlUrl = `https://${VOICE_DOMAIN}/twiml-outbound?${params}`;

    try {
      const call = await twilioClient.calls.create({ to: dialTo, from: PHONE_NUMBER, url: twimlUrl });
      console.log(`[outbound-call] initiated member=${name} callSid=${call.sid} conv_id=${convId}`);
      reply.send({ success: true, call_sid: call.sid });
    } catch (e) {
      reply.status(500).send({ success: false, error: String(e) });
    }
  });

  // ── Reset member call history ────────────────────────────────────────────
  app.post('/api/reset-member', async (req, reply) => {
    const { profile_id } = req.body as { profile_id: string };
    if (!profile_id) return reply.status(400).send({ success: false, error: 'profile_id required' });
    try {
      const profile = await fetchProfile(profile_id);
      const existingOutreach = (profile?.traits?.outreach ?? {}) as Record<string, unknown>;
      await updateProfileTraits(profile_id, 'outreach', {
        ...existingOutreach,
        lastCallSummary: '',
        outreachResponses: '',
        status: 'pending',
      });
      console.log(`[reset-member] cleared call history for profileId=${profile_id}`);
      reply.send({ success: true });
    } catch (e) {
      reply.status(500).send({ success: false, error: String(e) });
    }
  });

  // ── Send SMS ─────────────────────────────────────────────────────────────
  app.post('/api/send-sms', async (req, reply) => {
    const { name = 'Member', phone = '', goal = '', goalDesc = '' } = req.body as Record<string, string>;
    const sendTo = OUTBOUND_CALL_TO || normalizePhone(phone);
    if (!sendTo) return reply.status(400).send({ success: false, error: 'No destination number configured' });

    const lines = [`Hi ${name}, this is the Owl Health Care Team.`];
    if (goal) lines.push(`We're reaching out regarding: ${goal}.`);
    if (goalDesc) lines.push(goalDesc);
    lines.push('Please reply or call us if you have any questions.');

    try {
      const msg = await twilioClient.messages.create({ to: sendTo, from: PHONE_NUMBER, body: lines.join(' ') });
      console.log(`[send-sms] sent to ${sendTo} sid=${msg.sid}`);
      reply.send({ success: true, message_sid: msg.sid });
    } catch (e) {
      reply.status(500).send({ success: false, error: String(e) });
    }
  });

  // ── CI webhook ───────────────────────────────────────────────────────────
  app.post('/ci-webhook', async (req, reply) => {
    const payload = req.body as Record<string, unknown>;
    const convId: string = (payload.conversationId as string) ?? '';
    const eventType = payload.event ?? payload.status ?? payload.EventType ?? '(unknown)';
    console.log(`[CI WEBHOOK] event=${eventType} convId=${convId}`);

    const operatorResults: unknown[] = (payload.operatorResults as unknown[]) ?? [];
    console.log(`[CI] ${operatorResults.length} operator result(s) received`);
    console.log(`[CI] config: SUMMARY_SID=${CI_SUMMARY_OPERATOR_SID || '(not set)'} OUTREACH_SID=${CI_OUTREACH_OPERATOR_SID || '(not set)'}`);
    console.log(`[CI] operator ids in payload: ${operatorResults.map(r => ((r as Record<string,unknown>)?.operator as Record<string,unknown>)?.id ?? '?').join(', ')}`);

    // Resolve member profile once for the whole webhook payload
    let profileId: string | null = null;

    // Ask Python TAC server which member phone this conversation belongs to (outbound)
    let memberPhone: string | null = null;
    try {
      const tacRes = await fetch(`http://localhost:${TAC_PORT}/get-outbound-phone/${encodeURIComponent(convId)}`);
      const tacData = await tacRes.json() as { phone?: string };
      memberPhone = tacData.phone || null;
    } catch (e) {
      console.warn('[CI] TAC server phone lookup failed:', e);
    }

    if (memberPhone) {
      profileId = await lookupProfileId(memberPhone);
      console.log(`[CI] outbound convId=${convId} phone=${memberPhone} profileId=${profileId ?? '(not found)'}`);
    }

    // Collect updates from all recognized operators, then write once
    let summaryText = '';
    let outreachAnalysis = '';

    for (const raw of operatorResults) {
      const result = raw as Record<string, unknown>;
      const operator = result.operator as Record<string, unknown> | undefined;
      const operatorId = (operator?.id as string | undefined) ?? '';
      console.log(`[CI] operator id=${operatorId} name=${operator?.name}`);

      // Inbound fallback: resolve profile from participant if TAC phone lookup missed
      if (!profileId) {
        const execDetails = result.executionDetails as Record<string, unknown> | undefined;
        const participants = (execDetails?.participants as { id: string; profileId?: string; type: string }[]) ?? [];
        const customer = participants.find(p => p.type === 'CUSTOMER');
        if (customer?.profileId) {
          profileId = customer.profileId;
          console.log(`[CI] inbound profileId=${profileId}`);
        }
      }

      const outputFormat = (result.outputFormat as string | undefined) ?? '';
      const resultField  = result.result as Record<string, unknown> | undefined;

      const isOutreachOp = CI_OUTREACH_OPERATOR_SID && operatorId === CI_OUTREACH_OPERATOR_SID;
      const isSummaryOp  = !isOutreachOp && (CI_SUMMARY_OPERATOR_SID ? operatorId === CI_SUMMARY_OPERATOR_SID : true);
      console.log(`[CI] classification: isSummaryOp=${isSummaryOp} isOutreachOp=${isOutreachOp}`);

      // ── Summary operator ──────────────────────────────────────────────────
      if (isSummaryOp && !summaryText) {
        if (outputFormat === 'TEXT') {
          summaryText = (resultField?.result as string) ?? '';
        }
        if (!summaryText) {
          const p = resultField?.payload
            ?? (resultField?.['com.twilio.cai.intelligence.JSONResult'] as Record<string, unknown> | undefined)?.payload;
          if (typeof p === 'string') {
            try { const parsed = JSON.parse(p); summaryText = parsed?.summary ?? parsed?.summaries?.[0]?.summary ?? parsed?.text ?? ''; }
            catch { summaryText = p; }
          } else if (typeof p === 'object' && p !== null) {
            const po = p as Record<string, unknown>;
            summaryText = (po.summary as string) ?? (po.text as string) ?? ((po.summaries as { summary: string }[])?.[0]?.summary) ?? '';
          }
        }
        if (!summaryText) {
          summaryText = (resultField?.result as string) ?? (resultField?.summary as string) ?? (resultField?.text as string) ?? '';
        }
        console.log(`[CI] summary extracted: ${summaryText.length} chars`);
      }

      // ── Outreach response analysis operator ───────────────────────────────
      if (isOutreachOp && !outreachAnalysis) {
        console.log(`[CI] outreach resultField keys: ${Object.keys(resultField ?? {}).join(', ')}`);
        let parsed: { interactions?: { question: string; answer: string }[] } | null = null;
        if (Array.isArray((resultField as Record<string, unknown> | undefined)?.interactions)) {
          parsed = resultField as unknown as typeof parsed;
        } else {
          const p = resultField?.payload
            ?? (resultField?.['com.twilio.cai.intelligence.JSONResult'] as Record<string, unknown> | undefined)?.payload;
          console.log(`[CI] outreach payload type=${typeof p} value=${JSON.stringify(p)?.slice(0, 200)}`);
          if (typeof p === 'string') {
            try { parsed = JSON.parse(p); } catch (e) { console.warn(`[CI] outreach JSON parse failed: ${e}`); }
          } else if (typeof p === 'object' && p !== null) {
            parsed = p as unknown as typeof parsed;
          }
        }
        const interactions = parsed?.interactions ?? [];
        console.log(`[CI] outreach interactions count=${interactions.length}`);
        if (interactions.length > 0) {
          outreachAnalysis = 'Outreach Analysis\n' + interactions
            .map(i => `Q: ${i.question}\nA: ${i.answer}`)
            .join('\n\n');
        }
      }
    }

    if (!profileId) {
      console.warn('[CI] no profile identified — nothing written');
      return reply.send({ success: true });
    }

    const execDetails = (operatorResults[0] as Record<string, unknown> | undefined)?.executionDetails as Record<string, unknown> | undefined;
    const channels = (execDetails?.channels as string[] | undefined) ?? [];
    const ts = formatTimestampPST(
      ((operatorResults[0] as Record<string, unknown> | undefined)?.dateCreated as string | undefined)
    );
    const channel = channels[0] ?? 'voice';

    if (!summaryText && !outreachAnalysis) {
      console.warn('[CI] no content extracted from any operator — skipping write');
      return reply.send({ success: true });
    }

    // Buffer trait updates — both CI operators fire separate webhooks within milliseconds.
    // scheduleCIFlush waits 3s then does one read-then-write with all buffered updates.
    const pending = ciPendingTraits.get(profileId) ?? {};

    if (summaryText) {
      pending.lastCallSummary = `[${channel}, ${ts}] ${summaryText.trim()}`;
      console.log(`[CI] buffered lastCallSummary (${(pending.lastCallSummary as string).length} chars)`);
    }

    if (outreachAnalysis) {
      pending.outreachResponses = `[${channel}, ${ts}]\n${outreachAnalysis}`;
      console.log(`[CI] buffered outreachResponses (${(pending.outreachResponses as string).length} chars)`);
    }

    if (summaryText || outreachAnalysis) {
      pending.status = 'completed';
    }

    ciPendingTraits.set(profileId, pending);
    scheduleCIFlush(profileId);
    console.log(`[CI] scheduled flush for profileId=${profileId} buffered=${Object.keys(pending).join(', ')}`);

    reply.send({ success: true });
  });

  const port = parseInt(process.env.APP_PORT ?? '8001', 10);
  await app.listen({ port, host: '0.0.0.0' });
  console.log(`[app-server] healthcare dashboard on http://localhost:${port}/members.html`);
}
