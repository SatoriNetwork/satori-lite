-- =============================================================================
-- Migration: Rename competition* tables to bounty* in the neuron's network DB
-- Date: 2026-05-04
-- Description: Copies data from the old competition-named tables into the new
--              bounty-named tables and drops the old ones. Idempotent — safe
--              to run on a DB that has only old tables, only new tables, both,
--              or neither (re-running it is a no-op).
--
-- How to run:
--   sqlite3 <dataPath>/network.db < migrations/rename_competitions_to_bounties.sql
--
-- The neuron's data path resolves to whatever satorineuron.config.dataPath()
-- returns — typically ~/.satori or /Satori/Neuron. The DB file is network.db.
--
-- Back up first:  cp network.db network.db.bak
-- =============================================================================

-- Note: SQLite syntax. The neuron's network DB is local SQLite, not Postgres.

BEGIN TRANSACTION;

-- ─────────────────────────────────────────────────────────────────────────────
-- 1. Ensure the NEW (bounty*) tables exist. No-op if the new code already
--    created them on its first startup. Schemas must mirror the CREATE TABLE
--    statements in neuron-lite/satorineuron/network_db.py.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS bounties (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    stream_name             TEXT NOT NULL,
    stream_provider_pubkey  TEXT NOT NULL,
    host_pubkey             TEXT NOT NULL,
    pay_per_obs_sats        INTEGER NOT NULL,
    paid_predictors         INTEGER NOT NULL,
    competing_predictors    INTEGER NOT NULL,
    scoring_metric          TEXT NOT NULL,
    scoring_params          TEXT NOT NULL DEFAULT '{}',
    horizon                 INTEGER NOT NULL DEFAULT 1,
    active                  INTEGER NOT NULL DEFAULT 1,
    timestamp               INTEGER NOT NULL,
    UNIQUE(stream_name, stream_provider_pubkey, host_pubkey)
);

CREATE TABLE IF NOT EXISTS bounty_predictions (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    stream_name             TEXT NOT NULL,
    stream_provider_pubkey  TEXT NOT NULL,
    predictor_pubkey        TEXT NOT NULL,
    predictor_wallet_pubkey TEXT,
    host_pubkey             TEXT NOT NULL,
    seq_num                 INTEGER NOT NULL,
    predicted_value         TEXT NOT NULL,
    received_at             INTEGER NOT NULL,
    UNIQUE(stream_name, stream_provider_pubkey, predictor_pubkey, seq_num)
);
CREATE INDEX IF NOT EXISTS idx_comp_pred_seq
    ON bounty_predictions(stream_name, stream_provider_pubkey, seq_num);

CREATE TABLE IF NOT EXISTS bounty_payments (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    stream_name             TEXT NOT NULL,
    stream_provider_pubkey  TEXT NOT NULL,
    predictor_pubkey        TEXT NOT NULL,
    seq_num                 INTEGER NOT NULL,
    sats_paid               INTEGER NOT NULL,
    paid_at                 INTEGER NOT NULL,
    UNIQUE(stream_name, stream_provider_pubkey, predictor_pubkey, seq_num)
);
CREATE INDEX IF NOT EXISTS idx_comp_pay_stream
    ON bounty_payments(stream_name, stream_provider_pubkey);
CREATE UNIQUE INDEX IF NOT EXISTS idx_comp_pay_dedup
    ON bounty_payments(stream_name, stream_provider_pubkey,
                       predictor_pubkey, seq_num);

CREATE TABLE IF NOT EXISTS joined_bounties (
    stream_name             TEXT NOT NULL,
    stream_provider_pubkey  TEXT NOT NULL,
    host_pubkey             TEXT NOT NULL,
    joined_at               INTEGER NOT NULL,
    PRIMARY KEY (stream_name, stream_provider_pubkey, host_pubkey)
);
CREATE INDEX IF NOT EXISTS idx_joined_comp_stream
    ON joined_bounties(stream_name, stream_provider_pubkey);

