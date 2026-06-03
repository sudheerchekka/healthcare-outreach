import { FlexPlugin } from '@twilio/flex-plugin';
import { Tab, withTaskContext, Manager } from '@twilio/flex-ui';
import React from 'react';

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL || 'https://your-ngrok-domain.ngrok.io';
const PLUGIN_VERSION = '1.2.0';

// ── Design tokens ────────────────────────────────────────────────────────────

const T = {
  brand:    '#0263E0',
  brandBg:  '#EFF6FF',
  green:    '#16a34a',
  greenBg:  '#dcfce7',
  amber:    '#b45309',
  amberBg:  '#fef3c7',
  red:      '#dc2626',
  redBg:    '#fee2e2',
  slate:    '#64748b',
  slateBg:  '#f8fafc',
  border:   '#e2e8f0',
  text:     '#0f172a',
  textMid:  '#475569',
  textDim:  '#94a3b8',
  radius:   '10px',
  shadow:   '0 1px 4px rgba(0,0,0,0.07)',
};


// ── Adherence helpers ─────────────────────────────────────────────────────────

const ADHERENCE_DEFAULT = [
  { category_key: 'Greetings',                    criteria: [{ criteria_key: 'Required Phrase' }] },
  { category_key: 'Verification',                 criteria: [{ criteria_key: 'Goal' }, { criteria_key: 'Action' }, { criteria_key: 'Required Phrase' }] },
  { category_key: 'Symptom Deep-Dive',            criteria: [{ criteria_key: 'Goal' }, { criteria_key: 'Action' }] },
  { category_key: 'Red Flag Symptom (Call 911)',  criteria: [{ criteria_key: 'Goal' }, { criteria_key: 'Action' }] },
  { category_key: 'Empathy',                      criteria: [{ criteria_key: 'Goal' }, { criteria_key: 'Action' }] },
  { category_key: 'Wrap Up',                      criteria: [{ criteria_key: 'Goal' }, { criteria_key: 'Action' }] },
];

function extractAdherence(data) {
  const { operators = [], results = {} } = data;
  const op = operators.find(o => /adherence/i.test(o.label));
  if (!op) return null;
  return results[op.sid]?.json?.categories || null;
}

function buildCategories(cats) {
  const isMet = v => v === 'Passed' || v === 'Succeeded';
  const resultCats = cats || [];
  const resultKeys = new Set(resultCats.map(c => c.category_key));
  const missing = ADHERENCE_DEFAULT.filter(c => !resultKeys.has(c.category_key));
  return [...resultCats, ...missing].map(cat => {
    const allMet  = cats && cat.criteria?.every(c => isMet(c.criteria_met));
    const noneMet = cats && cat.criteria?.every(c => !isMet(c.criteria_met));
    const status  = !cats ? 'pending' : allMet ? 'met' : noneMet ? 'failed' : 'partial';
    return { cat, status };
  });
}

// ── Primitive components ──────────────────────────────────────────────────────

function Card({ children, style }) {
  return React.createElement('div', {
    style: {
      background: '#fff',
      border: `1px solid ${T.border}`,
      borderRadius: T.radius,
      boxShadow: T.shadow,
      overflow: 'hidden',
      marginBottom: '12px',
      ...style,
    }
  }, children);
}

function CardHeader({ icon, title, badge }) {
  return React.createElement('div', {
    style: {
      display: 'flex', alignItems: 'center', justifyContent: 'space-between',
      padding: '7px 12px',
      background: T.slateBg,
      borderBottom: `1px solid ${T.border}`,
    }
  },
    React.createElement('div', { style: { display: 'flex', alignItems: 'center', gap: '7px' } },
      React.createElement('span', { style: { fontSize: '14px' } }, icon),
      React.createElement('span', { style: { fontSize: '12px', fontWeight: 700, color: T.textMid, letterSpacing: '0.04em', textTransform: 'uppercase' } }, title),
    ),
    badge || null,
  );
}

function CardBody({ children }) {
  return React.createElement('div', {
    style: { padding: '0 12px 4px 12px' }
  }, children);
}

function Field({ label, value }) {
  if (!value) return null;
  return React.createElement('div', {
    style: { display: 'flex', alignItems: 'flex-start', gap: '8px', padding: '5px 0', borderBottom: `1px solid ${T.border}` }
  },
    React.createElement('div', {
      style: { flex: '0 0 38%', fontSize: '11px', fontWeight: 700, color: T.textMid, textTransform: 'capitalize', paddingTop: '1px', lineHeight: 1.3 }
    }, label),
    React.createElement('div', {
      style: { flex: 1, fontSize: '12px', color: T.text, lineHeight: 1.4, wordBreak: 'break-word' }
    }, value),
  );
}


