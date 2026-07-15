#!/usr/bin/env bash
# proxmox-autosnap installer — run ON a Proxmox VE host.
# Creates an unprivileged Debian 13 LXC, installs the autosnap manager inside it,
# and provisions a scoped API token on the host. The host itself is otherwise
# left untouched, so the tool survives Proxmox upgrades.
#
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/Kr1sCode/proxmox-autosnap/main/install.sh)"
#
set -euo pipefail

REPO="${AUTOSNAP_REPO:-Kr1sCode/proxmox-autosnap}"
BRANCH="${AUTOSNAP_BRANCH:-main}"
TARBALL="https://github.com/${REPO}/archive/refs/heads/${BRANCH}.tar.gz"

# ---------- pretty output ----------
RD=$'\033[31m'; GN=$'\033[32m'; YW=$'\033[33m'; BL=$'\033[36m'; BD=$'\033[1m'; NC=$'\033[0m'
msg()  { echo -e " ${GN}✔${NC} $*"; }
info() { echo -e " ${BL}➜${NC} $*"; }
warn() { echo -e " ${YW}!${NC} $*"; }
die()  { echo -e " ${RD}✘${NC} $*" >&2; exit 1; }
banner() {
  echo -e "${BL}${BD}"
  echo '   ┌──────────────────────────────────────────────┐'
  echo '   │   ◆  proxmox-autosnap  ·  snapshot scheduler   │'
  echo '   └──────────────────────────────────────────────┘'
  echo -e "${NC}"
}

# ---------- preflight ----------
banner
[ "$(id -u)" -eq 0 ] || die "uruchom jako root na hoście Proxmox VE"
command -v pveversion >/dev/null 2>&1 || die "to nie wygląda na host Proxmox VE (brak pveversion)"
command -v pct >/dev/null 2>&1 || die "brak pct"
info "Proxmox: $(pveversion | head -1)"

have_whiptail=0; command -v whiptail >/dev/null 2>&1 && have_whiptail=1

ask() {  # ask VAR "Prompt" "default"
  local __var="$1" __prompt="$2" __def="${3:-}" __val=""
  if [ "$have_whiptail" = 1 ]; then
    __val=$(whiptail --title "proxmox-autosnap" --inputbox "$__prompt" 10 64 "$__def" 3>&1 1>&2 2>&3) \
      || die "anulowano"
  else
    read -rp "$__prompt [$__def]: " __val
  fi
  printf -v "$__var" '%s' "${__val:-$__def}"
}

# ---------- defaults ----------
NEXTID=$(pvesh get /cluster/nextid 2>/dev/null || echo 200)
DEF_CTID="$NEXTID"; DEF_HOST="autosnap"; DEF_DISK="3"; DEF_CORES="1"; DEF_RAM="512"
DEF_BRIDGE="vmbr0"; DEF_NET="dhcp"

# storage that supports containers (rootdir)
mapfile -t STORES < <(pvesm status -content rootdir 2>/dev/null | awk 'NR>1{print $1}')
DEF_STORE="${STORES[0]:-local-lvm}"

MODE="Default"
if [ "$have_whiptail" = 1 ]; then
  MODE=$(whiptail --title "proxmox-autosnap" --menu "Tryb instalacji" 12 60 2 \
    "Default"  "Automatyczne ustawienia (zalecane)" \
    "Advanced" "Ręcznie: CTID, sieć, storage, zasoby" 3>&1 1>&2 2>&3) || die "anulowano"
fi

CTID="$DEF_CTID"; HOSTNAME="$DEF_HOST"; DISK="$DEF_DISK"; CORES="$DEF_CORES"; RAM="$DEF_RAM"
BRIDGE="$DEF_BRIDGE"; NETCFG="$DEF_NET"; STORE="$DEF_STORE"

if [ "$MODE" = "Advanced" ]; then
  ask CTID     "CTID kontenera"                  "$DEF_CTID"
  ask HOSTNAME "Hostname"                         "$DEF_HOST"
  ask CORES    "Rdzenie CPU"                      "$DEF_CORES"
  ask RAM      "RAM (MB)"                          "$DEF_RAM"
  ask DISK     "Dysk (GB)"                         "$DEF_DISK"
  ask STORE    "Storage (rootdir): ${STORES[*]:-local-lvm}" "$DEF_STORE"
  ask BRIDGE   "Bridge sieciowy"                  "$DEF_BRIDGE"
  ask NETCFG   "IP: 'dhcp' lub CIDR np. 10.0.0.50/24,gw=10.0.0.1" "$DEF_NET"
fi

# ---------- template ----------
info "Sprawdzam szablon Debian 13…"
TMPL=$(pveam list local 2>/dev/null | awk '/debian-13-standard/{print $1}' | head -1)
if [ -z "$TMPL" ]; then
  AV=$(pveam available --section system 2>/dev/null | awk '/debian-13-standard/{print $2}' | head -1)
  [ -n "$AV" ] || die "brak szablonu debian-13-standard w pveam"
  info "Pobieram $AV…"; pveam download local "$AV" >/dev/null
  TMPL="local:vztmpl/$AV"
fi
msg "Szablon: $TMPL"

# ---------- network arg ----------
if [ "$NETCFG" = "dhcp" ]; then
  NET="name=eth0,bridge=${BRIDGE},ip=dhcp"
else
  NET="name=eth0,bridge=${BRIDGE},ip=${NETCFG}"
