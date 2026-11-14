#!/usr/bin/env bash
# Puts wise-plus online for testing, on the same AWS server as Wise (or on a server of its own).
# Run it on the server:
#
#   sudo git clone -b emb-look-and-bill-scan https://github.com/vin3595/wise-plus /opt/wiseplus/src
#   sudo bash /opt/wiseplus/src/deploy/plus/install.sh
#
# Wise isn't touched: wise-plus has its own database and data folder (/opt/wiseplus). The only change
# outside it is one site block added to the end of Wise's Caddyfile, so the plus address gets HTTPS.
# Safe to run again.
set -euo pipefail

ROOT=/opt/wiseplus
SRC_IN=$(cd "$(dirname "$0")/../.." && pwd)
SRC=$ROOT/src
ENV_FILE=$ROOT/.env
step() { printf '\n\033[1m%s\033[0m\n' "$*"; }
die() { printf 'install: %s\n' "$*" >&2; exit 1; }
ask() { local q=$1 def=${2:-} a; read -r -p "$q${def:+ [$def]}: " a; echo "${a:-$def}"; }
secret() { head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c "${1:-32}"; }

[ "$(id -u)" -eq 0 ] || die "run it with sudo: sudo bash $0"

step "1/6 Checking the server"
command -v docker >/dev/null || {
  . /etc/os-release; [ "${ID:-}" = ubuntu ] || die "install Docker first"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -q && apt-get install -yq docker.io docker-compose-v2 git curl dnsutils >/dev/null
  systemctl enable --now docker >/dev/null
}
FREE=$(free -m | awk '/Mem:/{print $7}'); SWAP=$(free -m | awk '/Swap:/{print $2}')
echo "  Memory free: ${FREE} MB, swap: ${SWAP} MB"
if [ "$FREE" -lt 600 ]; then
  echo "  Warning: under 600 MB free. wise-plus needs about 450 MB while running, and building it needs swap."
fi
if [ "$SWAP" -lt 1024 ] && ! swapon --show | grep -q /swapfile; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  echo "  Added 2 GB of swap so the build doesn't run out of memory."
fi

step "2/6 Code in $SRC"
mkdir -p "$ROOT"/{data/uploads,data/postgres,data/caddy,backups}
if [ "$SRC_IN" != "$SRC" ]; then
  rm -rf "$SRC"; mkdir -p "$SRC"
  tar -C "$SRC_IN" --exclude=node_modules --exclude=__pycache__ --exclude=dist -cf - . | tar -C "$SRC" -xf -
fi
[ -d "$SRC/.git" ] && echo "  $(git -C "$SRC" log -1 --format='%h %s' 2>/dev/null)"
install -m 755 "$SRC/deploy/plus/wiseplus-cloud" /usr/local/bin/wiseplus-cloud

step "3/6 Settings"
touch "$ENV_FILE"; chmod 600 "$ENV_FILE"
get() { grep -E "^$1=" "$ENV_FILE" | tail -1 | cut -d= -f2- || true; }
put() { if grep -qE "^$1=" "$ENV_FILE"; then sed -i "s|^$1=.*|$1=$2|" "$ENV_FILE"; else echo "$1=$2" >> "$ENV_FILE"; fi; }
DOMAIN=$(get WISEPLUS_DOMAIN); [ -n "$DOMAIN" ] || DOMAIN=$(ask "Address for wise-plus" plus.easemybill.com)
put WISEPLUS_DOMAIN "$DOMAIN"
EMAIL=$(get WISE_ADMIN_EMAIL)
[ -n "$EMAIL" ] || EMAIL=$(grep -E '^WISE_ADMIN_EMAIL=' /opt/wise/.env 2>/dev/null | cut -d= -f2- || true)
[ -n "$EMAIL" ] || EMAIL=$(ask "Email for certificate notices")
put WISE_ADMIN_EMAIL "$EMAIL"
[ -n "$(get WISE_JWT_SECRET)" ] || put WISE_JWT_SECRET "$(secret 48)"
[ -n "$(get POSTGRES_PASSWORD)" ] || put POSTGRES_PASSWORD "$(secret)"
[ -n "$(get WISE_OWNER_PASSWORD)" ] || put WISE_OWNER_PASSWORD "$(secret)"
[ -n "$(get WISE_APP_PASSWORD)" ] || put WISE_APP_PASSWORD "$(secret)"
[ -n "$(get WISE_OTP_PROVIDER)" ] || put WISE_OTP_PROVIDER box
# A test app: anyone you give the address to can make a test restaurant. Close it with: wiseplus-cloud signup closed
[ -n "$(get WISE_SIGNUP_MODE)" ] || put WISE_SIGNUP_MODE open
grep -qE '^WISE_GEMINI_API_KEY=' "$ENV_FILE" || {
  K=$(ask "Gemini API key for Scan bill (Enter to skip; add later with: wiseplus-cloud gemini <key>)")
  put WISE_GEMINI_API_KEY "$K"
}

