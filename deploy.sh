#!/usr/bin/env bash
# Deploy the COMMITTED state of this repo (the `app/` package) to the VPS: tests here first, import check there, keep
# a copy of what was running, restart, and put the old version back if the manager does not come up.
#
#     ./deploy.sh                   (DEPLOY_HOST=user@host to aim elsewhere)
#     DEPLOY_ENV=staging ./deploy.sh   the staging manager on the same VPS (/opt/acm-staging, 127.0.0.1:8081, game ports 9800-9924; made by ops/staging_setup.sh).
#                                   Production takes only a tree that staging already ran (DEPLOY_SKIP_STAGING=1: hotfix), never a dirty working tree,
#                                   and not while drivers are connected (DEPLOY_FORCE_PLAYERS=1: hotfix).
#
# Restarting the manager does not touch running acServers (they are re-attached, see app/supervisor.py).
# Left alone on the server: .venv, data/, app/static (the built frontend), __pycache__.
set -euo pipefail
HOST=${DEPLOY_HOST:-root@157.173.196.89}
ENV=${DEPLOY_ENV:-prod}
case $ENV in prod|staging) ;; *) echo "DEPLOY_ENV es prod o staging, no '$ENV'." >&2; exit 1;; esac
cd "$(dirname "$0")"
TREE=HEAD; REV=$(git rev-parse --short HEAD)
if ! git diff --quiet HEAD || [ -n "$(git ls-files --others --exclude-standard)" ]; then
  if [ "${DEPLOY_DIRTY:-}" != 1 ]; then
    echo "Hay cambios sin commit: haz commit primero (o DEPLOY_DIRTY=1 para probar el árbol de trabajo sin commit EN STAGING)." >&2; exit 1
  fi
  if [ "$ENV" = prod ]; then echo "Producción no despliega un árbol sin commit: pruébalo en staging con DEPLOY_ENV=staging." >&2; exit 1; fi
  IDX=$(mktemp -u); GIT_INDEX_FILE=$IDX git add -A; TREE=$(GIT_INDEX_FILE=$IDX git write-tree); rm -f "$IDX"; REV="$REV-dirty"
fi
echo "0/5 tests locales (los mismos que corre la CI: .github/workflows/ci.yml)"
.venv/bin/python -m pytest -q -p no:warnings
echo "Desplegando $REV a $HOST ($ENV)"
TREEH=$(git rev-parse "$TREE^{tree}")   # content hash (see the web repo's ops/deploy.sh): what staging ran is what production may take
if [ "$ENV" = prod ] && [ "${DEPLOY_SKIP_STAGING:-}" != 1 ]; then
  RAN=$(ssh "$HOST" 'cat /opt/acm-staging/app/DEPLOYED.tree 2>/dev/null || true')
  [ "$RAN" = "$TREEH" ] || { echo "Staging no ha corrido este árbol (staging: ${RAN:-nada}, esto: $TREEH). Despliega primero con DEPLOY_ENV=staging, o DEPLOY_SKIP_STAGING=1 para un hotfix." >&2; exit 1; }
fi
STAGE=$(ssh "$HOST" 'mktemp -d $STAGE.XXXXXX')   # one folder per run
git archive $TREE app pyproject.toml | ssh "$HOST" "tar -x -C $STAGE"
ssh "$HOST" ENV="$ENV" REV="$REV" STAGE="$STAGE" TREEH="$TREEH" FORCE_PLAYERS="${DEPLOY_FORCE_PLAYERS:-0}" bash -s <<'REMOTE'
set -euo pipefail
if [ "$ENV" = prod ]; then ROOT=/opt/acm; UNIT=acm; PORT=8080; else ROOT=/opt/acm-staging; UNIT=acm-staging; PORT=8081; fi
[ -d "$ROOT/app" ] || { echo "$ROOT no existe: corre ops/staging_setup.sh en la VPS primero." >&2; rm -rf "$STAGE"; exit 1; }
APP=$ROOT/app; REL=$ROOT/releases; PY=$APP/.venv/bin/python
mkdir -p "$REL"
exec 9>/var/lock/$UNIT-deploy.lock
flock -n 9 || { echo "Otro despliegue está en marcha (/var/lock/$UNIT-deploy.lock)." >&2; rm -rf "$STAGE"; exit 1; }
trap 'rm -rf "$STAGE" "$STAGE.chk" "$STAGE.rb"' EXIT

