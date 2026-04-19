import axios from 'axios';
import { MemberProfile, TACMemoryResponse } from '../../../types';

const MEMORY_BASE    = 'https://memory.twilio.com';
const MEMORY_STORE_ID = process.env.MEMORY_STORE_ID ?? '';
const API_KEY        = process.env.TWILIO_API_KEY ?? '';
const API_TOKEN      = process.env.TWILIO_API_TOKEN ?? '';
const memoryAuth     = { username: API_KEY, password: API_TOKEN };

export function normalizePhone(phone: string): string {
  const digits = phone.replace(/\D/g, '');
  return digits.length === 10 ? `+1${digits}` : `+${digits}`;
}

export async function lookupProfileId(phone: string): Promise<string | null> {
  try {
    const res = await axios.post(
      `${MEMORY_BASE}/v1/Stores/${MEMORY_STORE_ID}/Profiles/Lookup`,
      { idType: 'phone', value: phone },
      { auth: memoryAuth },
    );
    return (res.data.profiles ?? [])[0] ?? null;
  } catch { return null; }
}

export async function fetchProfile(profileId: string): Promise<MemberProfile | null> {
  try {
    const res = await axios.get(
      `${MEMORY_BASE}/v1/Stores/${MEMORY_STORE_ID}/Profiles/${profileId}`,
      { params: { traitGroups: 'Contact,outreach' }, auth: memoryAuth },
    );
    return { id: profileId, traits: res.data.traits ?? {} };
  } catch { return null; }
}

export async function updateProfileTraits(
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

export async function retrieveMemory(profileId: string): Promise<TACMemoryResponse | null> {
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
      summaries:    (sumRes.data.summaries ?? []).map((s: { content: string }) => s.content),
    };
  } catch { return null; }
}

export { memoryAuth, MEMORY_BASE, MEMORY_STORE_ID };
