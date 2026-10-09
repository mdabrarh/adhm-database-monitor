"""
Raw PostgreSQL diagnostic queries.

Every function here takes an open psycopg2 connection and returns plain
dict/list data -- no MCP, no Claude, nothing agent-related. This module is
imported by BOTH:
  1. mcp_servers/postgres_server.py (exposes these as tools to Claude), and
  2. agent/quick_probe.py (calls these directly, cheaply, to decide whether
     a poll cycle is even worth escalating to the LLM).

Keeping the SQL in one place means you only ever have to fix/extend a query
once. This is the file to edit if you want to track an additional Postgres
signal (e.g. replication lag, WAL size, connection count).
"""
from __future__ import annotations

import time
from typing import Any

import psycopg2
import psycopg2.extras


def connect(host: str, port: int, database: str, user: str, password: str, connect_timeout: int = 5):
    return psycopg2.connect(
        host=host,
        port=port,
        dbname=database,
        user=user,
        password=password,
        connect_timeout=connect_timeout,
        # read-only intent at the session level; the DB USER should also be
        # provisioned read-only -- see deploy/EC2_SETUP.md.
        options="-c default_transaction_read_only=on",
    )


def _dict_rows(cur) -> list[dict[str, Any]]:
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def get_blocking_chains(conn) -> list[dict[str, Any]]:
    """Sessions currently blocked, and who is blocking them, with wait duration.
    Based on the standard pg_locks self-join pattern."""
    sql = """
    SELECT
        blocked_activity.pid            AS blocked_pid,
        blocked_activity.usename        AS blocked_user,
        blocked_activity.query          AS blocked_query,
        EXTRACT(EPOCH FROM (now() - blocked_activity.query_start)) AS blocked_wait_seconds,
        blocking_activity.pid           AS blocking_pid,
        blocking_activity.usename       AS blocking_user,
        blocking_activity.query         AS blocking_query,
        blocking_activity.state         AS blocking_state,
        EXTRACT(EPOCH FROM (now() - blocking_activity.xact_start)) AS blocking_txn_age_seconds
    FROM pg_catalog.pg_locks blocked_locks
    JOIN pg_catalog.pg_stat_activity blocked_activity
        ON blocked_activity.pid = blocked_locks.pid
    JOIN pg_catalog.pg_locks blocking_locks
        ON blocking_locks.locktype = blocked_locks.locktype
        AND blocking_locks.database IS NOT DISTINCT FROM blocked_locks.database
        AND blocking_locks.relation IS NOT DISTINCT FROM blocked_locks.relation
        AND blocking_locks.page IS NOT DISTINCT FROM blocked_locks.page
        AND blocking_locks.tuple IS NOT DISTINCT FROM blocked_locks.tuple
        AND blocking_locks.virtualxid IS NOT DISTINCT FROM blocked_locks.virtualxid
        AND blocking_locks.transactionid IS NOT DISTINCT FROM blocked_locks.transactionid
        AND blocking_locks.classid IS NOT DISTINCT FROM blocked_locks.classid
        AND blocking_locks.objid IS NOT DISTINCT FROM blocked_locks.objid
        AND blocking_locks.objsubid IS NOT DISTINCT FROM blocked_locks.objsubid
        AND blocking_locks.pid != blocked_locks.pid
    JOIN pg_catalog.pg_stat_activity blocking_activity
        ON blocking_activity.pid = blocking_locks.pid
    WHERE NOT blocked_locks.granted;
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        return _dict_rows(cur)


def get_undo_pressure(conn) -> dict[str, Any]:
    """PostgreSQL has no single 'history list length' -- the closest analogue
    is dead-tuple / bloat pressure from rows that can't be reclaimed because
    an old snapshot or long transaction is still referencing them, plus
    transaction-ID (XID) wraparound age. We normalize both into one 0-1
    'undo_pressure_ratio' comparable across engines (see db_mysql.py)."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                COALESCE(SUM(n_dead_tup), 0)  AS total_dead_tuples,
                COALESCE(SUM(n_live_tup), 0)  AS total_live_tuples
            FROM pg_stat_user_tables;
        """)
        row = cur.fetchone()
        total_dead, total_live = row[0], row[1]
        dead_ratio = float(total_dead) / float(total_live + 1)

        cur.execute("""
            SELECT
                EXTRACT(EPOCH FROM (now() - xact_start)) AS oldest_txn_age_seconds
            FROM pg_stat_activity
            WHERE xact_start IS NOT NULL
            ORDER BY xact_start ASC
            LIMIT 1;
        """)
        r = cur.fetchone()
        oldest_txn_age = float(r[0]) if r and r[0] is not None else 0.0

        cur.execute("SELECT age(datfrozenxid) FROM pg_database WHERE datname = current_database();")
        xid_age = int(cur.fetchone()[0])
        # 200M is the classic autovacuum_freeze_max_age default; treat that as "1.0 pressure"
        xid_age_ratio = min(xid_age / 200_000_000.0, 1.0)

    # Blend: whichever signal is worse dominates, since either alone can cause
    # an outage (bloat kills performance; wraparound kills the whole database).
    undo_pressure_ratio = max(min(dead_ratio, 1.0), xid_age_ratio)

    return {
        "total_dead_tuples": int(total_dead),
        "total_live_tuples": int(total_live),
        "dead_tuple_ratio": round(dead_ratio, 4),
        "oldest_open_txn_age_seconds": round(oldest_txn_age, 1),
        "xid_wraparound_age": xid_age,
        "xid_wraparound_ratio": round(xid_age_ratio, 4),
        "undo_pressure_ratio": round(undo_pressure_ratio, 4),
    }


def get_deadlock_count(conn) -> int:
    """Cumulative deadlock counter for the current database since the
    statistics were last reset. The caller (quick_probe / baseline_store)
    is responsible for diffing this against the previous poll's value to
    detect NEW deadlocks."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT deadlocks FROM pg_stat_database WHERE datname = current_database();"
        )
        row = cur.fetchone()
        return int(row[0]) if row else 0


