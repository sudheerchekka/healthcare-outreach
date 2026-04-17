
/**
 * Owl Health Member Outreach — Node.js / TypeScript
 *
 * Rewrite of getting_started/examples/langchain/app.py using:
 *   - Express.js  (replaces FastAPI + Uvicorn)
 *   - AWS AgentCore InvokeInlineAgent  (replaces LangChain ChatBedrock)
 *   - Twilio APIs called directly via axios  (replaces TAC Python SDK)
 */

import 'dotenv/config';
import express, { Request, Response } from 'express';
import expressWs from 'express-ws';
import axios from 'axios';
import { Twilio } from 'twilio';
import * as path from 'path';
import * as ws from 'ws';

import { invokeAgent } from './agent';
import {
  buildGreeting,
  buildSystemPrompt,
  buildInboundSystemPrompt,
  buildMemoryContext,
} from './prompts';
import { MemberProfile, MemberRow, OutboundContext, TACMemoryResponse } from './types';

// ── Env ────────────────────────────────────────────────────────────────────
const ACCOUNT_SID = process.env.TWILIO_TAC_ACCOUNT_SID ?? '';
const AUTH_TOKEN = process.env.TWILIO_TAC_AUTH_TOKEN ?? '';
const API_KEY = process.env.TWILIO_TAC_API_KEY ?? '';
const API_TOKEN = process.env.TWILIO_TAC_API_TOKEN ?? '';
const PHONE_NUMBER = process.env.TWILIO_TAC_PHONE_NUMBER ?? '';
const VOICE_DOMAIN = process.env.TWILIO_TAC_VOICE_PUBLIC_DOMAIN ?? 'NOT_SET';
const CONV_CONFIG_ID = process.env.TWILIO_TAC_CONVERSATION_CONFIGURATION_ID ?? '';
const OUTBOUND_CALL_TO = process.env.TWILIO_TAC_OUTBOUND_CALL_TO ?? '';
const MEMORY_STORE_ID = process.env.TWILIO_TAC_MEMORY_STORE_ID ?? '';
const CI_SUMMARY_OPERATOR_SID = process.env.TWILIO_TAC_CI_SUMMARY_OPERATOR_SID ?? '';

const MEMORY_BASE = 'https://memory.twilio.com';
const ORCH_BASE = 'https://conversations.twilio.com/v2';

const twilioClient = new Twilio(ACCOUNT_SID, AUTH_TOKEN);
const memoryAuth = { username: API_KEY, password: API_TOKEN };

// ── In-memory state ────────────────────────────────────────────────────────
// AgentCore manages conversation history via sessionId — no history Map needed.
let lastOutboundContext: OutboundContext | null = null;
const outboundConversationMap = new Map<string, string>(); // convId → mock phone
const systemPromptCache = new Map<string, string>();       // convId → system prompt

// ── Helpers: Twilio Memory API ─────────────────────────────────────────────

function normalizePhone(phone: string): string {
  const digits = phone.replace(/\D/g, '');
  return digits.length === 10 ? `+1${digits}` : `+${digits}`;
}

async function lookupProfileId(phone: string): Promise<string | null> {
  try {
    const res = await axios.post(
      `${MEMORY_BASE}/v1/Stores/${MEMORY_STORE_ID}/Profiles/Lookup`,
      { idType: 'phone', value: phone },
      { auth: memoryAuth },
    );
    const profiles: string[] = res.data.profiles ?? [];
    return profiles[0] ?? null;
  } catch {
    return null;
  }
}

async function fetchProfile(profileId: string): Promise<MemberProfile | null> {
  try {
    const res = await axios.get(
      `${MEMORY_BASE}/v1/Stores/${MEMORY_STORE_ID}/Profiles/${profileId}`,
      { params: { traitGroups: 'Contact,outreach' }, auth: memoryAuth },
    );
    return { id: profileId, traits: res.data.traits ?? {} };
  } catch {
    return null;
  }
}

async function fetchProfileByPhone(phone: string): Promise<MemberProfile | null> {
  const profileId = await lookupProfileId(phone);
  if (!profileId) return null;
  return fetchProfile(profileId);
}

