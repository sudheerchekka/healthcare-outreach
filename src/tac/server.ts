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

  tac.onMessageReady(async ({ conversationId, message, memory, session }) => {
    const reply = await callbacks.onMessageReady({ conversationId, message, memory, session });
    await voiceChannel.sendResponse(conversationId, reply);
  });

  tac.onInterrupt(async ({ conversationId }) => {
    await voiceChannel.sendResponse(conversationId, '');
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
