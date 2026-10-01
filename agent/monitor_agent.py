#!/usr/bin/env python3
"""
Entry point. Runs forever: every `poll_interval_seconds`, cheaply probes
every configured instance, and for any instance the pre-filter flags,
spins up a full Claude + MCP reasoning cycle and, depending on the
verdict's tier, sends an email.

Run directly for testing:
    python -m agent.monitor_agent

In production this is what deploy/db-monitor-agent.service execs under
systemd -- see deploy/EC2_SETUP.md.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent import claude_client, evidence as evidence_mod, notifier, quick_probe
from agent.prompts import build_user_prompt
from common import baseline_store
from common.config import AppConfig, InstanceConfig, load_config

MCP_SERVERS_DIR = os.path.join(os.path.dirname(__file__), "..", "mcp_servers")

logger = logging.getLogger("db_monitor")

_TIER_RANK = {"none": 0, "low": 1, "high": 2}


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,  # systemd/journald captures stdout
    )


async def handle_candidate(app_cfg: AppConfig, inst: InstanceConfig, snapshot: dict) -> None:
    logger.info("Escalating %s to Claude for full reasoning...", inst.name)

    # Deterministic, full-detail evidence -- exact query text, ages, and
    # deadlock detail -- gathered directly from the database. This is what
    # both the agent reasons over AND what the email renders verbatim, so
    # the notification is never missing a category regardless of which
    # single signal tripped the pre-filter this cycle.
    try:
        full_evidence = evidence_mod.gather_evidence(inst, app_cfg.prefilter)
    except Exception:
        logger.exception("Evidence gathering failed for %s -- falling back to pre-filter snapshot only.", inst.name)
        full_evidence = {"engine": inst.engine, "instance": inst.name, **snapshot}

    instance_env = inst.as_env()
    user_prompt = build_user_prompt(inst.name, inst.engine, full_evidence)

    try:
        verdict = await claude_client.run_agent_query(
            instance_env=instance_env,
            baseline_db_path=app_cfg.baseline_db_path,
            engine=inst.engine,
            user_prompt=user_prompt,
            mcp_servers_dir=MCP_SERVERS_DIR,
            model=app_cfg.anthropic_model,
            max_turns=app_cfg.max_agent_turns,
        )
    except json.JSONDecodeError:
        logger.exception("Agent response for %s wasn't valid JSON -- skipping this cycle's verdict.", inst.name)
        return
    except Exception:
        logger.exception("Agent query failed for %s -- skipping this cycle's verdict.", inst.name)
        return

    tier = verdict.get("tier", "none")
    logger.info("Verdict for %s: tier=%s confidence=%s", inst.name, tier, verdict.get("confidence"))

    should_notify = _TIER_RANK.get(tier, 0) >= _TIER_RANK.get(app_cfg.notify.min_tier, 1) and tier != "none"

    decision_id = baseline_store.record_decision(
        app_cfg.baseline_db_path,
        instance=inst.name,
        tier=tier,
        confidence=verdict.get("confidence"),
        summary=verdict.get("summary", ""),
        signals=snapshot,
        notified=should_notify,
    )
    logger.info("Recorded decision #%s for %s (notified=%s)", decision_id, inst.name, should_notify)

    if should_notify:
        subject, body = notifier.format_alert_email(inst.name, tier, verdict, full_evidence, decision_id=decision_id)
        notifier.send_email(app_cfg.notify, subject, body)


async def run_cycle(app_cfg: AppConfig) -> None:
    for inst in app_cfg.instances:
        try:
            is_candidate, snapshot = quick_probe.probe_instance(app_cfg, inst)
        except Exception:
            logger.exception("Pre-filter probe failed for instance %s -- check connectivity/credentials.", inst.name)
            continue

        if is_candidate:
            await handle_candidate(app_cfg, inst, snapshot)


async def main_loop() -> None:
    app_cfg = load_config(os.environ.get("DB_MONITOR_CONFIG", "config/config.yaml"))
    _setup_logging(app_cfg.log_level)
    baseline_store.init_db(app_cfg.baseline_db_path)

    logger.info(
        "Starting db-agentic-monitor: %d instance(s), poll every %ds, notify threshold=%s",
        len(app_cfg.instances), app_cfg.poll_interval_seconds, app_cfg.notify.min_tier,
    )

    last_prune = 0.0
    while True:
        cycle_start = time.time()
        try:
            await run_cycle(app_cfg)
        except Exception:
            # A bug in one cycle should never take the whole service down --
            # log it and try again next interval.
            logger.exception("Unhandled error in monitoring cycle")

        if cycle_start - last_prune > 86400:
            removed = baseline_store.prune_old_snapshots(app_cfg.baseline_db_path, keep_days=60)
            logger.info("Pruned %d old baseline rows", removed)
            last_prune = cycle_start

        elapsed = time.time() - cycle_start
        sleep_for = max(app_cfg.poll_interval_seconds - elapsed, 1.0)
        await asyncio.sleep(sleep_for)


if __name__ == "__main__":
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        logger.info("Shutting down (KeyboardInterrupt).")
