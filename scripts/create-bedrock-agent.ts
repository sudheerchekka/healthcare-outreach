/**
 * Creates an AWS Bedrock persistent agent for healthcare-outreach.
 *
 * What it does:
 *   1. Creates (or reuses) an IAM role that Bedrock Agents can assume
 *   2. Creates the Bedrock agent with a general-purpose system prompt
 *   3. Prepares the agent (required before it can be invoked)
 *   4. Creates an alias pointing at the prepared version
 *   5. Prints BEDROCK_AGENT_ID and BEDROCK_AGENT_ALIAS_ID to copy into .env
 *
 * Usage:
 *   npx ts-node scripts/create-bedrock-agent.ts
 *
 * Run once. Subsequent runs detect the existing agent by name and skip creation.
 */

import 'dotenv/config';
import {
  BedrockAgentClient,
  CreateAgentCommand,
  PrepareAgentCommand,
  CreateAgentAliasCommand,
  ListAgentsCommand,
  ListAgentAliasesCommand,
  GetAgentCommand,
  AgentStatus,
} from '@aws-sdk/client-bedrock-agent';
import {
  IAMClient,
  CreateRoleCommand,
  AttachRolePolicyCommand,
  GetRoleCommand,
  PutRolePolicyCommand,
} from '@aws-sdk/client-iam';

const REGION      = process.env.AWS_REGION ?? 'us-east-1';
const MODEL_ID    = process.env.BEDROCK_MODEL_ID ?? 'us.anthropic.claude-opus-4-6-v1';
const AGENT_NAME  = 'healthcare-outreach';
const ALIAS_NAME  = 'v1';
const ROLE_NAME   = 'BedrockAgentRole-healthcare-outreach';

const bedrockAgent = new BedrockAgentClient({ region: REGION });
const iam          = new IAMClient({ region: REGION });

// General system prompt — per-call context is injected by the app at runtime via inputText.
const AGENT_INSTRUCTION = `You are an Owl Health care coordination agent. \
You handle both outbound and inbound member calls on behalf of the care team. \
Be warm, professional, and concise. \
When a call context is provided, use it to personalize your responses. \
Avoid asking for information you already have from the customer's profile or prior call summaries.`;

async function ensureIamRole(): Promise<string> {
  try {
    const { Role } = await iam.send(new GetRoleCommand({ RoleName: ROLE_NAME }));
    console.log(`  IAM role already exists: ${Role!.Arn}`);
    return Role!.Arn!;
  } catch (e: any) {
    if (e.name !== 'NoSuchEntityException') throw e;
  }

  console.log(`  Creating IAM role ${ROLE_NAME}…`);
  const trustPolicy = {
    Version: '2012-10-17',
    Statement: [
      {
        Effect: 'Allow',
        Principal: { Service: 'bedrock.amazonaws.com' },
        Action: 'sts:AssumeRole',
      },
    ],
  };

  const { Role } = await iam.send(new CreateRoleCommand({
    RoleName: ROLE_NAME,
    AssumeRolePolicyDocument: JSON.stringify(trustPolicy),
    Description: 'Allows Bedrock Agents to invoke foundation models',
  }));

  // Attach managed policy for Bedrock model invocation.
  await iam.send(new AttachRolePolicyCommand({
    RoleName: ROLE_NAME,
    PolicyArn: 'arn:aws:iam::aws:policy/AmazonBedrockFullAccess',
  }));

  // Inline policy scoped to the specific model (best practice).
  const inlinePolicy = {
    Version: '2012-10-17',
    Statement: [
      {
        Effect: 'Allow',
        Action: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
        Resource: `arn:aws:bedrock:${REGION}::foundation-model/*`,
      },
    ],
  };
  await iam.send(new PutRolePolicyCommand({
    RoleName: ROLE_NAME,
    PolicyName: 'BedrockInvokeModel',
    PolicyDocument: JSON.stringify(inlinePolicy),
  }));

  console.log(`  IAM role created: ${Role!.Arn}`);

  // IAM propagation delay — Bedrock rejects the role if we move too fast.
  console.log('  Waiting 10 s for IAM role to propagate…');
  await new Promise(r => setTimeout(r, 10_000));

  return Role!.Arn!;
}

