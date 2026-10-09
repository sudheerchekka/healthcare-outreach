import 'dotenv/config';
import type { FastifyInstance } from 'fastify';
import axios from 'axios';
import { Twilio, jwt as twilioJwt } from 'twilio';
import * as path from 'path';
import { readFile, writeFile } from 'fs/promises';
import type { AppConfig } from '../../../app-config';
import type { MemberRow, OutboundContext } from '../../../types';
import { buildGreeting } from '../../../prompts';
import {
  normalizePhone,
  lookupProfileId,
  fetchProfile,
  updateProfileTraits,
  MEMORY_BASE,
} from './memory';
import type { MemoryCreds } from './memory';

const memoryAxios = axios.create();

export interface NbrPair { userText: string; response: string; ts: string }

export interface PendingAsk { profileId: string; question: string; askedAt: number }
export interface AskReply { question: string; response: string; elapsedMs: number; ts: string }

export interface AppRouteState {
  cfg: AppConfig;
  creds: MemoryCreds;
  ciLiveResults: Map<string, Record<string, { label: string; result: string; json?: unknown; ts: string }>>;
  ciSseClients: Map<string, Set<import('http').ServerResponse>>;
  ciPendingTraits: Map<string, Record<string, unknown>>;
  ciFlushTimers: Map<string, ReturnType<typeof setTimeout>>;
  ciFirstConvId: Map<string, string>;
  nbrResults: Map<string, NbrPair[]>;
  pendingNbrAsks: Map<string, PendingAsk>; // convId → pending on-demand ask
  askReplies: Map<string, AskReply[]>;     // profileId → history (for SSE replay on reconnect)
  askSseClients: Map<string, Set<import('http').ServerResponse>>;
  transcriptMessages: Map<string, { role: string; text: string; ts: string }[]>;
  transcriptSseClients: Map<string, Set<import('http').ServerResponse>>;
  chatSseClients: Map<string, Set<import('http').ServerResponse>>;
  formatTimestampPST: (dateStr?: string) => string;
  scheduleCIFlush: (profileId: string) => void;
  tacPort: number;
}