function EmptyState({ icon, message }) {
  return React.createElement('div', {
    style: { padding: '14px', textAlign: 'center', color: T.textDim, fontSize: '12px' }
  },
    React.createElement('div', { style: { fontSize: '20px', marginBottom: '4px' } }, icon),
    message,
  );
}

function Spinner() {
  return React.createElement('div', {
    style: { padding: '32px', textAlign: 'center', color: T.textDim, fontSize: '12px' }
  }, 'Loading…');
}

// ── Member Profile (CRM panel) ────────────────────────────────────────────────

function MemberProfile({ task: taskProp }) {
  // CRMContainer doesn't inject task via withTaskContext — fall back to Flex store
  const task = taskProp || (() => {
    const store = Manager.getInstance().store.getState();
    const tasks = store?.flex?.worker?.tasks;
    if (!tasks) return null;
    const selected = store?.flex?.view?.selectedTaskSid;
    if (selected) {
      const t = tasks.get ? tasks.get(selected) : tasks[selected];
      if (t) return t;
    }
    const all = tasks.valueSeq ? tasks.valueSeq().toArray() : Object.values(tasks);
    return all[0] || null;
  })();
  console.log('[MemberProfile] render task=', task?.sid, 'attrs=', JSON.stringify(task?.attributes || {}));
  const memberProfileId = task?.attributes?.memberProfileId || '';
  const direction = task?.attributes?.direction || 'inbound';
  // Inbound: member is from/caller. Outbound: member is customerAddress/to (system is from).
  const customerPhone = direction === 'outbound'
    ? (task?.attributes?.customerAddress || task?.attributes?.to || task?.attributes?.called || '')
    : (task?.attributes?.from || task?.attributes?.customerAddress || task?.attributes?.caller || '');
  const [profileId, setProfileId] = React.useState('');
  const [traits,    setTraits]    = React.useState(null);
  const [memory,    setMemory]    = React.useState(null);
  const [loading,   setLoading]   = React.useState(true);
  const [error,     setError]     = React.useState(null);

  React.useEffect(() => {
    setLoading(true); setError(null); setTraits(null); setMemory(null);

    const load = async () => {
      let pid = memberProfileId;
      console.log('[MemberProfile] load start memberProfileId=', memberProfileId, 'customerPhone=', customerPhone);

      // Voice tasks don't include memberProfileId — look it up by phone
      if (!pid && customerPhone) {
        try {
          const url = `${BACKEND_URL}/healthcare/api/lookup-profile?phone=${encodeURIComponent(customerPhone)}`;
          console.log('[MemberProfile] fetching', url);
          const res = await fetch(url);
          const data = await res.json();
          console.log('[MemberProfile] lookup response', data);
          pid = data.profileId || '';
        } catch (e) {
          console.warn('[MemberProfile] lookup-profile failed:', e);
        }
      }

      console.log('[MemberProfile] resolved pid=', pid);
      if (!pid) { setLoading(false); return; }
      setProfileId(pid);

      const [t, m] = await Promise.all([
        fetch(`${BACKEND_URL}/healthcare/api/member-detail/${encodeURIComponent(pid)}/traits`).then(r => r.json()),
        fetch(`${BACKEND_URL}/healthcare/api/member-detail/${encodeURIComponent(pid)}`).then(r => r.json()),
      ]);
      setTraits(t);
      setMemory(m);
    };

    load().catch(e => setError(String(e))).finally(() => setLoading(false));
  }, [memberProfileId, customerPhone]);

  const wrap = { padding: '8px', fontFamily: "'Inter', 'Segoe UI', sans-serif", overflowY: 'auto', height: '100%', boxSizing: 'border-box', background: '#f1f5f9', minWidth: 0 };

  if (loading) return React.createElement('div', { style: wrap }, React.createElement(Spinner));
  if (error)   return React.createElement('div', { style: wrap },
    React.createElement(EmptyState, { icon: '⚠️', message: `Failed to load profile: ${error}` })
  );
  if (!profileId) return React.createElement('div', { style: wrap },
    React.createElement(EmptyState, { icon: '👤', message: 'No member profile found for this task.' })
  );
  if (!traits) return React.createElement('div', { style: wrap }, React.createElement(Spinner));

  const c = traits?.contact  || {};
  const o = traits?.outreach || {};
  const observations = memory?.observations || [];
  const summaries    = memory?.summaries    || [];
  const fullName = [c.firstName, c.lastName].filter(Boolean).join(' ') || task?.attributes?.name || task?.attributes?.customerAddress || 'Unknown Member';

  return React.createElement('div', { style: wrap },

    // ── Hero header ──────────────────────────────────────────────────────────
    React.createElement('div', {
      style: {
        background: `linear-gradient(135deg, ${T.brand} 0%, #1d4ed8 100%)`,
        borderRadius: T.radius,
        padding: '10px 12px',
        marginBottom: '8px',
        color: '#fff',
        boxShadow: '0 2px 8px rgba(2,99,224,0.3)',
      }
    },
      React.createElement('div', { style: { display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: '10px' } },
        React.createElement('div', null,
          React.createElement('div', { style: { fontSize: '17px', fontWeight: 700, marginBottom: '3px' } }, fullName),
          c.phone && React.createElement('div', { style: { fontSize: '12px', opacity: 0.85 } }, c.phone),
        ),
      ),
    ),

    // ── 2×2 responsive grid ───────────────────────────────────────────────────
    React.createElement('div', { style: { display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '8px', width: '100%', boxSizing: 'border-box' } },
      React.createElement(Card, { style: { marginBottom: 0, minWidth: 0, overflow: 'hidden' } },
        React.createElement(CardHeader, { icon: '👤', title: 'Contact' }),
        React.createElement(CardBody, null,
          React.createElement(Field, { label: 'First Name', value: c.firstName }),
          React.createElement(Field, { label: 'Last Name',  value: c.lastName }),
          React.createElement(Field, { label: 'Phone',      value: c.phone }),
          React.createElement(Field, { label: 'Member ID',  value: c.memberId }),
          !Object.values(c).some(Boolean) && React.createElement(EmptyState, { icon: '—', message: 'No contact info.' }),
        ),
      ),
      React.createElement(Card, { style: { marginBottom: 0, minWidth: 0, overflow: 'hidden' } },
        React.createElement(CardHeader, { icon: '📋', title: 'Outreach' }),
        React.createElement(CardBody, null,
          React.createElement(Field, { label: 'Status',            value: o.status }),
          React.createElement(Field, { label: 'Next Follow-Up',    value: o.nextFollowUp }),
          React.createElement(Field, { label: 'Follow-Up Details', value: o.nextFollowUpReason }),
          React.createElement(Field, { label: 'Last Call Summary', value: o.lastCallSummary }),
          React.createElement(Field, { label: 'Outreach Responses',value: o.outreachResponses }),
          !o.nextFollowUp && !o.lastCallSummary && !o.status &&
            React.createElement(EmptyState, { icon: '📭', message: 'No outreach data.' }),
        ),
      ),
      React.createElement(Card, { style: { marginBottom: 0, minWidth: 0, overflow: 'hidden' } },
        React.createElement(CardHeader, { icon: '🔍', title: 'Observations',
          badge: observations.length
            ? React.createElement('span', { style: { background: T.brandBg, color: T.brand, borderRadius: '9999px', padding: '1px 8px', fontSize: '11px', fontWeight: 700 } }, observations.length)
            : null
        }),
        observations.length === 0
          ? React.createElement(CardBody, null, React.createElement(EmptyState, { icon: '🔍', message: 'No observations.' }))
          : React.createElement('div', { style: { maxHeight: '160px', overflowY: 'auto' } },
              observations.map((obs, i) =>
                React.createElement('div', {
                  key: obs.sid || i,
                  style: { padding: '8px 10px', borderBottom: i < observations.length - 1 ? `1px solid ${T.border}` : 'none' }
                },
                  React.createElement('div', { style: { fontSize: '11px', color: T.text, lineHeight: 1.4, wordBreak: 'break-word' } }, obs.content || obs.text || String(obs)),
                  obs.createdAt && React.createElement('div', { style: { color: T.textDim, fontSize: '10px', marginTop: '2px' } },
                    new Date(obs.createdAt).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })
                  ),
                )
              )
            ),
      ),
      React.createElement(Card, { style: { marginBottom: 0, minWidth: 0, overflow: 'hidden' } },
        React.createElement(CardHeader, { icon: '📝', title: 'Summaries',
          badge: summaries.length
            ? React.createElement('span', { style: { background: T.brandBg, color: T.brand, borderRadius: '9999px', padding: '1px 8px', fontSize: '11px', fontWeight: 700 } }, summaries.length)
            : null
        }),
        summaries.length === 0
          ? React.createElement(CardBody, null, React.createElement(EmptyState, { icon: '💬', message: 'No summaries yet.' }))
          : React.createElement('div', { style: { maxHeight: '160px', overflowY: 'auto' } },
              summaries.map((sum, i) =>
                React.createElement('div', {
                  key: sum.sid || i,
                  style: { padding: '8px 10px', borderBottom: i < summaries.length - 1 ? `1px solid ${T.border}` : 'none' }
                },
                  React.createElement('div', { style: { fontSize: '11px', color: T.text, lineHeight: 1.4, wordBreak: 'break-word' } }, sum.content || sum.summary || sum.text || String(sum)),
                  sum.createdAt && React.createElement('div', { style: { color: T.textDim, fontSize: '10px', marginTop: '2px' } },
                    new Date(sum.createdAt).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })
                  ),
                )
              )
            ),
      ),
    ),

    // Footer
    React.createElement('div', { style: { textAlign: 'center', fontSize: '10px', color: T.textDim, paddingBottom: '8px' } },
      `v${PLUGIN_VERSION} · ${profileId}`
    ),
  );
}

