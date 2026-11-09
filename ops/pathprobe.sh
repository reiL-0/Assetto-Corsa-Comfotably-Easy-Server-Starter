#!/usr/bin/env bash
# Route probe: is a high in-game ping the network path or the server?
# Run it on the pilot's PC during a race:   ops/pathprobe.sh 157.173.196.89
# (or on the VPS towards a pilot's IP, if their router answers ping). Ctrl-C stops it.
# Every 30 s: 10 pings -> one line "UTC host loss% min/avg/max/mdev"; if a ping went over 120 ms or any was lost, the route (hop by hop) is appended.
# Compare the lines with ops: `python -m app.resourcelog summary` on the VPS (late_ms / psi_cpu): both bad = the server; only this one bad = the route.
set -u
HOST=${1:?usage: pathprobe.sh <host-or-ip> [logfile]}
LOG=${2:-pathprobe-$HOST.log}
if command -v traceroute >/dev/null; then ROUTE=(traceroute -n -w1 -q1 -m 20 "$HOST"); else ROUTE=(tracepath -n -m 20 "$HOST"); fi
while true; do
  out=$(ping -c 10 -i 0.2 -q "$HOST" 2>&1)
  loss=$(grep -o '[0-9.]*% packet loss' <<<"$out" | cut -d% -f1)
  rtt=$(grep -o '= [0-9./]*' <<<"$out" | cut -c3-)
  echo "$(date -u +%FT%TZ) $HOST loss=${loss:-100}% rtt=${rtt:-none}" | tee -a "$LOG"
  max=$(cut -d/ -f3 <<<"$rtt" | cut -d. -f1)
  if [ "${loss:-100}" != "0" ] || [ "${max:-9999}" -ge 120 ]; then "${ROUTE[@]}" 2>&1 | sed 's/^/    /' | tee -a "$LOG"; fi
  sleep 20
done
