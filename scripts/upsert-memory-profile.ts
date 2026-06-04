/**
 * Creates or updates a member profile in Twilio Conversation Memory
 * for a given phone number identity.
 *
 * - Looks up the profile by phone (creates one if not found)
 * - Upserts Contact and outreach trait groups
 *
 * Required env vars (loaded from .env):
 *   HEALTHCARE_MEMORY_STORE_ID  — Conversation Memory store SID
 *   TWILIO_API_KEY              — Twilio API key SID
 *   TWILIO_API_TOKEN            — Twilio API key secret
 *
 * Usage:
 *   npx ts-node scripts/upsert-memory-profile.ts [phone] [--dry-run]
 *
 * Examples:
 *   npx ts-node scripts/upsert-memory-profile.ts +1408xxxxxxxx
 *   npx ts-node scripts/upsert-memory-profile.ts              # uses MEMBER.phone below
 */

import 'dotenv/config';
import axios from 'axios';

const MEMORY_BASE = 'https://memory.twilio.com';
const STORE_ID   = process.env.MEMORY_STORE_ID;
const API_KEY    = process.env.TWILIO_API_KEY  ?? '';
const API_TOKEN  = process.env.TWILIO_API_TOKEN ?? '';
const DRY_RUN    = process.argv.includes('--dry-run');

if (!STORE_ID || !API_KEY || !API_TOKEN) {
  console.error('Missing required env vars: HEALTHCARE_MEMORY_STORE_ID, TWILIO_API_KEY, TWILIO_API_TOKEN');
  process.exit(1);
}

const auth = { username: API_KEY, password: API_TOKEN };

// ── Member data to upsert ─────────────────────────────────────────────────────
// Edit this block or pass phone as a CLI arg to target a different member.

const PHONE = process.argv[2];

const CONTACT_TRAITS: Record<string, string> = {
  firstName:  'James',
  lastName:   'Carter',
  phone:      PHONE,
  memberId:   'MBR-2025-001',
  email: 'james@gmailx.com',
  dateOfBirth: 'Jan 12, 1983',
  nextApptDate: 'June 15, 2026',
  nextApptTime: '11:00am',
};

const OUTREACH_TRAITS: Record<string, string> = {
  nextFollowUp:       'Medication Adherence Check',
  nextFollowUpReason: 'Verify patient is taking prescribed Metamorphin. Also ask if they observed any new symptoms',
  status:             'pending',
  lastCallSummary:    '',
  outreachResponses:  '',
};

// ── Helpers ───────────────────────────────────────────────────────────────────

async function lookupOrCreateProfile(phone: string): Promise<string> {
  // 1. Try lookup by phone
  try {
    const res = await axios.post(
      `${MEMORY_BASE}/v1/Stores/${STORE_ID}/Profiles/Lookup`,
      { idType: 'phone', value: phone },
      { auth },
    );
    const profiles: string[] = res.data.profiles ?? [];
    if (profiles.length > 0) {
      console.log(`  Found existing profile: ${profiles[0]}`);
      return profiles[0];
    }
  } catch (e: any) {
    if (e.response?.status !== 404) throw e;
  }

  // 2. Create new profile
  console.log('  No profile found — creating new profile...');
  const res = await axios.post(
    `${MEMORY_BASE}/v1/Stores/${STORE_ID}/Profiles`,
    { identities: [{ type: 'phone', value: phone }] },
    { auth },
  );
  const profileId = res.data.id ?? res.data.sid;
  console.log(`  Created profile: ${profileId}`);
  return profileId;
}

async function upsertTraits(profileId: string, group: string, traits: Record<string, string>) {
  if (DRY_RUN) {
    console.log(`  [dry-run] Would patch ${group}:`, traits);
    return;
  }
  await axios.patch(
    `${MEMORY_BASE}/v1/Stores/${STORE_ID}/Profiles/${profileId}`,
    { traits: { [group]: traits } },
    { auth },
  );
}

async function readBack(profileId: string) {
  const res = await axios.get(
    `${MEMORY_BASE}/v1/Stores/${STORE_ID}/Profiles/${profileId}`,
    { params: { traitGroups: 'Contact,outreach' }, auth },
  );
  return res.data.traits ?? {};
}

// ── Main ──────────────────────────────────────────────────────────────────────

async function main() {
  console.log(`\nUpserting Conversation Memory profile for phone: ${PHONE}`);
  console.log(`Store: ${STORE_ID}${DRY_RUN ? '  [DRY RUN]' : ''}\n`);

  // 1. Look up or create the profile
  console.log('1. Looking up profile...');
  const profileId = await lookupOrCreateProfile(PHONE);

  // 2. Upsert Contact traits
  console.log('\n2. Upserting Contact traits...');
  if (!DRY_RUN) console.log('  ', CONTACT_TRAITS);
  await upsertTraits(profileId, 'Contact', CONTACT_TRAITS);
  console.log('   ✓');

  // 3. Upsert outreach traits (merge with existing — read first to preserve fields)
  console.log('\n3. Reading existing outreach traits...');
  const existing = await readBack(profileId);
  const existingOutreach = (existing.outreach ?? {}) as Record<string, string>;
  const mergedOutreach = { ...existingOutreach, ...OUTREACH_TRAITS };
  // Remove empty strings that would overwrite real values
  Object.keys(mergedOutreach).forEach(k => {
    if (mergedOutreach[k] === '' && existingOutreach[k]) mergedOutreach[k] = existingOutreach[k];
  });

  console.log('   Existing outreach:', existingOutreach);
  console.log('   Merged outreach:  ', mergedOutreach);
  console.log('\n4. Upserting outreach traits...');
  await upsertTraits(profileId, 'outreach', mergedOutreach);
  console.log('   ✓');

  // 4. Read back to confirm
  console.log('\n5. Reading back final profile...');
  const final = await readBack(profileId);
  console.log('\n   Final traits:');
  console.log(JSON.stringify(final, null, 2));

  console.log(`\n✓ Done. Profile ID: ${profileId}\n`);
}

main().catch(e => {
  console.error('\n✗ Error:', e.response?.data ?? e.message);
  process.exit(1);
});
