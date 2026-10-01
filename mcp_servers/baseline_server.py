#!/usr/bin/env python3
"""
MCP (stdio) server exposing the historical-baseline lookup as a tool, so
the agent can ask "is this normal for this time of day?" instead of judging
a raw number alone. Always attached alongside whichever engine-specific
server (postgres_server.py / mysql_server.py) is active for the current
instance -- see agent/claude_client.py.
"""
from __future__ import annotations

import os
import sys

from fastmcp import FastMCP

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common import baseline_store  # noqa: E402

mcp = FastMCP("baseline")

_BASELINE_DB_PATH = os.environ["BASELINE_DB_PATH"]
_LABEL = os.environ.get("DB_INSTANCE_LABEL", "unknown")


@mcp.tool()
def get_historical_baseline(metric: str, lookback_days: int = 28, tolerance_minutes: int = 45) -> dict:
    """Return the average/stddev/min/max of a named metric for THIS
    instance, computed only from samples taken within tolerance_minutes of
    the current time-of-day over the trailing lookback_days. Use this to
    tell whether a currently-elevated reading (e.g. undo_pressure_ratio,
    max_blocking_wait_seconds) is typical for this time of day/night (e.g.
    a recurring batch job) or genuinely anomalous. Valid metric names match
    the keys returned by this instance's get_undo_pressure / get_blocking_chains
    / etc. tools, e.g. "undo_pressure_ratio", "max_blocking_wait_seconds",
    "history_list_length". n=0 means no history yet for this metric --
    treat that as inconclusive, not as "normal"."""
    stats = baseline_store.get_baseline_stats(
        _BASELINE_DB_PATH, _LABEL, metric, lookback_days=lookback_days, tolerance_minutes=tolerance_minutes
    )
    return {"instance": _LABEL, "metric": metric, **stats}


@mcp.tool()
def get_recent_decisions(limit: int = 5) -> dict:
    """Return this instance's most recent past agent verdicts (severity,
    explanation, and any human feedback marking them as true/false
    positives), so you can factor in whether similar situations were
    previously judged correctly or incorrectly before rendering a new
    verdict. This is the one tool that reads across ALL instances' history,
    then the caller should mentally filter to entries where instance
    matches this one."""
    decisions = baseline_store.list_recent_decisions(_BASELINE_DB_PATH, limit=limit * 4)
    filtered = [d for d in decisions if d.get("instance") == _LABEL][:limit]
    return {"instance": _LABEL, "recent_decisions": filtered}


if __name__ == "__main__":
    mcp.run()
