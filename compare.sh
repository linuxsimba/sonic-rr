#!/usr/bin/env bash
# Dump routing state on every lab node, plus a same-rail and a cross-rail ping.
LAB=clab-sonic-rr
nodes() { docker ps --filter label=containerlab=sonic-rr --format '{{.Names}}' | sed "s/^$LAB-//" | grep -E "^$1" | sort -V; }
hdr() { printf '\n==================== %s ====================\n' "$*"; }

echo "GPU /32 routes in each router's routing table:"
for n in $(nodes spine) $(nodes leaf) $(nodes rr); do
  printf '  %-7s %s\n' "$n" "$(docker exec $LAB-$n vtysh -c 'show ip route' 2>/dev/null | grep -cE '10\.1[0-9]{2}\.[0-9]+\.[0-9]+/32')"
done
for n in $(nodes spine) $(nodes leaf) $(nodes rr); do
  hdr "$n: show bgp ipv4 summary"
  docker exec $LAB-$n vtysh -c 'show bgp ipv4 unicast summary' 2>/dev/null | sed -n '/Neighbor/,$p'
done
hdr "srv1: ip route / ip rule"
docker exec $LAB-srv1 sh -c 'ip route; ip rule'
hdr "data plane"
docker exec $LAB-srv1 ping -c2 -W1 10.101.3.1 >/dev/null && echo "same-rail  srv1 gpu1 -> srv3 gpu1 : OK" || echo "same-rail  : FAIL"
docker exec $LAB-srv1 ping -c2 -W1 -I 10.101.1.1 10.102.2.1 >/dev/null && echo "cross-rail srv1 gpu1 -> srv2 gpu2 : OK (via spine)" || echo "cross-rail : FAIL (strict mode?)"
