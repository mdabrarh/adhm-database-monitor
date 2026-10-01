"""
Configuration loader for the agentic database monitor.

Loads config/config.yaml (list of database instances + thresholds + polling
settings) and merges in secrets from environment variables (loaded from
.env via python-dotenv). Nothing sensitive lives in the YAML file itself --
passwords, API keys, and SMTP credentials are always referenced by
environment variable name, so config.yaml is safe to commit to git while
.env is not.

This is the file to edit first when you add a new database instance,
change polling cadence, or retune the "is this worth waking Claude up for"
thresholds.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv

load_dotenv()  # populates os.environ from a local .env file if present

ENGINE_POSTGRES = "postgres"
ENGINE_MYSQL = "mysql"


@dataclass
class InstanceConfig:
    name: str  # friendly label, e.g. "orders-prod-pg". Used as the baseline/history key.
    engine: Literal["postgres", "mysql"]
    host: str
    port: int
    database: str
    user: str
    password: str  # resolved from env, not stored in yaml directly
    # Optional, Postgres only: local path to the server log, used on a
    # best-effort basis to pull deadlock participant detail (see
    # common/db_pg.py:get_recent_deadlock_log_excerpt). Only works if this
    # process can read the file directly (i.e. running on the DB host, or
    # the log is mounted here) -- leave unset for RDS/managed instances.
    postgres_log_path: str | None = None

    def as_env(self, extra: dict | None = None) -> dict:
        """Environment variables passed to the spawned MCP server subprocess
        so it knows which instance to connect to for this one query."""
        env = {
            "DB_INSTANCE_LABEL": self.name,
            "DB_ENGINE": self.engine,
            "DB_HOST": self.host,
            "DB_PORT": str(self.port),
            "DB_NAME": self.database,
            "DB_USER": self.user,
            "DB_PASSWORD": self.password,
        }
        if extra:
            env.update(extra)
        return env


@dataclass
class PrefilterConfig:
    """Cheap, non-LLM thresholds that decide whether a given poll cycle is
    even worth handing to Claude for full reasoning. Keep these low/sensitive
    -- false positives here just cost one extra (cheap) LLM call; false
    negatives here mean a real incident never reaches the agent at all."""
    max_blocking_wait_seconds: float = 15.0
    undo_pressure_ratio: float = 0.4          # 0-1 normalized undo/version-store pressure
    deadlock_delta_min: int = 1               # any new deadlock since last poll
    long_txn_age_seconds: float = 120.0
    # Separate, higher bar than long_txn_age_seconds above: this is the age
    # at which a session is considered a kill CANDIDATE and gets a
    # ready-to-copy termination command included in the notification email.
    # It does NOT cause anything to be killed -- see common/kill_commands.py
    # and README "Safety model". Default 600s = 10 minutes, per the original
    # request this feature was built for.
    kill_suggestion_age_seconds: float = 600.0


@dataclass
class NotifyConfig:
    min_tier: Literal["low", "high"] = "low"  # tiers below this are logged only, never emailed
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    email_from: str = ""
    email_to: list[str] = field(default_factory=list)
    use_tls: bool = True


@dataclass
class AppConfig:
    instances: list[InstanceConfig]
    poll_interval_seconds: int
    prefilter: PrefilterConfig
    notify: NotifyConfig
    baseline_db_path: str
    decisions_log_path: str
    anthropic_model: str
    max_agent_turns: int
    log_level: str = "INFO"


def _resolve_secret(value) -> str:
    """config.yaml stores secrets as {"env": "SOME_ENV_VAR"} rather than
    literal values. This resolves that indirection."""
    if isinstance(value, dict) and "env" in value:
        resolved = os.environ.get(value["env"], "")
        if not resolved:
            raise RuntimeError(
                f"Environment variable '{value['env']}' is required but not set. "
                f"Set it in your .env file (see .env.example)."
            )
        return resolved
    return str(value)


def load_config(path: str | Path = "config/config.yaml") -> AppConfig:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found at {path}. Copy config/config.example.yaml "
            f"to config/config.yaml and edit it for your environment."
        )
    raw = yaml.safe_load(path.read_text())

    instances = []
    for inst in raw["instances"]:
        instances.append(
            InstanceConfig(
                name=inst["name"],
                engine=inst["engine"],
                host=inst["host"],
                port=int(inst["port"]),
                database=inst["database"],
                user=inst["user"],
                password=_resolve_secret(inst["password"]),
                postgres_log_path=inst.get("postgres_log_path"),
            )
        )

    pf = raw.get("prefilter", {})
    prefilter = PrefilterConfig(
        max_blocking_wait_seconds=float(pf.get("max_blocking_wait_seconds", 15.0)),
        undo_pressure_ratio=float(pf.get("undo_pressure_ratio", 0.4)),
        deadlock_delta_min=int(pf.get("deadlock_delta_min", 1)),
        long_txn_age_seconds=float(pf.get("long_txn_age_seconds", 120.0)),
        kill_suggestion_age_seconds=float(pf.get("kill_suggestion_age_seconds", 600.0)),
    )

    nf = raw.get("notify", {})
    notify = NotifyConfig(
        min_tier=nf.get("min_tier", "low"),
        smtp_host=nf.get("smtp_host", ""),
        smtp_port=int(nf.get("smtp_port", 587)),
        smtp_user=_resolve_secret(nf.get("smtp_user", "")) if nf.get("smtp_user") else "",
        smtp_password=_resolve_secret(nf.get("smtp_password", "")) if nf.get("smtp_password") else "",
        email_from=nf.get("email_from", ""),
        email_to=nf.get("email_to", []),
        use_tls=bool(nf.get("use_tls", True)),
    )

    return AppConfig(
        instances=instances,
        poll_interval_seconds=int(raw.get("poll_interval_seconds", 60)),
        prefilter=prefilter,
        notify=notify,
        baseline_db_path=raw.get("baseline_db_path", "data/baseline.sqlite3"),
        decisions_log_path=raw.get("decisions_log_path", "data/decisions.jsonl"),
        anthropic_model=raw.get("anthropic_model", "claude-sonnet-5"),
        max_agent_turns=int(raw.get("max_agent_turns", 6)),
        log_level=raw.get("log_level", "INFO"),
    )
