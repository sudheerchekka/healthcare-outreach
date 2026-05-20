import * as fs from 'fs';
import * as path from 'path';

export interface CiOperatorConfig {
  sid: string;
  label: string;
}

export interface AppConfig {
  id: string;
  displayName: string;
  routePrefix: string;        // e.g. /healthcare
  tacRoutePrefix: string;     // prefix used on TAC server for IPC
  accountSid: string;
  authToken: string;
  apiKey: string;
  apiToken: string;
  phoneNumber: string;
  memoryStoreId: string;
  outboundCallTo: string;
  voiceDomain: string;
  ciSummaryOperatorSid: string;
  ciOutreachOperatorSid: string;
  smsWriteObservation: boolean;
  ciOperators: CiOperatorConfig[];
  clientDir: string;          // absolute path to the client/ directory
}

interface RawAppJson {
  id: string;
  display_name: string;
  route_prefix: string;
  tac_route_prefix: string;
  account_sid_env: string;
  auth_token_env: string;
  api_key_env: string;
  api_token_env: string;
  phone_number_env: string;
  memory_store_env: string;
  outbound_call_to_env: string;
  voice_domain_env: string;
  ci_summary_operator_env: string;
  ci_outreach_operator_env: string;
  sms_write_observation_env: string;
  ci_operators: { sid_env: string; label_env: string; default_label: string }[];
}

export function loadAppConfigs(): AppConfig[] {
  const appsDir = path.join(__dirname, 'apps');
  const configs: AppConfig[] = [];

  for (const appId of fs.readdirSync(appsDir)) {
    const cfgFile = path.join(appsDir, appId, 'app.json');
    if (!fs.existsSync(cfgFile)) continue;
    try {
      const raw: RawAppJson = JSON.parse(fs.readFileSync(cfgFile, 'utf8'));
      const ev = (name: string, fallback = '') => process.env[name] ?? fallback;

      const ciOperators = (raw.ci_operators ?? [])
        .map(o => ({ sid: ev(o.sid_env), label: ev(o.label_env) || o.default_label }))
        .filter(o => o.sid);

      configs.push({
        id: raw.id,
        displayName: raw.display_name,
        routePrefix: raw.route_prefix.replace(/\/$/, ''),
        tacRoutePrefix: raw.tac_route_prefix.replace(/\/$/, ''),
        accountSid: ev(raw.account_sid_env),
        authToken: ev(raw.auth_token_env),
        apiKey: ev(raw.api_key_env),
        apiToken: ev(raw.api_token_env),
        phoneNumber: ev(raw.phone_number_env),
        memoryStoreId: ev(raw.memory_store_env),
        outboundCallTo: ev(raw.outbound_call_to_env),
        voiceDomain: ev(raw.voice_domain_env).replace(/^https?:\/\//, ''),
        ciSummaryOperatorSid: ev(raw.ci_summary_operator_env),
        ciOutreachOperatorSid: ev(raw.ci_outreach_operator_env),
        smsWriteObservation: ev(raw.sms_write_observation_env, 'false').toLowerCase() === 'true',
        ciOperators,
        clientDir: path.join(appsDir, appId, 'client'),
      });
      console.log(`[config] loaded app id=${raw.id} prefix=${raw.route_prefix}`);
    } catch (e) {
      console.error(`[config] failed to load ${cfgFile}:`, e);
    }
  }
  return configs;
}