export function registerAppRoutes(app: FastifyInstance, cfg: AppConfig, tacPort: number): AppRouteState {
  const px = cfg.routePrefix;
  const tacPx = cfg.tacRoutePrefix;

  const creds: MemoryCreds = {
    storeId: cfg.memoryStoreId,
    apiKey: cfg.apiKey,
    apiToken: cfg.apiToken,
  };
  const memoryAuth = { username: cfg.apiKey, password: cfg.apiToken };

  const twilioClient = cfg.accountSid ? new Twilio(cfg.accountSid, cfg.authToken) : null;

  // Per-app in-memory state
  const ciFirstConvId   = new Map<string, string>();  // profileId → first conv_id with CI results
  const ciPendingTraits = new Map<string, Record<string, unknown>>();
  const ciFlushTimers   = new Map<string, ReturnType<typeof setTimeout>>();
  const ciLiveResults   = new Map<string, Record<string, { label: string; result: string; json?: unknown; ts: string }>>();
  const ciSseClients    = new Map<string, Set<import('http').ServerResponse>>();
  const nbrResults      = new Map<string, NbrPair[]>();
  const pendingNbrAsks  = new Map<string, PendingAsk>();
  const askReplies      = new Map<string, AskReply[]>();
  const askSseClients   = new Map<string, Set<import('http').ServerResponse>>();
  const transcriptMessages    = new Map<string, { role: string; text: string; ts: string }[]>();
  const transcriptSseClients  = new Map<string, Set<import('http').ServerResponse>>();
  const chatSseClients        = new Map<string, Set<import('http').ServerResponse>>();

  function formatTimestampPST(dateStr?: string): string {
    const date = dateStr ? new Date(dateStr) : new Date();
    return date.toLocaleString('en-US', {
      timeZone: 'America/Los_Angeles',
      month: 'short', day: '2-digit', year: 'numeric',
      hour: '2-digit', minute: '2-digit', hour12: true,
    }).replace(',', '');
  }

  function scheduleCIFlush(profileId: string): void {
    const existing = ciFlushTimers.get(profileId);
    if (existing) clearTimeout(existing);
    ciFlushTimers.set(profileId, setTimeout(async () => {
      const traits = ciPendingTraits.get(profileId);
      ciPendingTraits.delete(profileId);
      ciFlushTimers.delete(profileId);
      if (!traits) return;
      if (!creds.storeId) { console.warn(`[${cfg.id}][CI] skipping flush — memoryStoreId not configured`); return; }
      try {
        const profile = await fetchProfile(profileId, creds);
        const existingOutreach = (profile?.traits?.outreach ?? {}) as Record<string, unknown>;
        await updateProfileTraits(profileId, 'outreach', { ...existingOutreach, ...traits }, creds);
        console.log(`[${cfg.id}][CI] flushed profileId=${profileId} traits=${Object.keys(traits).join(', ')}`);
      } catch (e: any) {
        console.error(`[${cfg.id}][CI] flush FAILED profileId=${profileId} storeId=${creds.storeId}:`, e?.response?.config?.url ?? e);
      }
    }, 3000));
  }

  // ── Members list ──────────────────────────────────────────────────────────
  app.get(`${px}/api/members`, async (_req, reply) => {
    try {
      const listRes = await memoryAxios.get(
        `${MEMORY_BASE}/v1/Stores/${cfg.memoryStoreId}/Profiles`,
        { auth: memoryAuth },
      );
      const profileIds: string[] = listRes.data.profiles ?? [];
      const results = await Promise.all(
        profileIds.map(async (pid): Promise<MemberRow | null> => {
          try {
            const profile = await fetchProfile(pid, creds);
            const contact  = profile?.traits.Contact ?? {};
            const outreach = profile?.traits.outreach ?? {};
            if (!contact.memberId) return null;
            const first = contact.firstName ?? '';
            const last  = contact.lastName ?? '';
            return {
              profile_id: pid,
              name: `${first} ${last}`.trim(),
              initials: `${first.slice(0,1)}${last.slice(0,1)}`.toUpperCase(),
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
      reply.status(500).send({ members: [], error: String(e) });
    }
  });

  // ── Reset member ──────────────────────────────────────────────────────────
  app.post(`${px}/api/reset-member`, async (req, reply) => {
    const { profile_id } = req.body as { profile_id: string };
    if (!profile_id) return reply.status(400).send({ success: false, error: 'profile_id required' });
    try {
      // Reset outreach traits
      const profile = await fetchProfile(profile_id, creds);
      const existingOutreach = (profile?.traits?.outreach ?? {}) as Record<string, unknown>;
      await updateProfileTraits(profile_id, 'outreach', {
        ...existingOutreach, lastCallSummary: '', outreachResponses: '', status: 'pending',
      }, creds);

      // Delete all observations
      const obsRes = await memoryAxios.get(
        `${MEMORY_BASE}/v1/Stores/${cfg.memoryStoreId}/Profiles/${profile_id}/Observations`,
        { auth: memoryAuth }
      );
      const observations = obsRes.data?.observations ?? [];
      await Promise.allSettled(observations.map((o: { id: string }) =>
        memoryAxios.delete(`${MEMORY_BASE}/v1/Stores/${cfg.memoryStoreId}/Profiles/${profile_id}/Observations/${o.id}`, { auth: memoryAuth })
      ));

      // Delete all conversation summaries
      const sumRes = await memoryAxios.get(
        `${MEMORY_BASE}/v1/Stores/${cfg.memoryStoreId}/Profiles/${profile_id}/ConversationSummaries`,
        { auth: memoryAuth }
      );
      const summaries = sumRes.data?.summaries ?? [];
      await Promise.allSettled(summaries.map((s: { id: string }) =>
        memoryAxios.delete(`${MEMORY_BASE}/v1/Stores/${cfg.memoryStoreId}/Profiles/${profile_id}/ConversationSummaries/${s.id}`, { auth: memoryAuth })
      ));

      // Also clear ciFirstConvId so next call's CI is treated as fresh
      ciFirstConvId.delete(profile_id);

      console.log(`[reset-member] profileId=${profile_id} reset: traits + ${observations.length} obs + ${summaries.length} summaries deleted`);
      reply.send({ success: true });
    } catch (e) {
      reply.status(500).send({ success: false, error: String(e) });
    }
  });

  // ── Member detail ─────────────────────────────────────────────────────────
  app.get(`${px}/api/member-detail/:profileId`, async (req, reply) => {
    const { profileId } = req.params as { profileId: string };
    try {
      const [obsRes, sumRes] = await Promise.all([
        memoryAxios.get(`${MEMORY_BASE}/v1/Stores/${cfg.memoryStoreId}/Profiles/${profileId}/Observations`, { auth: memoryAuth }),
        memoryAxios.get(`${MEMORY_BASE}/v1/Stores/${cfg.memoryStoreId}/Profiles/${profileId}/ConversationSummaries`, { auth: memoryAuth }),
      ]);
      reply.send({ observations: obsRes.data.observations ?? [], summaries: sumRes.data.summaries ?? [] });
    } catch (e) {
      reply.status(500).send({ observations: [], summaries: [], error: String(e) });
    }
  });

  app.get(`${px}/api/lookup-profile`, async (req, reply) => {
    const { phone = '' } = req.query as Record<string, string>;
    if (!phone) return reply.status(400).send({ profileId: null, error: 'phone required' });
    try {
      const profileId = await lookupProfileId(phone, creds);
      reply.send({ profileId: profileId ?? null });
    } catch (e) {
      reply.status(500).send({ profileId: null, error: String(e) });
    }
  });

  app.get(`${px}/api/member-detail/:profileId/traits`, async (req, reply) => {
    const { profileId } = req.params as { profileId: string };
    try {
      const profile = await fetchProfile(profileId, creds);
      reply.send({ contact: profile?.traits?.Contact ?? {}, outreach: profile?.traits?.outreach ?? {} });
    } catch (e) {
      reply.status(500).send({ contact: {}, outreach: {}, error: String(e) });
    }
  });

  app.patch(`${px}/api/member-detail/:profileId/traits`, async (req, reply) => {
    const { profileId } = req.params as { profileId: string };
    const { group, key, value } = req.body as { group: string; key: string; value: string };
    if (!group || !key) return reply.status(400).send({ success: false, error: 'group and key required' });
    try {
      const profile = await fetchProfile(profileId, creds);
      const existing = (profile?.traits?.[group as 'Contact' | 'outreach'] ?? {}) as Record<string, unknown>;
      await updateProfileTraits(profileId, group, { ...existing, [key]: value }, creds);
      reply.send({ success: true });
    } catch (e) {
      reply.status(500).send({ success: false, error: String(e) });
    }
  });

  app.delete(`${px}/api/member-detail/:profileId/observations/:obsId`, async (req, reply) => {
    const { profileId, obsId } = req.params as { profileId: string; obsId: string };
    try {
      await memoryAxios.delete(`${MEMORY_BASE}/v1/Stores/${cfg.memoryStoreId}/Profiles/${profileId}/Observations/${obsId}`, { auth: memoryAuth });
      reply.send({ success: true });
    } catch (e) {
      reply.status(500).send({ success: false, error: String(e) });
    }
  });

  app.delete(`${px}/api/member-detail/:profileId/summaries/:sumId`, async (req, reply) => {
    const { profileId, sumId } = req.params as { profileId: string; sumId: string };
    try {
      await memoryAxios.delete(`${MEMORY_BASE}/v1/Stores/${cfg.memoryStoreId}/Profiles/${profileId}/ConversationSummaries/${sumId}`, { auth: memoryAuth });
      reply.send({ success: true });
    } catch (e) {
      reply.status(500).send({ success: false, error: String(e) });
    }
  });

  // ── CI live results ───────────────────────────────────────────────────────
  app.get(`${px}/api/ci-results/:profileId`, async (req, reply) => {
    const { profileId } = req.params as { profileId: string };
    reply.send({
      operators: cfg.ciOperators,
      results: ciLiveResults.get(profileId) ?? {},
      nbr: nbrResults.get(profileId) ?? [],
    });
  });

  app.delete(`${px}/api/ci-results/:profileId`, async (req, reply) => {
    const { profileId } = req.params as { profileId: string };
    ciLiveResults.delete(profileId);
    nbrResults.delete(profileId);
    // Note: ciFirstConvId is intentionally NOT cleared here — the Flex plugin calls this
    // when the agent accepts, but we need to keep the first conv_id to prevent the
    // Flex conversation CI from overriding AI agent CI results.
    // ciFirstConvId is only reset when a NEW call starts (browser-call API).
    reply.send({ success: true });
  });

  app.get(`${px}/api/ci-results/:profileId/stream`, async (req, reply) => {
    const { profileId } = req.params as { profileId: string };
    reply.hijack();
    const raw = reply.raw;
    raw.setHeader('Content-Type', 'text/event-stream');
    raw.setHeader('Cache-Control', 'no-cache');
    raw.setHeader('Connection', 'keep-alive');
    raw.setHeader('Access-Control-Allow-Origin', '*');
    raw.flushHeaders();
    const cached = ciLiveResults.get(profileId) ?? {};
    const cachedNbr = nbrResults.get(profileId) ?? [];
    raw.write(`data: ${JSON.stringify({ operators: cfg.ciOperators, results: cached, nbr: cachedNbr })}\n\n`);
    if (!ciSseClients.has(profileId)) ciSseClients.set(profileId, new Set());
    ciSseClients.get(profileId)!.add(raw);
    console.log(`[CI] SSE client connected profileId=${profileId} total=${ciSseClients.get(profileId)!.size}`);
    const ping = setInterval(() => raw.write(': ping\n\n'), 25000);
    req.raw.on('close', () => {
      clearInterval(ping);
      ciSseClients.get(profileId)?.delete(raw);
      console.log(`[CI] SSE client disconnected profileId=${profileId} remaining=${ciSseClients.get(profileId)?.size ?? 0}`);
    });
  });

  app.delete(`${px}/api/transcript/:profileId`, async (req, reply) => {
    const { profileId } = req.params as { profileId: string };
    transcriptMessages.delete(profileId);
    reply.send({ success: true });
  });

  app.get(`${px}/api/transcript/:profileId/stream`, async (req, reply) => {
    const { profileId } = req.params as { profileId: string };
    reply.hijack();
    const raw = reply.raw;
    raw.setHeader('Content-Type', 'text/event-stream');
    raw.setHeader('Cache-Control', 'no-cache');
    raw.setHeader('Connection', 'keep-alive');
    raw.setHeader('Access-Control-Allow-Origin', '*');
    raw.flushHeaders();
    (transcriptMessages.get(profileId) ?? []).forEach(m => raw.write(`data: ${JSON.stringify(m)}\n\n`));
    if (!transcriptSseClients.has(profileId)) transcriptSseClients.set(profileId, new Set());
    transcriptSseClients.get(profileId)!.add(raw);
    const ping = setInterval(() => raw.write(': ping\n\n'), 25000);
    req.raw.on('close', () => {
      clearInterval(ping);
      transcriptSseClients.get(profileId)?.delete(raw);
    });
  });

  // ── Browser call ──────────────────────────────────────────────────────────
  app.get(`${px}/api/browser-call-token`, async (_req, reply) => {
    try {
      const { AccessToken } = twilioJwt;
      const { VoiceGrant } = AccessToken;
      const appSid = process.env.TWILIO_TWIML_APP_SID ?? '';
      const grant = new VoiceGrant({ incomingAllow: true, outgoingApplicationSid: appSid || undefined });
      const token = new AccessToken(cfg.accountSid, cfg.apiKey, cfg.apiToken, { identity: 'care-team-agent', ttl: 3600 });
      token.addGrant(grant);
      reply.send({ token: token.toJwt() });
    } catch (e) {
      reply.status(500).send({ error: String(e) });
    }
  });


  // ── AI-agent outbound call (ConversationRelay via TAC) ────────────────────
  // Pages served under `${px}/*.html` use the relative URL `api/outbound-call`,
  // which resolves to this route. (The shared `/api/outbound-call` in index.ts is
  // the TAC → Node IPC endpoint, used by schedule_call.)
  app.post(`${px}/api/outbound-call`, async (req, reply) => {
    const { name = 'Member', phone = '', goal = '', goalDesc = '', profileId = '' } =
      req.body as Record<string, string>;
    const memberPhone = normalizePhone(phone);
    const dialTo = cfg.outboundCallTo || memberPhone;
    if (!dialTo) return reply.status(400).send({ success: false, error: 'No destination number' });
    const greeting = buildGreeting(name, goal, goalDesc);

    if (profileId) {
      ciLiveResults.delete(profileId);
      nbrResults.delete(profileId);
      ciFirstConvId.delete(profileId);
      transcriptMessages.delete(profileId);
      const resetEvent = JSON.stringify({ operators: cfg.ciOperators, results: {}, nbr: [] });
      ciSseClients.get(profileId)?.forEach(c => c.write(`data: ${resetEvent}\n\n`));
      transcriptSseClients.get(profileId)?.forEach(c => c.write(`event: reset\ndata: {}\n\n`));
      console.log(`[${cfg.id}][outbound-call] cleared NBR, transcript, operator results for profileId=${profileId}`);
    }

    const convId = `outbound-${memberPhone}-${Date.now()}`;
    const ctx: OutboundContext & { conv_id: string } = { conv_id: convId, name, goal, goalDesc, phone: memberPhone, greeting };

    try {
      await fetch(`http://localhost:${tacPort}${tacPx}/set-outbound-context`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(ctx),
      });
    } catch (e) {
      console.error(`[${cfg.id}][outbound-call] failed to set outbound context:`, e);
      return reply.status(502).send({ success: false, error: 'TAC server unreachable' });
    }

    if (!twilioClient) return reply.status(500).send({ success: false, error: 'Twilio not configured' });

    const params = new URLSearchParams({ conv_id: convId });
    const twimlUrl = `https://${cfg.voiceDomain}${tacPx}/twiml-outbound?${params}`;

    try {
      const call = await twilioClient.calls.create({ to: dialTo, from: cfg.phoneNumber, url: twimlUrl });
      console.log(`[${cfg.id}][outbound-call] initiated member=${name} callSid=${call.sid} conv_id=${convId}`);
      reply.send({ success: true, call_sid: call.sid });
    } catch (e) {
      reply.status(500).send({ success: false, error: String(e) });
    }
  });

  app.post(`${px}/api/browser-call`, async (req, reply) => {
    const { phone = '', profileId = '', name = '' } = req.body as Record<string, string>;
    const dialTo = cfg.outboundCallTo || normalizePhone(phone);
    const memberPhone = normalizePhone(phone);
    if (!dialTo) return reply.status(400).send({ success: false, error: 'No destination number' });
    if (profileId) {
      ciLiveResults.delete(profileId);
      nbrResults.delete(profileId);
      ciFirstConvId.delete(profileId);
      transcriptMessages.delete(profileId);
      // Notify any open UI tabs so they clear immediately (don't wait for the next webhook)
      const resetEvent = JSON.stringify({ operators: cfg.ciOperators, results: {}, nbr: [] });
      ciSseClients.get(profileId)?.forEach(c => c.write(`data: ${resetEvent}\n\n`));
      transcriptSseClients.get(profileId)?.forEach(c => c.write(`event: reset\ndata: {}\n\n`));
      console.log(`[${cfg.id}][browser-call] cleared NBR, transcript, operator results for profileId=${profileId}`);
    }
    const answerParams = new URLSearchParams({ member_phone: memberPhone, profile_id: profileId, member_name: name });
    const answerUrl = `https://${cfg.voiceDomain}${tacPx}/browser-answer-twiml?${answerParams}`;
    const statusCallback = `https://${cfg.voiceDomain}${tacPx}/browser-call-status`;
    try {
      const call = await twilioClient!.calls.create({
        to: dialTo, from: cfg.phoneNumber, url: answerUrl,
        statusCallback, statusCallbackMethod: 'POST', statusCallbackEvent: ['completed'],
      });
      reply.send({ success: true, call_sid: call.sid });
    } catch (e) {
      reply.status(500).send({ success: false, error: String(e) });
    }
  });

  // ── Reset conversations ───────────────────────────────────────────────────
  app.post(`${px}/api/reset-conversations`, async (req, reply) => {
    const { phone } = req.body as { phone?: string };
    const target = phone || cfg.outboundCallTo;
    if (!target) return reply.status(400).send({ success: false, error: 'phone required' });
    try {
      const conversations = await twilioClient!.conversations.v1.participantConversations.list({ address: target });
      const open = conversations.filter(c => c.conversationState !== 'closed');
      const results = await Promise.allSettled(
        open.map(c => twilioClient!.conversations.v1.conversations(c.conversationSid).update({ state: 'closed' }))
      );
      reply.send({ success: true, closed: results.filter(r => r.status === 'fulfilled').length, total: open.length, phone: target });
    } catch (e) {
      reply.status(500).send({ success: false, error: String(e) });
    }
  });

  // ── Escalation ────────────────────────────────────────────────────────────
  app.post(`${px}/api/escalate-call`, async (req, reply) => {
    const { profileId, reason = 'care_team_requested' } = req.body as Record<string, string>;
    try {
      const res = await fetch(`http://localhost:${tacPort}${tacPx}/escalate-call`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ profile_id: profileId, reason }),
      });
      reply.send(await res.json());
    } catch (e) {
      reply.status(500).send({ success: false, error: String(e) });
    }
  });

  // ── Send SMS ──────────────────────────────────────────────────────────────
  app.post(`${px}/api/send-sms`, async (req, reply) => {
    const { name = 'Member', phone = '', goal = '', goalDesc = '', customBody = '' } = req.body as Record<string, string>;
    const sendTo = cfg.outboundCallTo || normalizePhone(phone);
    if (!sendTo) return reply.status(400).send({ success: false, error: 'No destination number configured' });
    let smsBody = customBody.trim();
    if (!smsBody) {
      const lines = [`Hi ${name}, this is the ${cfg.displayName} team.`];
      if (goal) lines.push(`We're reaching out regarding: ${goal}.`);
      if (goalDesc) lines.push(goalDesc);
      lines.push('Please reply or call us if you have any questions.');
      smsBody = lines.join(' ');
    }
    try {
      const msg = await twilioClient!.messages.create({ to: sendTo, from: cfg.phoneNumber, body: smsBody });

      // Write outbound SMS to conversation memory as an observation
      const memberPhone = normalizePhone(phone);
      const profileId   = await lookupProfileId(memberPhone, creds);
      if (profileId) {
        memoryAxios.post(
          `${MEMORY_BASE}/v1/Stores/${cfg.memoryStoreId}/Profiles/${profileId}/Observations`,
          { observations: [{ content: `[SMS outbound] ${smsBody}`, occurredAt: new Date().toISOString(), source: 'admin-sms' }] },
          { auth: memoryAuth },
        ).catch(e => console.warn(`[send-sms] memory write failed: ${e.message}`));
      }

      reply.send({ success: true, message_sid: msg.sid });
    } catch (e) {
      reply.status(500).send({ success: false, error: String(e) });
    }
  });

  // ── Twilio Verify ─────────────────────────────────────────────────────────
  app.post(`${px}/api/verify/send`, async (req, reply) => {
    const { phone = '' } = req.body as Record<string, string>;
    const memberPhone = normalizePhone(phone);
    const to = cfg.smsSimulateMemberPhone || memberPhone;
    if (!to) return reply.status(400).send({ success: false, error: 'phone required' });
    if (!cfg.verifyServiceSid) return reply.status(500).send({ success: false, error: 'TWILIO_VERIFY_SERVICE_SID not configured' });
    try {
      await twilioClient!.verify.v2.services(cfg.verifyServiceSid).verifications.create({ to, channel: 'sms' });
      reply.send({ success: true });
    } catch (e) {
      reply.status(500).send({ success: false, error: String(e) });
    }
  });

  app.post(`${px}/api/verify/check`, async (req, reply) => {
    const { phone = '', code = '' } = req.body as Record<string, string>;
    const to = cfg.smsSimulateMemberPhone || normalizePhone(phone);
    if (!to || !code) return reply.status(400).send({ success: false, error: 'phone and code required' });
    if (!cfg.verifyServiceSid) return reply.status(500).send({ success: false, error: 'TWILIO_VERIFY_SERVICE_SID not configured' });
    try {
      const check = await twilioClient!.verify.v2.services(cfg.verifyServiceSid).verificationChecks.create({ to, code });
      reply.send({ success: true, status: check.status, valid: check.status === 'approved' });
    } catch (e) {
      reply.status(500).send({ success: false, error: String(e) });
    }
  });

  // ── Admin: system prompts ─────────────────────────────────────────────────
  const SYSTEM_PROMPT_FILE         = path.join(process.cwd(), `src/apps/${cfg.id}/agent/src/system_prompt.txt`);
  const SYSTEM_PROMPT_INBOUND_FILE = path.join(process.cwd(), `src/tac/apps/${cfg.id}/system_prompt_inbound.txt`);

  async function readPromptFile(filePath: string): Promise<string> {
    try { return (await readFile(filePath, 'utf8')).trim(); } catch { return ''; }
  }

  // ── Ask (Twilio Knowledge Base search) ────────────────────────────────────
  app.post(`${px}/api/ask`, async (req, reply) => {
    const { question = '', top } = (req.body ?? {}) as { question?: string; top?: number };
    if (!question.trim()) return reply.status(400).send({ error: 'question required' });
    if (!cfg.knowledgeBaseId) return reply.status(500).send({ error: 'Knowledge base not configured (set HEALTHCARE_KB_ID in .env)' });

    const effectiveTop = Math.max(1, Number.isFinite(top) ? Math.floor(top as number) : cfg.knowledgeTop);
    const t0 = Date.now();
    try {
      const res = await axios.post(
        `https://knowledge.twilio.com/v2/KnowledgeBases/${cfg.knowledgeBaseId}/Search`,
        { query: question, top: effectiveTop },
        { auth: { username: cfg.apiKey, password: cfg.apiToken }, timeout: 15000 },
      );
      const chunks = ((res.data?.chunks ?? []) as Array<Record<string, unknown>>)
        .filter(c => c.content)
        .map(c => ({
          content: c.content as string,
          score: (c.score as number) ?? null,
          knowledgeId: (c.knowledgeId as string) ?? null,
          documentTitle: (c.documentTitle as string) ?? null,
          documentUrl: (c.documentUrl as string) ?? null,
          documentNumber: (c.documentNumber as number) ?? null,
          chunkIndex: (c.chunkIndex as number) ?? null,
        }));

      const elapsedMs = Date.now() - t0;
      console.log(`[${cfg.id}][ask] q="${question.slice(0, 80)}" top=${effectiveTop} chunks=${chunks.length} ms=${elapsedMs}`);
      reply.send({ chunks, elapsedMs, knowledgeBaseId: cfg.knowledgeBaseId });
    } catch (e: unknown) {
      const elapsedMs = Date.now() - t0;
      const axErr = e as { response?: { status?: number; data?: unknown }; message?: string };
      console.error(`[${cfg.id}][ask] FAILED status=${axErr.response?.status} ms=${elapsedMs} body=${JSON.stringify(axErr.response?.data)}`);
      reply.status(502).send({ error: axErr.message ?? String(e), elapsedMs });
    }
  });

  // ── Personalized Ask (on-demand NBR rule execution) ───────────────────────
  // Reaps `pendingNbrAsks` entries older than 2 minutes so leaked promises can't
  // mis-match a late webhook with a brand-new ask on the same convId.
  const PENDING_ASK_TTL_MS = 120_000;
  setInterval(() => {
    const now = Date.now();
    for (const [convId, p] of pendingNbrAsks) {
      if (now - p.askedAt > PENDING_ASK_TTL_MS) pendingNbrAsks.delete(convId);
    }
  }, 60_000).unref?.();

  app.post(`${px}/api/personalized-ask`, async (req, reply) => {
    const { profileId = '', question = '', convId = '' } = (req.body ?? {}) as {
      profileId?: string; question?: string; convId?: string;
    };
    if (!profileId || !question.trim()) return reply.status(400).send({ error: 'profileId and question required' });
    if (!convId) {
      return reply.status(400).send({
        error: 'convId required. Place a call first (the browser captures the conversation ID when the call connects).',
        convId: '',
        intelligenceConfigurationId: cfg.intelligenceConfigurationId,
        nbrRuleId: cfg.nbrRuleId,
      });
    }
    if (!cfg.intelligenceConfigurationId || !cfg.nbrRuleId) {
      return reply.status(500).send({ error: 'NBR not configured (set TWILIO_INTELLIGENCE_CONFIGURATION_ID and TWILIO_NBR_RULE_ID in .env)' });
    }

    pendingNbrAsks.set(convId, { profileId, question: question.trim(), askedAt: Date.now() });

    // Resolve the member's phone from TAC — needed as the "author" address for the injected message
    let memberPhone = '';
    try {
      const r = await fetch(`http://localhost:${tacPort}/get-outbound-phone/${encodeURIComponent(convId)}`);
      const d = await r.json() as { phone?: string };
      memberPhone = d.phone ?? '';
    } catch (e) {
      console.warn(`[${cfg.id}][personalized-ask] TAC phone lookup failed:`, e);
    }

    // Inject the typed question into the CO conversation so NBR has a live member message to react to.
    // Authored as the CUSTOMER (member) so NBR recommends what the care-team agent should say back.
    // Twilio requires: content.type="TEXT" (not "TRANSCRIPTION"), plus participantId on both author
    // and recipients — so we look up participants first.
    if (memberPhone) {
      try {
        const partsRes = await axios.get(
          `https://conversations.twilio.com/v2/Conversations/${convId}/Participants`,
          { auth: { username: cfg.apiKey, password: cfg.apiToken }, timeout: 10_000 },
        );
        const participants = (partsRes.data?.participants ?? []) as Array<{
          id: string; type: string;
          addresses?: Array<{ address: string; channel: string }>;
        }>;
        const customer  = participants.find(p => p.type === 'CUSTOMER');
        const aiAgent   = participants.find(p => p.type === 'AI_AGENT');
        if (!customer || !aiAgent) {
          console.warn(`[${cfg.id}][personalized-ask] convId=${convId} missing CUSTOMER or AI_AGENT participant — skipping injection`);
        } else {
          const authorAddr    = customer.addresses?.[0]?.address ?? memberPhone;
          const recipientAddr = aiAgent.addresses?.[0]?.address ?? cfg.phoneNumber;
          await axios.post(
            `https://conversations.twilio.com/v2/Conversations/${convId}/Communications`,
            {
              author:     { address: authorAddr,    channel: 'VOICE', participantId: customer.id },
              content:    { text: question.trim(), type: 'TEXT' },
              recipients: [{ address: recipientAddr, channel: 'VOICE', participantId: aiAgent.id }],
            },
            { auth: { username: cfg.apiKey, password: cfg.apiToken }, timeout: 10_000 },
          );
          console.log(`[${cfg.id}][personalized-ask] injected question into convId=${convId} (author=${customer.id} recipient=${aiAgent.id})`);
        }
      } catch (e: unknown) {
        const ax = e as { response?: { status?: number; data?: unknown } };
        console.warn(`[${cfg.id}][personalized-ask] question injection failed status=${ax.response?.status} body=${JSON.stringify(ax.response?.data)} — proceeding to rule execution anyway`);
      }
    } else {
      console.warn(`[${cfg.id}][personalized-ask] no memberPhone for convId=${convId} — skipping question injection (NBR may fail with empty transcript)`);
    }

    try {
      const res = await axios.post(
        'https://intelligence.twilio.com/v3/RuleExecutions',
        {
          intelligenceConfigurationId: cfg.intelligenceConfigurationId,
          ruleId: cfg.nbrRuleId,
          conversationId: convId,
        },
        { auth: { username: cfg.apiKey, password: cfg.apiToken }, timeout: 10_000 },
      );
      const executionSid: string = res.data?.id ?? res.data?.sid ?? '';
      console.log(`[${cfg.id}][personalized-ask] profileId=${profileId} convId=${convId} executionSid=${executionSid} status=${res.status}`);
      reply.status(202).send({
        executionSid,
        convId,
        intelligenceConfigurationId: cfg.intelligenceConfigurationId,
        nbrRuleId: cfg.nbrRuleId,
      });
    } catch (e: unknown) {
      pendingNbrAsks.delete(convId);
      const axErr = e as { response?: { status?: number; data?: unknown }; message?: string };
      console.error(`[${cfg.id}][personalized-ask] rule exec FAILED status=${axErr.response?.status} body=${JSON.stringify(axErr.response?.data)}`);
      reply.status(502).send({
        error: axErr.message ?? String(e),
        twilioStatus: axErr.response?.status,
        twilioBody: axErr.response?.data,
        convId,
        intelligenceConfigurationId: cfg.intelligenceConfigurationId,
        nbrRuleId: cfg.nbrRuleId,
      });
    }
  });

  app.get(`${px}/api/personalized-ask/config`, async (_req, reply) => {
    reply.send({
      intelligenceConfigurationId: cfg.intelligenceConfigurationId,
      nbrRuleId: cfg.nbrRuleId,
    });
  });

  app.get(`${px}/api/personalized-ask/:profileId/active-conv`, async (req, reply) => {
    const { profileId } = req.params as { profileId: string };
    try {
      const r = await fetch(`http://localhost:${tacPort}/get-latest-conv/${encodeURIComponent(profileId)}`);
      const d = await r.json() as { convId?: string };
      console.log(`[${cfg.id}][active-conv] profileId=${profileId} → convId=${d.convId || '(empty)'}`);
      reply.send({ convId: d.convId ?? '' });
    } catch (e) {
      console.error(`[${cfg.id}][active-conv] lookup failed profileId=${profileId}:`, e);
      reply.status(502).send({ convId: '', error: String(e) });
    }
  });

  app.get(`${px}/api/personalized-ask/:profileId/stream`, async (req, reply) => {
    const { profileId } = req.params as { profileId: string };
    reply.hijack();
    const raw = reply.raw;
    raw.setHeader('Content-Type', 'text/event-stream');
    raw.setHeader('Cache-Control', 'no-cache');
    raw.setHeader('Connection', 'keep-alive');
    raw.setHeader('Access-Control-Allow-Origin', '*');
    raw.flushHeaders();
    // Replay cached history so a tab reconnect shows prior Q&As.
    (askReplies.get(profileId) ?? []).forEach(r => raw.write(`data: ${JSON.stringify(r)}\n\n`));
    if (!askSseClients.has(profileId)) askSseClients.set(profileId, new Set());
    askSseClients.get(profileId)!.add(raw);
    const ping = setInterval(() => raw.write(': ping\n\n'), 25000);
    req.raw.on('close', () => {
      clearInterval(ping);
      askSseClients.get(profileId)?.delete(raw);
    });
  });

  app.delete(`${px}/api/personalized-ask/:profileId`, async (req, reply) => {
    const { profileId } = req.params as { profileId: string };
    askReplies.delete(profileId);
    reply.send({ success: true });
  });

  app.get(`${px}/api/admin/system-prompt`, async (_req, reply) => {
    reply.send({ prompt: await readPromptFile(SYSTEM_PROMPT_FILE) });
  });
  app.post(`${px}/api/admin/system-prompt`, async (req, reply) => {
    const { prompt } = req.body as { prompt: string };
    if (typeof prompt !== 'string') return reply.status(400).send({ success: false });
    await writeFile(SYSTEM_PROMPT_FILE, prompt.trim() + '\n', 'utf8');
    reply.send({ success: true });
  });
  app.get(`${px}/api/admin/system-prompt-inbound`, async (_req, reply) => {
    reply.send({ prompt: await readPromptFile(SYSTEM_PROMPT_INBOUND_FILE) });
  });
  app.post(`${px}/api/admin/system-prompt-inbound`, async (req, reply) => {
    const { prompt } = req.body as { prompt: string };
    if (typeof prompt !== 'string') return reply.status(400).send({ success: false });
    await writeFile(SYSTEM_PROMPT_INBOUND_FILE, prompt.trim() + '\n', 'utf8');
    reply.send({ success: true });
  });

  // ── Chat channel (web widget) ─────────────────────────────────────────────
  // POST /api/chat/start — create Twilio Conversation, add visitor participant, send first message
  app.post(`${px}/api/chat/start`, async (req, reply) => {
    const { phone = '', inquiry = '' } = req.body as Record<string, string>;
    if (!phone || !inquiry) return reply.status(400).send({ error: 'phone and inquiry required' });
    if (!twilioClient) return reply.status(500).send({ error: 'Twilio not configured' });

    const chatServiceSid = process.env.TWILIO_CHAT_CONVERSATION_SERVICE_SID ?? '';
    if (!chatServiceSid) return reply.status(500).send({ error: 'TWILIO_CHAT_CONVERSATION_SERVICE_SID not set' });

    try {
      console.log(`[${cfg.id}][chat] creating conversation phone=${phone} service=${chatServiceSid}`);
      const conv = await twilioClient.conversations.v1
        .services(chatServiceSid)
        .conversations.create({ friendlyName: `webchat-${phone}-${Date.now()}` });
      console.log(`[${cfg.id}][chat] conversation created sid=${conv.sid}`);

      // Add visitor as a chat participant — identity = phone for cross-channel profile lookup
      await (twilioClient.conversations.v1
        .services(chatServiceSid)
        .conversations(conv.sid)
        .participants.create as (opts: Record<string, string>) => Promise<unknown>)(
          { identity: phone, 'messagingBinding.type': 'chat' }
        );
      console.log(`[${cfg.id}][chat] participant added identity=${phone}`);

      // Tell TAC which app owns this conversation
      try {
        const tacRes = await fetch(`http://localhost:${tacPort}/register-conversation`, {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ conversationSid: conv.sid, appId: cfg.id }),
        });
        console.log(`[${cfg.id}][chat] TAC register-conversation status=${tacRes.status}`);
      } catch (e) {
        console.warn(`[${cfg.id}][chat] TAC register-conversation failed (non-fatal): ${e}`);
      }

      reply.send({ conversationSid: conv.sid });
    } catch (e) {
      console.error(`[${cfg.id}][chat] start failed: ${e}`);
      reply.status(500).send({ error: String(e) });
    }
  });

  // POST /api/chat/message — visitor sends a message; forward directly to TAC for agent invocation
  // (Twilio REST API messages don't trigger onMessageAdded webhooks, so we call TAC directly)
  app.post(`${px}/api/chat/message`, async (req, reply) => {
    const { conversationSid = '', body: msgBody = '', phone = '' } = req.body as Record<string, string>;
    if (!conversationSid || !msgBody) return reply.status(400).send({ error: 'conversationSid and body required' });
    console.log(`[${cfg.id}][chat] message conv=${conversationSid} phone=${phone} body="${msgBody.slice(0,80)}"`);
    try {
      const tacRes = await fetch(`http://localhost:${tacPort}/chat-message`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ conversationSid, body: msgBody, author: phone }),
      });
      const tacData = await tacRes.json() as { success: boolean; error?: string };
      if (!tacData.success) throw new Error(tacData.error ?? 'TAC error');
      reply.send({ success: true });
    } catch (e) {
      console.error(`[${cfg.id}][chat] message failed: ${e}`);
      reply.status(500).send({ error: String(e) });
    }
  });

  // GET /api/chat/:convSid/stream — SSE stream for agent replies to this conversation
  app.get(`${px}/api/chat/:convSid/stream`, async (req, reply) => {
    const { convSid } = req.params as { convSid: string };
    reply.hijack();
    const raw = reply.raw;
    raw.setHeader('Content-Type', 'text/event-stream');
    raw.setHeader('Cache-Control', 'no-cache');
    raw.setHeader('Connection', 'keep-alive');
    raw.setHeader('Access-Control-Allow-Origin', '*');
    raw.flushHeaders();
    if (!chatSseClients.has(convSid)) chatSseClients.set(convSid, new Set());
    chatSseClients.get(convSid)!.add(raw);
    console.log(`[${cfg.id}][chat] SSE client connected conv=${convSid} total=${chatSseClients.get(convSid)!.size}`);
    const ping = setInterval(() => raw.write(': ping\n\n'), 25000);
    req.raw.on('close', () => {
      clearInterval(ping);
      chatSseClients.get(convSid)?.delete(raw);
      console.log(`[${cfg.id}][chat] SSE client disconnected conv=${convSid}`);
    });
  });

  // POST /api/flex-dequeue-reservation — proxy TaskRouter dequeue from Flex plugin (CORS workaround)
  app.post(`${px}/api/flex-dequeue-reservation`, async (req, reply) => {
    const body = req.body as Record<string, string>;
    try {
      const res = await fetch(`http://localhost:${tacPort}/flex-dequeue-reservation`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      reply.status(res.status).send(data);
    } catch (e) {
      reply.status(500).send({ error: String(e) });
    }
  });

  // POST /api/flex-handoff — bridge browser WebRTC call to Flex agent by redirecting both legs into a conference
  app.post(`${px}/api/flex-handoff`, async (req, reply) => {
    const { callSid = '', workerIdentity = '' } = req.body as Record<string, string>;
    if (!callSid || !workerIdentity) {
      return reply.status(400).send({ error: 'callSid and workerIdentity required' });
    }
    if (!twilioClient) return reply.status(500).send({ error: 'Twilio not configured' });

    const conferenceName = `Handoff_${callSid}`;
    const fromNumber = cfg.phoneNumber;

    try {
      // 1. Redirect the browser WebRTC call into a conference room
      await twilioClient.calls(callSid).update({
        twiml: `<Response><Dial><Conference waitUrl="" beep="false">${conferenceName}</Conference></Dial></Response>`,
      });

      // 2. Dial the Flex agent (via client: identity) into the same conference
      await twilioClient.calls.create({
        to: `client:${workerIdentity}`,
        from: fromNumber,
        twiml: `<Response><Dial><Conference waitUrl="" beep="false">${conferenceName}</Conference></Dial></Response>`,
      });

      reply.send({ success: true, conferenceName });
    } catch (e) {
      console.error(`[${cfg.id}] flex-handoff failed:`, e);
      reply.status(500).send({ error: String(e) });
    }
  });

  // POST /api/flex-cancel-task — cancel pending Flex task when browser call hangs up
  app.post(`${px}/api/flex-cancel-task`, async (req, reply) => {
    const { profileId = '' } = req.body as Record<string, string>;
    try {
      await fetch(`http://localhost:${tacPort}/flex-cancel-task`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ profileId }),
      });
    } catch { /* non-fatal */ }
    reply.send({ success: true });
  });

  // POST /api/chat/close — close the CO conversation when widget is dismissed
  app.post(`${px}/api/chat/close`, async (req, reply) => {
    const { conversationSid = '' } = req.body as Record<string, string>;
    if (conversationSid) {
      try {
        await fetch(`http://localhost:${tacPort}/chat-close`, {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ conversationSid }),
        });
      } catch { /* non-fatal */ }
      chatSseClients.delete(conversationSid);
    }
    reply.send({ success: true });
  });

  // POST /chat-message-event — internal: TAC POSTs agent replies here for SSE fanout
  app.post(`${px}/chat-message-event`, async (req, reply) => {
    const raw = req.body as Record<string, unknown>;
    const conversationSid = (raw.conversationSid as string) || '';
    const msgBody = (raw.body as string) || '';
    const author = (raw.author as string) || 'agent';
    const showCallButton = raw.showCallButton ?? null;
    const handoffToFlex  = raw.handoffToFlex  ?? null;
    const clients = chatSseClients.get(conversationSid);
    console.log(`[${cfg.id}][chat] fanout conv=${conversationSid} clients=${clients?.size ?? 0} showCallButton=${!!showCallButton} handoffToFlex=${!!handoffToFlex} body="${msgBody.slice(0,80)}"`);
    if (clients?.size) {
      const payload: Record<string, unknown> = { body: msgBody, author };
      if (showCallButton) payload.showCallButton = showCallButton;
      if (handoffToFlex)  payload.handoffToFlex  = handoffToFlex;
      const event = `data: ${JSON.stringify(payload)}\n\n`;
      clients.forEach(c => c.write(event));
    } else {
      console.warn(`[${cfg.id}][chat] no SSE clients listening for conv=${conversationSid} — browser may not be connected`);
    }
    reply.send({ success: true });
  });

  // ── CI webhook (shared — registered in index.ts at /ci-webhook) ──────────
  // Handler body is below but registered as a shared route via the returned state.
  // ── END per-app routes ────────────────────────────────────────────────────

  return {
    cfg, creds, ciLiveResults, ciSseClients, ciPendingTraits, ciFlushTimers, ciFirstConvId, nbrResults,
    pendingNbrAsks, askReplies, askSseClients,
    transcriptMessages, transcriptSseClients, chatSseClients, formatTimestampPST, scheduleCIFlush, tacPort,
  };
}

