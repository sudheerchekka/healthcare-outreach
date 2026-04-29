/**
 * Creates or updates a member profile in Segment using the Profile API.
 *
 * Uses Linda Thompson as mock data. The script:
 *   1. Identifies (or creates) the profile via a TRACK event using the Analytics.js HTTP API
 *   2. Sets traits via an IDENTIFY call
 *   3. Reads the profile back via the Profile API to confirm
 *
 * Required env vars:
 *   SEGMENT_SPACE_ID      — your Segment Space ID (e.g. spc_xxx)
 *   SEGMENT_ACCESS_TOKEN  — Personal Access Token with Profile API read access
 *   SEGMENT_WRITE_KEY     — Source write key for identify/track calls
 *
 * Usage:
 *   npx ts-node scripts/upsert-segment-profile.ts
 */

import 'dotenv/config';

const SPACE_ID     = process.env.SEGMENT_SPACE_ID ?? '';
const ACCESS_TOKEN = process.env.SEGMENT_ACCESS_TOKEN ?? '';
const WRITE_KEY    = process.env.SEGMENT_WRITE_KEY ?? '';

if (!SPACE_ID || !ACCESS_TOKEN || !WRITE_KEY) {
  console.error('Missing required env vars: SEGMENT_SPACE_ID, SEGMENT_ACCESS_TOKEN, SEGMENT_WRITE_KEY');
  process.exit(1);
}

// ── Mock member data ──────────────────────────────────────────────────────────

const MEMBER = {
  userId:    '46782345',
  anonymousId: 'anon-linda-thompson-001',
  traits: {
    first_name:    'Linda',
    last_name:     'Thompson',
    name:         'Linda Thompson',
    email:        'linda.thompson@example.com',
    phone:        '+16465550278',
    // Outreach traits
    nextFollowUp:       'Wellness Check',
    nextFollowUpReason: 'Linda is due for her annual physical. Confirm appointment scheduled for next month.',
    status:             'pending',
    lastCallSummary:    'Member confirmed she received the appointment reminder. She plans to attend.',
    address: '123 Main St. San Francisco CA 95104',
    email_opt_in: 'false',
    sms_opt_in: 'true',
    insurance: 'Medicare',
    member_id: 'BAFD7783456',
    last_annual_appt: 'Jan 04, 2026',
    next_appt: 'May 15, 2026',
    profile_image: 'https://profile-images-7609.twil.io/thompson-profile.jpg',
    customer_since: 'July 05 2025'

  },
};

// ── Helpers ───────────────────────────────────────────────────────────────────

const b64 = (s: string) => Buffer.from(s).toString('base64');

async function segmentHTTP(path: string, body: object): Promise<{ status: number; json: unknown }> {
  const url = `https://api.segment.io${path}`;
  const res = await fetch(url, {
    method:  'POST',
    headers: {
      Authorization: `Basic ${b64(WRITE_KEY + ':')}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(body),
  });
  let json: unknown;
  try { json = await res.json(); } catch { json = null; }
  return { status: res.status, json };
}

async function profileAPI(path: string): Promise<{ status: number; json: unknown }> {
  const url = `https://profiles.segment.com/v1/spaces/${SPACE_ID}${path}`;
  const res = await fetch(url, {
    headers: {
      Authorization: `Basic ${b64(ACCESS_TOKEN + ':')}`,
      'Content-Type': 'application/json',
    },
  });
  let json: unknown;
  try { json = await res.json(); } catch { json = null; }
  return { status: res.status, json };
}

// ── Main ──────────────────────────────────────────────────────────────────────

async function main() {
  console.log(`\nUpserting Segment profile for ${MEMBER.traits.name} (${MEMBER.userId})\n`);

  // 1. IDENTIFY — creates/updates the profile with traits
  console.log('1. Sending identify call...');
  const identifyRes = await segmentHTTP('/v1/identify', {
    userId:      MEMBER.userId,
    anonymousId: MEMBER.anonymousId,
    traits:      MEMBER.traits,
    context: {
      app: { name: 'healthcare-outreach-node' },
    },
    timestamp: new Date().toISOString(),
  });
  console.log(`   Status: ${identifyRes.status}`, identifyRes.status === 200 ? '✓' : '✗');
  if (identifyRes.status !== 200) {
    console.error('   Response:', JSON.stringify(identifyRes.json, null, 2));
  }

  // 2. TRACK — records an event to confirm the profile is active
  console.log('\n2. Sending track call (Profile Created / Updated)...');
  const trackRes = await segmentHTTP('/v1/track', {
    userId:      MEMBER.userId,
    anonymousId: MEMBER.anonymousId,
    event:       'Profile Upserted',
    properties: {
      source: 'healthcare-outreach-node',
      reason: 'demo-setup',
    },
    timestamp: new Date().toISOString(),
  });
  console.log(`   Status: ${trackRes.status}`, trackRes.status === 200 ? '✓' : '✗');

  // 3. READ BACK — verify via Profile API (may take a few seconds to process)
  console.log('\n3. Reading profile back via Profile API...');
  console.log('   (Waiting 3s for Segment to process events...)');
  await new Promise(r => setTimeout(r, 3000));

  const profileRes = await profileAPI(`/collections/users/profiles/user_id:${MEMBER.userId}/traits`);
  console.log(`   Status: ${profileRes.status}`, profileRes.status === 200 ? '✓' : '✗');
  if (profileRes.status === 200) {
    console.log('\n   Profile traits:');
    console.log(JSON.stringify(profileRes.json, null, 2));
  } else {
    console.error('   Response:', JSON.stringify(profileRes.json, null, 2));
    console.log('\n   Note: Profile API may take longer to reflect new profiles.');
    console.log(`   You can check manually: GET https://profiles.segment.com/v1/spaces/${SPACE_ID}/collections/users/profiles/user_id:${MEMBER.userId}/traits`);
  }

  console.log('\nDone.\n');
}

main().catch(e => { console.error(e); process.exit(1); });
