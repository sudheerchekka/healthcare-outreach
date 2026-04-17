// ── Shared TypeScript interfaces ────────────────────────────────────────────

export interface ContactTraits {
  firstName?: string;
  lastName?: string;
  phone?: string;
  memberId?: string;
}

export interface OutreachTraits {
  nextFollowUp?: string;
  nextFollowUpReason?: string;
  status?: 'pending' | 'scheduled' | 'completed';
  lastCallSummary?: string;
}

export interface MemberProfile {
  id: string;
  traits: {
    Contact?: ContactTraits;
    outreach?: OutreachTraits;
  };
}

export interface TACMemoryResponse {
  observations: string[];
  summaries: string[];
}

export interface OutboundContext {
  name: string;
  goal: string;
  goalDesc: string;
  phone: string; // mock member phone (for CI webhook profile update)
}

export interface MemberRow {
  profile_id: string;
  name: string;
  initials: string;
  member_id: string;
  phone: string;
  next_follow_up: string;
  next_follow_up_reason: string;
  status: string;
  last_call_summary: string;
}
