import 'dotenv/config';
import Fastify from 'fastify';
import fastifyStatic from '@fastify/static';
import fastifyFormBody from '@fastify/formbody';
import * as fs from 'fs';
import { loadAppConfigs } from './app-config';
import { registerAppRoutes, handleCiWebhook } from './apps/healthcare/server/routes';
import type { AppRouteState } from './apps/healthcare/server/routes';
import { normalizePhone } from './apps/healthcare/server/memory';
import { buildGreeting } from './prompts';
import type { OutboundContext } from './types';
import { Twilio } from 'twilio';

const TAC_PORT = parseInt(process.env.TAC_PORT ?? '8000', 10);
const APP_PORT = parseInt(process.env.APP_PORT ?? '8001', 10);

async function start(): Promise<void> {
  const fastify = Fastify({ logger: false });
  await fastify.register(fastifyFormBody);

  fastify.addHook('onSend', async (_req, reply) => {
    reply.header('Access-Control-Allow-Origin', '*');
    reply.header('Access-Control-Allow-Methods', 'GET,POST,OPTIONS,PATCH,DELETE');
    reply.header('Access-Control-Allow-Headers', 'Content-Type');
  });

  // Handle CORS preflight for all routes
  fastify.options('*', async (_req, reply) => {
    reply.header('Access-Control-Allow-Origin', '*');
    reply.header('Access-Control-Allow-Methods', 'GET,POST,OPTIONS,PATCH,DELETE');
    reply.header('Access-Control-Allow-Headers', 'Content-Type');
    reply.status(204).send();
  });

  const configs = loadAppConfigs();
  if (!configs.length) {
    console.error('[startup] no app configs found in src/apps/ — exiting');
    process.exit(1);
  }

  // Register per-app static + prefixed routes; collect state for shared routes
  const appStates = new Map<string, AppRouteState>();
  for (const cfg of configs) {
    if (fs.existsSync(cfg.clientDir)) {
      await fastify.register(fastifyStatic, {
        root: cfg.clientDir,
        prefix: `${cfg.routePrefix}/`,
        decorateReply: false,
        index: ['members.html'],
      });
    }
    const state = registerAppRoutes(fastify, cfg, TAC_PORT);
    appStates.set(cfg.id, state);
  }

  // ── Shared: POST /api/outbound-call ───────────────────────────────────────
  // TAC server calls this when schedule_call tool fires; also called by each app's UI.
  // The UI calls /{app}/api/outbound-call (per-app) but the TAC→Node IPC uses /api/outbound-call.
  fastify.post('/api/outbound-call', async (req, reply) => {
    const { name = 'Member', phone = '', goal = '', goalDesc = '', profileId = '', appId = '' } =
      req.body as Record<string, string>;

    // Resolve which app to use: explicit appId, or first app as fallback
    const state = appStates.get(appId) ?? appStates.values().next().value as AppRouteState;
    const { cfg, ciLiveResults, transcriptMessages } = state;

    const memberPhone = normalizePhone(phone);
    const dialTo = cfg.outboundCallTo || memberPhone;
    const greeting = buildGreeting(name, goal, goalDesc);

    if (profileId) {
      ciLiveResults.delete(profileId);
      transcriptMessages.delete(profileId);
    }

    const convId = `outbound-${memberPhone}-${Date.now()}`;
    const ctx: OutboundContext & { conv_id: string } = { conv_id: convId, name, goal, goalDesc, phone: memberPhone, greeting };

    try {
      await fetch(`http://localhost:${TAC_PORT}${cfg.tacRoutePrefix}/set-outbound-context`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(ctx),
      });
    } catch (e) {
      console.error(`[${cfg.id}] failed to set outbound context:`, e);
      return reply.status(500).send({ success: false, error: 'TAC server unreachable' });
    }

    const twilioClient = cfg.accountSid ? new Twilio(cfg.accountSid, cfg.authToken) : null;
    if (!twilioClient) return reply.status(500).send({ success: false, error: 'Twilio not configured' });

    const params = new URLSearchParams({ conv_id: convId });
    const twimlUrl = `https://${cfg.voiceDomain}${cfg.tacRoutePrefix}/twiml-outbound?${params}`;

    try {
      const call = await twilioClient.calls.create({ to: dialTo, from: cfg.phoneNumber, url: twimlUrl });
      console.log(`[${cfg.id}] outbound call initiated member=${name} callSid=${call.sid}`);
      reply.send({ success: true, call_sid: call.sid });
    } catch (e) {
      reply.status(500).send({ success: false, error: String(e) });
    }
  });

  // ── Shared: POST /transcript-event ───────────────────────────────────────
  fastify.post('/transcript-event', async (req, reply) => {
    const { profileId, role, text } = req.body as Record<string, string>;
    if (!profileId || !text) return reply.send({ success: false });
    const msg = { role, text, ts: new Date().toISOString() };
    // Fan out to all apps — the profileId uniquely identifies the member regardless of app
    for (const state of appStates.values()) {
      const msgs = state.transcriptMessages.get(profileId) ?? [];
      msgs.push(msg);
      state.transcriptMessages.set(profileId, msgs);
      state.transcriptSseClients.get(profileId)?.forEach(c => c.write(`data: ${JSON.stringify(msg)}\n\n`));
    }
    reply.send({ success: true });
  });

  // ── Shared: POST /ci-webhook ──────────────────────────────────────────────
  fastify.post('/ci-webhook', async (req, reply) => {
    const payload = req.body as Record<string, unknown>;
    // Try to match to an app by conv_id; fall back to running all apps
    const data = (payload.data ?? payload) as Record<string, unknown>;
    const convId: string = (data.conversationId as string) ?? (payload.conversationId as string) ?? '';

    // Resolve which app owns this conversation via TAC server
    let matchedAppId: string | null = null;
    if (convId) {
      try {
        const res = await fetch(`http://localhost:${TAC_PORT}/get-outbound-phone/${encodeURIComponent(convId)}`);
        const d = await res.json() as { phone?: string; profileId?: string; appId?: string };
        if (d.appId) matchedAppId = d.appId;
      } catch { /* ignore */ }
    }

    const targets = matchedAppId
      ? [appStates.get(matchedAppId)].filter(Boolean) as AppRouteState[]
      : [...appStates.values()];

    await Promise.all(targets.map(state => handleCiWebhook(payload, state)));
    reply.send({ success: true });
  });

  await fastify.listen({ port: APP_PORT, host: '0.0.0.0' });
  for (const cfg of configs) {
    console.log(`[app-server] ${cfg.displayName} → http://localhost:${APP_PORT}${cfg.routePrefix}/members.html`);
  }
}

start().catch((err) => {
  console.error('[startup] fatal error:', err);
  process.exit(1);
});
