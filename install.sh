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

have_whiptail=0
if [ "${AUTOSNAP_NONINTERACTIVE:-0}" != 1 ] && command -v whiptail >/dev/null 2>&1; then
  have_whiptail=1
fi
WT=(whiptail --backtitle "proxmox-autosnap" --title "proxmox-autosnap")
wt_input()  { "${WT[@]}" --inputbox    "$1" 9  68 "$2" 3>&1 1>&2 2>&3; }
wt_pass()   { "${WT[@]}" --passwordbox  "$1" 9  68 ""  3>&1 1>&2 2>&3; }

# ---------- defaults ----------
NEXTID=$(pvesh get /cluster/nextid 2>/dev/null || echo 200)
DEF_CTID="$NEXTID"; DEF_HOST="autosnap"; DEF_DISK="3"; DEF_CORES="1"; DEF_RAM="512"

# detect bridges + container-capable storages
mapfile -t BRIDGES < <(ip -o link show type bridge 2>/dev/null | awk -F': ' '{print $2}' | sed 's/@.*//' | sort)
[ "${#BRIDGES[@]}" -gt 0 ] || BRIDGES=(vmbr0)
DEF_BRIDGE="vmbr0"; printf '%s\n' "${BRIDGES[@]}" | grep -qx vmbr0 || DEF_BRIDGE="${BRIDGES[0]}"
mapfile -t STORES < <(pvesm status -content rootdir 2>/dev/null | awk 'NR>1{print $1}')
DEF_STORE="${STORES[0]:-local-lvm}"

# ---------- base values (env overridable, also prefill the wizard) ----------
CTTYPE="${AUTOSNAP_UNPRIVILEGED:-1}"           # 1=unprivileged 0=privileged
PASSWORD="${AUTOSNAP_PASSWORD:-}"
CTID="${AUTOSNAP_CTID:-$DEF_CTID}";      HOSTNAME="${AUTOSNAP_HOSTNAME:-$DEF_HOST}"
DISK="${AUTOSNAP_DISK:-$DEF_DISK}";      CORES="${AUTOSNAP_CORES:-$DEF_CORES}"
RAM="${AUTOSNAP_RAM:-$DEF_RAM}";         STORE="${AUTOSNAP_STORE:-$DEF_STORE}"
BRIDGE="${AUTOSNAP_BRIDGE:-$DEF_BRIDGE}"; DNS="${AUTOSNAP_NS:-}"; NESTING="${AUTOSNAP_NESTING:-1}"
# IPv4: parse AUTOSNAP_NET ("dhcp" | "CIDR,gw=GW")
IP4MODE="dhcp"; IP4=""; GW=""
if [ -n "${AUTOSNAP_NET:-}" ] && [ "${AUTOSNAP_NET}" != "dhcp" ]; then
  IP4MODE="static"; IP4="${AUTOSNAP_NET%%,*}"; GW="$(sed -n 's/.*gw=\([^,]*\).*/\1/p' <<<"$AUTOSNAP_NET")"
fi

# ---------- wizard ----------
if [ "$have_whiptail" = 1 ]; then
  MODE=$("${WT[@]}" --menu "Tryb instalacji" 12 68 2 \
    "1" "Default  — automatyczne ustawienia (DHCP, vmbr0)" \
    "2" "Advanced — CT ID, hostname, sieć, DNS, zasoby" 3>&1 1>&2 2>&3) || die "anulowano"
  if [ "$MODE" = "2" ]; then
    CTTYPE=$("${WT[@]}" --radiolist "Typ kontenera" 11 68 2 \
      "1" "Unprivileged (zalecane)" ON  "0" "Privileged" OFF 3>&1 1>&2 2>&3) || die "anulowano"
    PASSWORD=$(wt_pass "Hasło root (puste = wygeneruj losowe)") || die "anulowano"
    CTID=$(wt_input "Container ID" "$DEF_CTID") || die "anulowano"
    HOSTNAME=$(wt_input "Hostname" "$DEF_HOST") || die "anulowano"
    DISK=$(wt_input "Dysk (GB)" "$DEF_DISK") || die "anulowano"
    CORES=$(wt_input "Rdzenie CPU" "$DEF_CORES") || die "anulowano"
    RAM=$(wt_input "RAM (MiB)" "$DEF_RAM") || die "anulowano"
    smenu=(); for s in "${STORES[@]}"; do smenu+=("$s" ""); done
    [ "${#smenu[@]}" -gt 0 ] && STORE=$("${WT[@]}" --menu "Storage (rootfs)" 14 68 6 "${smenu[@]}" 3>&1 1>&2 2>&3)
    bmenu=(); for b in "${BRIDGES[@]}"; do bmenu+=("$b" ""); done
    BRIDGE=$("${WT[@]}" --menu "Network bridge" 14 68 6 "${bmenu[@]}" 3>&1 1>&2 2>&3) || die "anulowano"
    IP4MODE=$("${WT[@]}" --menu "Konfiguracja IPv4" 11 68 2 \
      "dhcp" "Automatycznie (DHCP)" "static" "Statyczny adres" 3>&1 1>&2 2>&3) || die "anulowano"
    if [ "$IP4MODE" = "static" ]; then
      IP4=$(wt_input "Adres IPv4 w formacie CIDR (np. 192.168.1.50/24)" "$IP4") || die "anulowano"
      GW=$(wt_input "Brama (gateway), np. 192.168.1.1" "$GW") || die "anulowano"
    fi
    DNS=$(wt_input "Serwer DNS (puste = dziedzicz z hosta)" "$DNS") || die "anulowano"
    if "${WT[@]}" --yesno "Włączyć nesting? (wymagane przez systemd w kontenerze)" 8 68; then
      NESTING=1
    else
      NESTING=0
    fi
    "${WT[@]}" --yesno "Podsumowanie:

  CT ID:     $CTID   ($([ "$CTTYPE" = 1 ] && echo unprivileged || echo privileged))
  Hostname:  $HOSTNAME
  Zasoby:    ${CORES} vCPU · ${RAM} MiB · ${DISK} GB · $STORE
  Sieć:      $BRIDGE · $([ "$IP4MODE" = static ] && echo "$IP4 gw=$GW" || echo DHCP)
  DNS:       ${DNS:-<host>}

