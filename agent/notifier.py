"""
Email notification via plain SMTP, plus the full alert-email formatter.

Kept deliberately simple (no third-party email API dependency) so it works
with Gmail app passwords, Amazon SES SMTP credentials, or any other SMTP
relay by just changing config/env values.

format_alert_email() is the important function to read if you're
customizing what a notification contains: every section (blocking,
long-running/kill candidates, undo pressure, deadlocks, wait events) is
always rendered, regardless of which single signal caused the pre-filter
to escalate this cycle, per the "every notification should contain all
related details" requirement this was built for.

To add Slack instead of/in addition to email, add a send_slack(cfg, text)
function here following the same shape and call it alongside send_email()
in agent/monitor_agent.py -- format_alert_email()'s returned body is plain
text and posts fine into a Slack message as-is.
"""
from __future__ import annotations

import logging
import smtplib
from email.mime.text import MIMEText

from common.config import NotifyConfig

logger = logging.getLogger("db_monitor.notifier")


def send_email(cfg: NotifyConfig, subject: str, body: str) -> None:
    if not cfg.smtp_host or not cfg.email_to:
        logger.warning("Email not configured (smtp_host/email_to missing) -- skipping notification: %s", subject)
        return

    msg = MIMEText(body, "plain")
    msg["Subject"] = subject
    msg["From"] = cfg.email_from or cfg.smtp_user
    msg["To"] = ", ".join(cfg.email_to)

    try:
        with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=15) as server:
            if cfg.use_tls:
                server.starttls()
            if cfg.smtp_user:
                server.login(cfg.smtp_user, cfg.smtp_password)
            server.sendmail(msg["From"], cfg.email_to, msg.as_string())
        logger.info("Sent email notification: %s", subject)
    except Exception:
        logger.exception("Failed to send email notification: %s", subject)


_RULE = "-" * 70


def _num(value, default: float = 0.0) -> float:
    """Coerce a metric to float defensively. Postgres/MySQL drivers return
    Decimal for these fields; if the evidence bundle was ever round-tripped
    through JSON (e.g. read back from an audit log rather than used live),
    Decimal becomes a plain string -- round() rejects both str and, on some
    Python versions, mixing Decimal with a float default, so every call
    site normalizes through here instead of calling round() directly."""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _section(title: str) -> str:
    return f"\n{_RULE}\n{title}\n{_RULE}\n"


def _fmt_query(query: str | None, max_len: int = 500) -> str:
    if not query:
        return "(no query text available)"
    query = " ".join(query.split())  # collapse whitespace/newlines for readability
    return query if len(query) <= max_len else query[:max_len] + " ...[truncated]"


def _render_blocking_section(evidence: dict, verdict: dict) -> str:
    chains = evidence.get("blocking_chains", [])
    out = _section("LOCKING / BLOCKING CHAINS")
    if not chains:
        out += "No sessions currently blocked on a lock.\n"
        return out
    for i, c in enumerate(chains, 1):
        out += (
            f"\n[{i}] Blocked session (pid {c.get('blocked_pid')}, waited "
            f"{round(_num(c.get('blocked_wait_seconds')), 1)}s):\n"
            f"    Query:  {_fmt_query(c.get('blocked_query'))}\n"
            f"    Blocked by session (pid {c.get('blocking_pid')}, txn age "
            f"{round(_num(c.get('blocking_txn_age_seconds')), 1)}s, state={c.get('blocking_state')}):\n"
            f"    Query:  {_fmt_query(c.get('blocking_query'))}\n"
        )
        kc = c.get("blocking_session_kill_commands")
        if kc:
            out += (
                f"    >> Blocking session has been open past the kill-suggestion threshold.\n"
                f"       SUGGESTED COMMAND (NOT EXECUTED -- review and run manually if appropriate):\n"
                f"       {kc['terminate_session']}\n"
                f"       (gentler alternative, leaves the transaction open: {kc['cancel_current_query_only']})\n"
            )
    out += f"\nAgent's recommendation: {verdict.get('blocking_recommendation', 'n/a')}\n"
    return out


def _render_long_txn_section(evidence: dict, verdict: dict) -> str:
    txns = evidence.get("long_transactions", [])
    out = _section("LONG-RUNNING / IDLE-IN-TRANSACTION SESSIONS")
    if not txns:
        out += "No long-running transactions detected this cycle.\n"
        return out
    for i, t in enumerate(txns, 1):
        age = round(_num(t.get("txn_age_seconds")), 1)
        out += (
            f"\n[{i}] pid {t.get('pid')}  state={t.get('state')}  age={age}s\n"
            f"    Query: {_fmt_query(t.get('query'))}\n"
        )
        if t.get("should_consider_kill"):
            kc = t["kill_commands"]
            out += (
                f"    >> Open longer than the kill-suggestion threshold.\n"
                f"       SUGGESTED COMMAND (NOT EXECUTED -- review and run manually if appropriate):\n"
                f"       {kc['terminate_session']}\n"
                f"       (gentler alternative, leaves the transaction open: {kc['cancel_current_query_only']})\n"
            )
    out += f"\nAgent's kill recommendation: {verdict.get('kill_recommendation', 'n/a')}\n"
    return out


