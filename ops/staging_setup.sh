#!/usr/bin/env bash
# Makes the STAGING manager on the production VPS (idempotent). Run it on the VPS as root:
#     ssh root@<vps> 'STAGING_ALLOW_IP=<your public IP> bash -s' < ops/staging_setup.sh        (STAGING_ALLOW_IP optional, see the end)
# Then `DEPLOY_ENV=staging ./deploy.sh` puts the code in. The manager listens on 127.0.0.1:8081 only; its game servers take ports 9800-9899 (internal HTTP 9900-9924),
# production's are 9600-9699 (9700-9724), so the two can never share a port. It has its own user, data, database, acServer copy (143 MB, without cfg/logs) and no
# Discord, claim secret or CSP script: nothing it does reaches the league's players or the production website.
set -euo pipefail
ROOT=/opt/acm-staging; U=acm-staging
id "$U" >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin "$U"
mkdir -p "$ROOT/app" "$ROOT/data" "$ROOT/releases"
if [ ! -d "$ROOT/app/.venv" ]; then
  /opt/acm/app/.venv/bin/python -m venv "$ROOT/app/.venv"
  # the same dependency versions production runs (the manager itself is run from its folder, not installed)
  /opt/acm/app/.venv/bin/pip freeze --exclude-editable | grep -v '^acmanager' > "$ROOT/requirements.freeze"
  "$ROOT/app/.venv/bin/pip" install -q -r "$ROOT/requirements.freeze"
fi
[ -d "$ROOT/acserver" ] || rsync -a --exclude=cfg --exclude=logs /opt/acserver/ "$ROOT/acserver/"
cat > "$ROOT/acm.env" <<ENV
ACM_HOST=127.0.0.1
ACM_PORT=8081
ACM_DATA_DIR=$ROOT/data
ACM_SERVE_UI=false
ACM_IDLE_STOP_SECONDS=600
ACM_ACSERVER_CMD=$ROOT/acserver/acServer
ACM_UPLOAD_PERMISSIVE=true
ACM_PORT_RANGE_START=9800
ACM_PORT_RANGE_END=9900
ACM_CSP_HUB=true
ACM_CSP_HUB_URL=https://staging.oppenpaddockracing.site/static/csp/opr_hub.lua
ACM_CSP_TRAINER_URL=https://staging.oppenpaddockracing.site/static/csp/opr_trainer.lua
ACM_CSP_TRAINER_SERVERS=1
ACM_CSP_CLAIM_SERVERS=1
ACM_CLAIM_URL=http://127.0.0.1:5200/api/telemetry/claim/issue
ACM_CLAIM_SECRET=$(sed -n 's/^OPR_CLAIM_SECRET=//p' /opt/oprweb-staging/.env 2>/dev/null)
ENV
chmod 640 "$ROOT/acm.env"; chown root:"$U" "$ROOT/acm.env"
chown -R "$U:$U" "$ROOT/app" "$ROOT/data" "$ROOT/releases" "$ROOT/acserver"
cat > /etc/systemd/system/acm-staging.service <<UNIT
[Unit]
Description=AC Server Manager (STAGING, 127.0.0.1:8081, game ports 9800-9899)
After=network.target

[Service]
User=$U
WorkingDirectory=$ROOT/app
EnvironmentFile=$ROOT/acm.env
Environment=HOME=$ROOT/data
ExecStart=$ROOT/app/.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8081
Restart=on-failure
# like production: the acServer processes outlive a restart of the manager and are re-attached (app/supervisor.py), so deploying here rehearses that
KillMode=process
# never in the way of the league's servers (acm runs on cores 1-3 at nice -5)
Nice=10
CPUAffinity=0
IOSchedulingClass=idle
MemoryMax=1G
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$ROOT
UMask=0077

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
# to try the game against staging: open its game ports to ONE address (yours). Remove with `ufw status numbered` + `ufw delete <n>` when done.
if [ -n "${STAGING_ALLOW_IP:-}" ]; then
  [[ "$STAGING_ALLOW_IP" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || { echo "STAGING_ALLOW_IP no es una IPv4: $STAGING_ALLOW_IP" >&2; exit 1; }
  for proto in tcp udp; do ufw allow from "$STAGING_ALLOW_IP" to any port 9800:9899 proto "$proto" comment 'opr staging acServer'; done
fi
echo "acm-staging listo en $ROOT (aún sin código: DEPLOY_ENV=staging ./deploy.sh)"
