#!/usr/bin/env bash
# Promote staging to production, the one way: everything is built on the `staging` branch and tried on the staging site; when it works, this script ships THAT SAME tree
# to production and only then moves `main` up to it.
#
#     ./promote.sh          check and show what would go (changes nothing)
#     ./promote.sh --go     deploy to production, then fast-forward main and push it
#
# Stops (exit 1) unless: you are on a clean `staging`; staging has run exactly this tree (DEPLOYED.tree: deploy with DEPLOY_ENV=staging first); main has nothing staging
# lacks (a hotfix on main: merge main into staging and try it there first). Production's own deploy still refuses a tree that staging has not run and a busy server.
set -euo pipefail
HOST=${DEPLOY_HOST:-root@157.173.196.89}
cd "$(dirname "$0")"
die() { echo "No se promueve: $*" >&2; exit 1; }
[ "$(git branch --show-current)" = staging ] || die "estás en '$(git branch --show-current)', no en staging."
[ -z "$(git status --porcelain)" ] || die "hay cambios sin commit."
git merge-base --is-ancestor main staging || die "main tiene commits que staging no tiene: fusiona main en staging y pruébalo allí."
TREE=$(git rev-parse 'staging^{tree}')
RAN=$(ssh "$HOST" 'cat /opt/acm-staging/app/DEPLOYED.tree 2>/dev/null || true')
[ "$RAN" = "$TREE" ] || die "staging no ha corrido este árbol (staging: ${RAN:-nada}). Despliega antes con DEPLOY_ENV=staging ./deploy.sh y pruébalo."
echo "Irían a producción ($(git rev-list --count main..staging) commits):"; git log --oneline main..staging
[ "${1:-}" = --go ] || { echo "(sin cambios: ejecuta con --go para desplegar)"; exit 0; }
./deploy.sh                                   # production (the tree is the one staging ran); on failure nothing below happens and main stays where it was
git switch -q main && git merge --ff-only staging && git push origin main && git switch -q staging
~/.claude/skills/feature-done/deploy-log.sh manager || true   # the photo that proves what is deployed is now in main
echo "Producción y main = $(git rev-parse --short main)"