def _render_undo_section(evidence: dict, verdict: dict) -> str:
    undo = evidence.get("undo_pressure", {})
    out = _section("UNDO / ROLLBACK PRESSURE")
    if evidence.get("engine") == "mysql":
        out += (
            f"InnoDB History List Length: {undo.get('history_list_length')}\n"
            f"Normalized pressure ratio:  {undo.get('undo_pressure_ratio')}\n"
        )
    else:
        out += (
            f"Dead tuple ratio:              {undo.get('dead_tuple_ratio')}\n"
            f"Oldest open transaction age:   {undo.get('oldest_open_txn_age_seconds')}s\n"
            f"XID wraparound age:            {undo.get('xid_wraparound_age')} "
            f"(ratio {undo.get('xid_wraparound_ratio')})\n"
            f"Normalized pressure ratio:      {undo.get('undo_pressure_ratio')}\n"
        )
    out += f"\nLikely contributor / recommendation: {verdict.get('undo_pressure_recommendation', 'n/a')}\n"
    return out


def _render_deadlock_section(evidence: dict, verdict: dict) -> str:
    dl = evidence.get("deadlock", {})
    out = _section("DEADLOCKS")
    if evidence.get("engine") == "mysql":
        if dl.get("deadlock_detected"):
            out += f"Most recent deadlock timestamp: {dl.get('timestamp')}\n\n"
            out += "Full InnoDB deadlock detail (participants, locks held/wanted, victim):\n"
            out += f"{dl.get('raw_detail')}\n"
        else:
            out += f"{dl.get('limitation_note')}\n"
    else:
        out += f"Cumulative deadlock counter: {dl.get('deadlocks_cumulative')}\n"
        if dl.get("participant_detail_available"):
            out += "\nRecent deadlock log excerpt(s):\n"
            for excerpt in dl.get("log_excerpts", []):
                out += f"{excerpt}\n"
        else:
            out += f"\n{dl.get('limitation_note')}\n"
    out += f"\nAgent's recommendation: {verdict.get('deadlock_recommendation', 'n/a')}\n"
    return out


def _render_wait_events_section(evidence: dict, verdict: dict) -> str:
    events = evidence.get("wait_events", [])
    out = _section("WAIT EVENTS")
    if not events:
        out += "No significant wait events this cycle.\n"
    else:
        for e in events:
            if evidence.get("engine") == "mysql":
                # total_wait_seconds is a Decimal from mysql-connector, but may
                # arrive as a str if the evidence bundle was round-tripped
                # through JSON (e.g. read back from an audit log) -- coerce
                # defensively rather than assume the live-pipeline type.
                out += f"  {e.get('wait_event')}: {e.get('hit_count')} hits, {round(_num(e.get('total_wait_seconds')), 2)}s total\n"
            else:
                out += f"  {e.get('wait_event_type')}/{e.get('wait_event')}: {e.get('session_count')} session(s)\n"

    recs = verdict.get("wait_event_recommendations", [])
    if recs:
        out += "\nAgent's interpretation and recommendations:\n"
        for r in recs:
            out += (
                f"  - {r.get('wait_event')}: {r.get('interpretation', '')}\n"
                f"    Recommendation: {r.get('recommendation', '')}\n"
            )
    return out


def format_alert_email(instance: str, tier: str, verdict: dict, evidence: dict, decision_id: int | None = None) -> tuple[str, str]:
    """Builds the full, self-contained notification body. Every section is
    always included, regardless of which single signal triggered
    escalation this cycle -- the evidence bundle from agent/evidence.py
    always gathers all categories."""
    subject = f"[DB Monitor] {tier.upper()} - {instance}"

    body = (
        f"Instance:   {instance}\n"
        f"Engine:     {evidence.get('engine')}\n"
        f"Tier:       {tier}\n"
        f"Confidence: {verdict.get('confidence')}\n\n"
        f"SUMMARY\n{_RULE}\n{verdict.get('summary')}\n\n"
        f"Recommended action: {verdict.get('recommended_action')}\n"
    )
    body += _render_blocking_section(evidence, verdict)
    body += _render_long_txn_section(evidence, verdict)
    body += _render_undo_section(evidence, verdict)
    body += _render_deadlock_section(evidence, verdict)
    body += _render_wait_events_section(evidence, verdict)

    body += (
        f"\n{_RULE}\n"
        f"No command shown above has been executed. Every 'SUGGESTED COMMAND' line is for a "
        f"human to review and run manually -- this system is read-only by design.\n"
    )
    if decision_id is not None:
        body += f"\nDecision id: {decision_id} (use scripts/mark_feedback.py to mark true/false positive later)\n"

    return subject, body
