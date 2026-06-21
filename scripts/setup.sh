#!/bin/bash
# Turnkey installer for the valuebet daemon on a fresh Ubuntu/Debian host
# (e.g. a Google Cloud e2-micro VM). Run it once from inside the cloned repo:
#
#     bash scripts/setup.sh
#
# It installs system deps, builds the venv, and registers the systemd service
# for the CURRENT user and project path — no hard-coded /home/ubuntu anywhere.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME="$(whoami)"
cd "$PROJECT_DIR"

echo "==> Projet : $PROJECT_DIR  (utilisateur: $USER_NAME)"

echo "==> 1/4 Dépendances système"
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip git

echo "==> 2/4 Environnement Python (.venv) + dépendances"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

echo "==> 3/4 Vérification du .env"
if [ ! -f .env ]; then
    echo "   ⚠️  Aucun .env trouvé. Crée-le (copie depuis ton ancien VPS) AVANT de démarrer le service :"
    echo "       nano $PROJECT_DIR/.env"
fi

echo "==> 4/4 Service systemd"
chmod +x runscan.sh scripts/scan-daemon.sh
sudo tee /etc/systemd/system/valuebet-daemon.service > /dev/null <<UNIT
[Unit]
Description=Valuebet continuous scan daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER_NAME
WorkingDirectory=$PROJECT_DIR
Environment=PROJECT_DIR=$PROJECT_DIR
ExecStart=$PROJECT_DIR/scripts/scan-daemon.sh
Restart=always
RestartSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable valuebet-daemon.service

echo ""
echo "✅ Installation terminée."
echo "   1) Assure-toi que .env existe et est rempli :  cat $PROJECT_DIR/.env"
echo "   2) Démarre :   sudo systemctl start valuebet-daemon.service"
echo "   3) Vérifie :   tail -n 30 $PROJECT_DIR/valuebet.log"
