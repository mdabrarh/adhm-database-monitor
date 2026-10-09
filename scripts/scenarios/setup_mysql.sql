-- Test table for the MySQL deadlock scenario (paper, Stage 1/2).
-- Run once against a NON-PRODUCTION database with a user that can create tables:
--   mysql -h 127.0.0.1 -u <writer> -p <db> < scripts/scenarios/setup_mysql.sql
DROP TABLE IF EXISTS invoices;
CREATE TABLE invoices (
    id   INT PRIMARY KEY,
    paid TINYINT NOT NULL DEFAULT 0
) ENGINE=InnoDB;
INSERT INTO invoices (id, paid) VALUES (1, 0), (2, 0);
