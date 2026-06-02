import { FlexPlugin } from '@twilio/flex-plugin';
import { Tab, withTaskContext } from '@twilio/flex-ui';
import React from 'react';

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL || 'https://your-ngrok-domain.ngrok.io';
const PLUGIN_VERSION = '1.0.0';

const ADHERENCE_DEFAULT = [
  { category_key: 'Greetings',         criteria: [{ criteria_key: 'Required Phrase' }] },
  { category_key: 'Verification',      criteria: [{ criteria_key: 'Goal' }, { criteria_key: 'Action' }, { criteria_key: 'Required Phrase' }] },
  { category_key: 'Symptom Deep-Dive', criteria: [{ criteria_key: 'Goal' }, { criteria_key: 'Action' }] },
  { category_key: 'Red Flag Symptom (Call 911)', criteria: [{ criteria_key: 'Goal' }, { criteria_key: 'Action' }] },
  { category_key: 'Empathy',           criteria: [{ criteria_key: 'Goal' }, { criteria_key: 'Action' }] },
  { category_key: 'Wrap Up',           criteria: [{ criteria_key: 'Goal' }, { criteria_key: 'Action' }] },
];

const STATUS_COLORS = { met: '#16a34a', failed: '#dc2626', partial: '#ea580c', pending: '#9ca3af' };

function extractAdherence(data) {
  const { operators = [], results = {} } = data;
  const op = operators.find(o => /adherence/i.test(o.label));
  if (!op) return null;
  const r = results[op.sid];
  return r?.json?.categories || null;
}

function buildCategories(cats) {
  const isMet = v => v === 'Passed' || v === 'Succeeded';
  const resultCats = cats || [];
  const resultKeys = new Set(resultCats.map(c => c.category_key));
  const missing = ADHERENCE_DEFAULT.filter(c => !resultKeys.has(c.category_key));
  return [...resultCats, ...missing].map(cat => {
    const allMet = cats && cat.criteria?.every(c => isMet(c.criteria_met));
    const noneMet = cats && cat.criteria?.every(c => !isMet(c.criteria_met));
    const status = !cats ? 'pending' : allMet ? 'met' : noneMet ? 'failed' : 'partial';
    return { cat, status };
  });
}

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
    es.onmessage = (e) => {
      try { setAdherence(extractAdherence(JSON.parse(e.data))); } catch {}
    };
    return () => es.close();
  }, [profileId]);

  const cats = buildCategories(adherence === undefined ? null : adherence);

  return React.createElement('div', {
    style: { padding: '16px', fontFamily: 'sans-serif', fontSize: '13px', overflowY: 'auto' }
  },
    React.createElement('h3', { style: { marginTop: 0, marginBottom: '12px', fontSize: '14px' } }, 'Script Adherence'),
    !profileId
      ? React.createElement('p', { style: { color: '#888' } }, 'No profile ID in task attributes.')
      : cats.map(({ cat, status }) =>
          React.createElement('div', {
            key: cat.category_key,
            style: { marginBottom: '8px', display: 'flex', alignItems: 'center', gap: '8px' }
          },
            React.createElement('input', {
              type: 'checkbox',
              checked: status === 'met',
              readOnly: true,
              style: { accentColor: '#16a34a', width: '15px', height: '15px', flexShrink: 0 }
            }),
            React.createElement('span', {
              style: { color: STATUS_COLORS[status], fontWeight: status === 'met' ? 700 : 400 }
            }, cat.category_key)
          )
        ),
    React.createElement('p', { style: { color: '#aaa', fontSize: '11px', marginTop: '16px' } },
      `v${PLUGIN_VERSION} · ${profileId || 'no profile'}`)
  );
}

export default class HealthcareFlexPlugin extends FlexPlugin {
  constructor() {
    super('HealthcareFlexPlugin');
  }

  async init(flex, _manager) {
    console.log(`[HealthcareFlexPlugin] v${PLUGIN_VERSION} loaded — backend: ${BACKEND_URL}`);

    // Add Operators tab to TaskCanvasTabs (the Info/Activity area in Panel 2)
    const OperatorsTabWithContext = withTaskContext(OperatorsTab);

    flex.TaskCanvasTabs.Content.add(
      React.createElement(Tab, {
        key: 'operators-tab',
        label: 'Operators',
        uniqueName: 'operators-tab',
      }, React.createElement(OperatorsTabWithContext, { key: 'operators-tab-content' })),
      { sortOrder: 10 }
    );

  }
}
