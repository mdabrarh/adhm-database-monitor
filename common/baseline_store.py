"""
SQLite-backed history for two purposes:

1. Baselines: every poll cycle (even quiet ones) records a handful of raw
   metrics per instance. This lets the agent ask "is this normal for this
   time of day?" instead of judging a number in isolation -- the whole
   point of Section 4.4 in the design write-up. Old rows are pruned
   automatically so the file doesn't grow forever.

2. Decisions: every time the LLM actually renders a verdict (severity +
   explanation), that verdict is appended to an audit trail. scripts/mark_feedback.py
   lets a human mark a past decision as a true/false positive -- this is
   the human-in-the-loop guardrail, and the hook you'd extend if you later
   want the prefilter thresholds to adapt based on accumulated feedback.

Using SQLite (not just an append-only file) keeps "give me the avg/stddev
of undo_pressure_ratio for orders-prod-pg between 23:00-01:00 over the last
28 days" a single indexed query instead of a full log scan on every check.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    instance TEXT NOT NULL,
    metric TEXT NOT NULL,
    value REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_instance_metric_ts
    ON snapshots(instance, metric, ts);

CREATE TABLE IF NOT EXISTS markers (
    instance TEXT NOT NULL,
    metric TEXT NOT NULL,
    value TEXT,
    PRIMARY KEY (instance, metric)
);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    instance TEXT NOT NULL,
    tier TEXT NOT NULL,
    confidence REAL,
    summary TEXT,
    signals_json TEXT,
    notified INTEGER NOT NULL DEFAULT 0,
    human_feedback TEXT
);
"""


def init_db(path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def record_snapshot(path: str | Path, instance: str, metrics: dict[str, float]) -> None:
    """metrics: flat dict of metric_name -> numeric value. Non-numeric
    values (e.g. the MySQL deadlock marker string) should go through
    get_and_update_marker() instead, not this function."""
    ts = time.time()
    conn = sqlite3.connect(path)
    try:
        conn.executemany(
            "INSERT INTO snapshots (ts, instance, metric, value) VALUES (?, ?, ?, ?)",
            [(ts, instance, k, float(v)) for k, v in metrics.items() if isinstance(v, (int, float))],
        )
        conn.commit()
    finally:
        conn.close()


def get_baseline_stats(
    path: str | Path,
    instance: str,
    metric: str,
    lookback_days: int = 28,
    tolerance_minutes: int = 45,
) -> dict[str, Any]:
    """Average/stddev of `metric` for `instance`, restricted to samples
    taken within `tolerance_minutes` of the current time-of-day, over the
    trailing `lookback_days`. This is what lets the agent recognize
    "this happens every night around this time" versus a genuine anomaly.
    """
    import statistics
    from datetime import datetime, timedelta

    now = datetime.now()
    cutoff_ts = time.time() - lookback_days * 86400

    conn = sqlite3.connect(path)
    try:
        cur = conn.execute(
            "SELECT ts, value FROM snapshots WHERE instance = ? AND metric = ? AND ts >= ?",
            (instance, metric, cutoff_ts),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    matching_values = []
    for ts, value in rows:
        sample_time = datetime.fromtimestamp(ts)
        today_equivalent = sample_time.replace(
            year=now.year, month=now.month, day=now.day
        )
        delta = abs((today_equivalent - now).total_seconds()) / 60.0
        # also check the wrap-around case (e.g. now=00:05, sample=23:50)
        delta = min(delta, 1440 - delta)
        if delta <= tolerance_minutes:
            matching_values.append(value)

    if not matching_values:
        return {"n": 0, "avg": None, "stddev": None, "min": None, "max": None}

    return {
        "n": len(matching_values),
        "avg": round(statistics.mean(matching_values), 4),
        "stddev": round(statistics.pstdev(matching_values), 4) if len(matching_values) > 1 else 0.0,
        "min": round(min(matching_values), 4),
        "max": round(max(matching_values), 4),
    }


def prune_old_snapshots(path: str | Path, keep_days: int = 60) -> int:
    cutoff = time.time() - keep_days * 86400
    conn = sqlite3.connect(path)
    try:
        cur = conn.execute("DELETE FROM snapshots WHERE ts < ?", (cutoff,))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def get_and_update_marker(path: str | Path, instance: str, metric: str, new_value: str | None) -> bool:
    """Generic 'did this string-valued signal change since last time' check,
    used for MySQL's deadlock marker (InnoDB only remembers the single
    latest deadlock, so a changed timestamp == a new deadlock). Returns
    True if this is a NEW, non-empty value (i.e. a new deadlock occurred).
    """
    conn = sqlite3.connect(path)
    try:
        cur = conn.execute(
            "SELECT value FROM markers WHERE instance = ? AND metric = ?", (instance, metric)
        )
        row = cur.fetchone()
        previous = row[0] if row else None
        conn.execute(
            "INSERT INTO markers (instance, metric, value) VALUES (?, ?, ?) "
            "ON CONFLICT(instance, metric) DO UPDATE SET value = excluded.value",
            (instance, metric, new_value),
        )
        conn.commit()
    finally:
        conn.close()
    return bool(new_value) and new_value != previous


def get_and_update_counter_delta(path: str | Path, instance: str, metric: str, new_value: int) -> int:
    """For cumulative counters (e.g. Postgres pg_stat_database.deadlocks),
    returns how much the counter increased since the last poll (0 on the
    first-ever poll or if the counter was reset)."""
    conn = sqlite3.connect(path)
    try:
        cur = conn.execute(
            "SELECT value FROM markers WHERE instance = ? AND metric = ?", (instance, metric)
        )
        row = cur.fetchone()
        previous = int(row[0]) if row and row[0] is not None else None
        conn.execute(
            "INSERT INTO markers (instance, metric, value) VALUES (?, ?, ?) "
            "ON CONFLICT(instance, metric) DO UPDATE SET value = excluded.value",
            (instance, metric, str(new_value)),
        )
        conn.commit()
    finally:
        conn.close()
    if previous is None or new_value < previous:
        return 0
    return new_value - previous


def record_decision(
    path: str | Path,
    instance: str,
    tier: str,
    confidence: float | None,
    summary: str,
    signals: dict[str, Any],
    notified: bool,
) -> int:
    conn = sqlite3.connect(path)
    try:
        cur = conn.execute(
            "INSERT INTO decisions (ts, instance, tier, confidence, summary, signals_json, notified) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (time.time(), instance, tier, confidence, summary, json.dumps(signals), int(notified)),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def set_feedback(path: str | Path, decision_id: int, feedback: str) -> bool:
    conn = sqlite3.connect(path)
    try:
        cur = conn.execute(
            "UPDATE decisions SET human_feedback = ? WHERE id = ?", (feedback, decision_id)
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def list_recent_decisions(path: str | Path, limit: int = 20) -> list[dict[str, Any]]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute("SELECT * FROM decisions ORDER BY ts DESC LIMIT ?", (limit,))
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
