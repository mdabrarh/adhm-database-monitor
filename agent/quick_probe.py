"""
The cheap, non-LLM pre-filter. Runs a handful of direct SQL queries
(reusing the exact same functions the MCP servers expose, from
common/db_pg.py and common/db_mysql.py) to decide whether a poll cycle is
worth escalating to a full Claude reasoning call.

This exists purely for cost/latency control: invoking an LLM every ~60
seconds against every database instance in a fleet doesn't scale, so we
only pay for reasoning when something already looks like a candidate.
Tune the thresholds in config/config.yaml (prefilter section) -- lower
values escalate more often (safer, more LLM calls); higher values
escalate less often (cheaper, more risk of a slow-building issue not
reaching the agent quickly).
"""
from __future__ import annotations

import logging

from common import baseline_store, db_mysql, db_pg
from common.config import AppConfig, InstanceConfig

logger = logging.getLogger("db_monitor.quick_probe")


def probe_instance(app_cfg: AppConfig, inst: InstanceConfig) -> tuple[bool, dict]:
    """Returns (is_candidate, snapshot). snapshot is always recorded to the
    baseline store regardless of is_candidate, so history accumulates even
    on quiet cycles."""
    if inst.engine == "postgres":
        conn = db_pg.connect(inst.host, inst.port, inst.database, inst.user, inst.password)
        try:
            snapshot = db_pg.get_full_snapshot(conn)
        finally:
            conn.close()
        new_deadlocks = baseline_store.get_and_update_counter_delta(
            app_cfg.baseline_db_path, inst.name, "deadlock_counter", snapshot["deadlock_count_cumulative"]
        )
    else:
        conn = db_mysql.connect(inst.host, inst.port, inst.database, inst.user, inst.password)
        try:
            snapshot = db_mysql.get_full_snapshot(conn)
        finally:
            conn.close()
        new_deadlocks = int(
            baseline_store.get_and_update_marker(
                app_cfg.baseline_db_path, inst.name, "deadlock_marker", snapshot.get("deadlock_marker")
            )
        )

    snapshot["new_deadlocks_this_cycle"] = new_deadlocks

    # Persist the numeric metrics for future baseline lookups (skip the
    # marker/string fields -- record_snapshot only accepts numeric values).
    numeric_metrics = {
        k: v for k, v in snapshot.items()
        if isinstance(v, (int, float)) and k != "timestamp"
    }
    baseline_store.record_snapshot(app_cfg.baseline_db_path, inst.name, numeric_metrics)

    pf = app_cfg.prefilter
    is_candidate = (
        snapshot["max_blocking_wait_seconds"] >= pf.max_blocking_wait_seconds
        or snapshot["undo_pressure_ratio"] >= pf.undo_pressure_ratio
        or new_deadlocks >= pf.deadlock_delta_min
        or snapshot["max_long_txn_age_seconds"] >= pf.long_txn_age_seconds
    )

    if is_candidate:
        logger.info("Instance %s flagged as candidate: %s", inst.name, snapshot)
    else:
        logger.debug("Instance %s quiet this cycle: %s", inst.name, snapshot)

    return is_candidate, snapshot