// Exported so index.ts can build the shared /ci-webhook handler
export async function handleCiWebhook(
  payload: Record<string, unknown>,
  state: AppRouteState,
): Promise<void> {
  const { cfg, creds, ciLiveResults, ciSseClients, ciPendingTraits, scheduleCIFlush, formatTimestampPST, ciFirstConvId, nbrResults, transcriptMessages, transcriptSseClients, pendingNbrAsks, askReplies, askSseClients } = state;
  const data = (payload.data ?? payload) as Record<string, unknown>;
  const convId: string = (data.conversationId as string) ?? (payload.conversationId as string) ?? '';
  const operatorResults: unknown[] = (payload.operatorResults as unknown[]) ?? [];
  if (!operatorResults.length) return;

  let profileId: string | null = null;
  let memberPhone: string | null = null;
  try {
    const tacRes = await fetch(`http://localhost:${state.tacPort}/get-outbound-phone/${encodeURIComponent(convId)}`);
    const tacData = await tacRes.json() as { phone?: string; profileId?: string };
    memberPhone = tacData.phone || null;
    if (tacData.profileId) profileId = tacData.profileId;
  } catch { /* ignore */ }

  // Personalized-Ask fast path: if we triggered an on-demand NBR execution for this convId,
  // use the pending entry's profileId instead of requiring a TAC outbound record.
  const pendingAsk = pendingNbrAsks.get(convId);
  if (pendingAsk) {
    profileId = pendingAsk.profileId;
  }

  // Only process CI results from AI agent conversations — those tracked by TAC
  // If TAC has no phone record for this convId, it's a Flex/human agent conversation
  if (!memberPhone && !pendingAsk) {
    console.log(`[CI] skipping conv_id=${convId} — no TAC record (likely human agent conversation)`);
    return;
  }

  if (!profileId && memberPhone) profileId = await lookupProfileId(memberPhone, creds);

  // Only process CI from the first conv_id for this profile — skip subsequent convs (e.g. Flex conv).
  // On-demand Personalized Ask runs may target an older conv_id and must bypass this filter.
  if (profileId && !pendingAsk) {
    const firstConv = ciFirstConvId.get(profileId);
    if (!firstConv) {
      ciFirstConvId.set(profileId, convId);
    } else if (firstConv !== convId) {
      console.log(`[CI] skipping conv_id=${convId} for profileId=${profileId} — first conv was ${firstConv}`);
      return;
    }
  }

  let summaryText = '';
  let outreachAnalysis = '';

  const ciVerbose = process.env.CI_VERBOSE_LOGS === 'true';
  if (ciVerbose) console.log(`[CI] FULL PAYLOAD: ${JSON.stringify(payload).slice(0, 3000)}`);
  console.log(`[CI] webhook profileId=${profileId ?? '(pending)'} operatorResults=${operatorResults.length} convId=${convId}`);

  for (const raw of operatorResults) {
    const result = raw as Record<string, unknown>;
    const operator = result.operator as Record<string, unknown> | undefined;
    const operatorId = (operator?.id as string | undefined) ?? '';

    if (!profileId) {
      const execDetails = result.executionDetails as Record<string, unknown> | undefined;
      const participants = (execDetails?.participants as { profileId?: string; address?: string }[]) ?? [];
      const member = participants.find(p => p.profileId && p.address !== cfg.phoneNumber);
      if (member?.profileId) profileId = member.profileId;
    }

    const outputFormat = (result.outputFormat as string | undefined) ?? '';
    const resultRaw    = result.result;
    const resultField  = (resultRaw !== null && typeof resultRaw === 'object') ? resultRaw as Record<string, unknown> : undefined;
    const isOutreachOp = cfg.ciOutreachOperatorSid && operatorId === cfg.ciOutreachOperatorSid;
    const isSummaryOp  = !isOutreachOp && (cfg.ciSummaryOperatorSid ? operatorId === cfg.ciSummaryOperatorSid : true);
    if (ciVerbose) console.log(`[CI] operator id=${operatorId} outputFormat=${outputFormat} isOutreachOp=${!!isOutreachOp} isSummaryOp=${isSummaryOp} ciSummaryOpSid=${cfg.ciSummaryOperatorSid} result=${JSON.stringify(resultRaw).slice(0, 300)}`);

    const liveOp = cfg.ciOperators.find(o => o.sid === operatorId);
    if (liveOp && profileId && resultField) {
      let liveText = (resultField.result as string) ?? (resultField.label as string) ?? '';
      let liveJson: unknown = null;
      if (resultField.categories) { liveJson = resultField; liveText = '__json__'; }
      if (!liveText) {
        const p = resultField.payload
          ?? (resultField['com.twilio.cai.intelligence.JSONResult'] as Record<string,unknown> | undefined)?.payload;
        if (typeof p === 'string') {
          try {
            const parsed = JSON.parse(p);
            if (parsed?.categories) { liveJson = parsed; liveText = '__json__'; }
            else liveText = parsed?.summary ?? parsed?.text ?? JSON.stringify(parsed);
          } catch { liveText = p; }
        } else if (typeof p === 'object' && p !== null) {
          const po = p as Record<string,unknown>;
          if (po.categories) { liveJson = po; liveText = '__json__'; }
          else liveText = JSON.stringify(po, null, 2);
        }
      }
      const existing = ciLiveResults.get(profileId) ?? {};
      existing[operatorId] = { label: liveOp.label, result: liveText, json: liveJson, ts: formatTimestampPST() };
      ciLiveResults.set(profileId, existing);
      const event = JSON.stringify({ operators: cfg.ciOperators, results: existing, nbr: nbrResults.get(profileId) ?? [] });
      const sseClients = ciSseClients.get(profileId);
      console.log(`[CI] SSE push profileId=${profileId} operatorId=${operatorId} label=${liveOp.label} clients=${sseClients?.size ?? 0}`);
      sseClients?.forEach(client => client.write(`data: ${event}\n\n`));
    }

    // ── Next Best Response operator ─────────────────────────────────────────
    if (cfg.ciNbrOperatorSid && operatorId === cfg.ciNbrOperatorSid && profileId && resultField) {
      console.log(`[CI][NBR] matched operatorId=${operatorId}`);
      console.log(`[CI][NBR] result (full): ${JSON.stringify(result, null, 2)}`);

      // Collect candidate objects: resultField, plus any parsed `payload` wrapper
      const candidates: Record<string, unknown>[] = [resultField];
      const p = resultField.payload
        ?? (resultField['com.twilio.cai.intelligence.JSONResult'] as Record<string, unknown> | undefined)?.payload;
      if (typeof p === 'string') {
        try { candidates.push(JSON.parse(p) as Record<string, unknown>); }
        catch (e) { console.warn(`[CI][NBR] payload JSON parse failed: ${e}`); }
      } else if (typeof p === 'object' && p !== null) {
        candidates.push(p as Record<string, unknown>);
      }

      // Find an array of pairs if present
      let rawItems: unknown[] = [];
      for (const c of candidates) {
        const arr = (c.interactions as unknown[]) ?? (c.pairs as unknown[]) ?? (c.responses as unknown[]);
        if (Array.isArray(arr) && arr.length > 0) { rawItems = arr; break; }
      }

      const nowTs = formatTimestampPST();
      const items: NbrPair[] = [];
      for (const it of rawItems as Record<string, unknown>[]) {
        const userText = String(
          it.userText ?? it.user_text ?? it.user ?? it.member ?? it.memberText ?? it.question ?? it.utterance ?? ''
        ).trim();
        const response = String(
          it.response ?? it.nextBestResponse ?? it.next_best_response ?? it.answer ?? it.suggestion ?? it.recommendation ?? ''
        ).trim();
        if (userText || response) items.push({ userText, response, ts: nowTs });
      }

      // Single-response fallback: operator emits `{response: "..."}` directly
      if (items.length === 0) {
        let single = '';
        for (const c of candidates) {
          single = String(
            c.response ?? c.nextBestResponse ?? c.next_best_response
            ?? c.answer ?? c.suggestion ?? c.recommendation ?? c.result ?? c.text ?? ''
          ).trim();
          if (single) break;
        }
        if (single) {
          // Resolve the member utterance that triggered this NBR run. CO is authoritative — CI
          // operates on what's in /Communications, which may include messages we never saw on
          // our transcript SSE (e.g. passive capture rules, injected Ask text). Fall back to
          // the local transcript store only if CO lookup fails.
          let userText = '';
          try {
            const commRes = await axios.get(
              `https://conversations.twilio.com/v2/Conversations/${convId}/Communications`,
              { auth: { username: cfg.apiKey, password: cfg.apiToken }, timeout: 5_000 },
            );
            const comms = ((commRes.data?.communications ?? []) as Array<{
              author?: { address?: string }; content?: { text?: string }; occurredAt?: string;
            }>);
            // Latest message from the CUSTOMER (not our Twilio number). CO already orders by
            // createdAt desc by default, but sort explicitly to be safe.
            const sorted = [...comms].sort((a, b) => (b.occurredAt ?? '').localeCompare(a.occurredAt ?? ''));
            const latestCustomer = sorted.find(c => (c.author?.address ?? '') !== cfg.phoneNumber && !!c.content?.text);
            userText = latestCustomer?.content?.text ?? '';
          } catch (e: unknown) {
            const ax = e as { response?: { status?: number } };
            console.warn(`[CI][NBR] CO lookup for userText failed status=${ax.response?.status} — falling back to local transcript`);
          }
          if (!userText) {
            const msgs = transcriptMessages.get(profileId) ?? [];
            const lastMember = [...msgs].reverse().find(m => m.role === 'member');
            userText = lastMember?.text ?? '';
          }
          items.push({ userText, response: single, ts: nowTs });
        }
      }

      if (items.length > 0) {
        // Personalized Ask fast path: this NBR result was triggered by a pending on-demand ask.
        // Push to the Ask SSE stream with the question we captured at trigger time.
        if (pendingAsk) {
          pendingNbrAsks.delete(convId);
          const bestResponse = items.map(it => it.response).find(r => !!r) ?? '';
          const askReply: AskReply = {
            question: pendingAsk.question,
            response: bestResponse,
            elapsedMs: Date.now() - pendingAsk.askedAt,
            ts: nowTs,
          };
          const history = askReplies.get(profileId) ?? [];
          history.push(askReply);
          askReplies.set(profileId, history);
          const askEvent = JSON.stringify(askReply);
          const askClients = askSseClients.get(profileId);
          console.log(`[CI][personalized-ask] fulfilled profileId=${profileId} convId=${convId} elapsed=${askReply.elapsedMs}ms clients=${askClients?.size ?? 0}`);
          askClients?.forEach(c => c.write(`data: ${askEvent}\n\n`));
          // Skip the normal Operator Results store/push for on-demand runs — they aren't
          // part of the live-call narrative, just a per-ask answer.
        } else {
          const existingNbr = nbrResults.get(profileId) ?? [];
          existingNbr.push(...items);
          nbrResults.set(profileId, existingNbr);
          console.log(`[CI][NBR] stored profileId=${profileId} +${items.length} total=${existingNbr.length}`);
          const nbrEvent = JSON.stringify({ operators: cfg.ciOperators, results: ciLiveResults.get(profileId) ?? {}, nbr: existingNbr });
          ciSseClients.get(profileId)?.forEach(client => client.write(`data: ${nbrEvent}\n\n`));
        }
      } else {
        console.log(`[CI][NBR] no pairs extracted — resultField keys=${Object.keys(resultField).join(',')}`);
      }
    }

    if (isSummaryOp && !summaryText) {
      if (outputFormat === 'TEXT') summaryText = (typeof resultRaw === 'string' ? resultRaw : (resultField?.result as string) ?? (resultField?.text as string)) ?? '';
      if (!summaryText) {
        const p = resultField?.payload
          ?? (resultField?.['com.twilio.cai.intelligence.JSONResult'] as Record<string,unknown> | undefined)?.payload;
        if (typeof p === 'string') {
          try { const parsed = JSON.parse(p); summaryText = parsed?.summary ?? parsed?.summaries?.[0]?.summary ?? parsed?.text ?? ''; }
          catch { summaryText = p; }
        } else if (typeof p === 'object' && p !== null) {
          const po = p as Record<string,unknown>;
          summaryText = (po.summary as string) ?? (po.text as string) ?? ((po.summaries as { summary: string }[])?.[0]?.summary) ?? '';
        }
      }
      if (!summaryText) summaryText = (typeof resultRaw === 'string' ? resultRaw : '') || (resultField?.result as string) ?? (resultField?.summary as string) ?? (resultField?.text as string) ?? '';
    }

    if (isOutreachOp && !outreachAnalysis) {
      let parsed: { interactions?: unknown[] } | null = null;
      if (Array.isArray((resultField as Record<string,unknown> | undefined)?.interactions)) {
        parsed = resultField as unknown as typeof parsed;
      } else {
        const p = resultField?.payload
          ?? (resultField?.['com.twilio.cai.intelligence.JSONResult'] as Record<string,unknown> | undefined)?.payload;
        if (typeof p === 'string') { try { parsed = JSON.parse(p); } catch { /* ignore */ } }
        else if (typeof p === 'object' && p !== null) parsed = p as unknown as typeof parsed;
      }
      if ((parsed?.interactions ?? []).length > 0) outreachAnalysis = JSON.stringify(parsed!.interactions);
    }
  }

  console.log(`[CI] extracted summaryText=${summaryText.slice(0,200) || '(empty)'} outreachAnalysis=${outreachAnalysis ? '(set)' : '(empty)'} profileId=${profileId ?? '(none)'}`);
  if (!profileId || (!summaryText && !outreachAnalysis)) return;
  // On-demand Personalized Ask runs shouldn't write Summary/Outreach back into the member profile.
  if (pendingAsk) return;

  const execDetails = (operatorResults[0] as Record<string,unknown> | undefined)?.executionDetails as Record<string,unknown> | undefined;
  const channels = (execDetails?.channels as string[] | undefined) ?? [];
  const ts = formatTimestampPST(((operatorResults[0] as Record<string,unknown> | undefined)?.dateCreated as string | undefined));
  const channel = channels[0] ?? 'voice';
  const pending = ciPendingTraits.get(profileId) ?? {};
  if (summaryText) pending.lastCallSummary = `[${channel}, ${ts}] ${summaryText.trim()}`;
  if (outreachAnalysis) pending.outreachResponses = `[${channel}, ${ts}]\n${outreachAnalysis}`;
  if (summaryText || outreachAnalysis) {
    let interactions: { flag?: string }[] = [];
    try { interactions = outreachAnalysis ? JSON.parse(outreachAnalysis) : []; } catch { /* ignore */ }
    pending.status = interactions.some(i => String(i.flag).toLowerCase() === 'yes') ? 'needs_followup' : 'completed';
  }
  ciPendingTraits.set(profileId, pending);
  scheduleCIFlush(profileId);
}

