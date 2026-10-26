#!/usr/bin/env bash
# Inject or remove N synthetic GPU /32s behind leaf2 (10.100.192.0/18, inside leaf2's MY-GPU block).
# Applied as one vtysh batch so it lands as a single burst of BGP churn.
#   ./scale.sh add 10000
#   ./scale.sh del 10000
set -euo pipefail
ACTION=${1:?add|del}
N=${2:?count}
NODE=clab-sonic-rr-leaf2
(( N <= 16384 )) || { echo "max 16384 (10.100.192.0/18)"; exit 1; }

prefix=""; [[ $ACTION == del ]] && prefix="no "
f=$(mktemp)
trap 'rm -f "$f"' EXIT
for ((i = 0; i < N; i++)); do
  echo "${prefix}ip route 10.100.$((192 + i / 256)).$((i % 256))/32 blackhole"
done > "$f"
docker cp "$f" "$NODE:/tmp/scale.conf"
docker exec "$NODE" vtysh -f /tmp/scale.conf 2>&1 >/dev/null | grep -vE "^\[[0-9]+\|[a-z]+\] (done|sending)|Waiting for children" || true
echo "$ACTION $N routes on leaf2"
