#!/usr/bin/env bash
# Sets up the AC Server Manager on a fresh Debian 13 / Ubuntu 24.04 machine (the CDMX PC, a new VPS, a rebuilt one).
# Safe to run again: it only creates what is missing and never overwrites /etc/acm.env (your secrets) or the data.
#
#     sudo ops/install.sh [--ufw] [--db RESTORED/acmanager.db] [--no-start]
#       --ufw       also set the firewall: ssh + the game ports 9600-9699 tcp/udp (the web goes through a tunnel, no 80/443)
#       --db FILE   put that database (from ops restore.py of the nightly backup) in place before the first start
#       --no-start  do everything but do not start the manager
#     DRY=1 sudo ops/install.sh     prints what it would do and changes nothing
#
# Overridable: ACM_USER (acm) APP_DIR (/opt/acm/app) DATA_DIR (/opt/acm/data) ACSERVER_DIR (/opt/acserver) PORT (8080)
#              CPU_AFFINITY ("" = all cores; e.g. "1 2 3" keeps core 0 for the OS) LIMITS_SCOPE (user)
# NOT done here (it is yours to bring): the acServer binary + content/ in ACSERVER_DIR, and the secrets in /etc/acm.env.
set -euo pipefail

ACM_USER=${ACM_USER:-acm}; APP_DIR=${APP_DIR:-/opt/acm/app}; DATA_DIR=${DATA_DIR:-/opt/acm/data}
ACSERVER_DIR=${ACSERVER_DIR:-/opt/acserver}; PORT=${PORT:-8080}; CPU_AFFINITY=${CPU_AFFINITY:-}; LIMITS_SCOPE=${LIMITS_SCOPE:-user}
HOME_DIR=${HOME_DIR:-$(dirname "$APP_DIR")}; SRC=$(cd "$(dirname "$0")/.." && pwd)
UFW=${UFW:-0}; DB=""; START=1
while [ $# -gt 0 ]; do case $1 in
  --ufw) UFW=1;; --no-start) START=0;; --db) DB=${2:?--db needs a file}; shift;;
  *) echo "unknown option $1" >&2; exit 2;; esac; shift; done

# Values interpolated into systemd directives must not introduce whitespace or syntax.
for variable in ACM_USER APP_DIR DATA_DIR HOME_DIR ACSERVER_DIR PORT LIMITS_SCOPE; do
  value=${!variable}
  [[ "$value" =~ ^[A-Za-z0-9_./-]+$ ]] || { echo "$variable contains unsupported characters (allowed: A-Za-z0-9_./-)" >&2; exit 1; }
done
[[ "$CPU_AFFINITY" =~ ^[0-9[:space:]]*$ ]] && [[ "$CPU_AFFINITY" != *$'\n'* ]] && [[ "$CPU_AFFINITY" != *$'\r'* ]] || { echo "CPU_AFFINITY must contain only CPU numbers separated by spaces" >&2; exit 1; }

run() { if [ "${DRY:-}" = 1 ]; then echo "+ $*"; else "$@"; fi; }
say() { echo "== $*"; }
[ "${DRY:-}" = 1 ] || [ "$(id -u)" = 0 ] || { echo "run as root (sudo)" >&2; exit 1; }
[ -f "$SRC/pyproject.toml" ] && [ -d "$SRC/app" ] || { echo "run it from a checkout of the manager repo" >&2; exit 1; }

say "1/7 packages"
run apt-get update -qq
run apt-get install -y -qq python3 python3-venv python3-pip rsync sqlite3 zstd age rclone curl ca-certificates

say "2/7 user and folders"
id "$ACM_USER" >/dev/null 2>&1 || run useradd --system --create-home --home-dir "$HOME_DIR" --shell /bin/bash "$ACM_USER"
run install -d -o "$ACM_USER" -g "$ACM_USER" -m 700 "$HOME_DIR" "$DATA_DIR"
run install -d -o "$ACM_USER" -g "$ACM_USER" -m 755 "$APP_DIR" "$ACSERVER_DIR"
run install -d -o root -g root -m 755 "$HOME_DIR/releases"
# per-server CPU/RAM caps (ACM_LIMITS_SCOPE=user) run in the user's systemd instance, which must outlive logins
run loginctl enable-linger "$ACM_USER"

say "3/7 application + virtualenv"
run rsync -rlc --delete --exclude=static --exclude=__pycache__ --exclude='*.pyc' "$SRC/app/" "$APP_DIR/app/"
run cp "$SRC/pyproject.toml" "$APP_DIR/pyproject.toml"
[ -d "$APP_DIR/.venv" ] || run python3 -m venv "$APP_DIR/.venv"
run "$APP_DIR/.venv/bin/pip" install -q -e "$APP_DIR"
run chown -R "$ACM_USER:$ACM_USER" "$APP_DIR"