Utworzyć kontener?" 16 68 || die "anulowano przez użytkownika"
  fi
fi

# ---------- validate ----------
[[ "$CTID" =~ ^[0-9]+$ ]] || die "CTID musi być liczbą: $CTID"
if pct status "$CTID" >/dev/null 2>&1 || qm status "$CTID" >/dev/null 2>&1; then
  die "ID $CTID jest już zajęte"
fi
[ "$IP4MODE" = "static" ] && { [ -n "$IP4" ] || die "statyczny IPv4 wybrany, ale adres pusty"; }

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
if [ "$IP4MODE" = "static" ]; then
  NET="name=eth0,bridge=${BRIDGE},ip=${IP4}"
  [ -n "$GW" ] && NET="${NET},gw=${GW}"
else
  NET="name=eth0,bridge=${BRIDGE},ip=dhcp"
fi
NS_ARG=(); [ -n "$DNS" ] && NS_ARG=(--nameserver "$DNS")
[ -n "$PASSWORD" ] || PASSWORD="$(openssl rand -base64 12 2>/dev/null || head -c 12 /dev/urandom | base64)"

# ---------- create ----------
info "Tworzę LXC ${CTID} (${HOSTNAME})…"
pct create "$CTID" "$TMPL" \
  --hostname "$HOSTNAME" \
  --cores "$CORES" --memory "$RAM" --swap "$RAM" \
  --rootfs "${STORE}:${DISK}" \
  --net0 "$NET" "${NS_ARG[@]}" \
  --features "nesting=${NESTING}" \
  --password "$PASSWORD" \
  --ostype debian --unprivileged "$CTTYPE" --onboot 1 \
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

# ---------- API token + role on host (fully automatic) ----------
# Auto-detects the API endpoint (host IP on the container's bridge, port 8006)
# and auto-provisions a scoped token. The token can read every guest and its
# snapshots (VM.Audit) and create/delete them (VM.Snapshot) — nothing else.
info "Auto-provisioning roli/tokenu API na hoście (endpoint: ${PVE_HOST}:8006)…"
pveum role add AutoSnap --privs "VM.Snapshot VM.Audit" 2>/dev/null || \
  pveum role modify AutoSnap --privs "VM.Snapshot VM.Audit" 2>/dev/null || true
pveum user add autosnap@pve --comment "proxmox-autosnap" 2>/dev/null || true
pveum acl modify /vms --users autosnap@pve --roles AutoSnap 2>/dev/null || true
if pveum user token list autosnap@pve --output-format json 2>/dev/null | grep -q '"manager"'; then
  warn "Token autosnap@pve!manager już istniał — rotuję na nowy"
fi
pveum user token remove autosnap@pve manager 2>/dev/null || true
TOKVAL=$(pveum user token add autosnap@pve manager --privsep 0 --output-format json | \
  python3 -c "import sys,json;print(json.load(sys.stdin)['value'])")
[ -n "$TOKVAL" ] || die "nie udało się utworzyć tokenu"
# auto-verify the token actually works against the API
if curl -fsSk -H "Authorization: PVEAPIToken=autosnap@pve!manager=${TOKVAL}" \
     "https://${PVE_HOST}:8006/api2/json/version" >/dev/null 2>&1; then
  msg "Token API zweryfikowany (autosnap@pve!manager)"
else
  warn "Token utworzony, ale nie zweryfikowałem połączenia z ${PVE_HOST}:8006 — sprawdź sieć kontenera"
fi

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
echo -e "   Kontener: CT ${BD}${CTID}${NC} (${HOSTNAME}) · IP ${BD}${CT_IP}${NC}"
echo -e "   Root CT:  hasło ${BD}${PASSWORD}${NC}  (dostęp też przez: pct enter ${CTID})"
echo
echo -e "   ${YW}HTTPS:${NC} wystaw przez reverse proxy (np. Nginx Proxy Manager) → http://${CT_IP}:80"
echo -e "   ${YW}Uwaga:${NC} panel wpuszcza tylko allowlistę (domyślnie root@pam). Nie wystawiaj"
echo -e "          publicznie bez dodatkowej ochrony (Access List / VPN)."
echo
