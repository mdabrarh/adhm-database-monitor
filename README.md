# db-agentic-monitor

An agentic AI database monitor for PostgreSQL and MySQL: instead of firing
an email the instant a single metric crosses a static threshold, a cheap
non-AI pre-filter flags candidate situations (lock contention, undo/history
pressure, deadlocks, long-running transactions), and a Claude-based agent
investigates using real tool calls (via MCP) against the live database and
its own history before deciding whether, and how urgently, to notify you.

This is a runnable implementation of the architecture discussed earlier —
deliberately built to be read, understood, and modified, not a black box.

## How it works

```
 every N seconds, per configured instance
        |
        v
 agent/quick_probe.py  ---- direct SQL, no LLM ----> raw metrics
        |                                            (always logged to
        | is anything over a configurable                baseline)
        | threshold? (locks, undo pressure,
        | deadlocks, long transactions)
        v
      no -> sleep until next cycle
      yes
        |
        v
 agent/claude_client.py --spawns--> mcp_servers/postgres_server.py  (or mysql_server.py)
        |                            + mcp_servers/baseline_server.py
        |
        | Claude calls these tools to investigate: blocking chains,
        | undo pressure, deadlocks, wait events, long transactions,
        | and "is this normal for this time of day?"
        v
 JSON verdict: {tier: none|low|high, confidence, summary, recommended_action}
        |
        v
 recorded to data/baseline.sqlite3 (decisions table, for audit + feedback)
        |
        v
 tier >= notify.min_tier?  --yes--> agent/notifier.py sends email
```

The cheap pre-filter exists purely for cost control — it's the difference
between paying for an LLM call every 60 seconds against every database in
your fleet, versus only when something already looks like a candidate.

## Project layout

```
common/            Config loading + the actual SQL queries (shared by both
                    the MCP servers and the cheap pre-filter, so there's
                    only one place to fix/extend a query)
mcp_servers/        Three small stdio MCP servers Claude calls as tools:
                    postgres_server.py, mysql_server.py, baseline_server.py
agent/              prompts.py (what Claude is told), claude_client.py
                    (SDK wrapper), notifier.py (email), quick_probe.py
                    (the cheap filter), monitor_agent.py (the main loop)
scripts/            mark_feedback.py -- CLI to mark a past verdict as a
                    true/false positive; run_paper_experiments.py and
                    scenarios/ -- reproduce the paper's evaluation
deploy/             systemd unit, install script, EC2 setup walkthrough
config/             config.example.yaml -- copy to config.yaml and edit
```

## Local setup (before deploying anywhere)

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env                       # fill in ANTHROPIC_API_KEY, DB passwords, SMTP creds
cp config/config.example.yaml config/config.yaml   # point at your real instance(s)

