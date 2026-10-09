"""
MySQL/InnoDB two-transaction deadlock scenario used in the paper
(Stage 1 and Stage 2).

Session 1 updates invoices.id = 1, session 2 updates invoices.id = 2, then
each tries to update the other's row. InnoDB detects the cycle, rolls one
transaction back (error 1213), and records the full participant detail in
the LATEST DETECTED DEADLOCK section of SHOW ENGINE INNODB STATUS. Both
transactions are rolled back afterwards, so the table is left unchanged.

Requires a user with write access to the test table (see setup_mysql.sql).
Never point this at a production database.

    result = run_deadlock(host, port, db, user, password)
    print(result)   # {"victim_session": 2, "error": "1213 (40001): Deadlock found ..."}
"""
from __future__ import annotations

import threading
import time

import mysql.connector
from mysql.connector import errorcode

FIRST_SQL = "UPDATE invoices SET paid = 1 WHERE id = {}"


def run_deadlock(host: str, port: int, database: str, user: str, password: str) -> dict:
    args = dict(host=host, port=port, database=database, user=user, password=password)
    c1 = mysql.connector.connect(**args)
    c2 = mysql.connector.connect(**args)
    victim: dict = {}

    def second_update(conn, row_id: int, session: int) -> None:
        try:
            conn.cursor().execute(FIRST_SQL.format(row_id))
        except mysql.connector.Error as exc:
            if exc.errno == errorcode.ER_LOCK_DEADLOCK:
                victim.update(victim_session=session, error=str(exc))
            else:
                raise

    try:
        for conn, row in ((c1, 1), (c2, 2)):
            conn.start_transaction()
            conn.cursor().execute(FIRST_SQL.format(row))

        # Session 1 now waits on row 2 (held by session 2) ...
        t = threading.Thread(target=second_update, args=(c1, 2, 1), daemon=True)
        t.start()
        time.sleep(0.5)
        # ... and session 2 asks for row 1 (held by session 1): a cycle.
        second_update(c2, 1, 2)
        t.join(timeout=10)
    finally:
        for conn in (c1, c2):
            try:
                conn.rollback()
            finally:
                conn.close()

    if not victim:
        raise RuntimeError("No deadlock was raised -- is setup_mysql.sql applied?")
    return victim