async function updateProfileTraits(
  profileId: string,
  traitGroup: string,
  traits: Record<string, unknown>,
): Promise<void> {
  await axios.patch(
    `${MEMORY_BASE}/v1/Stores/${MEMORY_STORE_ID}/Profiles/${profileId}`,
    { traits: { [traitGroup]: traits } },
    { auth: memoryAuth },
  );
}

async function retrieveMemory(profileId: string): Promise<TACMemoryResponse | null> {
  try {
    const [obsRes, sumRes] = await Promise.all([
      axios.get(
        `${MEMORY_BASE}/v1/Stores/${MEMORY_STORE_ID}/Profiles/${profileId}/Observations`,
        { auth: memoryAuth },
      ),
      axios.get(
        `${MEMORY_BASE}/v1/Stores/${MEMORY_STORE_ID}/Profiles/${profileId}/ConversationSummaries`,
        { auth: memoryAuth },
      ),
    ]);
    return {
      observations: (obsRes.data.observations ?? []).map((o: { content: string }) => o.content),
      summaries: (sumRes.data.summaries ?? []).map((s: { content: string }) => s.content),
    };
  } catch {
    return null;
  }
}

// ── Helpers: Twilio Orchestrator API ───────────────────────────────────────

async function lookupConversationId(callSid: string): Promise<string | null> {
  // Retry up to 5 times with 1s delay — ConversationRelay may not have created
  // the Sierra conversation by the time the first prompt arrives.
  for (let attempt = 1; attempt <= 5; attempt++) {
    try {
      const res = await axios.get(`${ORCH_BASE}/Conversations`, {
        params: { channelId: callSid, status: 'ACTIVE', configurationId: CONV_CONFIG_ID },
        auth: memoryAuth,
      });
      const conversations = res.data.conversations ?? [];
      console.log(`[orch] lookupConversationId attempt=${attempt} callSid=${callSid} found=${conversations.length} results`);
      if (conversations.length > 0) {
        console.log(`[orch] resolved conv_id=${conversations[0].id}`);
        return conversations[0].id;
      }
    } catch (e) {
      console.error(`[orch] lookupConversationId attempt=${attempt} error:`, e);
    }
    if (attempt < 5) await new Promise(r => setTimeout(r, 1000));
  }
  console.warn(`[orch] lookupConversationId failed after 5 attempts for callSid=${callSid} — falling back to callSid`);
  return null;
}

async function closeConversation(convId: string): Promise<void> {
  await axios.put(
    `${ORCH_BASE}/Conversations/${convId}`,
    { status: 'CLOSED' },
    { auth: memoryAuth },
  );
}

async function listActiveConversations(callSid: string): Promise<{ id: string; configurationId: string }[]> {
  try {
    const res = await axios.get(`${ORCH_BASE}/Conversations`, {
      params: { channelId: callSid, status: 'ACTIVE' },
      auth: memoryAuth,
    });
    return res.data.conversations ?? [];
  } catch (e) {
    console.error(`[relay-callback] listActiveConversations error:`, e);
    return [];
  }
}

// ── TwiML builder ──────────────────────────────────────────────────────────

function buildTwiML(greeting: string, wsUrl: string, callbackUrl: string): string {
  const escaped = greeting.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  return `<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <ConversationRelay url="${wsUrl}" welcomeGreeting="${escaped}" dtmfDetection="true">
      <Parameter name="conversationConfigurationId" value="${CONV_CONFIG_ID}"/>
    </ConversationRelay>
  </Connect>
  <Redirect>${callbackUrl}</Redirect>
</Response>`;
}

// ── Express app ────────────────────────────────────────────────────────────

const { app } = expressWs(express());
app.use(express.json());
app.use(express.urlencoded({ extended: false }));

// Serve healthcare dashboard HTML + SVGs
app.use(express.static(path.join(__dirname, '..', 'healthcare_outreach')));

// CORS for browser dashboard
app.use((_req, res, next) => {
  res.header('Access-Control-Allow-Origin', '*');
  res.header('Access-Control-Allow-Methods', 'GET,POST,OPTIONS');
  res.header('Access-Control-Allow-Headers', 'Content-Type');
  next();
});

// ── Voice: TwiML endpoints ─────────────────────────────────────────────────

