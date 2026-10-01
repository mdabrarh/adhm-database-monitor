"""
Deterministic evidence gathering -- the ground truth attached to every
notification email, independent of anything the LLM says.

Why this exists as a separate step from the Claude/MCP reasoning call:
the paper's guardrail design (Section 4.6) requires that every
notification include the underlying raw data so a human can verify the
agent's reasoning, not just trust a natural-language summary. It also
means anything precise and potentially dangerous if wrong -- specifically,
the exact process id and SQL syntax in a suggested kill command -- is
computed by plain Python from a live query result, never transcribed by
the LLM into free text where a hallucinated digit could end up copy-pasted
into a production terminal.

agent/monitor_agent.py calls gather_evidence() once per escalated cycle;
the result is used both to enrich the prompt Claude reasons over AND,
unmodified, to render the email in agent/notifier.py.
"""
from __future__ import annotations

from typing import Any

from common import db_mysql, db_pg, kill_commands
from common.config import InstanceConfig, PrefilterConfig


def _connect(inst: InstanceConfig):
    if inst.engine == "postgres":
        return db_pg.connect(inst.host, inst.port, inst.database, inst.user, inst.password)
    return db_mysql.connect(inst.host, inst.port, inst.database, inst.user, inst.password)


def _gather_postgres(conn, inst: InstanceConfig, kill_threshold: float) -> dict[str, Any]:
    blocking_chains = db_pg.get_blocking_chains(conn)
    for chain in blocking_chains:
        age = chain.get("blocking_txn_age_seconds") or 0
        if age >= kill_threshold:
            chain["blocking_session_kill_commands"] = kill_commands.suggest_kill_commands(
                "postgres", chain["blocking_pid"]
            )

    long_txns = db_pg.get_long_transactions(conn, min_age_seconds=10.0)
    for txn in long_txns:
        age = txn.get("txn_age_seconds") or 0
        txn["should_consider_kill"] = age >= kill_threshold
        if age >= kill_threshold:
            txn["kill_commands"] = kill_commands.suggest_kill_commands("postgres", txn["pid"])

    undo = db_pg.get_undo_pressure(conn)
    wait_events = db_pg.get_wait_events(conn, top_n=10)
    deadlock_count = db_pg.get_deadlock_count(conn)

    log_excerpts: list[str] = []
    if inst.postgres_log_path:
        log_excerpts = db_pg.get_recent_deadlock_log_excerpt(inst.postgres_log_path)

    deadlock_detail = {
        "deadlocks_cumulative": deadlock_count,
        "participant_detail_available": bool(log_excerpts),
        "log_excerpts": log_excerpts,
        "limitation_note": (
            None
            if log_excerpts
            else (
                "PostgreSQL does not expose deadlock participants/queries through SQL system "
                "views -- only a cumulative counter. Participant-level detail requires reading "
                "the server log (enable log_lock_waits) and configuring postgres_log_path for "
                "this instance in config.yaml. Not configured or no recent match for this instance."
            )
        ),
    }

    return {
        "engine": "postgres",
        "instance": inst.name,
        "blocking_chains": blocking_chains,
        "long_transactions": long_txns,
        "undo_pressure": undo,
        "wait_events": wait_events,
        "deadlock": deadlock_detail,
    }


def _gather_mysql(conn, inst: InstanceConfig, kill_threshold: float) -> dict[str, Any]:
    blocking_chains = db_mysql.get_blocking_chains(conn)
    for chain in blocking_chains:
        age = chain.get("blocking_txn_age_seconds") or 0
        if age >= kill_threshold:
            chain["blocking_session_kill_commands"] = kill_commands.suggest_kill_commands(
                "mysql", chain["blocking_pid"]
            )

    long_txns = db_mysql.get_long_transactions(conn, min_age_seconds=10.0)
    for txn in long_txns:
        age = txn.get("txn_age_seconds") or 0
        txn["should_consider_kill"] = age >= kill_threshold
        if age >= kill_threshold:
            txn["kill_commands"] = kill_commands.suggest_kill_commands("mysql", txn["pid"])

    undo = db_mysql.get_undo_pressure(conn)
    wait_events = db_mysql.get_wait_events(conn, top_n=10)

    status_text = db_mysql.get_innodb_status_text(conn)
    dl = db_mysql.parse_latest_deadlock(status_text)
    deadlock_detail = {
        "deadlock_detected": dl is not None,
        "participant_detail_available": dl is not None,
        "timestamp": dl["timestamp"] if dl else None,
        # InnoDB's own status text already names each participating
        # transaction, the query it was running, the lock it wanted, and
        # which transaction was rolled back -- verbatim is more trustworthy
        # here than trying to regex it into separate fields.
        "raw_detail": dl["raw_text"] if dl else None,
        "limitation_note": None if dl else "InnoDB has not recorded a deadlock since server startup.",
    }

    return {
        "engine": "mysql",
        "instance": inst.name,
        "blocking_chains": blocking_chains,
        "long_transactions": long_txns,
        "undo_pressure": undo,
        "wait_events": wait_events,
        "deadlock": deadlock_detail,
    }


def gather_evidence(inst: InstanceConfig, prefilter: PrefilterConfig) -> dict[str, Any]:
    """Connects fresh, pulls the full diagnostic picture (not just the
    aggregate numbers quick_probe.py uses), and closes the connection.
    Safe to call even when the specific signal that triggered escalation
    was, say, undo pressure -- it always gathers ALL categories, so the
    resulting email is never missing a section regardless of which signal
    tripped the pre-filter (this is what was explicitly asked for)."""
    conn = _connect(inst)
    try:
        if inst.engine == "postgres":
            return _gather_postgres(conn, inst, prefilter.kill_suggestion_age_seconds)
        return _gather_mysql(conn, inst, prefilter.kill_suggestion_age_seconds)
    finally:
        conn.close()
