/**
 * Close all ACTIVE/INACTIVE Conversation Orchestrator conversations.
 *
 * Usage:
 *   npx ts-node scripts/close-open-conversations.ts
 *   npx ts-node scripts/close-open-conversations.ts --dry-run
 */

import 'dotenv/config';

const ACCOUNT_SID = process.env.TWILIO_ACCOUNT_SID ?? '';
const AUTH_TOKEN  = process.env.TWILIO_AUTH_TOKEN  ?? '';
const CO_SID      = process.env.TWILIO_CONVERSATION_CONFIGURATION_ID ?? '';
const DRY_RUN     = process.argv.includes('--dry-run');

if (!ACCOUNT_SID || !AUTH_TOKEN || !CO_SID) {
  console.error('Missing: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_CONVERSATION_CONFIGURATION_ID');
  process.exit(1);
}

const auth = 'Basic ' + Buffer.from(`${ACCOUNT_SID}:${AUTH_TOKEN}`).toString('base64');
const BASE = 'https://conversations.twilio.com/v2';

async function listConversations(status: 'ACTIVE' | 'INACTIVE'): Promise<string[]> {
  const ids: string[] = [];
  let url: string | null = `${BASE}/Conversations?status=${status}&PageSize=100`;
  while (url) {
    const res = await fetch(url, { headers: { Authorization: auth } });
    const data = await res.json() as any;
    for (const c of data.conversations ?? []) ids.push(c.id);
    url = data.meta?.next_page_url ?? null;
  }
  return ids;
}

async function closeConversation(id: string): Promise<boolean> {
  const res = await fetch(`${BASE}/Conversations/${id}`, {
    method: 'PUT',
    headers: { Authorization: auth, 'Content-Type': 'application/json' },
    body: JSON.stringify({ status: 'CLOSED' }),
  });
  return res.ok;
}

async function main() {
  console.log(`\nCO: ${CO_SID}${DRY_RUN ? '  [DRY RUN]' : ''}\n`);

  const [active, inactive] = await Promise.all([
    listConversations('ACTIVE'),
    listConversations('INACTIVE'),
  ]);

  const all = [...new Set([...active, ...inactive])];
  console.log(`Found ${active.length} ACTIVE + ${inactive.length} INACTIVE = ${all.length} total\n`);

  if (all.length === 0) { console.log('Nothing to close.'); return; }

  let closed = 0, failed = 0;
  for (const id of all) {
    if (DRY_RUN) {
      console.log(`  [dry-run] would close ${id}`);
      closed++;
      continue;
    }
    const ok = await closeConversation(id);
    if (ok) { closed++; console.log(`  ✓ closed ${id}`); }
    else     { failed++; console.log(`  ✗ failed ${id}`); }
    // Small delay to avoid rate limits
    await new Promise(r => setTimeout(r, 100));
  }

  console.log(`\nDone: ${closed} closed, ${failed} failed.\n`);
}

main().catch(e => { console.error(e); process.exit(1); });
