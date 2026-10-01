"""
Raw MySQL / InnoDB diagnostic queries.

Mirrors common/db_pg.py so the two engines expose comparable signals to the
agent. Imported by both mcp_servers/mysql_server.py (tool exposure) and
agent/quick_probe.py (cheap pre-filter).

Requires MySQL 8.0+ for performance_schema.data_lock_waits. On 5.7 or
MariaDB, swap get_blocking_chains() for a query against
information_schema.innodb_lock_waits (see comment inline) -- this is the
main thing you're likely to need to adapt for an older fleet.
"""
from __future__ import annotations

import re
import time
from typing import Any

import mysql.connector


def connect(host: str, port: int, database: str, user: str, password: str, connect_timeout: int = 5):
    return mysql.connector.connect(
        host=host,
        port=port,
        database=database,
        user=user,
        password=password,
        connection_timeout=connect_timeout,
    )


def _dict_rows(cur) -> list[dict[str, Any]]:
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def get_blocking_chains(conn) -> list[dict[str, Any]]:
    """MySQL 8.0+ via performance_schema. For MySQL 5.7 / MariaDB, replace
    this query body with a SELECT against information_schema.innodb_lock_waits
    joined to information_schema.innodb_trx -- the output shape below is
    what the rest of this codebase expects, so keep the column names."""
    sql = """
        SELECT
            waiting.trx_mysql_thread_id   AS blocked_pid,
            waiting.trx_query             AS blocked_query,
            TIMESTAMPDIFF(SECOND, waiting.trx_wait_started, NOW()) AS blocked_wait_seconds,
            blocking.trx_mysql_thread_id  AS blocking_pid,
            blocking.trx_query            AS blocking_query,
            blocking.trx_state            AS blocking_state,
            TIMESTAMPDIFF(SECOND, blocking.trx_started, NOW()) AS blocking_txn_age_seconds
        FROM performance_schema.data_lock_waits w
        JOIN information_schema.innodb_trx waiting
            ON waiting.trx_id = w.requesting_engine_transaction_id
        JOIN information_schema.innodb_trx blocking
            ON blocking.trx_id = w.blocking_engine_transaction_id;
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        return _dict_rows(cur)


def get_innodb_status_text(conn) -> str:
    with conn.cursor() as cur:
        cur.execute("SHOW ENGINE INNODB STATUS;")
        row = cur.fetchone()
        # SHOW ENGINE INNODB STATUS returns (Type, Name, Status)
        return row[2] if row else ""


def parse_history_list_length(innodb_status_text: str) -> int:
    m = re.search(r"History list length\s+(\d+)", innodb_status_text)
    return int(m.group(1)) if m else 0


def parse_latest_deadlock(innodb_status_text: str) -> dict[str, Any] | None:
    """Extracts the LATEST DETECTED DEADLOCK section, if present. Returns
    None if InnoDB hasn't recorded a deadlock since startup."""
    marker = "LATEST DETECTED DEADLOCK"
    idx = innodb_status_text.find(marker)
    if idx == -1:
        return None
    end_idx = innodb_status_text.find("TRANSACTIONS", idx)
    section = innodb_status_text[idx: end_idx if end_idx != -1 else idx + 4000]
    ts_match = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", section)
    return {
        "raw_text": section.strip()[:4000],
        "timestamp": ts_match.group(1) if ts_match else None,
    }


def get_undo_pressure(conn, purge_lag_alert_threshold: int = 1_000_000) -> dict[str, Any]:
    """InnoDB's History List Length IS the direct undo-pressure signal --
    unlike Postgres, no derivation needed. We still normalize it to a 0-1
    ratio against a configurable 'this is definitely a problem' ceiling so
    it's comparable to Postgres's undo_pressure_ratio in the agent's prompt."""
    status_text = get_innodb_status_text(conn)
    hll = parse_history_list_length(status_text)
    ratio = min(hll / float(purge_lag_alert_threshold), 1.0)
    return {
        "history_list_length": hll,
        "purge_lag_alert_threshold": purge_lag_alert_threshold,
        "undo_pressure_ratio": round(ratio, 4),
    }


def get_deadlock_marker(conn) -> str | None:
    """Returns a stable fingerprint (the timestamp string) of the most
    recent deadlock InnoDB has recorded, or None. The caller diffs this
    against the last-seen marker (stored in baseline_store) to detect a
    NEW deadlock, since InnoDB only ever remembers the single latest one."""
    status_text = get_innodb_status_text(conn)
    dl = parse_latest_deadlock(status_text)
    return dl["timestamp"] if dl else None


def get_wait_events(conn, top_n: int = 10) -> list[dict[str, Any]]:
    sql = """
        SELECT
            EVENT_NAME AS wait_event,
            COUNT_STAR AS hit_count,
            SUM_TIMER_WAIT / 1000000000000 AS total_wait_seconds
        FROM performance_schema.events_waits_summary_global_by_event_name
        WHERE EVENT_NAME != 'idle' AND COUNT_STAR > 0
        ORDER BY SUM_TIMER_WAIT DESC
        LIMIT %s;
    """
    with conn.cursor() as cur:
        cur.execute(sql, (top_n,))
        return _dict_rows(cur)


def get_long_transactions(conn, min_age_seconds: float = 30.0) -> list[dict[str, Any]]:
    sql = """
        SELECT
            trx_mysql_thread_id AS pid,
            trx_state AS state,
            trx_query AS query,
            TIMESTAMPDIFF(SECOND, trx_started, NOW()) AS txn_age_seconds
        FROM information_schema.innodb_trx
        WHERE TIMESTAMPDIFF(SECOND, trx_started, NOW()) >= %s
        ORDER BY trx_started ASC;
    """
    with conn.cursor() as cur:
        cur.execute(sql, (min_age_seconds,))
        return _dict_rows(cur)


def get_full_snapshot(conn) -> dict[str, Any]:
    """Convenience bundle used by quick_probe.py for the cheap pre-filter pass."""
    blocking = get_blocking_chains(conn)
    undo = get_undo_pressure(conn)
    long_txns = get_long_transactions(conn, min_age_seconds=10.0)
    max_wait = max([b["blocked_wait_seconds"] or 0 for b in blocking], default=0.0)
    max_txn_age = max([t["txn_age_seconds"] or 0 for t in long_txns], default=0.0)
    return {
        "engine": "mysql",
        "timestamp": time.time(),
        "blocking_chain_count": len(blocking),
        "max_blocking_wait_seconds": max_wait,
        "undo_pressure_ratio": undo["undo_pressure_ratio"],
        "history_list_length": undo["history_list_length"],
        "deadlock_marker": get_deadlock_marker(conn),
        "max_long_txn_age_seconds": max_txn_age,
        "long_txn_count": len(long_txns),
    }
