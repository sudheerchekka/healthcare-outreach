import 'dotenv/config';
import Fastify from 'fastify';
import fastifyStatic from '@fastify/static';
import fastifyFormBody from '@fastify/formbody';
import axios from 'axios';
import { Twilio, jwt as twilioJwt } from 'twilio';
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

// ── Configurable live-results operator grid (up to 4 slots) ────────────────
const CI_OPERATORS = [1, 2, 3, 4].map(n => ({
  sid:   process.env[`CI_OPERATOR_${n}_SID`]   ?? '',
  label: process.env[`CI_OPERATOR_${n}_LABEL`] ?? `Operator ${n}`,
})).filter(o => o.sid);
const twilioClient = new Twilio(ACCOUNT_SID, AUTH_TOKEN);

// ── CI write debouncer ──────────────────────────────────────────────────────
// Both CI operators fire separate webhooks within milliseconds. Without debouncing,
// whichever writes second reads a stale profile (before the first write landed) and
// overwrites the first update. We buffer trait updates per profileId for 3s then flush once.
const ciPendingTraits = new Map<string, Record<string, unknown>>();
const ciFlushTimers   = new Map<string, ReturnType<typeof setTimeout>>();

// ── Live operator results store (profileId → operatorSid → result) ──────────
const ciLiveResults = new Map<string, Record<string, { label: string; result: string; json?: unknown; ts: string }>>();
// ── SSE subscribers (profileId → set of response streams) ───────────────────
const ciSseClients = new Map<string, Set<import('http').ServerResponse>>();

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

    try {
      await fetch(`http://localhost:${TAC_PORT}/set-outbound-context`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(ctx),
      });
    } catch (e) {
      console.error('[outbound-call] failed to set outbound context on TAC server:', e);
      return reply.status(500).send({ success: false, error: 'TAC server unreachable' });
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

  // ── Member detail: observations + summaries ─────────────────────────────
  app.get('/api/member-detail/:profileId', async (req, reply) => {
    const { profileId } = req.params as { profileId: string };
    try {
      const [obsRes, sumRes] = await Promise.all([
        axios.get(`${MEMORY_BASE}/v1/Stores/${MEMORY_STORE_ID}/Profiles/${profileId}/Observations`, { auth: memoryAuth }),
        axios.get(`${MEMORY_BASE}/v1/Stores/${MEMORY_STORE_ID}/Profiles/${profileId}/ConversationSummaries`, { auth: memoryAuth }),
      ]);
      reply.send({
        observations: obsRes.data.observations ?? [],
        summaries:    sumRes.data.summaries ?? [],
      });
    } catch (e) {
      reply.status(500).send({ observations: [], summaries: [], error: String(e) });
    }
  });

  // ── Delete observation ───────────────────────────────────────────────────
  app.delete('/api/member-detail/:profileId/observations/:obsId', async (req, reply) => {
    const { profileId, obsId } = req.params as { profileId: string; obsId: string };
    try {
      await axios.delete(
        `${MEMORY_BASE}/v1/Stores/${MEMORY_STORE_ID}/Profiles/${profileId}/Observations/${obsId}`,
        { auth: memoryAuth },
      );
      reply.send({ success: true });
    } catch (e) {
      reply.status(500).send({ success: false, error: String(e) });
    }
  });

  // ── Delete conversation summary ──────────────────────────────────────────
  app.delete('/api/member-detail/:profileId/summaries/:sumId', async (req, reply) => {
    const { profileId, sumId } = req.params as { profileId: string; sumId: string };
    try {
      await axios.delete(
        `${MEMORY_BASE}/v1/Stores/${MEMORY_STORE_ID}/Profiles/${profileId}/ConversationSummaries/${sumId}`,
        { auth: memoryAuth },
      );
      reply.send({ success: true });
    } catch (e) {
      reply.status(500).send({ success: false, error: String(e) });
    }
  });

  // ── Live CI operator results (snapshot) ─────────────────────────────────
  app.get('/api/ci-results/:profileId', async (req, reply) => {
    const { profileId } = req.params as { profileId: string };
    reply.send({ operators: CI_OPERATORS, results: ciLiveResults.get(profileId) ?? {} });
  });

  // ── Live CI operator results (SSE stream) ────────────────────────────────
  // Subscribers keyed by profileId; each call to this endpoint registers a sender.
  app.get('/api/ci-results/:profileId/stream', async (req, reply) => {
    const { profileId } = req.params as { profileId: string };
    reply.hijack();
    const raw = reply.raw;
    raw.setHeader('Content-Type', 'text/event-stream');
    raw.setHeader('Cache-Control', 'no-cache');
    raw.setHeader('Connection', 'keep-alive');
    raw.setHeader('Access-Control-Allow-Origin', '*');
    raw.flushHeaders();

    // Clear stored results so page always starts fresh on reload
    ciLiveResults.delete(profileId);
    const snapshot = { operators: CI_OPERATORS, results: {} };
    raw.write(`data: ${JSON.stringify(snapshot)}\n\n`);

    // Register subscriber
    if (!ciSseClients.has(profileId)) ciSseClients.set(profileId, new Set());
    ciSseClients.get(profileId)!.add(raw);

    // Keepalive ping every 25s
    const ping = setInterval(() => raw.write(': ping\n\n'), 25000);

    req.raw.on('close', () => {
      clearInterval(ping);
      ciSseClients.get(profileId)?.delete(raw);
    });
  });

  // ── Browser call token (Twilio Client SDK) ───────────────────────────────
  app.get('/api/browser-call-token', async (_req, reply) => {
    try {
      const { AccessToken } = twilioJwt;
      const { VoiceGrant } = AccessToken;
      const token = new AccessToken(ACCOUNT_SID, process.env.TWILIO_API_KEY ?? '', process.env.TWILIO_API_TOKEN ?? '', { identity: 'care-team-agent', ttl: 3600 });
      token.addGrant(new VoiceGrant({ incomingAllow: true }));
      reply.send({ token: token.toJwt() });
    } catch (e) {
      reply.status(500).send({ error: String(e) });
    }
  });

  // ── Browser call: place outbound via REST API so CO captures it ──────────
  app.post('/api/browser-call', async (req, reply) => {
    const { phone = '' } = req.body as Record<string, string>;
    const dialTo = OUTBOUND_CALL_TO || normalizePhone(phone);
    if (!dialTo) return reply.status(400).send({ success: false, error: 'No destination number' });
    const answerUrl = `https://${VOICE_DOMAIN}/browser-answer-twiml`;
    try {
      const call = await twilioClient.calls.create({ to: dialTo, from: PHONE_NUMBER, url: answerUrl });
      console.log(`[browser-call] placed callSid=${call.sid} to=${dialTo}`);
      reply.send({ success: true, call_sid: call.sid });
    } catch (e) {
      reply.status(500).send({ success: false, error: String(e) });
    }
  });


  // ── Send SMS ─────────────────────────────────────────────────────────────
  app.post('/api/send-sms', async (req, reply) => {
    const { name = 'Member', phone = '', goal = '', goalDesc = '', customBody = '' } = req.body as Record<string, string>;
    const sendTo = OUTBOUND_CALL_TO || normalizePhone(phone);
    if (!sendTo) return reply.status(400).send({ success: false, error: 'No destination number configured' });

    let smsBody = customBody.trim();
    if (!smsBody) {
      const lines = [`Hi ${name}, this is the Owl Health Care Team.`];
      if (goal) lines.push(`We're reaching out regarding: ${goal}.`);
      if (goalDesc) lines.push(goalDesc);
      lines.push('Please reply or call us if you have any questions.');
      smsBody = lines.join(' ');
    }

    try {
      const msg = await twilioClient.messages.create({ to: sendTo, from: PHONE_NUMBER, body: smsBody });
      console.log(`[send-sms] sent to ${sendTo} sid=${msg.sid}`);

      // Write observation to member's profile
      try {
        const profileId = await lookupProfileId(normalizePhone(phone));
        if (profileId) {
          const obsContent = `SMS sent to member: "${smsBody}"`;
          await axios.post(
            `${MEMORY_BASE}/v1/Stores/${MEMORY_STORE_ID}/Profiles/${profileId}/Observations`,
            { observations: [{ content: obsContent, occurredAt: new Date().toISOString(), source: 'care-team-portal' }] },
            { auth: memoryAuth },
          );
          console.log(`[send-sms] observation written profileId=${profileId}`);
        } else {
          console.warn(`[send-sms] no profileId found for ${phone} — skipping observation`);
        }
      } catch (obsErr) {
        const axErr = obsErr as { response?: { data?: unknown } };
        console.warn(`[send-sms] observation write failed:`, axErr.response?.data ?? obsErr);
      }

      reply.send({ success: true, message_sid: msg.sid });
    } catch (e) {
      reply.status(500).send({ success: false, error: String(e) });
    }
  });

  // ── CI webhook ───────────────────────────────────────────────────────────
  app.post('/ci-webhook', async (req, reply) => {
    const payload = req.body as Record<string, unknown>;
    const data = (payload.data ?? payload) as Record<string, unknown>;
    const convId: string = (data.conversationId as string) ?? (payload.conversationId as string) ?? '';
    const eventType = payload.eventType ?? payload.event ?? payload.status ?? payload.EventType ?? '(unknown)';
    console.log(`[CI WEBHOOK] event=${eventType} convId=${convId}`);

    const operatorResults: unknown[] = (payload.operatorResults as unknown[]) ?? [];

    // Skip non-operator events (CONVERSATION_CREATED, COMMUNICATION_CREATED, etc.)
    if (!operatorResults.length) {
      return reply.send({ success: true });
    }

    console.log(`[CI] ${operatorResults.length} operator result(s) received convId=${convId}`);
    console.log(`[CI] config: SUMMARY_SID=${CI_SUMMARY_OPERATOR_SID || '(not set)'} OUTREACH_SID=${CI_OUTREACH_OPERATOR_SID || '(not set)'}`);
    console.log(`[CI] operator ids in payload: ${operatorResults.map(r => ((r as Record<string,unknown>)?.operator as Record<string,unknown>)?.id ?? '?').join(', ')}`);

    // Resolve member profile once for the whole webhook payload
    let profileId: string | null = null;

    // Ask Python TAC server which member phone this conversation belongs to (outbound)
    let memberPhone: string | null = null;
    try {
      const tacRes = await fetch(`http://localhost:${TAC_PORT}/get-outbound-phone/${encodeURIComponent(convId)}`);
      const tacData = await tacRes.json() as { phone?: string; profileId?: string };
      memberPhone = tacData.phone || null;
      if (tacData.profileId) profileId = tacData.profileId;
    } catch (e) {
      console.warn('[CI] TAC server phone lookup failed:', e);
    }

    if (!profileId && memberPhone) {
      profileId = await lookupProfileId(memberPhone);
    }
    if (memberPhone || profileId) {
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

      // Inbound fallback: resolve profile from participant if TAC phone lookup missed.
      // For outbound calls the Orchestrator labels our Twilio number as CUSTOMER —
      // so skip any participant whose address matches our Twilio number.
      if (!profileId) {
        const execDetails = result.executionDetails as Record<string, unknown> | undefined;
        const participants = (execDetails?.participants as { id: string; profileId?: string; type: string; address?: string }[]) ?? [];
        const ourNumber = process.env.TWILIO_TAC_PHONE_NUMBER ?? '';
        const member = participants.find(p => p.profileId && p.address !== ourNumber);
        if (member?.profileId) {
          profileId = member.profileId;
          console.log(`[CI] inbound fallback profileId=${profileId}`);
        }
      }

      const outputFormat = (result.outputFormat as string | undefined) ?? '';
      const resultField  = result.result as Record<string, unknown> | undefined;

      const isOutreachOp = CI_OUTREACH_OPERATOR_SID && operatorId === CI_OUTREACH_OPERATOR_SID;
      const isSummaryOp  = !isOutreachOp && (CI_SUMMARY_OPERATOR_SID ? operatorId === CI_SUMMARY_OPERATOR_SID : true);
      console.log(`[CI] classification: isSummaryOp=${isSummaryOp} isOutreachOp=${isOutreachOp}`);

      // ── Live results grid: store result for any configured operator ───────
      const liveOp = CI_OPERATORS.find(o => o.sid === operatorId);
      console.log(`[CI] live check operatorId=${operatorId} liveOp=${liveOp?.label ?? 'none'} profileId=${profileId ?? 'null'} hasResult=${!!resultField}`);
      if (liveOp) console.log(`[CI] resultField raw: ${JSON.stringify(resultField).slice(0, 500)}`);
      if (liveOp && profileId && resultField) {
        // Extract result — preserves raw JSON object for structured operators (e.g. adherence)
        let liveText = (resultField.result as string) ?? '';
        if (!liveText) liveText = (resultField.label as string) ?? '';  // CLASSIFICATION
        let liveJson: unknown = null;
        // Check for categories directly on resultField (Script Adherence format)
        if (resultField.categories) { liveJson = resultField; liveText = '__json__'; }
        if (!liveText) {
          const p = resultField.payload
            ?? (resultField['com.twilio.cai.intelligence.JSONResult'] as Record<string, unknown> | undefined)?.payload;
          if (typeof p === 'string') {
            try {
              const parsed = JSON.parse(p);
              if (parsed?.categories) { liveJson = parsed; liveText = '__json__'; }
              else liveText = parsed?.summary ?? parsed?.text ?? JSON.stringify(parsed);
            } catch { liveText = p; }
          } else if (typeof p === 'object' && p !== null) {
            const po = p as Record<string, unknown>;
            if (po.categories) { liveJson = po; liveText = '__json__'; }
            else liveText = JSON.stringify(po, null, 2);
          }
        }
        const existing = ciLiveResults.get(profileId) ?? {};
        existing[operatorId] = { label: liveOp.label, result: liveText, json: liveJson, ts: formatTimestampPST(undefined) };
        ciLiveResults.set(profileId, existing);
        if (liveJson && (liveJson as Record<string,unknown>).categories) {
          const cats = (liveJson as { categories: { category_key: string; criteria: { criteria_key: string; criteria_met: string }[] }[] }).categories;
          cats.forEach(cat => {
            cat.criteria.forEach(c => {
              const met = c.criteria_met === 'Passed' || c.criteria_met === 'Succeeded';
              console.log(`[CI] adherence ${met ? '✓' : '✗'} ${cat.category_key} / ${c.criteria_key}: ${c.criteria_met}`);
            });
          });
        }
        console.log(`[CI] live result stored operatorId=${operatorId} label=${liveOp.label} profileId=${profileId}`);
        const sseCount = ciSseClients.get(profileId)?.size ?? 0;
        console.log(`[CI] SSE push to ${sseCount} subscriber(s) for profileId=${profileId}`);
        const event = JSON.stringify({ operators: CI_OPERATORS, results: existing });
        ciSseClients.get(profileId)?.forEach(client => client.write(`data: ${event}\n\n`));
      }

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
