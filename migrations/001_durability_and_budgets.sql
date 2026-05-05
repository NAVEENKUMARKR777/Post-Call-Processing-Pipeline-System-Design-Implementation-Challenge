-- Migration 001: Durability, per-customer budgets, audit log, token ledger.
--
-- Adds the four tables that turn the post-call pipeline from a
-- Redis-only fire-and-forget design into one with persistent state and
-- attribution:
--
--   processing_tasks      — durable per-step task ledger (replaces
--                           Celery+Redis as the source of truth).
--   customer_llm_budgets  — per-customer reserved RPM/TPM and burst
--                           ceilings (drives the rate limiter).
--   interaction_audit_log — append-only per-step state transitions
--                           (debug-3-days-later requirement).
--   llm_token_ledger      — per-call attribution of LLM consumption
--                           (billing + capacity attribution).
--
-- It also extends the existing `interactions` table with the four
-- workflow-level status columns the new pipeline needs.
--
-- Designed to be idempotent: re-running this migration on an
-- already-migrated database is a no-op.

BEGIN;

-- ── customer_llm_budgets ────────────────────────────────────────────────────
-- A customer's reservation contract. reserved_tpm/rpm is the guaranteed
-- floor; burst_tpm is the maximum amount they can borrow from shared
-- headroom on top of the reservation. priority breaks ties when multiple
-- customers compete for headroom (lower number = higher priority).
CREATE TABLE IF NOT EXISTS customer_llm_budgets (
    customer_id   UUID PRIMARY KEY,
    reserved_tpm  INTEGER  NOT NULL DEFAULT 0,
    reserved_rpm  INTEGER  NOT NULL DEFAULT 0,
    burst_tpm     INTEGER  NOT NULL DEFAULT 0,
    priority      SMALLINT NOT NULL DEFAULT 5,
    config        JSONB    NOT NULL DEFAULT '{}',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── processing_tasks ────────────────────────────────────────────────────────
-- The durable workflow ledger. Every post-call step (recording_poll,
-- llm_analysis, signal_jobs, lead_stage, crm_push) gets one row per
-- interaction. Workers claim rows via SELECT ... FOR UPDATE SKIP LOCKED
-- with a lease window (locked_until); a crashed worker's lease expires
-- and another worker picks the row up. Tasks that exhaust max_attempts
-- transition to status='dead_letter' rather than being silently dropped.
CREATE TABLE IF NOT EXISTS processing_tasks (
    id                UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    interaction_id    UUID NOT NULL REFERENCES interactions(id) ON DELETE CASCADE,
    customer_id       UUID NOT NULL,
    campaign_id       UUID NOT NULL,
    correlation_id    TEXT NOT NULL,
    step              TEXT NOT NULL,
    lane              TEXT NOT NULL DEFAULT 'cold',
    status            TEXT NOT NULL DEFAULT 'pending',
    attempts          INTEGER NOT NULL DEFAULT 0,
    max_attempts      INTEGER NOT NULL DEFAULT 6,
    next_run_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_error        TEXT,
    payload           JSONB NOT NULL DEFAULT '{}',
    estimated_tokens  INTEGER NOT NULL DEFAULT 0,
    actual_tokens     INTEGER,
    locked_by         TEXT,
    locked_until      TIMESTAMPTZ,
    completed_at      TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Idempotency: replaying the same step for the same interaction is
    -- a no-op at the table level. The unique key carries the contract.
    CONSTRAINT processing_tasks_step_unique UNIQUE (interaction_id, step),
    CONSTRAINT processing_tasks_lane_check  CHECK (lane IN ('hot', 'cold', 'skip')),
    CONSTRAINT processing_tasks_status_check CHECK (
        status IN ('pending', 'scheduled', 'in_progress', 'completed', 'failed', 'dead_letter')
    )
);

-- Partial index: workers only ever query for due-and-runnable tasks.
-- Restricting the index to those statuses keeps it small at 100K-row scale.
CREATE INDEX IF NOT EXISTS idx_processing_tasks_due
    ON processing_tasks (lane, next_run_at)
    WHERE status IN ('pending', 'scheduled');

-- Dead-letter triage / replay surface — must be cheap to scan.
CREATE INDEX IF NOT EXISTS idx_processing_tasks_dead_letter
    ON processing_tasks (updated_at DESC)
    WHERE status = 'dead_letter';

-- For per-interaction debug queries (audit-3-days-later use case).
CREATE INDEX IF NOT EXISTS idx_processing_tasks_interaction
    ON processing_tasks (interaction_id);

-- ── interaction_audit_log ───────────────────────────────────────────────────
-- Append-only state-transition log. One row per (step, attempt, status)
-- combination. This is the source of truth for "what happened to
-- interaction X" — independent of whether the processing_tasks row
-- has since been updated or deleted.
CREATE TABLE IF NOT EXISTS interaction_audit_log (
    id              BIGSERIAL PRIMARY KEY,
    interaction_id  UUID NOT NULL,
    customer_id     UUID,
    correlation_id  TEXT NOT NULL,
    step            TEXT NOT NULL,
    status          TEXT NOT NULL,
    attempt         INTEGER,
    detail          JSONB NOT NULL DEFAULT '{}',
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_audit_interaction ON interaction_audit_log (interaction_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_audit_correlation ON interaction_audit_log (correlation_id);
CREATE INDEX IF NOT EXISTS idx_audit_customer_time ON interaction_audit_log (customer_id, occurred_at DESC);

-- ── llm_token_ledger ────────────────────────────────────────────────────────
-- Per-LLM-call consumption record. Required by the README constraint
-- "all LLM spending must be attributable" — we need to be able to
-- answer "how many tokens did Customer X spend in the last hour?"
-- in a single SQL query.
CREATE TABLE IF NOT EXISTS llm_token_ledger (
    id              BIGSERIAL PRIMARY KEY,
    customer_id     UUID NOT NULL,
    campaign_id     UUID,
    interaction_id  UUID NOT NULL,
    request_kind    TEXT NOT NULL,
    tokens_used     INTEGER NOT NULL,
    model           TEXT,
    correlation_id  TEXT,
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ledger_customer_time ON llm_token_ledger (customer_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_ledger_campaign_time ON llm_token_ledger (campaign_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_ledger_interaction   ON llm_token_ledger (interaction_id);

-- ── interactions: workflow-level status columns ─────────────────────────────
-- The existing interaction_metadata JSONB column is the dashboard's hot
-- cache. We add explicit columns for the workflow-level state so the
-- dispatcher and dashboards don't need to JSONB-extract on every read.
ALTER TABLE interactions
    ADD COLUMN IF NOT EXISTS analysis_status   TEXT DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS recording_status  TEXT DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS processing_lane   TEXT,
    ADD COLUMN IF NOT EXISTS correlation_id    TEXT;

CREATE INDEX IF NOT EXISTS idx_interactions_analysis_status
    ON interactions (analysis_status)
    WHERE analysis_status IN ('pending', 'failed');

CREATE INDEX IF NOT EXISTS idx_interactions_correlation
    ON interactions (correlation_id);

-- ── seed customer budgets for fixture customers ─────────────────────────────
-- The fixture in data/schema.sql seeds two customers:
--   d0000000-...-0001 (with two campaigns, three sessions)
--   d0000000-...-0002 (with two leads)
-- We give them representative reservations so tests against the budget
-- manager are reproducible.
INSERT INTO customer_llm_budgets (customer_id, reserved_tpm, reserved_rpm, burst_tpm, priority)
VALUES
    ('d0000000-0000-0000-0000-000000000001', 30000, 150, 15000, 1),
    ('d0000000-0000-0000-0000-000000000002', 20000, 100, 10000, 3)
ON CONFLICT (customer_id) DO NOTHING;

COMMIT;
