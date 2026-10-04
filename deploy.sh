#!/usr/bin/env bash
# Deploy the COMMITTED state of this repo (the `app/` package) to the VPS: tests here first, import check there, keep
# a copy of what was running, restart, and put the old version back if the manager does not come up.
#
#     ./deploy.sh                   (DEPLOY_HOST=user@host to aim elsewhere)
#
# Restarting the manager does not touch running acServers (they are re-attached, see app/supervisor.py).
# Left alone on the server: .venv, data/, app/static (the built frontend), __pycache__.
set -euo pipefail
HOST=${DEPLOY_HOST:-root@157.173.196.89}
cd "$(dirname "$0")"
if ! git diff --quiet HEAD || [ -n "$(git ls-files --others --exclude-standard)" ]; then
  echo "Hay cambios sin commit: haz commit primero (se despliega lo commiteado)." >&2; exit 1
fi
REV=$(git rev-parse --short HEAD)
echo "0/5 tests locales"
.venv/bin/python -m pytest -q -p no:warnings
echo "Desplegando $REV a $HOST"
git archive HEAD app pyproject.toml | ssh "$HOST" 'rm -rf /tmp/stage-acm && mkdir /tmp/stage-acm && tar -x -C /tmp/stage-acm'
ssh "$HOST" REV="$REV" bash -s <<'REMOTE'
set -euo pipefail
APP=/opt/acm/app; REL=/opt/acm/releases; PY=$APP/.venv/bin/python
mkdir -p "$REL"

echo "1/5 comprobación de importación (no toca nada del servidor)"
cd /tmp/stage-acm
ACM_DATA_DIR=/tmp/acm-deploy-check "$PY" -m compileall -q app >/dev/null
ACM_DATA_DIR=/tmp/acm-deploy-check "$PY" -c "import app.main"
rm -rf /tmp/acm-deploy-check
cmp -s pyproject.toml "$APP/pyproject.toml" || echo "AVISO: pyproject.toml cambió; instala las dependencias nuevas a mano en $APP/.venv"

echo "2/5 copia de lo que corre ahora"
tar czf "$REL/before-$REV-$(date +%Y%m%d-%H%M%S).tgz" --exclude=__pycache__ --exclude=static -C "$APP" app pyproject.toml
ls -1t "$REL"/*.tgz | tail -n +6 | xargs -r rm -f

echo "3/5 copiando"
rsync -rlc --delete --exclude=static --exclude=__pycache__ --exclude='*.pyc' /tmp/stage-acm/app/ "$APP/app/"
cp /tmp/stage-acm/pyproject.toml "$APP/pyproject.toml"
echo "$REV" > "$APP/DEPLOYED"
chown -R acm:acm "$APP/app" "$APP/DEPLOYED" "$APP/pyproject.toml"

echo "4/5 reiniciando"
systemctl restart acm
ok=0
for i in $(seq 1 20); do
  sleep 1
  [ "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/healthz)" = 200 ] && { ok=1; break; }
done

if [ "$ok" = 1 ]; then echo "5/5 listo: $REV en marcha"; exit 0; fi
echo "5/5 NO arrancó: volviendo a la versión anterior" >&2
last=$(ls -1t "$REL"/before-*.tgz | head -1)
rm -rf /tmp/rollback-acm && mkdir /tmp/rollback-acm && tar xzf "$last" -C /tmp/rollback-acm
rsync -rlc --delete --exclude=static --exclude=__pycache__ /tmp/rollback-acm/app/ "$APP/app/"
cp /tmp/rollback-acm/pyproject.toml "$APP/pyproject.toml"
chown -R acm:acm "$APP/app"; systemctl restart acm
echo "Restaurado desde $last" >&2; exit 1
REMOTE
