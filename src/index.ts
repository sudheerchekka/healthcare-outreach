import 'dotenv/config';
import { startHealthcareAppServer } from './apps/healthcare/server/api';

// TAC Server (port 8000) is now the Python server: src/tac/server.py
// Start it separately: uvicorn src.tac.server:app --port 8000
// (or via: cd src/tac && pip install -r requirements.txt && uvicorn server:app --port 8000)

startHealthcareAppServer().catch((err) => {
  console.error('[startup] fatal error:', err);
  process.exit(1);
});
