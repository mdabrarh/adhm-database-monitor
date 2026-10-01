#!/usr/bin/env python3
"""
MCP (stdio) server exposing read-only PostgreSQL diagnostic tools to the
Claude agent.

This process is spawned fresh by the Claude Agent SDK for each monitoring
cycle (see agent/claude_client.py), with connection details for ONE
specific instance passed in via environment variables -- it is not a
long-running server you start by hand. That's what makes it safe to add
more Postgres instances: nothing here changes, only config/config.yaml
grows a new entry.

IMPORTANT: keep all logging on stderr. MCP over stdio uses stdout for the
JSON-RPC protocol itself -- printing anything else there corrupts the
stream and Claude will fail to parse tool responses.
"""
from __future__ import annotations

import os
import sys

from fastmcp import FastMCP

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common import db_pg  # noqa: E402

mcp = FastMCP("postgres_monitor")

_HOST = os.environ["DB_HOST"]
_PORT = int(os.environ.get("DB_PORT", 5432))
_DATABASE = os.environ["DB_NAME"]
_USER = os.environ["DB_USER"]
_PASSWORD = os.environ["DB_PASSWORD"]
_LABEL = os.environ.get("DB_INSTANCE_LABEL", _DATABASE)


def _connect():
    return db_pg.connect(_HOST, _PORT, _DATABASE, _USER, _PASSWORD)


@mcp.tool()
def get_blocking_chains() -> dict:
    """Return every session currently blocked on a lock, paired with the
    session blocking it, including how long each has been waiting/running.
    An empty list means no lock contention right now."""
    conn = _connect()
    try:
        chains = db_pg.get_blocking_chains(conn)
        return {"instance": _LABEL, "blocking_chains": chains, "count": len(chains)}
    finally:
        conn.close()


@mcp.tool()
def get_undo_pressure() -> dict:
    """Return PostgreSQL's undo/version-store pressure: dead-tuple ratio
    (rows that can't be vacuumed because something still references them),
    the oldest currently-open transaction's age, and transaction-ID
    wraparound age. undo_pressure_ratio is a normalized 0-1 score -- this
    is Postgres's analogue of Oracle's rollback segment history list length
    and MySQL's InnoDB history list length."""
    conn = _connect()
    try:
        result = db_pg.get_undo_pressure(conn)
        result["instance"] = _LABEL
        return result
    finally:
        conn.close()


@mcp.tool()
def get_deadlock_count() -> dict:
    """Cumulative deadlock count for this database since statistics were
    last reset (pg_stat_database.deadlocks). This is a running total, not
    a per-cycle count -- compare it to the value from get_historical_baseline
    or a previous call to determine whether NEW deadlocks have occurred."""
    conn = _connect()
    try:
        count = db_pg.get_deadlock_count(conn)
        return {"instance": _LABEL, "deadlocks_cumulative": count}
    finally:
        conn.close()


@mcp.tool()
def get_wait_events(top_n: int = 10) -> dict:
    """Current top wait events across active sessions right now (a live
    snapshot, e.g. Lock, IO, LWLock), with how many sessions are in each."""
    conn = _connect()
    try:
        events = db_pg.get_wait_events(conn, top_n=top_n)
        return {"instance": _LABEL, "wait_events": events}
    finally:
        conn.close()


@mcp.tool()
def get_long_transactions(min_age_seconds: float = 30.0) -> dict:
    """Sessions with a transaction open longer than min_age_seconds,
    including their current query and state (active / idle in transaction).
    Idle-in-transaction sessions older than a minute or two are almost
    always the root cause of both lock contention and undo pressure."""
    conn = _connect()
    try:
        txns = db_pg.get_long_transactions(conn, min_age_seconds=min_age_seconds)
        return {"instance": _LABEL, "long_transactions": txns, "count": len(txns)}
    finally:
        conn.close()


if __name__ == "__main__":
    mcp.run()
