#!/usr/bin/env python3
"""
Human-in-the-loop feedback CLI. Lets you mark a past agent verdict as a
true or false positive, closing the audit-trail loop described in the
architecture (Section 4.6 of the accompanying paper): every disagreement
between the agent and a human is recorded, not silently discarded.

Usage:
    python -m scripts.mark_feedback --list                      # show recent decisions
    python -m scripts.mark_feedback 42 false_positive
    python -m scripts.mark_feedback 42 true_positive

This is currently a manual review tool. A natural next extension (see
README.md "Customizing further") is to have quick_probe.py read accumulated
feedback and auto-adjust prefilter thresholds for instances with a high
false-positive rate.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from common import baseline_store
from common.config import load_config


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("decision_id", nargs="?", type=int)
    parser.add_argument("feedback", nargs="?", choices=["true_positive", "false_positive"])
    parser.add_argument("--list", action="store_true", help="List recent decisions instead of setting feedback")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--config", default=os.environ.get("DB_MONITOR_CONFIG", "config/config.yaml"))
    args = parser.parse_args()

    app_cfg = load_config(args.config)

    if args.list or not args.decision_id:
        rows = baseline_store.list_recent_decisions(app_cfg.baseline_db_path, limit=args.limit)
        for r in rows:
            print(
                f"[{r['id']}] {r['instance']:<20} tier={r['tier']:<5} "
                f"notified={bool(r['notified'])} feedback={r['human_feedback']}\n"
                f"      {r['summary']}\n"
            )
        return

    ok = baseline_store.set_feedback(app_cfg.baseline_db_path, args.decision_id, args.feedback)
    if ok:
        print(f"Decision {args.decision_id} marked as {args.feedback}.")
    else:
        print(f"No decision found with id {args.decision_id}.")


if __name__ == "__main__":
    main()
