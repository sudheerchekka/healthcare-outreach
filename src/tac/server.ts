import 'dotenv/config';
import * as http from 'http';
import Fastify from 'fastify';
import fastifyFormBody from '@fastify/formbody';
import { WebSocketServer } from 'ws';
import {
  TAC,
  TACConfig,
  TACMemoryResponse,
  VoiceChannel,
  ConversationSession,
  ConversationRelayCallbackPayloadSchema,
} from 'twilio-agent-connect';

export interface AgentCallbacks {
  onConversationSetup?(data: {
    callSid: string;
    from: string;
    to: string;
    customParameters?: Record<string, string>;
  }): void;
  onMessageReady(data: {
    conversationId: string;
    message: string;
    memory: TACMemoryResponse | undefined;
    session: ConversationSession;
  }): Promise<string>;
  onConversationEnded?(data: { conversationId: string }): void;
}

// Builds TwiML that connects the call to ConversationRelay.
function buildTwiML(
  wsUrl: string,
  callbackUrl: string,
  welcomeGreeting: string,
  convConfigId: string,
): string {
  const escaped = welcomeGreeting
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
  return `<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <ConversationRelay url="${wsUrl}" welcomeGreeting="${escaped}" dtmfDetection="true">
      <Parameter name="conversationConfigurationId" value="${convConfigId}"/>
    </ConversationRelay>
  </Connect>
  <Redirect>${callbackUrl}</Redirect>
</Response>`;
}

export async function startTACServer(
  callbacks: AgentCallbacks,
  getOutboundGreeting: (member: string, goal: string, desc: string) => string,
): Promise<void> {
  const tac = new TAC({ config: TACConfig.fromEnv() });
  // We manage memory retrieval ourselves in callbacks.ts (turn-1 cache pattern).
  // Override retrieveMemory so the SDK doesn't attempt its own profile lookup,
  // which fails for outbound calls where `from` is the Twilio number, not a member.
  (tac as any).retrieveMemory = async () => undefined;
  const voiceChannel = new VoiceChannel(tac);
  tac.registerChannel(voiceChannel);

  voiceChannel.on('setup', (data: { callSid: string; from: string; to: string; customParameters?: Record<string, string> }) => {
    callbacks.onConversationSetup?.(data);
  });

  // Suppress the noisy "Channel error" log when a member hangs up mid-response.
  // The SDK emits 'error' before our onMessageReady catch can intercept it.
  voiceChannel.on('error', ({ error }: { error: Error }) => {
    if (error.message.includes('No active WebSocket connection')) {
      console.log(`[tac-server] suppressed hangup error: ${error.message}`);
      return;
    }
    console.error(`[tac-server] channel error: ${error.message}`);
  });

  tac.onMessageReady(async ({ conversationId, message, memory, session }) => {
    const reply = await callbacks.onMessageReady({ conversationId, message, memory, session });
    try {
      await voiceChannel.sendResponse(conversationId, reply);
    } catch (e) {
      // Member hung up before the reply could be sent — not an error worth logging as ERROR
      if (e instanceof Error && e.message.includes('No active WebSocket connection')) {
        console.log(`[tac-server] call ended before reply delivered convId=${conversationId}`);
      } else {
        throw e;
      }
    }
  });

  tac.onInterrupt(async ({ conversationId }) => {
    try {
      await voiceChannel.sendResponse(conversationId, '');
    } catch (e) {
      if (!(e instanceof Error && e.message.includes('No active WebSocket connection'))) throw e;
    }
  });

  tac.onConversationEnded(async ({ session }) => {
    callbacks.onConversationEnded?.({ conversationId: session.conversationId });
  });

  const fastify = Fastify({ logger: false, serverFactory: (handler) => {
    const server = http.createServer(handler);
    const wss = new WebSocketServer({ server, path: '/ws' });
    wss.on('connection', (socket) => {
      voiceChannel.handleWebSocketConnection(socket);
    });
    return server;
  }});
  await fastify.register(fastifyFormBody);

  const VOICE_DOMAIN = (process.env.VOICE_PUBLIC_DOMAIN ?? '').replace(/^https?:\/\//, '');
  const CONV_CONFIG_ID = process.env.CONVERSATION_SERVICE_ID ?? '';

  function getUrls(req: { headers: Record<string, string | string[] | undefined> }) {
    const proto = (req.headers['x-forwarded-proto'] as string) ?? 'https';
    const host = (req.headers.host as string) ?? VOICE_DOMAIN;
    return {
      wsUrl: `${proto === 'https' ? 'wss' : 'ws'}://${host}/ws`,
      callbackUrl: `${proto}://${host}/conversation-relay-callback`,
    };
  }

  // Inbound calls — plain greeting
  fastify.post('/twiml', async (req, reply) => {
    const { wsUrl, callbackUrl } = getUrls(req as any);
    const greeting = 'Hello! This is the Owl Health Care Team. How can I assist you today?';
    reply.type('application/xml').send(buildTwiML(wsUrl, callbackUrl, greeting, CONV_CONFIG_ID));
  });

  // Outbound calls — personalized greeting from query params
  fastify.post('/twiml-outbound', async (req, reply) => {
    const query = req.query as Record<string, string>;
    const member = query.member ?? '';
    const goal   = query.goal ?? '';
    const desc   = query.desc ?? '';
    const { wsUrl, callbackUrl } = getUrls(req as any);
    const greeting = getOutboundGreeting(member, goal, desc);
    reply.type('application/xml').send(buildTwiML(wsUrl, callbackUrl, greeting, CONV_CONFIG_ID));
  });

  // CI webhook proxy — Twilio's statusCallback URL points to this server (port 8000 via ngrok),
  // but /ci-webhook is handled by the App Server (port 8001). Forward it there.
  fastify.post('/ci-webhook', async (req, reply) => {
    const appPort = parseInt(process.env.APP_PORT ?? '8001', 10);
    try {
      const res = await fetch(`http://localhost:${appPort}/ci-webhook`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(req.body),
      });
      const data = await res.json();
      reply.status(res.status).send(data);
    } catch (e) {
      console.error('[tac-server] ci-webhook proxy failed:', e);
      reply.status(500).send({ success: false });
    }
  });

  // ConversationRelay callback (call-end / status)
  fastify.post('/conversation-relay-callback', async (req, reply) => {
    const parsed = ConversationRelayCallbackPayloadSchema.safeParse(req.body);
    if (parsed.success) {
      await voiceChannel.handleConversationRelayCallback(parsed.data);
    } else {
      console.warn('[tac-server] relay-callback parse failed:', parsed.error.issues);
    }
    reply.send({ ok: true });
  });

  const port = parseInt(process.env.TAC_PORT ?? '8000', 10);
  console.log(`[tac-server] starting on port ${port}`);
  await fastify.listen({ port, host: '0.0.0.0' });
  console.log(`[tac-server] /twiml  /twiml-outbound  /ws  /conversation-relay-callback`);
}
