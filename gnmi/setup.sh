#!/usr/bin/env bash
# Run SONiC's gNMI server (docker-sonic-gnmi) as a sidecar of a sonic-vs node.
# docker-sonic-vs has no gNMI server; the sidecar shares the node's network namespace, so it
# reaches the node's Redis on 127.0.0.1:6379 and serves gNMI on <node mgmt IP>:8080 (TLS with a self-signed cert).
#   gnmi/setup.sh [node]      (default leaf1)
set -euo pipefail
NODE=${1:-leaf1}
LAB=clab-sonic-rr
IMAGE=docker-sonic-gnmi:latest
DIR=$(cd "$(dirname "$0")" && pwd)/run-$NODE
NAME=gnmi-$NODE

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  img=$(dirname "$DIR")/../images/docker-sonic-gnmi.gz
  [[ -f $img ]] || curl -fL -o "$img" \
    "https://sonic-build.azurewebsites.net/api/sonic/artifacts?branchName=master&platform=vs&target=target/docker-sonic-gnmi.gz"
  docker load < "$img"
fi

# the node's DB config, unchanged (swsscommon only talks to Redis over the unix socket)
mkdir -p "$DIR/sonic-db"
docker exec "$LAB-$NODE" cat /var/run/redis/sonic-db/database_config.json > "$DIR/sonic-db/database_config.json"

# swsscommon needs /var/run/redis/redis.sock, which lives in the node's container filesystem;
# redis_sock_proxy.py recreates it inside the sidecar, forwarding to the node's 127.0.0.1:6379
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" --network "container:$LAB-$NODE" \
  -v "$DIR/sonic-db:/var/run/redis/sonic-db:ro" \
  -v "$(dirname "$DIR")/redis_sock_proxy.py:/redis_sock_proxy.py:ro" \
  --entrypoint sh "$IMAGE" -c \
  'python3 /redis_sock_proxy.py & sleep 1; exec /usr/sbin/telemetry -port 8080 -insecure -allow_no_client_auth -client_auth none -logtostderr -v=2' >/dev/null

ip=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$LAB-$NODE")
echo "gNMI for $NODE: $ip:8080 (TLS, self-signed: skip verify)"
