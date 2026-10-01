#!/usr/bin/env bash
# Run on the EC2 instance, from inside the project directory
# (e.g. /opt/db-agentic-monitor), as a user with sudo access.
#
#   sudo bash deploy/install.sh
#
# Idempotent -- safe to re-run after pulling code updates.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_USER="dbmonitor"

echo "==> Project directory: $PROJECT_DIR"

if ! id "$SERVICE_USER" &>/dev/null; then
    echo "==> Creating service user '$SERVICE_USER'"
    sudo useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi

echo "==> Installing OS packages (python3-venv)"
sudo apt-get update -y
sudo apt-get install -y python3-venv python3-pip

echo "==> Creating virtualenv"
python3 -m venv "$PROJECT_DIR/venv"
"$PROJECT_DIR/venv/bin/pip" install --upgrade pip
"$PROJECT_DIR/venv/bin/pip" install -r "$PROJECT_DIR/requirements.txt"

if [ ! -f "$PROJECT_DIR/.env" ]; then
    echo "==> No .env found -- copying .env.example. EDIT THIS BEFORE STARTING THE SERVICE."
    cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"
fi

if [ ! -f "$PROJECT_DIR/config/config.yaml" ]; then
    echo "==> No config/config.yaml found -- copying config.example.yaml. EDIT THIS BEFORE STARTING."
    cp "$PROJECT_DIR/config/config.example.yaml" "$PROJECT_DIR/config/config.yaml"
fi

mkdir -p "$PROJECT_DIR/data"

echo "==> Setting ownership to $SERVICE_USER"
sudo chown -R "$SERVICE_USER":"$SERVICE_USER" "$PROJECT_DIR"
sudo chmod 600 "$PROJECT_DIR/.env" || true

echo "==> Installing systemd unit"
sudo cp "$PROJECT_DIR/deploy/db-monitor-agent.service" /etc/systemd/system/db-monitor-agent.service
sudo systemctl daemon-reload
sudo systemctl enable db-monitor-agent

echo ""
echo "==> Done. Before starting the service:"
echo "    1. Edit $PROJECT_DIR/.env with real secrets"
echo "    2. Edit $PROJECT_DIR/config/config.yaml with your real DB instances"
echo "    3. sudo systemctl start db-monitor-agent"
echo "    4. journalctl -u db-monitor-agent -f   (watch logs)"
