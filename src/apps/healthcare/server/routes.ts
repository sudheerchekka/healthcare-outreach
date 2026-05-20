import 'dotenv/config';
import type { FastifyInstance } from 'fastify';
import axios from 'axios';
import { Twilio, jwt as twilioJwt } from 'twilio';
import * as path from 'path';
import { readFile, writeFile } from 'fs/promises';
import type { AppConfig } from '../../../app-config';
import type { MemberRow } from '../../../types';
import {
  normalizePhone,
  lookupProfileId,
  fetchProfile,
  updateProfileTraits,
  MEMORY_BASE,
} from './memory';
import type { MemoryCreds } from './memory';

const memoryAxios = axios.create();

export interface AppRouteState {
  cfg: AppConfig;
  creds: MemoryCreds;
  ciLiveResults: Map<string, Record<string, { label: string; result: string; json?: unknown; ts: string }>>;
  ciSseClients: Map<string, Set<import('http').ServerResponse>>;
  ciPendingTraits: Map<string, Record<string, unknown>>;
  ciFlushTimers: Map<string, ReturnType<typeof setTimeout>>;
  transcriptMessages: Map<string, { role: string; text: string; ts: string }[]>;
  transcriptSseClients: Map<string, Set<import('http').ServerResponse>>;
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
  const ciPendingTraits = new Map<string, Record<string, unknown>>();
  const ciFlushTimers   = new Map<string, ReturnType<typeof setTimeout>>();
  const ciLiveResults   = new Map<string, Record<string, { label: string; result: string; json?: unknown; ts: string }>>();
  const ciSseClients    = new Map<string, Set<import('http').ServerResponse>>();
  const transcriptMessages    = new Map<string, { role: string; text: string; ts: string }[]>();
  const transcriptSseClients  = new Map<string, Set<import('http').ServerResponse>>();

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
      try {
        const profile = await fetchProfile(profileId, creds);
        const existingOutreach = (profile?.traits?.outreach ?? {}) as Record<string, unknown>;
        await updateProfileTraits(profileId, 'outreach', { ...existingOutreach, ...traits }, creds);
        console.log(`[${cfg.id}][CI] flushed profileId=${profileId} traits=${Object.keys(traits).join(', ')}`);
      } catch (e) {
        console.error(`[${cfg.id}][CI] flush FAILED profileId=${profileId}:`, e);
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
      const profile = await fetchProfile(profile_id, creds);
      const existingOutreach = (profile?.traits?.outreach ?? {}) as Record<string, unknown>;
      await updateProfileTraits(profile_id, 'outreach', {
        ...existingOutreach, lastCallSummary: '', outreachResponses: '', status: 'pending',
      }, creds);
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
    reply.send({ operators: cfg.ciOperators, results: ciLiveResults.get(profileId) ?? {} });
  });

  app.delete(`${px}/api/ci-results/:profileId`, async (req, reply) => {
    const { profileId } = req.params as { profileId: string };
    ciLiveResults.delete(profileId);
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
    raw.write(`data: ${JSON.stringify({ operators: cfg.ciOperators, results: cached })}\n\n`);
    if (!ciSseClients.has(profileId)) ciSseClients.set(profileId, new Set());
    ciSseClients.get(profileId)!.add(raw);
    const ping = setInterval(() => raw.write(': ping\n\n'), 25000);
    req.raw.on('close', () => {
      clearInterval(ping);
      ciSseClients.get(profileId)?.delete(raw);
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
      const token = new AccessToken(cfg.accountSid, cfg.apiKey, cfg.apiToken, { identity: 'care-team-agent', ttl: 3600 });
      token.addGrant(new VoiceGrant({ incomingAllow: true }));
      reply.send({ token: token.toJwt() });
    } catch (e) {
      reply.status(500).send({ error: String(e) });
    }
  });

  app.post(`${px}/api/browser-call`, async (req, reply) => {
    const { phone = '', profileId = '', name = '' } = req.body as Record<string, string>;
    const dialTo = cfg.outboundCallTo || normalizePhone(phone);
    const memberPhone = normalizePhone(phone);
    if (!dialTo) return reply.status(400).send({ success: false, error: 'No destination number' });
    if (profileId) ciLiveResults.delete(profileId);
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
      reply.send({ success: true, message_sid: msg.sid });
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

  // ── CI webhook (shared — registered in index.ts at /ci-webhook) ──────────
  // Handler body is below but registered as a shared route via the returned state.
  // ── END per-app routes ────────────────────────────────────────────────────

  return {
    cfg, creds, ciLiveResults, ciSseClients, ciPendingTraits, ciFlushTimers,
    transcriptMessages, transcriptSseClients, formatTimestampPST, scheduleCIFlush, tacPort,
  };
}

// Exported so index.ts can build the shared /ci-webhook handler
export async function handleCiWebhook(
  payload: Record<string, unknown>,
  state: AppRouteState,
): Promise<void> {
  const { cfg, creds, ciLiveResults, ciSseClients, ciPendingTraits, scheduleCIFlush, formatTimestampPST } = state;
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

  if (!profileId && memberPhone) profileId = await lookupProfileId(memberPhone, creds);

  let summaryText = '';
  let outreachAnalysis = '';

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
    const resultField  = result.result as Record<string, unknown> | undefined;
    const isOutreachOp = cfg.ciOutreachOperatorSid && operatorId === cfg.ciOutreachOperatorSid;
    const isSummaryOp  = !isOutreachOp && (cfg.ciSummaryOperatorSid ? operatorId === cfg.ciSummaryOperatorSid : true);

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
      const event = JSON.stringify({ operators: cfg.ciOperators, results: existing });
      ciSseClients.get(profileId)?.forEach(client => client.write(`data: ${event}\n\n`));
    }

    if (isSummaryOp && !summaryText) {
      if (outputFormat === 'TEXT') summaryText = (resultField?.result as string) ?? '';
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
      if (!summaryText) summaryText = (resultField?.result as string) ?? (resultField?.summary as string) ?? '';
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

  if (!profileId || (!summaryText && !outreachAnalysis)) return;

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

