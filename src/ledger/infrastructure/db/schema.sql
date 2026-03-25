-- The Ledger Event Store Schema
-- Optimized for high-throughput appends and global position replay.

-- gen_random_uuid() is provided by pgcrypto (not uuid-ossp) in Postgres < 13.
-- In Postgres 13+ it is a built-in, but enabling pgcrypto is harmless and
-- ensures compatibility across all supported versions.
CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

CREATE TABLE IF NOT EXISTS events (  
  event_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),  
  stream_id        TEXT NOT NULL,  
  stream_position  BIGINT NOT NULL,  
  global_position  BIGINT GENERATED ALWAYS AS IDENTITY,  
  event_type       TEXT NOT NULL,  
  event_version    SMALLINT NOT NULL DEFAULT 1,  
  payload          JSONB NOT NULL,  
  metadata         JSONB NOT NULL DEFAULT '{}'::jsonb,  
  recorded_at      TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),  
  CONSTRAINT uq_stream_position UNIQUE (stream_id, stream_position)  
);

CREATE INDEX IF NOT EXISTS idx_events_stream_id ON events (stream_id, stream_position);  
CREATE INDEX IF NOT EXISTS idx_events_global_pos ON events (global_position);  
CREATE INDEX IF NOT EXISTS idx_events_type ON events (event_type);  
CREATE INDEX IF NOT EXISTS idx_events_recorded ON events (recorded_at);

CREATE TABLE IF NOT EXISTS event_streams (  
  stream_id        TEXT PRIMARY KEY,  
  aggregate_type   TEXT NOT NULL,  
  current_version  BIGINT NOT NULL DEFAULT 0,  
  created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),  
  archived_at      TIMESTAMPTZ,  
  metadata         JSONB NOT NULL DEFAULT '{}'::jsonb  
);

CREATE TABLE IF NOT EXISTS projection_checkpoints (  
  projection_name  TEXT PRIMARY KEY,  
  last_position    BIGINT NOT NULL DEFAULT 0,  
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()  
);

CREATE TABLE IF NOT EXISTS outbox (  
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),  
  event_id         UUID NOT NULL REFERENCES events(event_id),  
  destination      TEXT NOT NULL,  
  payload          JSONB NOT NULL,  
  created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),  
  published_at     TIMESTAMPTZ,  
  attempts         SMALLINT NOT NULL DEFAULT 0  
);

CREATE TABLE IF NOT EXISTS projection_application_summary (
  application_id         TEXT PRIMARY KEY,
  state                  TEXT NOT NULL,
  applicant_id           TEXT NOT NULL,
  requested_amount_usd   NUMERIC NOT NULL,
  approved_amount_usd    NUMERIC,
  risk_tier              TEXT,
  fraud_score            DECIMAL(5,2),
  compliance_status      TEXT,
  decision               TEXT,
  orchestrator_agent_id  TEXT,
  orchestrator_model_version TEXT,
  agent_sessions_completed TEXT[] DEFAULT '{}',
  last_event_type        TEXT NOT NULL,
  last_event_at          TIMESTAMPTZ NOT NULL,
  human_reviewer_id      TEXT,
  final_decision_at      TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS projection_agent_performance (
  agent_id               TEXT NOT NULL,
  model_version          TEXT NOT NULL,
  analyses_completed     INT DEFAULT 0,
  decisions_generated    INT DEFAULT 0,
  avg_confidence_score   NUMERIC DEFAULT 0,
  avg_duration_ms        NUMERIC DEFAULT 0,
  approve_rate           NUMERIC DEFAULT 0,
  decline_rate           NUMERIC DEFAULT 0,
  refer_rate             NUMERIC DEFAULT 0,
  human_reviews_total    INT DEFAULT 0,
  human_overrides_total  INT DEFAULT 0,
  human_override_rate    NUMERIC DEFAULT 0,
  first_seen_at          TIMESTAMPTZ NOT NULL,
  last_seen_at           TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (agent_id, model_version)
);

CREATE TABLE IF NOT EXISTS projection_compliance_history (
  application_id         TEXT NOT NULL,
  event_type             TEXT NOT NULL,
  payload                JSONB NOT NULL,
  global_position        BIGINT NOT NULL,
  recorded_at            TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (application_id, global_position)
);

CREATE TABLE IF NOT EXISTS projection_audit_trail (
  application_id         TEXT NOT NULL,
  event_type             TEXT NOT NULL,
  payload                JSONB NOT NULL,
  global_position        BIGINT NOT NULL,
  recorded_at            TIMESTAMPTZ NOT NULL,
  source_stream_id       TEXT NOT NULL,
  PRIMARY KEY (application_id, global_position)
);

CREATE TABLE IF NOT EXISTS projection_agent_sessions (
  session_id             TEXT PRIMARY KEY,
  agent_id               TEXT,
  model_version          TEXT,
  is_active              BOOLEAN NOT NULL DEFAULT TRUE,
  last_completed_action  TEXT,
  pending_work           TEXT,
  needs_reconciliation   BOOLEAN NOT NULL DEFAULT FALSE,
  total_events           INT NOT NULL DEFAULT 0,
  summary                JSONB NOT NULL DEFAULT '[]'::jsonb,
  recent_events          JSONB NOT NULL DEFAULT '[]'::jsonb,
  updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


CREATE TABLE IF NOT EXISTS projection_snapshots (
  projection_name        TEXT NOT NULL,
  entity_id              TEXT NOT NULL,
  global_position        BIGINT NOT NULL,
  state                  JSONB NOT NULL,
  created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (projection_name, entity_id, global_position)
);

CREATE INDEX IF NOT EXISTS idx_projection_snapshots_entity_pos ON projection_snapshots (projection_name, entity_id, global_position DESC);
CREATE INDEX IF NOT EXISTS idx_projection_snapshots_created ON projection_snapshots (projection_name, entity_id, created_at DESC);

-- Distributed daemon coordination tables
-- Each row represents one shard owned by one daemon node.
-- Advisory lock key = hashtext(projection_name || ':' || shard_id)
CREATE TABLE IF NOT EXISTS projection_shards (
  shard_id         TEXT NOT NULL,
  projection_name  TEXT NOT NULL,
  assigned_node    TEXT NOT NULL,          -- hostname / pod ID
  heartbeat_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  global_pos_from  BIGINT NOT NULL DEFAULT 0,
  global_pos_to    BIGINT,                 -- NULL = open-ended tail shard
  PRIMARY KEY (shard_id, projection_name)
);

CREATE INDEX IF NOT EXISTS idx_projection_shards_heartbeat
  ON projection_shards (projection_name, heartbeat_at);

-- Per-shard checkpoints (separate from the single-node projection_checkpoints table
-- so the existing daemon continues to work without any changes).
CREATE TABLE IF NOT EXISTS projection_shard_checkpoints (
  projection_name  TEXT NOT NULL,
  shard_id         TEXT NOT NULL,
  last_position    BIGINT NOT NULL DEFAULT 0,
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (projection_name, shard_id)
);
