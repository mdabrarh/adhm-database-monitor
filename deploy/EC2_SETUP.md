# EC2 setup

## 1. Launch the instance

- AMI: Ubuntu 22.04 LTS (these instructions assume `apt`; adapt `install.sh` if you use Amazon Linux/`yum`).
- Size: a `t3.small` is comfortable -- this workload is almost entirely idle, waking up briefly per poll cycle.
- Security group:
  - Inbound: SSH (22) from your IP/bastion only. No other inbound ports are needed -- this service makes outbound calls, nothing listens for incoming traffic.
  - Outbound: HTTPS (443) to the Anthropic API and your SMTP provider; the DB port (5432/3306) to your database host(s)/security groups.
- IAM role: none required for the code as written (no AWS SDK calls). If you later store secrets in AWS Secrets Manager or SSM Parameter Store instead of `.env`, attach a role scoped to just those resources.

## 2. Create a read-only database user

Do this once per database instance you want monitored. Least privilege matters here — this account should never be able to write or DDL anything.

**PostgreSQL:**
```sql
CREATE ROLE db_monitor_ro WITH LOGIN PASSWORD 'change-me';
GRANT pg_monitor TO db_monitor_ro;         -- read-only access to stats views (PG 10+)
GRANT CONNECT ON DATABASE orders TO db_monitor_ro;
GRANT USAGE ON SCHEMA public TO db_monitor_ro;
```

**MySQL:**
```sql
CREATE USER 'db_monitor_ro'@'%' IDENTIFIED BY 'change-me';
GRANT PROCESS, SELECT ON *.* TO 'db_monitor_ro'@'%';   -- PROCESS is needed for SHOW ENGINE INNODB STATUS
GRANT SELECT ON performance_schema.* TO 'db_monitor_ro'@'%';
GRANT SELECT ON information_schema.* TO 'db_monitor_ro'@'%';
```

If the database is in RDS, restrict the RDS security group to allow inbound traffic on the DB port only from the EC2 instance's security group (not `0.0.0.0/0`).

## 3. Get the code onto the instance

```bash
scp -r db-agentic-monitor ubuntu@<ec2-ip>:/tmp/
ssh ubuntu@<ec2-ip>
sudo mv /tmp/db-agentic-monitor /opt/db-agentic-monitor
cd /opt/db-agentic-monitor
```

(Swap this for `git clone` if you push this project to your own repo -- recommended, since you said you want to keep modifying it.)

## 4. Install

```bash
sudo bash deploy/install.sh
```

This creates a `dbmonitor` system user, a venv, installs dependencies, and copies `.env.example` / `config.example.yaml` into place if you haven't already created real versions.

## 5. Configure

```bash
sudo -u dbmonitor nano /opt/db-agentic-monitor/.env
sudo -u dbmonitor nano /opt/db-agentic-monitor/config/config.yaml
```

Fill in `ANTHROPIC_API_KEY`, each instance's DB password env var, and SMTP credentials. Add every database instance you want monitored under `instances:` in `config.yaml`.

## 6. Start it

```bash
sudo systemctl start db-monitor-agent
sudo systemctl status db-monitor-agent
journalctl -u db-monitor-agent -f
```

You should see a startup log line listing how many instances were loaded, then either "quiet this cycle" debug lines or "flagged as candidate" / "Verdict for ..." info lines as it runs.

## 7. Verify email delivery works

The easiest way to test end-to-end without waiting for a real incident: temporarily lower `prefilter.long_txn_age_seconds` to something like `5` in `config.yaml`, open a transaction in `psql`/`mysql` and hold it open (`BEGIN; SELECT pg_sleep(30);` or equivalent) for a minute, restart the service, and confirm you get an email. Revert the threshold afterward.

## Ongoing operations

- **Logs:** `journalctl -u db-monitor-agent -f`. Rotation is handled by journald's normal retention policy.
- **Updating code:** `git pull` (or re-`scp`), then `sudo systemctl restart db-monitor-agent`.
- **Baseline/decision history:** lives in `data/baseline.sqlite3` on the instance. Back this up if you want to preserve learned baselines across an instance replacement; it's safe to delete if you want to start fresh (the agent just has less history to reason from until it rebuilds).
- **Reviewing past verdicts / marking false positives:** `python -m scripts.mark_feedback --list` and `python -m scripts.mark_feedback <decision_id> false_positive` (see scripts/mark_feedback.py).