step "4/6 HTTPS"
if docker ps --format '{{.Names}}' | grep -qx wise-caddy-1 && [ -f /opt/wise/src/deploy/aws/Caddyfile ]; then
  echo "  Wise is on this server: its Caddy will serve $DOMAIN too."
  put WISEPLUS_MODE shared
  put WISEPLUS_EDGE_NETWORK "$(docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' wise-caddy-1 | awk '{print $1}')"
  put WISEPLUS_EDGE_EXTERNAL true
else
  echo "  No Wise here: wise-plus runs its own Caddy on ports 80/443."
  put WISEPLUS_MODE own
  put WISEPLUS_EDGE_NETWORK wiseplus_edge
  put WISEPLUS_EDGE_EXTERNAL false
fi
IP=$(curl -s --max-time 5 https://checkip.amazonaws.com || true)
DNS=$(dig +short "$DOMAIN" A 2>/dev/null | tail -1 || true)
if [ -n "$IP" ] && [ "$DNS" != "$IP" ]; then
  echo "  Note: $DOMAIN points to '${DNS:-nothing}', this server is $IP."
  echo "  In Cloudflare add: type A, name ${DOMAIN%%.*}, IPv4 $IP, proxy OFF (grey cloud). HTTPS starts once it does."
fi

step "5/6 Nightly backup and keeping the address linked"
cat > /etc/systemd/system/wiseplus-backup.service <<UNIT
[Unit]
Description=wise-plus nightly database backup
[Service]
Type=oneshot
ExecStart=/usr/local/bin/wiseplus-cloud backup
UNIT
cat > /etc/systemd/system/wiseplus-backup.timer <<UNIT
[Unit]
Description=wise-plus nightly database backup
[Timer]
OnCalendar=*-*-* 04:40:00
Persistent=true
[Install]
WantedBy=timers.target
UNIT
# A Wise update replaces Wise's Caddyfile; this puts the plus address back within 10 minutes.
cat > /etc/systemd/system/wiseplus-link.service <<UNIT
[Unit]
Description=Keep wise-plus's address on Wise's Caddy
[Service]
Type=oneshot
ExecStart=/usr/local/bin/wiseplus-cloud link quiet
UNIT
cat > /etc/systemd/system/wiseplus-link.timer <<UNIT
[Unit]
Description=Keep wise-plus's address on Wise's Caddy
[Timer]
OnBootSec=3min
OnUnitActiveSec=10min
[Install]
WantedBy=timers.target
UNIT
if [ -d /run/systemd/system ]; then
  systemctl daemon-reload
  systemctl enable --now wiseplus-backup.timer >/dev/null
  if [ "$(get WISEPLUS_MODE)" = shared ]; then systemctl enable --now wiseplus-link.timer >/dev/null; fi
else
  echo "  No systemd here: nightly backups won't run by themselves (use: sudo wiseplus-cloud backup)."
fi

step "6/6 Building and starting wise-plus (10-20 minutes the first time on a small server)"
/usr/local/bin/wiseplus-cloud start-fresh

printf '\n\033[1mwise-plus is online at https://%s\033[0m\n' "$DOMAIN"
echo "  Sign up a test restaurant there. The sign-in code: sudo wiseplus-cloud login-code"
echo "  Wise is still at its own address, untouched. Check both with: sudo wiseplus-cloud status"