python -m agent.monitor_agent              # runs in the foreground; Ctrl+C to stop
```

Watch the logs: on a quiet database you'll see periodic "quiet this cycle"
debug lines; open a long-running transaction against a test database to see
it escalate to a candidate, invoke Claude, and (if configured) send an
email.

## Deploying to EC2 (24/7)

See **[deploy/EC2_SETUP.md](deploy/EC2_SETUP.md)** for the full walkthrough:
launching the instance, creating a least-privilege read-only DB user,
copying the code over, running `deploy/install.sh`, and starting it as a
systemd service that restarts automatically and survives reboots.

Short version:
```bash
scp -r db-agentic-monitor ubuntu@<ec2-ip>:/tmp/
ssh ubuntu@<ec2-ip> "sudo mv /tmp/db-agentic-monitor /opt/db-agentic-monitor"
ssh ubuntu@<ec2-ip> "cd /opt/db-agentic-monitor && sudo bash deploy/install.sh"
# then edit .env and config/config.yaml on the instance, and:
ssh ubuntu@<ec2-ip> "sudo systemctl start db-monitor-agent"
```

## What a notification email actually contains

Every email (any verdict tier at or above `notify.min_tier`) is fully
self-contained — it always includes **every** category below, regardless
of which single signal tripped the pre-filter that cycle, so the on-call
engineer never has to go pull additional data just to start investigating:

- **Locking / blocking chains** — the exact blocked and blocking query
  text, wait time, and blocking session age.
- **Long-running / idle-in-transaction sessions** — query text and age for
  each one found.
- **Undo / rollback pressure** — the raw numbers (History List Length for
  MySQL; dead-tuple ratio, oldest open transaction age, and XID
  wraparound age for PostgreSQL) plus the agent's identification of which
  specific session is the likely contributor.
- **Deadlocks** — full participant/query detail for MySQL (InnoDB records
  this natively); for PostgreSQL, the cumulative counter plus, if you've
  configured `postgres_log_path`, a best-effort excerpt from the server
  log (PostgreSQL doesn't expose deadlock participants through SQL at
  all — see "Known limitations").
- **Wait events** — the raw distribution plus a per-event interpretation
  and recommendation from the agent.
- **Kill command suggestions** — for any session open longer than
  `prefilter.kill_suggestion_age_seconds` (default 10 minutes), the email
  includes the exact, ready-to-copy termination command
  (`SELECT pg_terminate_backend(pid);` / `KILL <id>;`), plus the gentler
  query-only-cancel alternative. **Nothing is ever executed automatically**
  — see Safety model below. These commands are computed by plain Python
  from the live query result (`common/kill_commands.py`), never by the
  LLM, specifically so a hallucinated PID or malformed command can't end
  up in front of an on-call engineer's terminal.

See `agent/notifier.py` (`format_alert_email`) if you want to change the
formatting, and `agent/evidence.py` if you want to change what's gathered.

## Safety model

- The database user this connects with should be **read-only** (see
  `deploy/EC2_SETUP.md` for the exact `GRANT` statements) — the agent has
  no tool that can write to, or kill sessions on, your database.
- Claude's MCP tools are explicitly allow-listed per query
  (`agent/claude_client.py`) to exactly the read-only diagnostic tools —
  nothing else is reachable even if the model tried.
- **Kill commands are suggested, never run.** The system has no code path
  that opens a write connection or executes a termination command against
  any database. Every "SUGGESTED COMMAND" in an email is text for a human
  to paste into their own `psql`/`mysql` client after reviewing it — see
  `common/kill_commands.py`, which only ever returns strings.
- Every verdict is written to `data/baseline.sqlite3` (`decisions` table)
  whether or not it was emailed, so you have a full audit trail of what the
  agent concluded and why, and `scripts/mark_feedback.py` lets a human
  record disagreement.

## Customizing further

This was built to be edited, not just run. Some likely next steps:

- **Add another instance:** just add an entry under `instances:` in
  `config/config.yaml` and a matching password variable in `.env`. Nothing
  else needs to change.
- **Add a new signal (e.g. replication lag, connection count):** add a
  query function to `common/db_pg.py` / `common/db_mysql.py`, expose it as
  a new `@mcp.tool()` in the matching `mcp_servers/*.py` file, add it to
  `allowed_tools` in `agent/claude_client.py`, and mention it in
  `agent/prompts.py` so the model knows to use it.
- **Add Slack instead of/alongside email:** add a `send_slack()` function
  to `agent/notifier.py` (a plain `requests.post` to an incoming webhook
  URL) and call it from `agent/monitor_agent.py` next to `send_email()`.
- **Support Oracle or SQL Server:** add `common/db_oracle.py` following the
  same function shape as `common/db_pg.py`, a matching
  `mcp_servers/oracle_server.py`, and an `"oracle"` branch in
  `agent/claude_client.py`'s engine-to-script mapping. This is exactly the
  cross-engine normalization pattern the other two engines already follow.
- **Feed human feedback back into thresholds:** `scripts/mark_feedback.py`
  already records true/false-positive labels in the `decisions` table;
  `agent/quick_probe.py` doesn't yet read them back to auto-tune
  `prefilter` thresholds per instance, but the data is there to do it.
- **Allow one bounded remediation action:** if you later decide the agent
  should be allowed to, say, terminate a specific idle-in-transaction
  session it has identified as the sole blocker, add that as a new,
  narrowly-scoped MCP tool (not a general "run any SQL" tool), require a
  human-confirmation step before it fires, and log every invocation. Treat
  this as a deliberate, reviewed change, not a config flag flipped in
  passing — it's the one place this system can affect production, not just
  observe it.

## Reproducing the paper's evaluation

`scripts/run_paper_experiments.py` reproduces the proof-of-concept
evaluation in the accompanying paper (*An Agentic AI Framework for Database
Health Monitoring Across Engines: Reasoning Beyond Static Thresholds*). It
only observes the monitor: nothing in `agent/` or `common/` is changed for
the experiment. **Use non-production databases only.**

| Stage | Cycle | Scenario | What runs |
|---|---|---|---|
| 1 | – | PostgreSQL blocking chain | Evidence path + rendered email, LLM fields held out (no API call) |
| 1 | – | MySQL deadlock | Evidence path only (no API call) |
| 2 | 1 | PostgreSQL blocking chain (active) | Full `handle_candidate()` with live MCP tools |
| 2 | 2 | PostgreSQL quiescent | Full `handle_candidate()` |
| 2 | 3 | MySQL deadlock (just occurred) | Full `handle_candidate()` |
| 2 | 4 | MySQL quiescent | Full `handle_candidate()` |

Scenarios (`scripts/scenarios/`):

- `pg_blocking_chain.py` -- session A takes a row lock with
  `SELECT ... FOR UPDATE` and sits idle in its transaction; session B's
  `UPDATE` on the same row blocks behind it.
- `mysql_deadlock.py` -- two sessions update `invoices` rows 1 and 2 in
  opposite order; InnoDB detects the cycle and rolls one back.

Both scenarios roll back everything they do, leaving the test tables unchanged.

### Steps

1. Start PostgreSQL and MySQL test instances. The paper used PostgreSQL 16
   and MySQL 8.4.
2. Create the test tables with a user that can write:
   ```bash
   psql  -h localhost -U <writer> -d <db> -f scripts/scenarios/setup_postgres.sql
   mysql -h 127.0.0.1 -u <writer> -p <db> < scripts/scenarios/setup_mysql.sql
   ```
   Keep the PostgreSQL table at a single row: the quiescent-cycle result
   (a dead-tuple ratio of 1.0 that the agent correctly dismisses) depends
   on the table being nearly empty.
3. Add both instances to `config/config.yaml` with the usual read-only
   monitor user, and set `anthropic_model` to the model you want to test.
4. Give the scenarios writer credentials (the monitor itself stays read-only):
   ```bash
   export SCENARIO_PG_USER=... SCENARIO_PG_PASSWORD=...
   export SCENARIO_MYSQL_USER=... SCENARIO_MYSQL_PASSWORD=...
   export ANTHROPIC_API_KEY=...        # Stage 2 only
   ```
5. Run:
   ```bash
   python scripts/run_paper_experiments.py --stage all \
       --pg-instance <pg name in config> --mysql-instance <mysql name in config> \
       --out results/
   ```
   `--stage 1` needs no API key. `--kill-threshold-seconds` (default 2)
   lowers the kill-suggestion age so the termination-command path is
   exercised within seconds, as in the paper; the production default is
   600 s. `--settle-seconds` (default 3) is how long the blocking chain
   accumulates wait before it is polled.

### Output

Everything is written to `results/` in machine-readable form:

- `environment.json` -- OS, CPU, Python and package versions, database
  versions, model name, and pre-filter settings.
- `stage1_*_evidence.json`, `stage1_pg_blocking_email.txt` -- Stage 1 evidence
  bundles and the rendered email.
- `stage2_cycleN_*.json` -- per cycle: pre-filter snapshot, full evidence
  bundle, the agent's complete message trace (every tool call and
  result), the parsed verdict, and whether a notification was sent.
- `stage2_cycleN_*_email.txt` -- the rendered notification, when one was sent
  (captured instead of emailed).

Zip `results/` to publish it as supplemental data.

Stage 2 calls the Anthropic API, so LLM wording and tool-call sequences
will vary from run to run; the deterministic evidence (query text,
process ids, termination commands, InnoDB deadlock text) will not.

## Known limitations

- MySQL's `get_blocking_chains` uses `performance_schema.data_lock_waits`,
  which requires MySQL 8.0+. On 5.7 or MariaDB, swap it for
  `information_schema.innodb_lock_waits` (noted inline in `common/db_mysql.py`).
- InnoDB only remembers its single most recent deadlock at a time (there's
  no built-in deadlock history) — `get_latest_deadlock` and the
  marker-diffing in `quick_probe.py` work around this, but if two
  deadlocks occur in the same poll interval, only the second is visible.
- **PostgreSQL exposes no deadlock participant detail through SQL at all**
  — `pg_stat_database.deadlocks` is a bare cumulative counter. Getting the
  actual queries/processes involved requires reading the server log
  (`log_lock_waits` and the default deadlock logging), which only works if
  `postgres_log_path` is set AND this process can read that file directly
  — i.e. it's running on the DB host, or the log is mounted here. This
  will not work unmodified against a managed RDS instance; you'd need to
  separately ship RDS logs somewhere this process can read (e.g. sync
  CloudWatch Logs to a local file) to get MySQL-equivalent deadlock detail
  on Postgres. When unavailable, the email says so plainly rather than
  guessing at participants.
- The Claude Agent SDK's public API has changed before and may change
  again; if `agent/claude_client.py` throws an import or attribute error
  after an SDK upgrade, see the troubleshooting comment at the top of that
  file.
- This monitors one server process per instance list, sequentially, each
  poll cycle. For a very large fleet (dozens+ of instances) you'd want to
  parallelize `run_cycle()` in `agent/monitor_agent.py` with
  `asyncio.gather` instead of a plain `for` loop — left sequential here
  for simplicity and clearer logs.