/** Inbound calls — personalise greeting from caller's Memory profile. */
app.post('/twiml', async (req: Request, res: Response) => {
  const fromNumber: string = req.body.From ?? '';
  let greeting = "Hello! This is the Owl Health Care Team. How can I assist you today?";

  if (fromNumber) {
    try {
      const profile = await fetchProfileByPhone(fromNumber);
      if (profile?.traits) {
        const contact = profile.traits.Contact;
        const outreach = profile.traits.outreach;
        const firstName = contact?.firstName ?? '';
        const nextFollowUp = outreach?.nextFollowUp ?? '';
        const lastSummary = (outreach?.lastCallSummary ?? '').replace(/^\[.*?\]\s*/, '').slice(0, 120);
        const topic = nextFollowUp || lastSummary;

        if (firstName) {
          greeting = topic
            ? `Hi ${firstName}, this is the Owl Health Care Team. Are you calling about ${topic}?`
            : `Hi ${firstName}, this is the Owl Health Care Team. How can I assist you today?`;
        }
      }
    } catch (e) {
      console.warn(`[twiml] Profile lookup failed for ${fromNumber}:`, e);
    }
  }

  const wsUrl = `wss://${VOICE_DOMAIN}/ws`;
  const callbackUrl = `https://${VOICE_DOMAIN}/conversation-relay-callback`;
  res.type('text/xml').send(buildTwiML(greeting, wsUrl, callbackUrl));
});

/** Outbound calls — greeting and goal context passed as query params. */
app.post('/twiml-outbound', async (req: Request, res: Response) => {
  const memberName = (req.query.member as string) ?? '';
  const goal = (req.query.goal as string) ?? '';
  const goalDesc = (req.query.desc as string) ?? '';

  const greeting = memberName
    ? buildGreeting(memberName, goal, goalDesc)
    : 'Hello! How can I assist you today?';

  const wsUrl = `wss://${VOICE_DOMAIN}/ws`;
  const callbackUrl = `https://${VOICE_DOMAIN}/conversation-relay-callback`;
  res.type('text/xml').send(buildTwiML(greeting, wsUrl, callbackUrl));
});

/** ConversationRelay callback — close conversation when call ends. */
app.post('/conversation-relay-callback', async (req: Request, res: Response) => {
  const callSid: string = req.body.CallSid ?? '';
  const callStatus: string = req.body.CallStatus ?? '';
  console.log(`[relay-callback] CallSid=${callSid} Status=${callStatus}`);

  if (callStatus === 'completed') {
    console.log(`[relay-callback] looking up active conversations for callSid=${callSid} configId=${CONV_CONFIG_ID}`);
    console.log(`[relay-callback] GET ${ORCH_BASE}/Conversations?channelId=${callSid}&status=ACTIVE`);
    const conversations = await listActiveConversations(callSid);
    console.log(`[relay-callback] found ${conversations.length} active conversation(s): ${JSON.stringify(conversations.map(c => ({ id: c.id, configurationId: c.configurationId })))}`);
    for (const conv of conversations) {
      if (conv.configurationId !== CONV_CONFIG_ID) {
        console.log(`[relay-callback] skipping conv ${conv.id} — configId ${conv.configurationId} !== ${CONV_CONFIG_ID}`);
        continue;
      }
      try {
        await closeConversation(conv.id);
        console.log(`[relay-callback] Closed conversation ${conv.id}`);
      } catch (e) {
        console.error(`[relay-callback] Failed to close ${conv.id}:`, e);
      }
    }
    if (conversations.length === 0) {
      console.warn(`[relay-callback] No active conversations found — conversation may have already closed or channelId lookup failed`);
    }
  }
  res.sendStatus(200);
});

// ── Voice: ConversationRelay WebSocket ─────────────────────────────────────

