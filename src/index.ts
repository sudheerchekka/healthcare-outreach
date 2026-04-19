import 'dotenv/config';
import { startTACServer } from './tac/server';
import { createHealthcareCallbacks } from './apps/healthcare/server/callbacks';
import { startHealthcareAppServer } from './apps/healthcare/server/api';
import { buildGreeting } from './prompts';

Promise.all([
  startTACServer(createHealthcareCallbacks(), buildGreeting),
  startHealthcareAppServer(),
]).catch((err) => {
  console.error('[startup] fatal error:', err);
  process.exit(1);
});
