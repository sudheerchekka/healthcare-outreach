/**
 * Adds an outbound VOICE capture rule to the Conversation Configuration.
 *
 * The default config only captures inbound calls (from=*, to=<twilio-number>).
 * Outbound calls need the reverse rule (from=<twilio-number>, to=*) so the
 * Orchestrator creates a conv_conversation_* for them and CI can write summaries.
 *
 * Safe to run multiple times — checks for the rule before adding it.
 *
 * Usage:
 *   npx ts-node scripts/add-outbound-capture-rule.ts
 */

import 'dotenv/config';
import axios from 'axios';

const API_KEY    = process.env.TWILIO_TAC_API_KEY ?? '';
const API_TOKEN  = process.env.TWILIO_TAC_API_TOKEN ?? '';
const CONV_CONFIG_ID = process.env.TWILIO_TAC_CONVERSATION_SERVICE_SID ?? process.env.TWILIO_TAC_CONVERSATION_CONFIGURATION_ID ?? '';
const PHONE_NUMBER   = process.env.TWILIO_TAC_PHONE_NUMBER ?? '';
const ORCH_BASE  = 'https://conversations.twilio.com/v2/ControlPlane';
const auth = { username: API_KEY, password: API_TOKEN };

async function main() {
  if (!API_KEY || !API_TOKEN || !CONV_CONFIG_ID || !PHONE_NUMBER) {
    console.error('Missing required env vars: TWILIO_TAC_API_KEY, TWILIO_TAC_API_TOKEN, TWILIO_TAC_CONVERSATION_CONFIGURATION_ID, TWILIO_TAC_PHONE_NUMBER');
    process.exit(1);
  }

  console.log(`Fetching configuration ${CONV_CONFIG_ID}…`);
  const { data: config } = await axios.get(
    `${ORCH_BASE}/Configurations/${CONV_CONFIG_ID}`,
    { auth },
  );

  const voiceSettings = config.channelSettings?.VOICE ?? {};
  const captureRules: { from: string; to: string; metadata: Record<string, string> }[] =
    voiceSettings.captureRules ?? [];

  console.log('Current VOICE capture rules:');
  captureRules.forEach(r => console.log(`  from=${r.from} to=${r.to}`));

  const outboundExists = captureRules.some(
    r => r.from === PHONE_NUMBER && r.to === '*',
  );

  if (outboundExists) {
    console.log(`\nOutbound rule (from=${PHONE_NUMBER}, to=*) already exists — nothing to do.`);
    return;
  }

  const updatedRules = [
    ...captureRules,
    { from: PHONE_NUMBER, to: '*', metadata: { callType: 'PSTN' } },
  ];

  console.log(`\nAdding outbound rule: from=${PHONE_NUMBER}, to=*`);

  const { data: updated } = await axios.put(
    `${ORCH_BASE}/Configurations/${CONV_CONFIG_ID}`,
    {
      displayName:                 config.displayName,
      conversationGroupingType:    config.conversationGroupingType,
      memoryExtractionEnabled:     config.memoryExtractionEnabled,
      memoryStoreId:               config.memoryStoreId,
      intelligenceConfigurationIds: config.intelligenceConfigurationIds,
      statusCallbacks:             config.statusCallbacks,
      channelSettings: {
        ...config.channelSettings,
        VOICE: {
          ...voiceSettings,
          captureRules: updatedRules,
        },
      },
    },
    { auth },
  );

  console.log('\nUpdated VOICE capture rules:');
  updated.channelSettings.VOICE.captureRules.forEach(
    (r: { from: string; to: string }) => console.log(`  from=${r.from} to=${r.to}`),
  );
  console.log(`\nConfiguration version: ${updated.version}`);
  console.log('Done.');
}

main().catch(e => {
  console.error('Error:', e.response?.data ?? e.message);
  process.exit(1);
});