app.ws('/ws', (socket: ws.WebSocket) => {
  let callSid = '';
  let fromNumber = '';
  let convId = '';
  let memberPhone = ''; // phone number of the member (not the Twilio number)

  socket.on('message', async (raw: ws.RawData) => {
    let data: Record<string, unknown>;
    try {
      data = JSON.parse(raw.toString());
    } catch {
      return;
    }

    const msgType = data.type as string;

    // ── setup: first message — capture call identifiers
    if (msgType === 'setup') {
      callSid = (data.callSid as string) ?? '';
      fromNumber = (data.from as string) ?? '';
      console.log(`[ws] setup callSid=${callSid} from=${fromNumber}`);
      return;
    }

    // ── prompt: user spoke (final transcription only)
    if (msgType === 'prompt') {
      console.log(`[ws] prompt received last=${data.last} voicePrompt="${data.voicePrompt}"`);
    }

    if (msgType === 'prompt' && data.last === true) {
      const userMessage = (data.voicePrompt as string) ?? '';
      console.log(`[ws] prompt final — userMessage="${userMessage}" callSid=${callSid} convId=${convId || '(not yet resolved)'}`);

      // First turn: resolve convId and build system prompt
      if (!convId) {
        console.log(`[ws] first turn — resolving convId from callSid=${callSid}`);
        const resolvedConvId = await lookupConversationId(callSid);
        if (!resolvedConvId) {
          console.error(`[ws] FATAL: could not resolve Sierra conv_id for callSid=${callSid} — aborting turn`);
          socket.send(JSON.stringify({ type: 'text', token: 'I apologize, I had trouble connecting. Please try again.', last: true }));
          return;
        }
        convId = resolvedConvId;
        console.log(`[ws] convId resolved to ${convId}`);

        const ctx = lastOutboundContext;
        lastOutboundContext = null;

        let systemPrompt: string;
        if (ctx) {
          // Outbound: member phone comes from the outbound context
          memberPhone = ctx.phone ?? '';
          systemPrompt = buildSystemPrompt(ctx.name, ctx.goal, ctx.goalDesc);
          if (ctx.phone) outboundConversationMap.set(convId, ctx.phone);
          console.log(`[ws] outbound context applied for ${ctx.name} memberPhone=${memberPhone}`);
        } else {
          // Inbound: member phone is the caller's From number
          memberPhone = fromNumber;
          console.log(`[ws] no outbound context — fetching inbound profile for memberPhone=${memberPhone}`);
          const profile = memberPhone ? await fetchProfileByPhone(memberPhone) : null;
          systemPrompt = buildInboundSystemPrompt(profile);
          console.log(`[ws] inbound context applied, profile=${profile?.id ?? 'none'}`);
        }
        console.log(`[ws] systemPrompt (first 120 chars): ${systemPrompt.slice(0, 120)}`);
        systemPromptCache.set(convId, systemPrompt);
      }

      // Retrieve TAC Conversation Memory for this profile
      console.log(`[ws] looking up profileId for memberPhone=${memberPhone}`);
      const profileId = memberPhone ? await lookupProfileId(memberPhone) : null;
      console.log(`[ws] profileId=${profileId ?? 'none'}`);
      const memory = profileId ? await retrieveMemory(profileId) : null;
      console.log(`[ws] memory=${memory ? `${memory.observations.length} observations, ${memory.summaries.length} summaries` : 'none'}`);
      const memoryContext = buildMemoryContext(memory);

      // Invoke AgentCore — sessionId = convId, no history array needed
      console.log(`[ws] invoking AgentCore sessionId=${convId} model=${process.env.BEDROCK_MODEL_ID ?? 'default'}`);
      try {
        const reply = await invokeAgent(
          convId,
          userMessage,
          systemPromptCache.get(convId) ?? '',
          memoryContext,
        );
        console.log(`[ws] agent reply (${reply.length} chars): ${reply.slice(0, 120)}…`);
        socket.send(JSON.stringify({ type: 'text', token: reply, last: true }));
        console.log(`[ws] reply sent to ConversationRelay`);
      } catch (e) {
        console.error('[ws] invokeAgent error:', e);
        socket.send(JSON.stringify({ type: 'text', token: 'I apologize, I encountered an issue. Please try again.', last: true }));
      }
    }

    // ── interrupt: user spoke while agent was responding
    if (msgType === 'interrupt') {
      console.log(`[ws] interrupt received for conv=${convId}`);
      // In-flight invokeAgent calls cannot be cancelled mid-stream with current SDK.
      // Send an empty last=true to acknowledge the interrupt.
      socket.send(JSON.stringify({ type: 'text', token: '', last: true }));
    }
  });

  socket.on('close', (code: number, reason: Buffer) => {
    console.log(`[ws] connection closed conv=${convId} callSid=${callSid} code=${code} reason=${reason.toString() || '(none)'}`);
    if (!convId) {
      console.warn(`[ws] convId was never set — agent was never invoked. Did the member speak before the call dropped?`);
    }
    systemPromptCache.delete(convId);
  });

  socket.on('error', (err: Error) => {
    console.error(`[ws] socket error conv=${convId} callSid=${callSid}:`, err.message);
  });
});