// ── Script Adherence (Operators tab) ─────────────────────────────────────────

const ADHERENCE_STATUS_COLORS = { met: T.green, failed: T.red, partial: '#ea580c', pending: T.textDim };

function OperatorsTab({ task }) {
  const profileId = task?.attributes?.memberProfileId || '';
  const [adherence, setAdherence] = React.useState(undefined);

  React.useEffect(() => {
    if (!profileId) return;
    fetch(`${BACKEND_URL}/healthcare/api/ci-results/${encodeURIComponent(profileId)}`)
      .then(r => r.json())
      .then(data => setAdherence(extractAdherence(data)))
      .catch(() => setAdherence(null));
    const es = new EventSource(`${BACKEND_URL}/healthcare/api/ci-results/${encodeURIComponent(profileId)}/stream`);
    es.onmessage = e => { try { setAdherence(extractAdherence(JSON.parse(e.data))); } catch {} };
    return () => es.close();
  }, [profileId]);

  const cats = buildCategories(adherence === undefined ? null : adherence);

  return React.createElement('div', {
    style: { padding: '16px', fontFamily: "'Inter','Segoe UI',sans-serif", fontSize: '13px', overflowY: 'auto' }
  },
    React.createElement('h3', { style: { marginTop: 0, marginBottom: '12px', fontSize: '13px', fontWeight: 700, color: T.textMid, textTransform: 'uppercase', letterSpacing: '0.05em' } }, 'Script Adherence'),
    !profileId
      ? React.createElement('p', { style: { color: T.textDim } }, 'No profile ID in task attributes.')
      : cats.map(({ cat, status }) =>
          React.createElement('div', {
            key: cat.category_key,
            style: { marginBottom: '8px', display: 'flex', alignItems: 'center', gap: '8px', padding: '6px 8px', borderRadius: '6px', background: status === 'met' ? T.greenBg : T.slateBg }
          },
            React.createElement('input', { type: 'checkbox', checked: status === 'met', readOnly: true, style: { accentColor: T.green, width: '14px', height: '14px', flexShrink: 0 } }),
            React.createElement('span', { style: { color: ADHERENCE_STATUS_COLORS[status], fontWeight: status === 'met' ? 600 : 400, fontSize: '12px' } }, cat.category_key),
          )
        ),
    React.createElement('p', { style: { color: T.textDim, fontSize: '10px', marginTop: '16px' } }, `v${PLUGIN_VERSION} · ${profileId || 'no profile'}`),
  );
}

// ── Plugin ────────────────────────────────────────────────────────────────────

export default class HealthcareFlexPlugin extends FlexPlugin {
  constructor() {
    super('HealthcareFlexPlugin');
  }

  async init(flex, _manager) {
    console.log(`[HealthcareFlexPlugin] v${PLUGIN_VERSION} loaded — backend: ${BACKEND_URL}`);

    // Always show member profile — replace CRM container for all task types
    const MemberProfileWithContext = withTaskContext(MemberProfile);
    flex.CRMContainer.Content.replace(
      React.createElement(MemberProfileWithContext, { key: 'member-profile-crm' })
    );

    // Operators tab — all tasks
    const OperatorsTabWithContext = withTaskContext(OperatorsTab);
    flex.TaskCanvasTabs.Content.add(
      React.createElement(Tab, { key: 'operators-tab', label: 'Operators', uniqueName: 'operators-tab' },
        React.createElement(OperatorsTabWithContext, { key: 'operators-tab-content' })
      ),
      { sortOrder: 10 }
    );
  }
}
