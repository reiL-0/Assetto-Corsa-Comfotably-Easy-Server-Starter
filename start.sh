#!/usr/bin/env bash
# Checks dependencies, installs/builds via the Makefile, and manages the
# backend process. UI inclusion is controlled by start.conf (BUILD_UI=true|false).
#
# Usage: ./start.sh [start|stop|restart|status]   (default: start)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

PIDFILE=".start.pid"
LOGFILE="data/server.log"
BUILD_UI=true
[ -f start.conf ] && source start.conf

check_deps() {
    command -v python3 >/dev/null || { echo "error: python3 not found" >&2; exit 1; }
    if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)'; then
        echo "error: Python 3.12+ required, found $(python3 --version)" >&2
        exit 1
    fi
    if [ "$BUILD_UI" = "true" ]; then
        command -v node >/dev/null || { echo "error: Node.js not found (set BUILD_UI=false in start.conf to run headless)" >&2; exit 1; }
        command -v npm >/dev/null || { echo "error: npm not found (set BUILD_UI=false in start.conf to run headless)" >&2; exit 1; }
    fi
}

is_running() {
    [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null
}

do_start() {
    if is_running; then
        echo "already running (pid $(cat "$PIDFILE"))"
        return
    fi
    check_deps
    make install
    if [ "$BUILD_UI" = "true" ]; then
        make web
        export ACM_SERVE_UI=true
    else
        export ACM_SERVE_UI=false
    fi
    mkdir -p data
    # own process group (setsid) so `stop` can kill uvicorn's --reload worker too
    setsid .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8080 --reload \
        >>"$LOGFILE" 2>&1 < /dev/null &
    echo $! >"$PIDFILE"
    disown
    echo "started (pid $(cat "$PIDFILE")), logs: $LOGFILE"
}

do_stop() {
    if ! is_running; then
        echo "not running"
        rm -f "$PIDFILE"
        return
    fi
    pid=$(cat "$PIDFILE")
    kill -TERM "-$pid" 2>/dev/null || kill -TERM "$pid"
    for _ in $(seq 1 20); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.5
    done
    kill -0 "$pid" 2>/dev/null && kill -KILL "-$pid" 2>/dev/null
    rm -f "$PIDFILE"
    echo "stopped"
}

do_status() {
    if is_running; then
        echo "running (pid $(cat "$PIDFILE"))"
    else
        echo "not running"
    fi
}

case "${1:-start}" in
    start) do_start ;;
    stop) do_stop ;;
    restart) do_stop; do_start ;;
    status) do_status ;;
    *) echo "usage: $0 {start|stop|restart|status}" >&2; exit 1 ;;
esac