// ── CI Webhook ─────────────────────────────────────────────────────────────

/** Process Conversation Intelligence event and update mock member's profile. */
app.post('/ci-webhook', async (req: Request, res: Response) => {
  console.log(`\n${'='.repeat(60)}\n[CI WEBHOOK] HIT — ${new Date().toISOString()}\n${'='.repeat(60)}`);
  console.log(`[CI WEBHOOK] headers: content-type=${req.headers['content-type']} user-agent=${req.headers['user-agent']}`);
  console.log(`[CI WEBHOOK] event=${req.body?.event ?? req.body?.status ?? req.body?.EventType ?? '(no event field)'} convId=${req.body?.conversationId ?? req.body?.ConversationSid ?? '(none)'}`);
  console.log(`[CI WEBHOOK] top-level keys: ${JSON.stringify(Object.keys(req.body ?? {}))}`);
  console.log(`[CI WEBHOOK] full payload:\n${JSON.stringify(req.body, null, 2).slice(0, 2000)}`);

  const payload = req.body;
  const convId: string = payload.conversationId ?? '';
  console.log(`\n${'='.repeat(60)}\n[CI WEBHOOK] conv=${convId}\n${'='.repeat(60)}`);

  // Find the summary operator result
  const operatorResults: unknown[] = payload.operatorResults ?? [];
  console.log(`[CI] ${operatorResults.length} operator result(s) received`);
  operatorResults.forEach((r, i) => {
    const op = (r as Record<string, unknown>).operator as Record<string, unknown> | undefined;
    console.log(`[CI] operator[${i}] id=${op?.id} name=${op?.name}`);
  });

  for (const raw of operatorResults) {
    const result = raw as Record<string, unknown>;
    const operator = result.operator as Record<string, unknown> | undefined;
    // If no SID is configured, accept any operator result; otherwise match exactly.
    if (CI_SUMMARY_OPERATOR_SID && operator?.id !== CI_SUMMARY_OPERATOR_SID) {
      console.log(`[CI] skipping operator ${operator?.id} — does not match configured SID`);
      continue;
    }

    // Extract summary text from result.result field (Python SDK pattern).
    // outputFormat "JSON" → result.result.payload or result.result["com.twilio..."].payload
    // outputFormat "TEXT"/"GENERATION" → result.result.result
    const outputFormat = (result.outputFormat as string | undefined) ?? '';
    const resultField = result.result as Record<string, unknown> | undefined;
    console.log(`[CI] outputFormat=${outputFormat} result keys=${JSON.stringify(Object.keys(resultField ?? {}))}`);
    console.log(`[CI] result field (raw): ${JSON.stringify(resultField).slice(0, 400)}`);

    let summaryText = '';
    if (outputFormat === 'JSON' || !outputFormat) {
      // Try result.payload first, then nested Twilio key
      const payload = resultField?.payload
        ?? (resultField?.['com.twilio.cai.intelligence.JSONResult'] as Record<string, unknown> | undefined)?.payload;
      if (typeof payload === 'string') {
        try {
          const parsed = JSON.parse(payload);
          summaryText = parsed?.summary ?? parsed?.summaries?.[0]?.summary ?? parsed?.text ?? '';
        } catch {
          summaryText = payload;
        }
      } else if (typeof payload === 'object' && payload !== null) {
        const p = payload as Record<string, unknown>;
        summaryText = (p.summary as string)
          ?? (p.text as string)
          ?? ((p.summaries as { summary: string }[])?.[0]?.summary)
          ?? '';
      }
    }
    // Fallback: TEXT / GENERATION format, also try top-level text field
    if (!summaryText) {
      summaryText = (resultField?.result as string)
        ?? (resultField?.summary as string)
        ?? (resultField?.text as string)
        ?? '';
    }

    console.log(`[CI] summaryText (${summaryText.length} chars): "${summaryText.slice(0, 120)}"`);
    if (!summaryText) {
      console.warn(`[CI] summaryText is empty — skipping profile update`);
      continue;
    }

    // Extract customer profileId directly from executionDetails.participants
    const execDetails = result.executionDetails as Record<string, unknown> | undefined;
    const participants = (execDetails?.participants as { id: string; profileId?: string; type: string }[]) ?? [];
    console.log(`[CI] participants: ${JSON.stringify(participants)}`);
    const customerParticipant = participants.find(p => p.type === 'CUSTOMER');
    const profileIdFromPayload = customerParticipant?.profileId ?? null;
    console.log(`[CI] customer profileId from payload: ${profileIdFromPayload ?? 'NOT FOUND'}`);

    // Format timestamp in PST
    const ts = formatTimestampPST(result.dateCreated as string | undefined);
    const channels = (execDetails?.channels as string[] | undefined) ?? [];
    const channel = channels[0] ?? 'voice';
    const prefixed = `[${channel}, ${ts}] ${summaryText.trim()}`;

    // For outbound calls, OUTBOUND_CALL_TO may redirect the call to a test phone,
    // so the participant profile in the payload belongs to that test number, not the
    // intended member. Always prefer the outboundConversationMap (which holds the
    // actual member phone) and only fall back to the payload profileId for inbound.
    const memberPhone = outboundConversationMap.get(convId);
    console.log(`[CI] outboundConversationMap lookup convId=${convId}: ${memberPhone ?? 'NOT FOUND (inbound or map expired)'}`);

    if (memberPhone) {
      outboundConversationMap.delete(convId);
      console.log(`[CI] outbound call — looking up profile for member phone ${memberPhone}`);
      await syncSummaryToMemberProfile(memberPhone, prefixed);
    } else if (profileIdFromPayload) {
      // Inbound call — participant profileId from payload is correct
      console.log(`[CI] inbound call — updating profile ${profileIdFromPayload} from payload`);
      await updateProfileTraits(profileIdFromPayload, 'outreach', { lastCallSummary: prefixed });
      console.log(`[CI] lastCallSummary updated on profile ${profileIdFromPayload}`);
    } else {
      console.warn(`[CI] no outbound mapping and no profileId in payload — summary will not be written to profile`);
      console.warn(`[CI] Summary text was: "${prefixed.slice(0, 120)}"`);
    }
    break;
  }

  res.json({ success: true });
});