fi

# ---------- create ----------
info "Tworzę LXC ${CTID} (${HOSTNAME})…"
pct create "$CTID" "$TMPL" \
  --hostname "$HOSTNAME" \
  --cores "$CORES" --memory "$RAM" --swap "$RAM" \
  --rootfs "${STORE}:${DISK}" \
  --net0 "$NET" \
  --features nesting=1 \
  --ostype debian --unprivileged 1 --onboot 1 \
  --description "proxmox-autosnap — scheduled snapshots + retention" >/dev/null
msg "Kontener utworzony"
pct start "$CTID" >/dev/null
info "Czekam na sieć kontenera…"
for _ in $(seq 1 30); do
  CT_IP=$(pct exec "$CTID" -- bash -c "ip -4 -o addr show eth0 2>/dev/null | awk '{print \$4}' | cut -d/ -f1" 2>/dev/null || true)
  [ -n "${CT_IP:-}" ] && break; sleep 2
done
[ -n "${CT_IP:-}" ] || die "kontener nie dostał IP"
msg "Kontener IP: ${CT_IP}"

# host IP on that bridge — what the container uses to reach the PVE API
PVE_HOST=$(ip -4 -o addr show "$BRIDGE" 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -1)
[ -n "${PVE_HOST:-}" ] || PVE_HOST=$(hostname -I | awk '{print $1}')

# ---------- API token + role on host ----------
info "Provisioning roli/tokenu API na hoście…"
pveum role add AutoSnap --privs "VM.Snapshot VM.Audit" 2>/dev/null || \
  pveum role modify AutoSnap --privs "VM.Snapshot VM.Audit" 2>/dev/null || true
pveum user add autosnap@pve --comment "proxmox-autosnap" 2>/dev/null || true
pveum acl modify /vms --users autosnap@pve --roles AutoSnap 2>/dev/null || true
pveum user token remove autosnap@pve manager 2>/dev/null || true
TOKVAL=$(pveum user token add autosnap@pve manager --privsep 0 --output-format json | \
  python3 -c "import sys,json;print(json.load(sys.stdin)['value'])")
[ -n "$TOKVAL" ] || die "nie udało się utworzyć tokenu"
msg "Token API: autosnap@pve!manager"

# ---------- install app inside container ----------
info "Instaluję aplikację w kontenerze…"
pct exec "$CTID" -- bash -s -- "$REPO" "$BRANCH" "$PVE_HOST" "autosnap@pve!manager=${TOKVAL}" <<'INNER'
set -euo pipefail
REPO="$1"; BRANCH="$2"; PVE_HOST="$3"; TOKEN="$4"
export DEBIAN_FRONTEND=noninteractive
apt-get -o Acquire::ForceIPv4=true -qq update >/dev/null
apt-get -o Acquire::ForceIPv4=true -qq install -y curl python3 python3-flask python3-requests python3-gunicorn >/dev/null
ln -sf /usr/share/zoneinfo/$(cat /etc/timezone 2>/dev/null || echo UTC) /etc/localtime 2>/dev/null || true

mkdir -p /opt/autosnap /etc/autosnap /var/lib/autosnap /var/log/autosnap
tmp=$(mktemp -d)
curl -fsSL "https://github.com/${REPO}/archive/refs/heads/${BRANCH}.tar.gz" | tar -xz -C "$tmp"
src="$tmp"/*/
cp -r $src/app/. /opt/autosnap/
cp $src/systemd/*.service $src/systemd/*.timer /etc/systemd/system/

# config + token + session secret
cat > /etc/autosnap/config.json <<JSON
{"settings":{"pve_host":"${PVE_HOST}","pve_port":8006,"verify_tls":false,"paused":false},"auth":{"allowlist":["root@pam"]},"guests":{}}
JSON
printf '%s' "$TOKEN" > /etc/autosnap/token; chmod 600 /etc/autosnap/token
python3 -c "import secrets;open('/etc/autosnap/secret','w').write(secrets.token_hex(32))"; chmod 600 /etc/autosnap/secret
rm -rf "$tmp"

systemctl daemon-reload
systemctl enable --now autosnap-web.service >/dev/null 2>&1
systemctl enable --now autosnap-scheduler.timer >/dev/null 2>&1
INNER

# ---------- verify ----------
sleep 2
if pct exec "$CTID" -- curl -fsS -o /dev/null -w '%{http_code}' http://127.0.0.1/login 2>/dev/null | grep -q 200; then
  msg "Web UI odpowiada"
else
  warn "Web UI jeszcze nie odpowiada — sprawdź: pct exec $CTID -- journalctl -u autosnap-web"
fi

echo
echo -e "${GN}${BD} ✔ Gotowe!${NC}"
echo -e "   Panel:    ${BD}http://${CT_IP}/${NC}"
echo -e "   Login:    Twoje poświadczenia Proxmoksa (domyślnie ${BD}root@pam${NC})"
echo -e "   Kontener: CT ${BD}${CTID}${NC} (${HOSTNAME})"
echo
echo -e "   ${YW}HTTPS:${NC} wystaw przez reverse proxy (np. Nginx Proxy Manager) → http://${CT_IP}:80"
echo -e "   ${YW}Uwaga:${NC} panel wpuszcza tylko allowlistę (domyślnie root@pam). Nie wystawiaj"
echo -e "          publicznie bez dodatkowej ochrony (Access List / VPN)."
echo
