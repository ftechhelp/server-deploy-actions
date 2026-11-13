#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m unittest discover -s tests -v
sudo install -d -m 0755 /opt/server-deploy /opt/server-deploy/bin /opt/server-deploy/scripts
sudo cp -R server_deploy /opt/server-deploy/
sudo find /opt/server-deploy/server_deploy -type d -name __pycache__ -exec rm -rf '{}' +
sudo chown -R root:root /opt/server-deploy
sudo chmod -R go-w /opt/server-deploy
sudo install -m 0755 scripts/host-action.py /opt/server-deploy/scripts/host-action.py
sudo install -m 0755 scripts/server-deploy /opt/server-deploy/bin/server-deploy
sudo install -d -m 0700 /etc/server-deploy /var/lib/server-deploy /etc/server-deploy/apps /etc/server-deploy/docker
sudo /opt/server-deploy/bin/server-deploy version
