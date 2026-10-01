"""
Engine-correct session/query termination command TEXT.

These functions only ever return a string. Nothing in this codebase calls
them against a live connection or executes their output -- the read-only
design decision from the README's Safety Model section applies here too.
The string is meant to be read by a human in an email and copy-pasted into
a psql/mysql client by that human, after they've decided it's warranted.

Two variants per engine are provided because they have different blast
radii, and the notification should let a human pick:
  - "terminate" kills the whole session/connection (rolls back any open
    transaction). This is almost always what you want for an
    idle-in-transaction session that's causing undo/lock pressure.
  - "cancel" / "kill query" stops only the currently running statement,
    leaving the session and any open transaction alive. Rarely what you
    want for a *long-running transaction* problem specifically (the
    transaction stays open), but included because it's the gentler option
    when the concern is one runaway query rather than an abandoned
    transaction.
"""
from __future__ import annotations


def pg_terminate_backend(pid: int) -> str:
    return f"SELECT pg_terminate_backend({int(pid)});"


def pg_cancel_backend(pid: int) -> str:
    return f"SELECT pg_cancel_backend({int(pid)});"


def mysql_kill_connection(connection_id: int) -> str:
    return f"KILL {int(connection_id)};"


def mysql_kill_query(connection_id: int) -> str:
    return f"KILL QUERY {int(connection_id)};"


def suggest_kill_commands(engine: str, pid: int) -> dict:
    """Returns both variants for the given engine, labeled, for direct use
    in an email. `pid` is a PostgreSQL backend pid or a MySQL
    trx_mysql_thread_id -- both are ints identifying a session/connection."""
    if engine == "postgres":
        return {
            "terminate_session": pg_terminate_backend(pid),
            "cancel_current_query_only": pg_cancel_backend(pid),
            "recommended_default": "terminate_session",
        }
    elif engine == "mysql":
        return {
            "terminate_session": mysql_kill_connection(pid),
            "cancel_current_query_only": mysql_kill_query(pid),
            "recommended_default": "terminate_session",
        }
    else:
        raise ValueError(f"Unknown engine: {engine}")
