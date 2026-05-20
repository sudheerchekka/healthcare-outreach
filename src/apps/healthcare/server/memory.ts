import axios from 'axios';
import { MemberProfile, TACMemoryResponse } from '../../../types';

export const MEMORY_BASE = 'https://memory.twilio.com';

const memoryAxios = axios.create();

// Legacy module-level values (healthcare defaults) kept for backwards compat
export const MEMORY_STORE_ID = process.env.HEALTHCARE_MEMORY_STORE_ID ?? process.env.MEMORY_STORE_ID ?? '';
const API_KEY   = process.env.TWILIO_API_KEY ?? '';
const API_TOKEN = process.env.TWILIO_API_TOKEN ?? '';
export const memoryAuth = { username: API_KEY, password: API_TOKEN };

export interface MemoryCreds {
  storeId: string;
  apiKey: string;
  apiToken: string;
}

export function normalizePhone(phone: string): string {
  const digits = phone.replace(/\D/g, '');
  return digits.length === 10 ? `+1${digits}` : `+${digits}`;
}

export async function lookupProfileId(phone: string, creds?: MemoryCreds): Promise<string | null> {
  const { storeId, apiKey, apiToken } = creds ?? { storeId: MEMORY_STORE_ID, apiKey: API_KEY, apiToken: API_TOKEN };
  try {
    const res = await memoryAxios.post(
      `${MEMORY_BASE}/v1/Stores/${storeId}/Profiles/Lookup`,
      { idType: 'phone', value: phone },
      { auth: { username: apiKey, password: apiToken } },
    );
    return (res.data.profiles ?? [])[0] ?? null;
  } catch { return null; }
}

export async function fetchProfile(profileId: string, creds?: MemoryCreds): Promise<MemberProfile | null> {
  const { storeId, apiKey, apiToken } = creds ?? { storeId: MEMORY_STORE_ID, apiKey: API_KEY, apiToken: API_TOKEN };
  try {
    const res = await memoryAxios.get(
      `${MEMORY_BASE}/v1/Stores/${storeId}/Profiles/${profileId}`,
      { params: { traitGroups: 'Contact,outreach' }, auth: { username: apiKey, password: apiToken } },
    );
    return { id: profileId, traits: res.data.traits ?? {} };
  } catch { return null; }
}

export async function updateProfileTraits(
  profileId: string,
  traitGroup: string,
  traits: Record<string, unknown>,
  creds?: MemoryCreds,
): Promise<void> {
  const { storeId, apiKey, apiToken } = creds ?? { storeId: MEMORY_STORE_ID, apiKey: API_KEY, apiToken: API_TOKEN };
  await memoryAxios.patch(
    `${MEMORY_BASE}/v1/Stores/${storeId}/Profiles/${profileId}`,
    { traits: { [traitGroup]: traits } },
    { auth: { username: apiKey, password: apiToken } },
  );
}

export async function retrieveMemory(profileId: string, creds?: MemoryCreds): Promise<TACMemoryResponse | null> {
  const { storeId, apiKey, apiToken } = creds ?? { storeId: MEMORY_STORE_ID, apiKey: API_KEY, apiToken: API_TOKEN };
  const auth = { username: apiKey, password: apiToken };
  try {
    const [obsRes, sumRes] = await Promise.all([
      memoryAxios.get(`${MEMORY_BASE}/v1/Stores/${storeId}/Profiles/${profileId}/Observations`, { auth }),
      memoryAxios.get(`${MEMORY_BASE}/v1/Stores/${storeId}/Profiles/${profileId}/ConversationSummaries`, { auth }),
    ]);
    return {
      observations: (obsRes.data.observations ?? []).map((o: { content: string }) => o.content),
      summaries:    (sumRes.data.summaries ?? []).map((s: { content: string }) => s.content),
    };
  } catch { return null; }
}
