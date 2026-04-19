import 'dotenv/config';
import Fastify from 'fastify';
import fastifyStatic from '@fastify/static';
import fastifyFormBody from '@fastify/formbody';
import axios from 'axios';
import { Twilio } from 'twilio';
import * as path from 'path';

import { buildGreeting } from '../../../prompts';
import { MemberRow, OutboundContext } from '../../../types';
import { outboundConversationMap, setLastOutboundContext } from './state';
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
const ACCOUNT_SID      = process.env.TWILIO_ACCOUNT_SID ?? '';
const AUTH_TOKEN       = process.env.TWILIO_AUTH_TOKEN ?? '';
const PHONE_NUMBER     = process.env.TWILIO_PHONE_NUMBER ?? '';
const VOICE_DOMAIN     = (process.env.VOICE_PUBLIC_DOMAIN ?? 'NOT_SET').replace(/^https?:\/\//, '');
const OUTBOUND_CALL_TO = process.env.OUTBOUND_CALL_TO ?? '';
const CI_SUMMARY_OPERATOR_SID = process.env.TWILIO_TAC_CI_SUMMARY_OPERATOR_SID ?? '';

const twilioClient = new Twilio(ACCOUNT_SID, AUTH_TOKEN);

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
              last_call_summary: outreach.lastCallSummary ?? '',
            };
          } catch { return null; }
        }),
      );

      reply.send({ members: results.filter((m): m is MemberRow => m !== null) });
    } catch (e) {
      reply.status(500).send({ members: [], error: String(e) });
    }
  });

  // ── Outbound call ────────────────────────────────────────────────────────
  app.post('/api/outbound-call', async (req, reply) => {
    const { name = 'Member', phone = '', goal = '', goalDesc = '' } = req.body as Record<string, string>;
    const memberPhone = normalizePhone(phone);
    const dialTo = OUTBOUND_CALL_TO || memberPhone;
    const greeting = buildGreeting(name, goal, goalDesc);

    const ctx: OutboundContext = { name, goal, goalDesc, phone: memberPhone, greeting };
    setLastOutboundContext(ctx);

    const params = new URLSearchParams({ member: name, goal, desc: goalDesc });
    const twimlUrl = `https://${VOICE_DOMAIN}/twiml-outbound?${params}`;

    try {
      const call = await twilioClient.calls.create({ to: dialTo, from: PHONE_NUMBER, url: twimlUrl });
      console.log(`[outbound-call] initiated member=${name} callSid=${call.sid}`);
      reply.send({ success: true, call_sid: call.sid });
    } catch (e) {
      setLastOutboundContext(null);
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

    // -- Verbose debug logs (uncomment to troubleshoot) --
    // console.log(`[CI WEBHOOK] full payload:\n${JSON.stringify(req.body, null, 2).slice(0, 2000)}`);

    const operatorResults: unknown[] = (payload.operatorResults as unknown[]) ?? [];
    console.log(`[CI] ${operatorResults.length} operator result(s) received`);

    for (const raw of operatorResults) {
      const result = raw as Record<string, unknown>;
      const operator = result.operator as Record<string, unknown> | undefined;
      if (CI_SUMMARY_OPERATOR_SID && operator?.id !== CI_SUMMARY_OPERATOR_SID) continue;

      const outputFormat = (result.outputFormat as string | undefined) ?? '';
      const resultField  = result.result as Record<string, unknown> | undefined;

      let summaryText = '';
      if (outputFormat === 'JSON' || !outputFormat) {
        const p = resultField?.payload
          ?? (resultField?.['com.twilio.cai.intelligence.JSONResult'] as Record<string, unknown> | undefined)?.payload;
        if (typeof p === 'string') {
          try {
            const parsed = JSON.parse(p);
            summaryText = parsed?.summary ?? parsed?.summaries?.[0]?.summary ?? parsed?.text ?? '';
          } catch { summaryText = p; }
        } else if (typeof p === 'object' && p !== null) {
          const po = p as Record<string, unknown>;
          summaryText = (po.summary as string) ?? (po.text as string)
            ?? ((po.summaries as { summary: string }[])?.[0]?.summary) ?? '';
        }
      }
      if (!summaryText) {
        summaryText = (resultField?.result as string) ?? (resultField?.summary as string) ?? (resultField?.text as string) ?? '';
      }

      console.log(`[CI] summaryText (${summaryText.length} chars): "${summaryText.slice(0, 120)}"`);
      if (!summaryText) { console.warn('[CI] empty summaryText — skipping'); continue; }

      const execDetails = result.executionDetails as Record<string, unknown> | undefined;
      const participants = (execDetails?.participants as { id: string; profileId?: string; type: string }[]) ?? [];
      const customerParticipant = participants.find(p => p.type === 'CUSTOMER');
      const profileIdFromPayload = customerParticipant?.profileId ?? null;

      const ts = formatTimestampPST(result.dateCreated as string | undefined);
      const channels = (execDetails?.channels as string[] | undefined) ?? [];
      const prefixed = `[${channels[0] ?? 'voice'}, ${ts}] ${summaryText.trim()}`;

      const memberPhone = outboundConversationMap.get(convId);
      if (memberPhone) {
        outboundConversationMap.delete(convId);
        console.log(`[CI] outbound — updating profile for ${memberPhone}`);
        const profileId = await lookupProfileId(memberPhone);
        if (profileId) await updateProfileTraits(profileId, 'outreach', { lastCallSummary: prefixed });
      } else if (profileIdFromPayload) {
        console.log(`[CI] inbound — updating profile ${profileIdFromPayload}`);
        await updateProfileTraits(profileIdFromPayload, 'outreach', { lastCallSummary: prefixed });
      } else {
        console.warn('[CI] no profile identified — summary not written');
      }
      break;
    }

    reply.send({ success: true });
  });

  const port = parseInt(process.env.APP_PORT ?? '8001', 10);
  await app.listen({ port, host: '0.0.0.0' });
  console.log(`[app-server] healthcare dashboard on http://localhost:${port}/members.html`);
}
