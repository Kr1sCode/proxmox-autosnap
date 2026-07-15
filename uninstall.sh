#!/usr/bin/env bash
# Remove proxmox-autosnap: destroy the container and (optionally) the host-side
# API token/role/user. Guest snapshots created by autosnap are NOT touched.
#   bash uninstall.sh <CTID>
set -euo pipefail

CTID="${1:-}"
[ "$(id -u)" -eq 0 ] || { echo "uruchom jako root na hoście PVE"; exit 1; }
[ -n "$CTID" ] || { echo "użycie: bash uninstall.sh <CTID>"; exit 1; }

read -rp "Usunąć kontener $CTID i token API autosnap@pve? [y/N] " ans
[ "${ans,,}" = "y" ] || { echo "przerwano"; exit 0; }

if pct status "$CTID" >/dev/null 2>&1; then
  pct stop "$CTID" >/dev/null 2>&1 || true
  pct destroy "$CTID" >/dev/null 2>&1 || true
  echo "✔ Kontener $CTID usunięty"
fi

pveum user token remove autosnap@pve manager 2>/dev/null || true
pveum acl delete /vms --users autosnap@pve --roles AutoSnap 2>/dev/null || true
pveum user delete autosnap@pve 2>/dev/null || true
pveum role delete AutoSnap 2>/dev/null || true
echo "✔ Token/rola/user na hoście usunięte"
echo "ℹ Migawki auto_* w guestach pozostały nietknięte — usuń je ręcznie, jeśli chcesz."
