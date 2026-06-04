import { FlexPlugin } from '@twilio/flex-plugin';
import { Tab, withTaskContext, Manager } from '@twilio/flex-ui';
import React from 'react';

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL || 'https://your-ngrok-domain.ngrok.io';
const PLUGIN_VERSION = '1.3.0';

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
];

function extractSentiment(data) {
  const { operators = [], results = {} } = data;
  const op = operators.find(o => /sentiment/i.test(o.label));
  if (!op) return null;
  return results[op.sid]?.result || null;
}

const SENTIMENT_CFG = {
  positive: { icon: '😊', color: '#16a34a', bg: '#dcfce7', label: 'Positive' },
  negative: { icon: '😟', color: '#dc2626', bg: '#fee2e2', label: 'Negative' },
  neutral:  { icon: '😐', color: '#64748b', bg: '#f1f5f9', label: 'Neutral' },
  mixed:    { icon: '😕', color: '#b45309', bg: '#fef3c7', label: 'Mixed' },
};

function sentimentCfg(val) {
  const key = (val || '').toLowerCase().trim();
  return SENTIMENT_CFG[key] || { icon: '❓', color: '#64748b', bg: '#f1f5f9', label: val || 'Unknown' };
}

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

function traitLabel(key) {
  return key.replace(/([A-Z])/g, ' $1').replace(/^./, s => s.toUpperCase());
}