say "4/7 secrets file /etc/acm.env (never overwritten)"
if [ ! -f /etc/acm.env ]; then
  if [ "${DRY:-}" = 1 ]; then echo "+ write /etc/acm.env (0600) with the settings below and empty secrets"; else
  umask 077; cat > /etc/acm.env <<ENV
ACM_LIMITS_SCOPE=$LIMITS_SCOPE
ACM_PUBLIC_URL=
ACM_DISCORD_STATUS_WEBHOOK=
ACM_DISCORD_WEBHOOK=
ACM_DISCORD_BOT_TOKEN=
ACM_DISCORD_CHANNEL=
ACM_DISCORD_ROLE=
ACM_DISCORD_CLIENT_ID=
ACM_DISCORD_CLIENT_SECRET=
ENV
  fi
  echo "   -> fill in /etc/acm.env with the values from your password manager"
fi

say "5/7 systemd unit"
UNIT=$(cat <<UNIT
[Unit]
Description=AC Server Manager
After=network.target

[Service]
User=$ACM_USER
WorkingDirectory=$APP_DIR
Environment=ACM_HOST=127.0.0.1 ACM_PORT=$PORT ACM_DATA_DIR=$DATA_DIR ACM_SERVE_UI=false ACM_IDLE_STOP_SECONDS=600 ACM_ACSERVER_CMD=$ACSERVER_DIR/acServer
EnvironmentFile=-/etc/acm.env
ExecStart=$APP_DIR/.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port $PORT
Restart=on-failure
# the acServer processes must outlive a restart of the manager (app/supervisor.py re-attaches to them)
KillMode=process
Nice=-5
${CPU_AFFINITY:+CPUAffinity=$CPU_AFFINITY}

[Install]
WantedBy=multi-user.target
UNIT
)
if [ "${DRY:-}" = 1 ]; then echo "+ write /etc/systemd/system/acm.service"; else echo "$UNIT" > /etc/systemd/system/acm.service; fi
run install -m 755 "$SRC/ops/watchdog.py" "$HOME_DIR/watchdog.py"
WATCHDOG_UNIT=$(sed "s|/opt/acm/watchdog.py|$HOME_DIR/watchdog.py|" "$SRC/ops/acm-watchdog.service")
if [ "${DRY:-}" = 1 ]; then echo "+ write /etc/systemd/system/acm-watchdog.service: $HOME_DIR/watchdog.py"; else echo "$WATCHDOG_UNIT" > /etc/systemd/system/acm-watchdog.service; fi
run install -m 644 "$SRC/ops/acm-watchdog.timer" /etc/systemd/system/

say "6/7 restored database (optional)"
if [ -n "$DB" ]; then
  [ -f "$DB" ] || { echo "$DB not found" >&2; exit 1; }
  [ ! -e "$DATA_DIR/acmanager.db" ] || { echo "$DATA_DIR/acmanager.db already exists: refusing to overwrite it" >&2; exit 1; }
  run install -o "$ACM_USER" -g "$ACM_USER" -m 600 "$DB" "$DATA_DIR/acmanager.db"
fi

if [ "$UFW" = 1 ]; then
  say "firewall"
  run apt-get install -y -qq ufw
  SSH_PORTS=$( { sshd -T 2>/dev/null || true; } | awk '/^port [0-9]+$/{print $2}')
  if [ -z "$SSH_PORTS" ]; then
    SSH_PORTS=$( { ss -ltnp 2>/dev/null || true; } | awk '/sshd/ {n=split($4, address, ":"); if (address[n] ~ /^[0-9]+$/) print address[n]}')
  fi
  SSH_PORTS=${SSH_PORTS:-22}
  while IFS= read -r ssh_port; do
    run ufw allow "$ssh_port/tcp"
  done <<< "$SSH_PORTS"
  run ufw allow 9600:9699/tcp
  run ufw allow 9600:9699/udp
  run ufw --force enable
fi

say "7/7 start"
run systemctl daemon-reload
run systemctl enable acm.service acm-watchdog.timer
if [ "$START" = 1 ] && [ "${DRY:-}" != 1 ]; then
  [ -x "$ACSERVER_DIR/acServer" ] || echo "WARNING: $ACSERVER_DIR/acServer is missing: the manager starts but cannot launch game servers until you bring the binary and content/"
  systemctl restart acm.service acm-watchdog.timer
  for _ in $(seq 1 20); do
    sleep 1; [ "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/healthz")" = 200 ] && { echo "== done: manager answering on :$PORT"; exit 0; }
  done
  echo "the manager did not answer /healthz: journalctl -u acm -n 50" >&2; exit 1
fi
echo "== done (not started)"
