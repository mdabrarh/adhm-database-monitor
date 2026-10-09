#!/usr/bin/env python3
"""
Reproduces the proof-of-concept evaluation reported in the paper
("An Agentic AI Framework for Database Health Monitoring Across Engines").

Stage 1 -- deterministic evidence path only (no LLM call):
    * PostgreSQL blocking chain: evidence bundle + rendered email with the
      language-model fields held out.
    * MySQL deadlock: evidence bundle (full LATEST DETECTED DEADLOCK detail).

Stage 2 -- full pipeline, four cycles, using the UNMODIFIED
agent.monitor_agent.handle_candidate() against the live databases:
    1. PostgreSQL blocking chain (active)
    2. PostgreSQL quiescent (chain released)
    3. MySQL deadlock (just occurred)
    4. MySQL quiescent (no new deadlock)

Nothing in agent/ or common/ is changed. The runner only observes: it wraps
gather_evidence(), the Agent SDK query() stream and send_email() so that
every cycle's evidence bundle, full agent message trace, verdict and
rendered notification are written to the output directory instead of being
discarded or emailed. Those files are the raw data for Supplemental Data S1.

Usage (non-production databases only):
    psql  ... -f scripts/scenarios/setup_postgres.sql
    mysql ... < scripts/scenarios/setup_mysql.sql
    python scripts/run_paper_experiments.py --stage all \
        --pg-instance local-pg --mysql-instance local-mysql --out results/

The scenarios need write access to their test tables, while the monitor
itself stays read-only. Supply writer credentials with the environment
variables SCENARIO_PG_USER / SCENARIO_PG_PASSWORD and
SCENARIO_MYSQL_USER / SCENARIO_MYSQL_PASSWORD (default: the instance's own
credentials from config.yaml).

Stage 2 calls the Anthropic API (model = anthropic_model in config.yaml),
so it costs a small amount and its wording will differ between runs: LLM
output is not deterministic.
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import datetime as dt
import importlib.metadata as md
import json
import os
import platform
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import claude_client, evidence as evidence_mod, monitor_agent, notifier, quick_probe  # noqa: E402
from common import baseline_store, db_mysql, db_pg  # noqa: E402
from common.config import AppConfig, InstanceConfig, load_config  # noqa: E402
from scripts.scenarios.mysql_deadlock import run_deadlock  # noqa: E402
from scripts.scenarios.pg_blocking_chain import BlockingChain  # noqa: E402

HELD_OUT_VERDICT = {
    "tier": "n/a",
    "confidence": None,
    "summary": "[Stage 1: language-model fields deliberately held out; evidence path only]",
    "recommended_action": None,
}


# --------------------------------------------------------------------------- helpers
def _jsonable(obj):
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {"_type": type(obj).__name__, **{k: _jsonable(v) for k, v in dataclasses.asdict(obj).items()}}
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    if isinstance(obj, (dt.datetime, dt.date)):
        return obj.isoformat()
    return repr(obj)


def _write(out: Path, name: str, data) -> None:
    out.mkdir(parents=True, exist_ok=True)
    path = out / name
    if isinstance(data, str):
        path.write_text(data)
    else:
        path.write_text(json.dumps(_jsonable(data), indent=2, default=str))
    print(f"  wrote {path}")


def _instance(app_cfg: AppConfig, name: str, engine: str) -> InstanceConfig:
    for inst in app_cfg.instances:
        if inst.name == name:
            if inst.engine != engine:
                raise SystemExit(f"Instance '{name}' is {inst.engine}, expected {engine}.")
            return inst
    raise SystemExit(f"Instance '{name}' not found in config.yaml.")


def _writer_creds(inst: InstanceConfig, prefix: str) -> tuple[str, str]:
    return (os.environ.get(f"SCENARIO_{prefix}_USER", inst.user),
            os.environ.get(f"SCENARIO_{prefix}_PASSWORD", inst.password))


def _environment(app_cfg: AppConfig, pg: InstanceConfig | None, my: InstanceConfig | None) -> dict:
    pkgs = {}
    for p in ("claude-agent-sdk", "fastmcp", "psycopg2-binary", "mysql-connector-python", "PyYAML", "python-dotenv"):
        try:
            pkgs[p] = md.version(p)
        except md.PackageNotFoundError:
            pkgs[p] = None
    env = {
        "run_started_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "python": sys.version,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "cpu_count": os.cpu_count(),
        "packages": pkgs,
        "anthropic_model": app_cfg.anthropic_model,
        "max_agent_turns": app_cfg.max_agent_turns,
        "prefilter": dataclasses.asdict(app_cfg.prefilter),
    }
    if pg:
        conn = db_pg.connect(pg.host, pg.port, pg.database, pg.user, pg.password)
        with conn.cursor() as cur:
            cur.execute("SELECT version()")
            env["postgres_version"] = cur.fetchone()[0]
        conn.close()
    if my:
        conn = db_mysql.connect(my.host, my.port, my.database, my.user, my.password)
        cur = conn.cursor()
        cur.execute("SELECT VERSION()")
        env["mysql_version"] = cur.fetchone()[0]
        conn.close()
    return env


# --------------------------------------------------------------------------- stage 1
def stage1(app_cfg: AppConfig, pg: InstanceConfig | None, my: InstanceConfig | None,
           out: Path, settle: float) -> None:
    print("Stage 1: deterministic evidence path (no LLM)")
    if pg:
        user, pw = _writer_creds(pg, "PG")
        with BlockingChain(pg.host, pg.port, pg.database, user, pw) as chain:
            time.sleep(settle)
            ev = evidence_mod.gather_evidence(pg, app_cfg.prefilter)
            ev["_scenario"] = {"blocker_pid": chain.blocker_pid, "waiter_pid": chain.waiter_pid,
                               "polled_after_seconds": settle}
        _write(out, "stage1_pg_blocking_evidence.json", ev)
        _, body = notifier.format_alert_email(pg.name, "n/a", HELD_OUT_VERDICT, ev)
        _write(out, "stage1_pg_blocking_email.txt", body)
    if my:
        user, pw = _writer_creds(my, "MYSQL")
        victim = run_deadlock(my.host, my.port, my.database, user, pw)
        ev = evidence_mod.gather_evidence(my, app_cfg.prefilter)
        ev["_scenario"] = victim
        _write(out, "stage1_mysql_deadlock_evidence.json", ev)


# --------------------------------------------------------------------------- stage 2
class Recorder:
    """Observes handle_candidate() without changing it."""

    def __init__(self):
        self.evidence = None
        self.messages: list = []
        self.verdict = None
        self.email = None
        self._orig_gather = evidence_mod.gather_evidence
        self._orig_query = claude_client.query
        self._orig_run = claude_client.run_agent_query
        self._orig_send = notifier.send_email

    def install(self) -> None:
        rec = self

        def gather(*a, **k):
            rec.evidence = rec._orig_gather(*a, **k)
            return rec.evidence

        async def query(*a, **k):
            async for m in rec._orig_query(*a, **k):
                rec.messages.append(m)
                yield m

        async def run(*a, **k):
            rec.verdict = await rec._orig_run(*a, **k)
            return rec.verdict

        def send(cfg, subject, body):  # capture instead of emailing
            rec.email = {"subject": subject, "body": body}

        evidence_mod.gather_evidence = gather
        claude_client.query = query
        claude_client.run_agent_query = run
        notifier.send_email = send

    def reset(self) -> None:
        self.evidence, self.messages, self.verdict, self.email = None, [], None, None

    def dump(self, out: Path, prefix: str, extra: dict) -> None:
        _write(out, f"{prefix}.json", {
            **extra,
            "evidence": self.evidence,
            "verdict": self.verdict,
            "notified": self.email is not None,
            "agent_trace": self.messages,
        })
        if self.email:
            _write(out, f"{prefix}_email.txt", f"Subject: {self.email['subject']}\n\n{self.email['body']}")


async def _cycle(app_cfg, inst, rec, out, prefix, label) -> None:
    rec.reset()
    is_candidate, snapshot = quick_probe.probe_instance(app_cfg, inst)
    t0 = time.time()
    # Quiescent cycles are handed to the agent even when the pre-filter would
    # not escalate them -- the point is to test whether the agent itself
    # correctly withholds a notification.
    await monitor_agent.handle_candidate(app_cfg, inst, snapshot)
    rec.dump(out, prefix, {"cycle": label, "instance": inst.name, "prefilter_flagged": is_candidate,
                           "prefilter_snapshot": snapshot, "agent_wall_seconds": round(time.time() - t0, 2)})


async def stage2(app_cfg: AppConfig, pg: InstanceConfig | None, my: InstanceConfig | None,
                 out: Path, settle: float) -> None:
    print("Stage 2: full pipeline, live reasoning (calls the Anthropic API)")
    rec = Recorder()
    rec.install()
    if pg:
        user, pw = _writer_creds(pg, "PG")
        with BlockingChain(pg.host, pg.port, pg.database, user, pw):
            time.sleep(settle)
            await _cycle(app_cfg, pg, rec, out, "stage2_cycle1_pg_blocking", "1: PostgreSQL blocking chain")
        await _cycle(app_cfg, pg, rec, out, "stage2_cycle2_pg_quiescent", "2: PostgreSQL quiescent")
    if my:
        user, pw = _writer_creds(my, "MYSQL")
        quick_probe.probe_instance(app_cfg, my)  # record the current deadlock marker first
        run_deadlock(my.host, my.port, my.database, user, pw)
        await _cycle(app_cfg, my, rec, out, "stage2_cycle3_mysql_deadlock", "3: MySQL deadlock")
        await _cycle(app_cfg, my, rec, out, "stage2_cycle4_mysql_quiescent", "4: MySQL quiescent")


# --------------------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=os.environ.get("DB_MONITOR_CONFIG", "config/config.yaml"))
    ap.add_argument("--stage", choices=["1", "2", "all"], default="all")
    ap.add_argument("--pg-instance", help="name of a postgres instance in config.yaml")
    ap.add_argument("--mysql-instance", help="name of a mysql instance in config.yaml")
    ap.add_argument("--out", default="results")
    ap.add_argument("--settle-seconds", type=float, default=3.0,
                    help="how long the blocking chain is left to accumulate wait before polling (paper: 3)")
    ap.add_argument("--kill-threshold-seconds", type=float, default=2.0,
                    help="lowered kill-suggestion age so the termination-command path is exercised "
                         "within seconds (production default: 600)")
    args = ap.parse_args()
    if not (args.pg_instance or args.mysql_instance):
        ap.error("give --pg-instance and/or --mysql-instance")

    app_cfg = load_config(args.config)
    app_cfg.prefilter.kill_suggestion_age_seconds = args.kill_threshold_seconds
    monitor_agent._setup_logging(app_cfg.log_level)
    baseline_store.init_db(app_cfg.baseline_db_path)

    pg = _instance(app_cfg, args.pg_instance, "postgres") if args.pg_instance else None
    my = _instance(app_cfg, args.mysql_instance, "mysql") if args.mysql_instance else None
    out = Path(args.out)
    _write(out, "environment.json", _environment(app_cfg, pg, my))

    if args.stage in ("1", "all"):
        stage1(app_cfg, pg, my, out, args.settle_seconds)
    if args.stage in ("2", "all"):
        asyncio.run(stage2(app_cfg, pg, my, out, args.settle_seconds))
    print(f"Done. Zip {out}/ as Supplemental Data S1.")


if __name__ == "__main__":
    main()