function TraitFields({ traits }) {
  const keys = Object.keys(traits || {}).filter(k => traits[k]);
  if (!keys.length) return React.createElement(EmptyState, { icon: '—', message: 'No data.' });
  return React.createElement(React.Fragment, null,
    ...keys.map(k => React.createElement(Field, { key: k, label: traitLabel(k), value: String(traits[k]) }))
  );
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

// ── Sentiment Card ────────────────────────────────────────────────────────────

function SentimentCard({ profileId }) {
  const [sentiment, setSentiment] = React.useState(null);

  React.useEffect(() => {
    if (!profileId) return;
    fetch(`${BACKEND_URL}/healthcare/api/ci-results/${encodeURIComponent(profileId)}`)
      .then(r => r.json())
      .then(data => setSentiment(extractSentiment(data)))
      .catch(() => {});
    const es = new EventSource(`${BACKEND_URL}/healthcare/api/ci-results/${encodeURIComponent(profileId)}/stream`);
    es.onmessage = e => {
      try {
        const s = extractSentiment(JSON.parse(e.data));
        if (s) setSentiment(s);
      } catch {}
    };
    return () => es.close();
  }, [profileId]);

  const sc = sentiment ? sentimentCfg(sentiment) : null;

  return React.createElement(Card, { style: { marginBottom: 0, minWidth: 0, overflow: 'hidden' } },
    React.createElement(CardHeader, { icon: '🎭', title: 'Sentiment' }),
    React.createElement(CardBody, null,
      !sc
        ? React.createElement(EmptyState, { icon: '⏳', message: 'Waiting for results…' })
        : React.createElement('div', {
            style: { display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', padding: '12px 0', gap: '6px' }
          },
            React.createElement('span', { style: { fontSize: '32px' } }, sc.icon),
            React.createElement('span', { style: { fontSize: '13px', fontWeight: 700, color: sc.color } }, sc.label),
            React.createElement('span', { style: { fontSize: '10px', color: T.textDim } }, 'Member Sentiment'),
          ),
    ),
  );
}

// ── Member Profile (CRM panel) ────────────────────────────────────────────────

const ADHERENCE_STATUS_COLORS = { met: T.green, failed: T.red, partial: '#ea580c', pending: T.textDim };

function AdherencePanel({ profileId, taskAccepted }) {
  const [adherence, setAdherence] = React.useState(undefined);

  React.useEffect(() => {
    console.log('[AdherencePanel] useEffect profileId=', profileId);
    if (!profileId) return;
    fetch(`${BACKEND_URL}/healthcare/api/ci-results/${encodeURIComponent(profileId)}`)
      .then(r => r.json())
      .then(data => setAdherence(extractAdherence(data)))
      .catch(() => setAdherence(null));
    const es = new EventSource(`${BACKEND_URL}/healthcare/api/ci-results/${encodeURIComponent(profileId)}/stream`);
    es.onmessage = e => {
      try {
        const parsed = JSON.parse(e.data);
        const incoming = extractAdherence(parsed);
        console.log('[AdherencePanel] SSE event operators=', parsed.operators?.length, 'adherence=', incoming);
        if (!incoming) return;
        // Merge: once a category is met, never downgrade it
        setAdherence(prev => {
          if (!prev) return incoming;
          const prevMap = {};
          prev.forEach(c => { prevMap[c.category_key] = c; });
          return incoming.map(c => {
            const isMet = v => v === 'Passed' || v === 'Succeeded';
            const prevCat = prevMap[c.category_key];
            const wasAlreadyMet = prevCat?.criteria?.every(cr => isMet(cr.criteria_met));
            if (wasAlreadyMet) return prevCat;
            return c;
          });
        });
      } catch {}
    };
    return () => es.close();
  }, [profileId]);

  // Show all pending until agent accepts the task
  const cats = buildCategories(taskAccepted ? (adherence === undefined ? null : adherence) : null);
  const total = cats.length;
  const metCount = cats.filter(c => c.status === 'met').length;

  return React.createElement('div', { style: { padding: '8px 0' } },
    React.createElement('div', {
      style: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '10px' }
    },
      React.createElement('span', { style: { fontSize: '11px', fontWeight: 700, color: T.textMid, textTransform: 'uppercase', letterSpacing: '0.04em' } }, 'Script Adherence'),
      adherence !== undefined && React.createElement('span', {
        style: { fontSize: '11px', fontWeight: 700, color: metCount === total ? T.green : T.brand }
      }, `${metCount}/${total}`),
    ),
    !profileId
      ? React.createElement('p', { style: { color: T.textDim, fontSize: '12px' } }, 'No profile ID in task attributes.')
      : cats.map(({ cat, status }) =>
          React.createElement('div', {
            key: cat.category_key,
            style: {
              marginBottom: '6px', display: 'flex', alignItems: 'center', gap: '8px',
              padding: '6px 10px', borderRadius: '6px',
              background: status === 'met' ? T.greenBg : T.slateBg,
              border: `1px solid ${status === 'met' ? '#bbf7d0' : T.border}`,
            }
          },
            React.createElement('input', { type: 'checkbox', checked: status === 'met', readOnly: true, style: { accentColor: T.green, width: '14px', height: '14px', flexShrink: 0 } }),
            React.createElement('span', { style: { color: ADHERENCE_STATUS_COLORS[status], fontWeight: status === 'met' ? 600 : 400, fontSize: '12px' } }, cat.category_key),
          )
        ),
  );
}

function MemberProfile({ task: taskProp }) {
  // CRMContainer doesn't inject task via withTaskContext — fall back to Flex store
  const task = taskProp || (() => {
    try {
      const store = Manager.getInstance().store.getState();
      const flexKeys = store?.flex ? Object.keys(store.flex) : [];
      const tasks = store?.flex?.worker?.tasks;
      const selected = store?.flex?.view?.selectedTaskSid;
      console.log('[MemberProfile] store flex keys=', flexKeys, 'selected=', selected, 'tasks type=', tasks ? (tasks.get ? 'ImmutableMap' : 'plain') : 'null');
      if (!tasks) return null;
      if (selected) {
        const t = tasks.get ? tasks.get(selected) : tasks[selected];
        if (t) return t;
      }
      const all = tasks.valueSeq ? tasks.valueSeq().toArray() : Object.values(tasks);
      console.log('[MemberProfile] fallback tasks count=', all.length);
      return all[0] || null;
    } catch (e) {
      console.error('[MemberProfile] store lookup error', e);
      return null;
    }
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

    // ── 3×2 responsive grid ───────────────────────────────────────────────────
    React.createElement('div', { style: { display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '8px', width: '100%', boxSizing: 'border-box' } },
      React.createElement(Card, { style: { marginBottom: 0, minWidth: 0, overflow: 'hidden' } },
        React.createElement(CardHeader, { icon: '👤', title: 'Contact' }),
        React.createElement(CardBody, null, React.createElement(TraitFields, { traits: c })),
      ),
      React.createElement(Card, { style: { marginBottom: 0, minWidth: 0, overflow: 'hidden' } },
        React.createElement(CardHeader, { icon: '📋', title: 'Care Plan' }),
        React.createElement(CardBody, null, React.createElement(TraitFields, { traits: o })),
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
        React.createElement(CardHeader, { icon: '📝', title: 'History',
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
      // Adherence card (5th card in the 3×2 grid)
      React.createElement(Card, { style: { marginBottom: 0, minWidth: 0, overflow: 'hidden' } },
        React.createElement(CardHeader, { icon: '✅', title: 'Script Adherence' }),
        React.createElement(CardBody, null,
          React.createElement(AdherencePanel, {
            profileId,
            taskAccepted: (() => { console.log('[taskAccepted] task.status=', task?.status, 'taskStatus=', task?.taskStatus, 'reservation.status=', task?.reservation?.status); return task?.status === 'accepted' || task?.taskStatus === 'accepted' || task?.reservation?.status === 'accepted'; })(),
          }),
        ),
      ),
      // Sentiment card (6th card in the 3×2 grid)
      React.createElement(SentimentCard, { profileId }),
    ),

    React.createElement('div', { style: { textAlign: 'center', fontSize: '10px', color: T.textDim, paddingBottom: '8px', marginTop: '4px' } },
      `v${PLUGIN_VERSION} · ${profileId}`
    ),
  );
}


// ── Transcript Tab ────────────────────────────────────────────────────────────

function TranscriptTab({ task }) {
  const profileId = task?.attributes?.memberProfileId || '';
  const [messages, setMessages] = React.useState([]);
  const bottomRef = React.useRef(null);

  React.useEffect(() => {
    if (!profileId) return;
    const es = new EventSource(`${BACKEND_URL}/healthcare/api/transcript/${encodeURIComponent(profileId)}/stream`);
    es.onmessage = e => {
      try {
        const msg = JSON.parse(e.data);
        if (msg.role && msg.text) setMessages(prev => [...prev, msg]);
      } catch {}
    };
    return () => es.close();
  }, [profileId]);

  React.useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  const wrap = { padding: '12px', fontFamily: "'Inter','Segoe UI',sans-serif", overflowY: 'auto', height: '100%', boxSizing: 'border-box', background: '#f8fafc' };

  if (!profileId) return React.createElement('div', { style: wrap },
    React.createElement('p', { style: { color: '#94a3b8', fontSize: '12px', textAlign: 'center', marginTop: '20px' } }, 'No profile ID in task attributes.')
  );

  return React.createElement('div', { style: wrap },
    messages.length === 0
      ? React.createElement('p', { style: { color: '#94a3b8', fontSize: '12px', textAlign: 'center', marginTop: '20px' } }, 'No transcript yet.')
      : messages.map((msg, i) =>
          React.createElement('div', {
            key: i,
            style: { marginBottom: '10px', display: 'flex', flexDirection: msg.role === 'agent' ? 'row' : 'row-reverse', gap: '8px', alignItems: 'flex-start' }
          },
            React.createElement('div', {
              style: {
                maxWidth: '80%', padding: '8px 12px', borderRadius: '10px',
                fontSize: '12px', lineHeight: 1.5,
                background: msg.role === 'agent' ? '#fff' : '#EFF6FF',
                color: '#0f172a', border: '1px solid #e2e8f0',
                boxShadow: '0 1px 2px rgba(0,0,0,0.05)',
              }
            },
              React.createElement('div', {
                style: { fontSize: '9px', fontWeight: 700, marginBottom: '3px', textTransform: 'uppercase', color: msg.role === 'agent' ? '#0263E0' : '#64748b' }
              }, msg.role === 'agent' ? '🤖 AI Agent' : '👤 Member'),
              msg.text,
              msg.ts && React.createElement('div', { style: { fontSize: '9px', color: '#94a3b8', marginTop: '3px' } }, msg.ts),
            )
          )
        ),
    React.createElement('div', { ref: bottomRef })
  );
}

// ── Plugin ────────────────────────────────────────────────────────────────────

export default class HealthcareFlexPlugin extends FlexPlugin {
  constructor() {
    super('HealthcareFlexPlugin');
  }

  async init(flex, _manager) {
    console.log(`[HealthcareFlexPlugin] v${PLUGIN_VERSION} loaded — backend: ${BACKEND_URL}`);

    // Member profile in CRM container
    const MemberProfileWithContext = withTaskContext(MemberProfile);
    flex.CRMContainer.Content.replace(
      React.createElement(MemberProfileWithContext, { key: 'member-profile-crm' })
    );

    // Transcript tab in TaskCanvasTabs
    const TranscriptTabWithContext = withTaskContext(TranscriptTab);
    flex.TaskCanvasTabs.Content.add(
      React.createElement(Tab, { key: 'transcript-tab', uniqueName: 'transcript-tab', label: 'Transcript' },
        React.createElement(TranscriptTabWithContext, { key: 'transcript-tab-content' })
      ),
      { sortOrder: 5 }
    );
  }
}