async function findExistingAgent(): Promise<string | null> {
  let nextToken: string | undefined;
  do {
    const { agentSummaries, nextToken: nt } = await bedrockAgent.send(
      new ListAgentsCommand({ maxResults: 100, nextToken }),
    );
    const match = agentSummaries?.find(a => a.agentName === AGENT_NAME);
    if (match) return match.agentId!;
    nextToken = nt;
  } while (nextToken);
  return null;
}

async function findExistingAlias(agentId: string): Promise<string | null> {
  let nextToken: string | undefined;
  do {
    const { agentAliasSummaries, nextToken: nt } = await bedrockAgent.send(
      new ListAgentAliasesCommand({ agentId, maxResults: 100, nextToken }),
    );
    const match = agentAliasSummaries?.find(a => a.agentAliasName === ALIAS_NAME);
    if (match) return match.agentAliasId!;
    nextToken = nt;
  } while (nextToken);
  return null;
}

async function waitForAgentPrepared(agentId: string): Promise<void> {
  for (let i = 0; i < 30; i++) {
    const { agent } = await bedrockAgent.send(new GetAgentCommand({ agentId }));
    const status = agent?.agentStatus;
    if (status === AgentStatus.PREPARED) return;
    if (status === AgentStatus.FAILED) throw new Error(`Agent preparation failed (status=${status})`);
    console.log(`  Agent status: ${status} — waiting…`);
    await new Promise(r => setTimeout(r, 3_000));
  }
  throw new Error('Timed out waiting for agent to reach PREPARED status');
}

async function main() {
  console.log(`\nCreating Bedrock persistent agent: ${AGENT_NAME}`);
  console.log(`Region: ${REGION}  Model: ${MODEL_ID}\n`);

  // Step 1 — IAM role
  console.log('Step 1: IAM role');
  const roleArn = await ensureIamRole();

  // Step 2 — Agent
  console.log('\nStep 2: Bedrock agent');
  let agentId = await findExistingAgent();

  if (agentId) {
    console.log(`  Agent "${AGENT_NAME}" already exists: ${agentId}`);
  } else {
    console.log(`  Creating agent "${AGENT_NAME}"…`);
    const { agent } = await bedrockAgent.send(new CreateAgentCommand({
      agentName:        AGENT_NAME,
      foundationModel:  MODEL_ID,
      instruction:      AGENT_INSTRUCTION,
      agentResourceRoleArn: roleArn,
      description:      'Owl Health care coordination agent (healthcare-outreach-node)',
      idleSessionTTLInSeconds: 1800,
    }));
    agentId = agent!.agentId!;
    console.log(`  Agent created: ${agentId}`);
  }

  // Step 3 — Prepare (required before alias creation / invocation)
  console.log('\nStep 3: Prepare agent');
  await bedrockAgent.send(new PrepareAgentCommand({ agentId }));
  await waitForAgentPrepared(agentId);
  console.log('  Agent is PREPARED');

  // Step 4 — Alias
  console.log('\nStep 4: Agent alias');
  let agentAliasId = await findExistingAlias(agentId);

  if (agentAliasId) {
    console.log(`  Alias "${ALIAS_NAME}" already exists: ${agentAliasId}`);
  } else {
    console.log(`  Creating alias "${ALIAS_NAME}"…`);
    const { agentAlias } = await bedrockAgent.send(new CreateAgentAliasCommand({
      agentId,
      agentAliasName: ALIAS_NAME,
      description:    'Initial version',
    }));
    agentAliasId = agentAlias!.agentAliasId!;
    console.log(`  Alias created: ${agentAliasId}`);
  }

  // Done — print env vars
  console.log(`
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Add these to your .env to use persistent mode:

BEDROCK_AGENT_MODE=persistent
BEDROCK_AGENT_ID=${agentId}
BEDROCK_AGENT_ALIAS_ID=${agentAliasId}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
`);
}

main().catch(e => {
  console.error('Error:', e.message ?? e);
  process.exit(1);
});