async function syncSummaryToMemberProfile(memberPhone: string, summary: string): Promise<void> {
  const profileId = await lookupProfileId(memberPhone);
  if (!profileId) {
    console.warn(`[CI] No profile found for mock phone ${memberPhone}`);
    return;
  }
  await updateProfileTraits(profileId, 'outreach', { lastCallSummary: summary });
  console.log(`[CI] lastCallSummary updated on profile ${profileId} (mock phone ${memberPhone})`);
}

function formatTimestampPST(dateStr: string | undefined): string {
  const date = dateStr ? new Date(dateStr) : new Date();
  return date.toLocaleString('en-US', {
    timeZone: 'America/Los_Angeles',
    month: 'short', day: '2-digit', year: 'numeric',
    hour: '2-digit', minute: '2-digit', hour12: true,
  }).replace(',', '');
}

// ── Dashboard API endpoints ────────────────────────────────────────────────

/** Return all member profiles from the Memory Store. */
app.get('/api/members', async (_req: Request, res: Response) => {
  try {
    const listRes = await axios.get(
      `${MEMORY_BASE}/v1/Stores/${MEMORY_STORE_ID}/Profiles`,
      { auth: memoryAuth },
    );
    const profileIds: string[] = listRes.data.profiles ?? [];

    const results = await Promise.all(
      profileIds.map(async (pid): Promise<MemberRow | null> => {
        try {
          const pRes = await axios.get(
            `${MEMORY_BASE}/v1/Stores/${MEMORY_STORE_ID}/Profiles/${pid}`,
            { params: { traitGroups: 'Contact,outreach' }, auth: memoryAuth },
          );
          const traits = pRes.data.traits ?? {};
          const contact = traits.Contact ?? {};
          const outreach = traits.outreach ?? {};

          if (!contact.memberId) return null;

          const first: string = contact.firstName ?? '';
          const last: string = contact.lastName ?? '';
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
        } catch {
          return null;
        }
      }),
    );

    const members = results.filter((m): m is MemberRow => m !== null);
    res.json({ members });
  } catch (e) {
    console.error('[api/members] error:', e);
    res.status(500).json({ members: [], error: String(e) });
  }
});