def get_wait_events(conn, top_n: int = 10) -> list[dict[str, Any]]:
    """Current distribution of wait events across active sessions -- a
    snapshot, not a cumulative counter, so it reflects what's happening
    right now."""
    sql = """
        SELECT wait_event_type, wait_event, count(*) AS session_count
        FROM pg_stat_activity
        WHERE wait_event IS NOT NULL
        GROUP BY wait_event_type, wait_event
        ORDER BY session_count DESC
        LIMIT %s;
    """
    with conn.cursor() as cur:
        cur.execute(sql, (top_n,))
        return _dict_rows(cur)


def get_long_transactions(conn, min_age_seconds: float = 30.0) -> list[dict[str, Any]]:
    sql = """
        SELECT
            pid,
            usename,
            state,
            query,
            EXTRACT(EPOCH FROM (now() - xact_start)) AS txn_age_seconds,
            EXTRACT(EPOCH FROM (now() - query_start)) AS query_age_seconds
        FROM pg_stat_activity
        WHERE xact_start IS NOT NULL
          AND EXTRACT(EPOCH FROM (now() - xact_start)) >= %s
        ORDER BY xact_start ASC;
    """
    with conn.cursor() as cur:
        cur.execute(sql, (min_age_seconds,))
        return _dict_rows(cur)


def get_recent_deadlock_log_excerpt(
    log_path: str, lookback_seconds: float = 180.0, max_lines_scanned: int = 20000
) -> list[str]:
    """PostgreSQL does NOT expose deadlock participants/queries through any
    system view -- pg_stat_database.deadlocks is a bare counter. The only
    place PostgreSQL records which queries/processes were involved is its
    own server log, and only if log_lock_waits (or the deadlock_timeout
    default logging) is enabled. This is a best-effort reader: it tails
    the given log file and returns any "deadlock detected" entries from
    roughly the last `lookback_seconds`.

    Returns an empty list (never raises) if the file doesn't exist, isn't
    readable, or contains no recent deadlock entries -- callers should
    treat an empty list as "no detail available", not "no deadlock
    occurred" (the cumulative counter is the authority on whether one
    occurred; this is only trying to add color on what was involved).

    This only works if the monitoring process can read the Postgres log
    file directly, i.e. it's running on the DB host or the log is mounted
    somewhere reachable -- it will not work against a managed RDS instance
    unless you separately ship RDS logs (e.g. via CloudWatch) somewhere
    this function can read them, which is outside the scope of this
    reference implementation.
    """
    import os
    import time as _time

    if not log_path or not os.path.exists(log_path):
        return []

    try:
        with open(log_path, "r", errors="replace") as f:
            # Cheap tail: read from the end rather than the whole file,
            # since production log files can be large.
            f.seek(0, os.SEEK_END)
            file_size = f.tell()
            read_size = min(file_size, 2_000_000)  # last ~2MB
            f.seek(file_size - read_size)
            lines = f.readlines()[-max_lines_scanned:]
    except OSError:
        return []

    cutoff = _time.time() - lookback_seconds
    excerpts = []
    current_block: list[str] = []
    in_block = False
    for line in lines:
        if "deadlock detected" in line:
            in_block = True
            current_block = [line]
        elif in_block:
            # Deadlock log entries continue onto subsequent lines carrying
            # DETAIL:/CONTEXT:/HINT:/STATEMENT: markers (each still prefixed
            # by whatever log_line_prefix is configured, so we match on the
            # marker text appearing anywhere in the line, not on leading
            # whitespace, which varies by configuration) or lines that are
            # plainly indented continuations. Anything else ends the block.
            if any(marker in line for marker in ("DETAIL:", "CONTEXT:", "HINT:", "STATEMENT:")) or line.startswith((" ", "\t")):
                current_block.append(line)
            else:
                excerpts.append("".join(current_block))
                in_block = False
    if in_block:
        excerpts.append("".join(current_block))

    # We don't have a reliable per-line timestamp parser for every possible
    # log_line_prefix configuration, so we return the last few matches
    # unfiltered by exact time rather than risk silently dropping a real
    # one -- the caller already knows this is best-effort.
    return excerpts[-3:]


def get_full_snapshot(conn) -> dict[str, Any]:
    """Convenience bundle used by quick_probe.py for the cheap pre-filter pass."""
    blocking = get_blocking_chains(conn)
    undo = get_undo_pressure(conn)
    deadlocks = get_deadlock_count(conn)
    long_txns = get_long_transactions(conn, min_age_seconds=10.0)
    # float(): PostgreSQL 14+ returns EXTRACT(EPOCH ...) as numeric -> Decimal,
    # which json.dumps (record_decision) and record_snapshot cannot handle.
    max_wait = float(max([b["blocked_wait_seconds"] or 0 for b in blocking], default=0.0))
    max_txn_age = float(max([t["txn_age_seconds"] or 0 for t in long_txns], default=0.0))
    return {
        "engine": "postgres",
        "timestamp": time.time(),
        "blocking_chain_count": len(blocking),
        "max_blocking_wait_seconds": max_wait,
        "undo_pressure_ratio": undo["undo_pressure_ratio"],
        "deadlock_count_cumulative": deadlocks,
        "max_long_txn_age_seconds": max_txn_age,
        "long_txn_count": len(long_txns),
    }
