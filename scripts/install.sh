#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m unittest discover -s tests -v
if test -n "$(git status --porcelain)"; then
  echo 'Commit and review the engine source before installing it.' >&2
  exit 1
fi
engine_revision=$(git rev-parse HEAD)
engine_release="/opt/server-deploy/releases/$engine_revision"
sudo install -d -m 0755 /opt/server-deploy /opt/server-deploy/bin "$engine_release" "$engine_release/scripts"
sudo cp -R server_deploy "$engine_release/"
sudo install -m 0755 scripts/host-action.py "$engine_release/scripts/host-action.py"
sudo chown -R root:root "$engine_release"
sudo chmod -R go-w "$engine_release"
sudo ln -s "$engine_release" /opt/server-deploy/current.next
sudo mv -Tf /opt/server-deploy/current.next /opt/server-deploy/current
if ! test -e /opt/server-deploy/scripts; then
  sudo ln -s /opt/server-deploy/current/scripts /opt/server-deploy/scripts
fi
sudo install -m 0755 scripts/server-deploy /opt/server-deploy/bin/server-deploy
sudo install -d -m 0700 /etc/server-deploy /var/lib/server-deploy /etc/server-deploy/apps /etc/server-deploy/docker
sudo /opt/server-deploy/bin/server-deploy version
