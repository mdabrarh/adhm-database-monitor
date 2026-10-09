"""
PostgreSQL blocking-chain scenario used in the paper (Stage 1 and Stage 2).

Session A opens a transaction, takes a row lock with SELECT ... FOR UPDATE,
and then sits idle in that transaction. Session B issues an UPDATE against
the same row and blocks behind A. The chain stays in place until the
context manager exits, at which point A rolls back, B's UPDATE completes,
and B rolls back too -- the table is left unchanged.

Requires a user with write access to the test table (see setup_postgres.sql).
Never point this at a production database.

    with BlockingChain(host, port, db, user, password) as chain:
        time.sleep(3)          # let the wait accumulate
        ...                    # run the monitor against the instance
        print(chain.blocker_pid, chain.waiter_pid)
"""
from __future__ import annotations

import threading
import time

import psycopg2

LOCK_SQL = "SELECT id, balance FROM adhm_accounts WHERE id = 1 FOR UPDATE"
BLOCKED_SQL = "UPDATE adhm_accounts SET balance = balance + 1 WHERE id = 1"


class BlockingChain:
    def __init__(self, host: str, port: int, database: str, user: str, password: str):
        self._args = dict(host=host, port=port, dbname=database, user=user, password=password,
                          application_name="adhm_scenario")
        self.blocker_pid: int | None = None
        self.waiter_pid: int | None = None
        self.started_at: float | None = None
        self._a = self._b = None
        self._thread: threading.Thread | None = None
        self._waiter_error: Exception | None = None

    def _run_waiter(self) -> None:
        try:
            with self._b.cursor() as cur:
                cur.execute(BLOCKED_SQL)  # blocks until session A releases the lock
        except Exception as exc:  # recorded, re-raised on exit
            self._waiter_error = exc

    def _wait_until_blocked(self, timeout: float = 10.0) -> None:
        deadline = time.time() + timeout
        probe = psycopg2.connect(**self._args)
        probe.autocommit = True
        try:
            with probe.cursor() as cur:
                while time.time() < deadline:
                    cur.execute("SELECT cardinality(pg_blocking_pids(%s))", (self.waiter_pid,))
                    if cur.fetchone()[0] > 0:
                        return
                    time.sleep(0.1)
        finally:
            probe.close()
        raise RuntimeError("Session B never became blocked -- is setup_postgres.sql applied?")

    def __enter__(self) -> "BlockingChain":
        self._a = psycopg2.connect(**self._args)
        self._b = psycopg2.connect(**self._args)
        self.blocker_pid = self._a.get_backend_pid()
        self.waiter_pid = self._b.get_backend_pid()
        with self._a.cursor() as cur:
            cur.execute(LOCK_SQL)  # implicit BEGIN; lock held, session now idle in transaction
        self._thread = threading.Thread(target=self._run_waiter, daemon=True)
        self._thread.start()
        self._wait_until_blocked()
        self.started_at = time.time()
        return self

    def __exit__(self, *exc) -> None:
        try:
            self._a.rollback()           # release the lock
            if self._thread:
                self._thread.join(timeout=10)
            self._b.rollback()           # undo B's update; table unchanged
        finally:
            self._a.close()
            self._b.close()
        if self._waiter_error and not exc[0]:
            raise self._waiter_error
