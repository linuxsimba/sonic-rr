#!/usr/bin/env bash
# Dump and compare routing state on every node of the HPN Pivot lab.
LAB=clab-sonic-rr
hdr() { printf '\n==================== %s ====================\n' "$*"; }

echo "GPU /32 count in FIB per node (10.100.x.1/32):"
for n in spine1 spine2 leaf1 leaf2 rr1 rr2; do
  c=$(docker exec $LAB-$n vtysh -c 'show ip route' 2>/dev/null | grep -c '10\.100\.[0-9]*\.1/32')
  printf '  %-7s %s\n' "$n" "$c"
done

for n in spine1 spine2 leaf1 leaf2 rr1 rr2; do
  hdr "$n: show ip route"
  docker exec $LAB-$n vtysh -c 'show ip route' 2>/dev/null | grep -v '^Codes\|^ *[a-zA-Z]* - \|^$'
  hdr "$n: show bgp ipv4 summary"
  docker exec $LAB-$n vtysh -c 'show bgp ipv4 unicast summary' 2>/dev/null | sed -n '/Neighbor/,$p'
done

hdr "leaf1: BGP paths for a leaf2 GPU (expect 2 paths, both from the RSC, NH 10.0.0.12)"
docker exec $LAB-leaf1 vtysh -c 'show bgp ipv4 unicast 10.100.130.1/32' 2>/dev/null

for n in srv1 srv2 srv3 srv4; do
  hdr "$n: ip route / ip rule"
  docker exec $LAB-$n sh -c 'ip route; ip rule | grep -v "lookup local\|lookup main\|lookup default"; ip route show table 100; ip route show table 101'
done

hdr "data plane"
docker exec $LAB-srv1 ping -c2 -W1 10.100.3.1 >/dev/null && echo "same-rail  srv1.gpu0 -> srv3.gpu0 : OK" || echo "same-rail  srv1.gpu0 -> srv3.gpu0 : FAIL"
docker exec $LAB-srv1 ping -c2 -W1 -I 10.100.1.1 10.100.130.1 >/dev/null && echo "cross-rail srv1.gpu0 -> srv2.gpu1 : OK (via spine)" || echo "cross-rail srv1.gpu0 -> srv2.gpu1 : FAIL (blackholed at spine?)"
docker exec $LAB-srv1 traceroute -n -m5 -w1 -q1 -s 10.100.1.1 10.100.130.1
