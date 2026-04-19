/**
 * Grants AgentCore Runtime execution role access to the AgentCore Memory resource.
 *
 * The execution role created by `agentcore deploy` does not include memory permissions
 * by default. Without this, STM (short-term memory) fails with AccessDeniedException
 * on ListEvents — the agent responds but loses conversation history between turns.
 *
 * Run once after every fresh deploy into a new environment.
 * Safe to re-run — overwrites the inline policy with the same permissions.
 *
 * Usage:
 *   npx ts-node scripts/grant-agentcore-memory-permissions.ts
 *
 * Required .env vars (fill in after `agentcore deploy` + `agentcore status`):
 *   AGENTCORE_EXECUTION_ROLE_NAME  e.g. AmazonBedrockAgentCoreSDKRuntime-us-east-1-707172926d
 *   AGENTCORE_MEMORY_ARN           e.g. arn:aws:bedrock-agentcore:us-east-1:123456789:memory/xxx
 */

import 'dotenv/config';
import { IAMClient, PutRolePolicyCommand, GetRolePolicyCommand } from '@aws-sdk/client-iam';

const ROLE_NAME  = process.env.AGENTCORE_EXECUTION_ROLE_NAME ?? '';
const MEMORY_ARN = process.env.AGENTCORE_MEMORY_ARN ?? '';
const POLICY_NAME = 'BedrockAgentCoreMemoryAccess';

const iam = new IAMClient({ region: process.env.AWS_REGION ?? 'us-east-1' });

async function main() {
  if (!ROLE_NAME || !MEMORY_ARN) {
    console.error('Missing required env vars: AGENTCORE_EXECUTION_ROLE_NAME, AGENTCORE_MEMORY_ARN');
    console.error('Get these values from: agentcore status (run from healthcare_outreach/agents/outreachAgent)');
    process.exit(1);
  }

  console.log(`Role:       ${ROLE_NAME}`);
  console.log(`Memory ARN: ${MEMORY_ARN}`);
  console.log(`Policy:     ${POLICY_NAME}`);

  const policyDocument = {
    Version: '2012-10-17',
    Statement: [
      {
        Effect: 'Allow',
        Action: [
          'bedrock-agentcore:ListEvents',
          'bedrock-agentcore:CreateEvent',
          'bedrock-agentcore:GetEvent',
          'bedrock-agentcore:DeleteEvent',
          'bedrock-agentcore:ListMemoryRecords',
          'bedrock-agentcore:GetMemoryRecord',
          'bedrock-agentcore:CreateMemoryRecord',
          'bedrock-agentcore:DeleteMemoryRecord',
          'bedrock-agentcore:RetrieveMemoryRecords',
        ],
        Resource: MEMORY_ARN,
      },
    ],
  };

  await iam.send(new PutRolePolicyCommand({
    RoleName: ROLE_NAME,
    PolicyName: POLICY_NAME,
    PolicyDocument: JSON.stringify(policyDocument),
  }));

  // Verify
  const { PolicyDocument } = await iam.send(new GetRolePolicyCommand({
    RoleName: ROLE_NAME,
    PolicyName: POLICY_NAME,
  }));

  const doc = JSON.parse(decodeURIComponent(PolicyDocument ?? '{}'));
  const actions = doc?.Statement?.[0]?.Action ?? [];
  console.log(`\nPolicy attached — ${actions.length} actions granted on ${MEMORY_ARN}`);
  console.log('Done.');
}

main().catch(e => {
  console.error('Error:', e.message ?? e);
  process.exit(1);
});