echo "1/5 comprobación de importación (no toca nada del servidor)"
cd "$STAGE"
ACM_DATA_DIR=$STAGE.chk "$PY" -m compileall -q app >/dev/null
ACM_DATA_DIR=$STAGE.chk "$PY" -c "import app.main"
rm -rf $STAGE.chk
cmp -s pyproject.toml "$APP/pyproject.toml" || echo "AVISO: pyproject.toml cambió; instala las dependencias nuevas a mano en $APP/.venv"

# prod: nobody driving (a restart of the manager mid-race would lose the race-control state it keeps in memory). The check is the web repo's, already on the VPS.
if [ "$ENV" = prod ]; then
  if [ "$FORCE_PLAYERS" = 1 ]; then echo "AVISO: DEPLOY_FORCE_PLAYERS=1, no se comprueba si hay pilotos conectados" >&2
  else
    ( set -a; . /opt/oprweb/.env; set +a; /opt/oprweb/app/.venv/bin/python /opt/oprweb/app/ops/players_gate.py --live ) \
      || { echo "Despliegue cancelado: hay gente conectada o el gestor no responde (DEPLOY_FORCE_PLAYERS=1 para saltarlo)." >&2; exit 1; }
  fi
fi

echo "2/5 copia de lo que corre ahora"
PREV=$(cat "$APP/DEPLOYED" 2>/dev/null || echo desconocida)
tar czf "$REL/before-$REV-$(date +%Y%m%d-%H%M%S).tgz" --exclude=__pycache__ --exclude=static -C "$APP" app pyproject.toml
ls -1t "$REL"/*.tgz | tail -n +6 | xargs -r rm -f

echo "3/5 copiando"
rsync -rlc --delete --exclude=static --exclude=__pycache__ --exclude='*.pyc' $STAGE/app/ "$APP/app/"
cp $STAGE/pyproject.toml "$APP/pyproject.toml"
echo "$REV" > "$APP/DEPLOYED"
chown -R "$UNIT:$UNIT" "$APP/app" "$APP/DEPLOYED" "$APP/pyproject.toml"

echo "4/5 reiniciando"
systemctl restart "$UNIT"
ok=0
for i in $(seq 1 20); do
  sleep 1
  [ "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:$PORT/healthz)" = 200 ] && { ok=1; break; }
done

if [ "$ok" = 1 ]; then echo "$TREEH" > "$APP/DEPLOYED.tree"; chown "$UNIT:$UNIT" "$APP/DEPLOYED.tree"   # only now: what staging "ran" is a tree that came up
  echo "5/5 listo: $REV en marcha"; exit 0; fi
echo "5/5 NO arrancó: volviendo a la versión anterior" >&2
last=$(ls -1t "$REL"/before-*.tgz | head -1)
rm -rf $STAGE.rb && mkdir $STAGE.rb && tar xzf "$last" -C $STAGE.rb
rsync -rlc --delete --exclude=static --exclude=__pycache__ $STAGE.rb/app/ "$APP/app/"
cp $STAGE.rb/pyproject.toml "$APP/pyproject.toml"
echo "$PREV" > "$APP/DEPLOYED"; chown -R "$UNIT:$UNIT" "$APP/app" "$APP/DEPLOYED"; systemctl restart "$UNIT"
echo "Restaurado desde $last" >&2; exit 1
REMOTE

# Deja en el changelog del dia que esta construido, desplegado y si ya esta en main (git + DEPLOYED del VPS, ver el script): le pasa la REV enviada y el HOST al que se desplego. Solo se llega aqui si el despliegue salio bien.
LOG=${DEPLOY_LOG:-$HOME/.claude/skills/feature-done/deploy-log.sh}
if [ "$ENV" != prod ]; then echo "(staging: no se anota en el changelog)"
elif [ -x "$LOG" ]; then DEPLOY_HOST="$HOST" "$LOG" manager --deploy "$REV" || echo "OJO: el registro del changelog no cuadra con lo que hay en la VPS (ver arriba)" >&2
else echo "(no existe $LOG: este despliegue no queda en el changelog)" >&2; fi