-- ─────────────────────────────────────────────────────────────────────────────
-- 2. Ensure the OLD (competition*) tables exist as empty placeholders. No-op
--    if they already exist. If a tester's DB never had the old schema, these
--    create empty stubs so the INSERTs below are 0-row no-ops, and the DROPs
--    at the end clean them up. Either way, the old tables are gone after this
--    migration.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS competitions (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    stream_name             TEXT NOT NULL,
    stream_provider_pubkey  TEXT NOT NULL,
    host_pubkey             TEXT NOT NULL,
    pay_per_obs_sats        INTEGER NOT NULL,
    paid_predictors         INTEGER NOT NULL,
    competing_predictors    INTEGER NOT NULL,
    scoring_metric          TEXT NOT NULL,
    scoring_params          TEXT NOT NULL DEFAULT '{}',
    horizon                 INTEGER NOT NULL DEFAULT 1,
    active                  INTEGER NOT NULL DEFAULT 1,
    timestamp               INTEGER NOT NULL,
    UNIQUE(stream_name, stream_provider_pubkey, host_pubkey)
);

CREATE TABLE IF NOT EXISTS competition_predictions (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    stream_name             TEXT NOT NULL,
    stream_provider_pubkey  TEXT NOT NULL,
    predictor_pubkey        TEXT NOT NULL,
    predictor_wallet_pubkey TEXT,
    host_pubkey             TEXT NOT NULL,
    seq_num                 INTEGER NOT NULL,
    predicted_value         TEXT NOT NULL,
    received_at             INTEGER NOT NULL,
    UNIQUE(stream_name, stream_provider_pubkey, predictor_pubkey, seq_num)
);

CREATE TABLE IF NOT EXISTS competition_payments (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    stream_name             TEXT NOT NULL,
    stream_provider_pubkey  TEXT NOT NULL,
    predictor_pubkey        TEXT NOT NULL,
    seq_num                 INTEGER NOT NULL,
    sats_paid               INTEGER NOT NULL,
    paid_at                 INTEGER NOT NULL,
    UNIQUE(stream_name, stream_provider_pubkey, predictor_pubkey, seq_num)
);

CREATE TABLE IF NOT EXISTS joined_competitions (
    stream_name             TEXT NOT NULL,
    stream_provider_pubkey  TEXT NOT NULL,
    host_pubkey             TEXT NOT NULL,
    joined_at               INTEGER NOT NULL,
    PRIMARY KEY (stream_name, stream_provider_pubkey, host_pubkey)
);

-- ─────────────────────────────────────────────────────────────────────────────
-- 3. Copy data old → new. INSERT OR IGNORE skips UNIQUE-conflict rows so the
--    migration is rerunnable; if the new table already has a row for the
--    same key, the existing row wins (which is what you want — it's the
--    fresher copy from running the new code).
--
--    Explicit column lists so the migration still works on older DBs that
--    might be missing a column we added later.
-- ─────────────────────────────────────────────────────────────────────────────

INSERT OR IGNORE INTO bounties (
    id, stream_name, stream_provider_pubkey, host_pubkey,
    pay_per_obs_sats, paid_predictors, competing_predictors,
    scoring_metric, scoring_params, horizon, active, timestamp
) SELECT
    id, stream_name, stream_provider_pubkey, host_pubkey,
    pay_per_obs_sats, paid_predictors, competing_predictors,
    scoring_metric, scoring_params, horizon, active, timestamp
FROM competitions;

INSERT OR IGNORE INTO bounty_predictions (
    id, stream_name, stream_provider_pubkey, predictor_pubkey,
    predictor_wallet_pubkey, host_pubkey, seq_num,
    predicted_value, received_at
) SELECT
    id, stream_name, stream_provider_pubkey, predictor_pubkey,
    predictor_wallet_pubkey, host_pubkey, seq_num,
    predicted_value, received_at
FROM competition_predictions;

INSERT OR IGNORE INTO bounty_payments (
    id, stream_name, stream_provider_pubkey, predictor_pubkey,
    seq_num, sats_paid, paid_at
) SELECT
    id, stream_name, stream_provider_pubkey, predictor_pubkey,
    seq_num, sats_paid, paid_at
FROM competition_payments;

INSERT OR IGNORE INTO joined_bounties (
    stream_name, stream_provider_pubkey, host_pubkey, joined_at
) SELECT
    stream_name, stream_provider_pubkey, host_pubkey, joined_at
FROM joined_competitions;

-- ─────────────────────────────────────────────────────────────────────────────
-- 4. Drop the old tables. Safe even on fresh DBs because step 2 ensured they
--    exist (possibly as empty stubs).
-- ─────────────────────────────────────────────────────────────────────────────

DROP TABLE competitions;
DROP TABLE competition_predictions;
DROP TABLE competition_payments;
DROP TABLE joined_competitions;

COMMIT;
