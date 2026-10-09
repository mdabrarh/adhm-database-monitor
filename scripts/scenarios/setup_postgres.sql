-- Test table for the PostgreSQL blocking-chain scenario (paper, Stage 1/2).
-- Run once against a NON-PRODUCTION database with a user that can create tables:
--   psql -h localhost -U <writer> -d <db> -f scripts/scenarios/setup_postgres.sql
-- The table is deliberately tiny: the quiescent-cycle result in the paper
-- (dead-tuple ratio of 1.0 explained as an artifact of a near-empty table)
-- depends on this.
DROP TABLE IF EXISTS adhm_accounts;
CREATE TABLE adhm_accounts (
    id      integer PRIMARY KEY,
    balance integer NOT NULL
);
INSERT INTO adhm_accounts (id, balance) VALUES (1, 100);
