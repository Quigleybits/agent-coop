CREATE TABLE IF NOT EXISTS agents (
  name TEXT PRIMARY KEY,
  kind TEXT,
  provider TEXT,
  display_name TEXT,
  registered_at TEXT NOT NULL,
  last_seen_at TEXT
);

CREATE TABLE IF NOT EXISTS rooms (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  body TEXT,
  objective TEXT,
  scope TEXT,
  done_when TEXT,
  output_contract TEXT,
  context TEXT,
  allowed_actions TEXT,
  stop_conditions TEXT,
  status TEXT NOT NULL DEFAULT 'todo',
  owner_agent_id TEXT REFERENCES agents(name),
  next_actor_agent_id TEXT REFERENCES agents(name),
  preferred_resume_owner_agent_id TEXT REFERENCES agents(name),
  review_required INTEGER NOT NULL DEFAULT 1 CHECK (review_required IN (0, 1)),
  review_waiver_reason TEXT,
  contract_version INTEGER NOT NULL DEFAULT 1,
  created_by TEXT NOT NULL REFERENCES agents(name),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  answered_at TEXT,
  resume_grace_started_at TEXT,
  resume_grace_expires_at TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
  session_id TEXT PRIMARY KEY,
  agent_id TEXT NOT NULL REFERENCES agents(name),
  provider TEXT NOT NULL,
  status TEXT NOT NULL CHECK (
    status IN ('running', 'exited', 'cancelled', 'timed_out')
  ),
  command_json TEXT NOT NULL,
  working_directory TEXT NOT NULL,
  started_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  exited_at TEXT,
  exit_code INTEGER,
  termination_reason TEXT,
  max_runtime_seconds INTEGER NOT NULL,
  shutdown_grace_seconds INTEGER NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_sessions_one_running
ON sessions(agent_id) WHERE status='running';

CREATE TABLE IF NOT EXISTS claims (
  claim_id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id INTEGER NOT NULL REFERENCES items(id),
  claim_kind TEXT NOT NULL,
  subject_id INTEGER,
  lane_key TEXT NOT NULL,
  claimed_by_agent TEXT NOT NULL REFERENCES agents(name),
  owner_session_id TEXT NOT NULL REFERENCES sessions(session_id),
  status TEXT NOT NULL DEFAULT 'active' CHECK (
    status IN ('active', 'stale', 'released', 'completed', 'closed')
  ),
  intent_note TEXT NOT NULL,
  fencing_token INTEGER NOT NULL,
  claimed_at TEXT NOT NULL,
  last_renewed_at TEXT NOT NULL,
  lease_expires_at TEXT NOT NULL,
  last_checkpoint_at TEXT NOT NULL,
  closed_at TEXT,
  close_reason TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_claims_one_active_lane
ON claims(lane_key) WHERE status='active';

CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  room_id INTEGER REFERENCES rooms(id),
  item_id INTEGER REFERENCES items(id),
  from_agent TEXT NOT NULL REFERENCES agents(name),
  to_agent TEXT REFERENCES agents(name),
  kind TEXT NOT NULL DEFAULT 'chat',
  checkpoint TEXT,
  body TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_offsets (
  agent TEXT PRIMARY KEY REFERENCES agents(name),
  last_message_id INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS assignments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id INTEGER NOT NULL REFERENCES items(id),
  agent TEXT NOT NULL REFERENCES agents(name),
  agent_id TEXT REFERENCES agents(name),
  role TEXT NOT NULL DEFAULT 'owner',
  state TEXT NOT NULL DEFAULT 'active',
  assigned_by TEXT REFERENCES agents(name),
  claimed_at TEXT NOT NULL,
  assigned_at TEXT,
  released_at TEXT,
  release_reason TEXT,
  legacy INTEGER NOT NULL DEFAULT 0 CHECK (legacy IN (0, 1))
);

CREATE TABLE IF NOT EXISTS receipts (
  receipt_id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id INTEGER NOT NULL REFERENCES items(id),
  claim_id INTEGER NOT NULL REFERENCES claims(claim_id),
  fencing_token INTEGER NOT NULL,
  contract_version INTEGER NOT NULL,
  submitted_by_agent TEXT NOT NULL REFERENCES agents(name),
  submitted_by_session TEXT NOT NULL REFERENCES sessions(session_id),
  summary TEXT NOT NULL,
  proof TEXT NOT NULL,
  proof_references_json TEXT NOT NULL,
  source_path TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL,
  superseded_at TEXT
);

CREATE TABLE IF NOT EXISTS reviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id INTEGER NOT NULL REFERENCES items(id),
  receipt_id INTEGER REFERENCES receipts(receipt_id),
  contract_version INTEGER,
  requested_by TEXT NOT NULL REFERENCES agents(name),
  requested_by_agent TEXT REFERENCES agents(name),
  reviewer TEXT REFERENCES agents(name),
  reviewer_agent_id TEXT REFERENCES agents(name),
  status TEXT NOT NULL DEFAULT 'requested',
  body TEXT,
  verdict_body TEXT,
  legacy INTEGER NOT NULL DEFAULT 0 CHECK (legacy IN (0, 1)),
  created_at TEXT NOT NULL,
  resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS questions (
  question_id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id INTEGER NOT NULL REFERENCES items(id),
  exact_question TEXT NOT NULL,
  asked_by_agent TEXT NOT NULL REFERENCES agents(name),
  asked_by_session TEXT NOT NULL REFERENCES sessions(session_id),
  assigned_to_agent TEXT NOT NULL REFERENCES agents(name),
  status TEXT NOT NULL DEFAULT 'open' CHECK (
    status IN ('open', 'answered', 'withdrawn')
  ),
  answer TEXT,
  answered_by_agent TEXT REFERENCES agents(name),
  answered_by_session TEXT REFERENCES sessions(session_id),
  asked_at TEXT NOT NULL,
  answered_at TEXT,
  preferred_resume_owner_agent_id TEXT REFERENCES agents(name),
  resume_grace_expires_at TEXT
);

CREATE TABLE IF NOT EXISTS handoffs (
  handoff_id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id INTEGER NOT NULL REFERENCES items(id),
  claim_id INTEGER NOT NULL REFERENCES claims(claim_id),
  from_agent TEXT NOT NULL REFERENCES agents(name),
  from_session TEXT NOT NULL REFERENCES sessions(session_id),
  execution_fencing_token INTEGER NOT NULL,
  to_agent TEXT NOT NULL REFERENCES agents(name),
  reason TEXT NOT NULL,
  summary TEXT NOT NULL,
  completed_work TEXT NOT NULL,
  remaining_work TEXT NOT NULL,
  risks TEXT NOT NULL,
  proof_references TEXT NOT NULL,
  suggested_next_action TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (
    status IN ('pending', 'accepted', 'declined', 'withdrawn')
  ),
  created_at TEXT NOT NULL,
  resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS debates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id INTEGER REFERENCES items(id),
  topic TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  judge TEXT REFERENCES agents(name),
  created_by TEXT NOT NULL REFERENCES agents(name),
  created_at TEXT NOT NULL,
  closed_at TEXT
);

CREATE TABLE IF NOT EXISTS debate_posts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  debate_id INTEGER NOT NULL REFERENCES debates(id),
  round INTEGER NOT NULL DEFAULT 1,
  agent TEXT NOT NULL REFERENCES agents(name),
  body TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id INTEGER REFERENCES items(id),
  debate_id INTEGER REFERENCES debates(id),
  text TEXT NOT NULL,
  rationale TEXT,
  decided_by TEXT NOT NULL REFERENCES agents(name),
  decided_by_agent TEXT REFERENCES agents(name),
  decided_by_session TEXT REFERENCES sessions(session_id),
  claim_id INTEGER REFERENCES claims(claim_id),
  fencing_token INTEGER,
  legacy INTEGER NOT NULL DEFAULT 0 CHECK (legacy IN (0, 1)),
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id INTEGER REFERENCES items(id),
  event_type TEXT NOT NULL,
  actor_agent_id TEXT REFERENCES agents(name),
  actor_session_id TEXT REFERENCES sessions(session_id),
  claim_id INTEGER REFERENCES claims(claim_id),
  fencing_token INTEGER,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inbox_entries (
  inbox_entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
  recipient_agent_id TEXT NOT NULL REFERENCES agents(name),
  source_event_id INTEGER NOT NULL REFERENCES events(event_id),
  item_id INTEGER REFERENCES items(id),
  category TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inbox_offsets (
  agent_id TEXT PRIMARY KEY REFERENCES agents(name),
  last_consumed_entry_id INTEGER NOT NULL DEFAULT 0
);