/** Initiate an outbound call to a member. */
app.post('/api/outbound-call', async (req: Request, res: Response) => {
  const { name = 'Member', phone = '', goal = '', goalDesc = '' } = req.body;
  const memberPhone = normalizePhone(phone);
  const dialTo = OUTBOUND_CALL_TO || memberPhone;

  lastOutboundContext = { name, goal, goalDesc, phone: memberPhone };

  const params = new URLSearchParams({ member: name, goal, desc: goalDesc });
  const twimlUrl = `https://${VOICE_DOMAIN}/twiml-outbound?${params}`;

  try {
    const call = await twilioClient.calls.create({
      to: dialTo,
      from: PHONE_NUMBER,
      url: twimlUrl,
    });
    console.log(`[outbound-call] initiated member=${name} callSid=${call.sid}`);
    res.json({ success: true, call_sid: call.sid });
  } catch (e) {
    console.error('[outbound-call] failed:', e);
    lastOutboundContext = null;
    res.status(500).json({ success: false, error: String(e) });
  }
});

/** Send an outreach SMS to a member (to OUTBOUND_CALL_TO override or member phone). */
app.post('/api/send-sms', async (req: Request, res: Response) => {
  const { name = 'Member', phone = '', goal = '', goalDesc = '' } = req.body;
  const sendTo = OUTBOUND_CALL_TO || normalizePhone(phone);

  if (!sendTo) {
    res.status(400).json({ success: false, error: 'No destination number configured' });
    return;
  }

  const lines = [`Hi ${name}, this is the Owl Health Care Team.`];
  if (goal) lines.push(`We're reaching out regarding: ${goal}.`);
  if (goalDesc) lines.push(goalDesc);
  lines.push('Please reply or call us if you have any questions.');
  const body = lines.join(' ');

  try {
    const msg = await twilioClient.messages.create({ to: sendTo, from: PHONE_NUMBER, body });
    console.log(`[send-sms] sent to ${sendTo} sid=${msg.sid}`);
    res.json({ success: true, message_sid: msg.sid });
  } catch (e) {
    console.error('[send-sms] failed:', e);
    res.status(500).json({ success: false, error: String(e) });
  }
});

/** Return the most recent call summary for a given phone number. */
app.get('/api/latest-summary', async (req: Request, res: Response) => {
  const phone = (req.query.phone as string) ?? '';
  try {
    const profileId = await lookupProfileId(phone);
    if (!profileId) return res.json({ summary: null });

    const sumRes = await axios.get(
      `${MEMORY_BASE}/v1/Stores/${MEMORY_STORE_ID}/Profiles/${profileId}/ConversationSummaries`,
      { auth: memoryAuth },
    );
    const summaries: { content: string; createdAt: string; conversationId?: string }[] =
      sumRes.data.summaries ?? [];
    summaries.sort((a, b) => new Date(b.createdAt).getTime() - new Date(a.createdAt).getTime());

    if (summaries.length === 0) return res.json({ summary: null });

    const latest = summaries[0];
    return res.json({
      summary: latest.content,
      conversation_id: latest.conversationId,
      created_at: latest.createdAt,
    });
  } catch (e) {
    console.error('[latest-summary] error:', e);
    return res.json({ summary: null, error: String(e) });
  }
});

// ── Start ──────────────────────────────────────────────────────────────────

const PORT = parseInt(process.env.PORT ?? '8000', 10);
app.listen(PORT, () => {
  console.log(`\nOwl Health Outreach Server (Node.js)\n${'─'.repeat(40)}`);
  console.log(`Listening on http://0.0.0.0:${PORT}`);
  console.log(`Dashboard:  http://localhost:${PORT}/members.html`);
  console.log(`Voice domain: ${VOICE_DOMAIN}`);
  if (VOICE_DOMAIN === 'NOT_SET') {
    console.warn('\nWARNING: TWILIO_TAC_VOICE_PUBLIC_DOMAIN is not set — voice calls will not work.');
  }
});
