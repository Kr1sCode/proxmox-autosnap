#!/usr/bin/env bash
# Update the autosnap app inside an existing container. Config, token and
# session secret are preserved.
#   bash update.sh <CTID>
set -euo pipefail

REPO="${AUTOSNAP_REPO:-Kr1sCode/proxmox-autosnap}"
BRANCH="${AUTOSNAP_BRANCH:-main}"
CTID="${1:-}"

[ "$(id -u)" -eq 0 ] || { echo "uruchom jako root na hoście PVE"; exit 1; }
[ -n "$CTID" ] || { echo "użycie: bash update.sh <CTID>"; exit 1; }
pct status "$CTID" >/dev/null 2>&1 || { echo "brak kontenera $CTID"; exit 1; }

echo "➜ Aktualizuję autosnap w CT $CTID z ${REPO}@${BRANCH}…"
pct exec "$CTID" -- bash -s -- "$REPO" "$BRANCH" <<'INNER'
set -euo pipefail
REPO="$1"; BRANCH="$2"
tmp=$(mktemp -d)
curl -fsSL "https://github.com/${REPO}/archive/refs/heads/${BRANCH}.tar.gz" | tar -xz -C "$tmp"
src="$tmp"/*/
cp -r $src/app/. /opt/autosnap/
cp $src/systemd/*.service $src/systemd/*.timer /etc/systemd/system/
rm -rf "$tmp"
systemctl daemon-reload
systemctl restart autosnap-web.service
echo "  web: $(systemctl is-active autosnap-web.service)"
INNER
echo "✔ Zaktualizowano."
