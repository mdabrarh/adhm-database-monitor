#!/usr/bin/env python3
"""
MCP (stdio) server exposing read-only MySQL/InnoDB diagnostic tools to the
Claude agent. Mirrors mcp_servers/postgres_server.py -- see that file's
docstring for the lifecycle model (spawned fresh per monitoring cycle,
one instance's credentials via env vars).
"""
from __future__ import annotations

import os
import sys

from fastmcp import FastMCP

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common import db_mysql  # noqa: E402

mcp = FastMCP("mysql_monitor")

_HOST = os.environ["DB_HOST"]
_PORT = int(os.environ.get("DB_PORT", 3306))
_DATABASE = os.environ["DB_NAME"]
_USER = os.environ["DB_USER"]
_PASSWORD = os.environ["DB_PASSWORD"]
_LABEL = os.environ.get("DB_INSTANCE_LABEL", _DATABASE)


def _connect():
    return db_mysql.connect(_HOST, _PORT, _DATABASE, _USER, _PASSWORD)


@mcp.tool()
def get_blocking_chains() -> dict:
    """Return every InnoDB transaction currently waiting on a lock, paired
    with the transaction blocking it. Requires MySQL 8.0+ (performance_schema
    .data_lock_waits). An empty list means no lock contention right now."""
    conn = _connect()
    try:
        chains = db_mysql.get_blocking_chains(conn)
        return {"instance": _LABEL, "blocking_chains": chains, "count": len(chains)}
    finally:
        conn.close()


@mcp.tool()
def get_undo_pressure() -> dict:
    """Return InnoDB's History List Length (the number of not-yet-purged
    undo log entries -- rows from committed/rolled-back transactions that
    InnoDB can't reclaim yet because an old transaction or snapshot is still
    open) and a normalized 0-1 undo_pressure_ratio. This is the direct MySQL
    analogue of Oracle's rollback segment history list length."""
    conn = _connect()
    try:
        result = db_mysql.get_undo_pressure(conn)
        result["instance"] = _LABEL
        return result
    finally:
        conn.close()


@mcp.tool()
def get_latest_deadlock() -> dict:
    """Return InnoDB's most recently recorded deadlock (participants,
    resource cycle, victim), parsed from SHOW ENGINE INNODB STATUS. Returns
    deadlock_detected: false if InnoDB has not recorded one since startup.
    Note InnoDB only remembers the SINGLE latest deadlock -- compare the
    timestamp against what you've seen in a previous check to know whether
    this is a new one or the same one you already reasoned about."""
    conn = _connect()
    try:
        status_text = db_mysql.get_innodb_status_text(conn)
        dl = db_mysql.parse_latest_deadlock(status_text)
        if dl is None:
            return {"instance": _LABEL, "deadlock_detected": False}
        return {"instance": _LABEL, "deadlock_detected": True, **dl}
    finally:
        conn.close()


@mcp.tool()
def get_wait_events(top_n: int = 10) -> dict:
    """Top wait events by cumulative time from performance_schema, excluding
    idle. This is a cumulative-since-startup view, not a live snapshot --
    useful for spotting a wait type that dominates overall, less useful for
    "what's happening this second" (use get_blocking_chains for that)."""
    conn = _connect()
    try:
        events = db_mysql.get_wait_events(conn, top_n=top_n)
        return {"instance": _LABEL, "wait_events": events}
    finally:
        conn.close()


@mcp.tool()
def get_long_transactions(min_age_seconds: float = 30.0) -> dict:
    """InnoDB transactions open longer than min_age_seconds, with their
    current state and query. A long-open transaction is almost always
    either the cause of, or about to be a victim of, rising History List
    Length and lock contention."""
    conn = _connect()
    try:
        txns = db_mysql.get_long_transactions(conn, min_age_seconds=min_age_seconds)
        return {"instance": _LABEL, "long_transactions": txns, "count": len(txns)}
    finally:
        conn.close()


if __name__ == "__main__":
    mcp.run()
