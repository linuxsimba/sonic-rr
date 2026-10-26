#!/usr/bin/env bash
# Configure the SONiC spines/leaves (and BFD on the route servers) for the HPN Pivot lab.
#   MODE=pivot  (default) leaves advertise loopback + their OWN GPU /32s to spines; spines send
#                         only loopbacks down; leaves learn GPU routes only from the RSC (rr1/rr2)
#                         (Alibaba HPN Pivot: spines hold one path per GPU, cross-rail works)
#   MODE=strict           like pivot, but leaves advertise loopback only -> spines have no GPU
#                         routes (same-rail works, cross-rail blackholes at the spine)
#   MODE=traditional      classic leaf/spine: spines relay every GPU route to every leaf,
#                         RSC sessions shut -> each leaf learns each GPU from every spine
#   BFD=off (default) | on   BFD (FAST profile) on every BGP session
#   SPINES=8                 number of spines (spine1..spineN)
# Safe to re-run on a running lab to switch MODE/BFD.
set -euo pipefail
MODE=${MODE:-pivot}
BFD=${BFD:-off}
SPINES=${SPINES:-8}
LAB=clab-sonic-rr
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# leaf port facing spineN: spine1/2 on Ethernet0/4, spine3..8 on Ethernet56..76
spine_port() { (( $1 <= 2 )) && echo "Ethernet$(( 4*($1-1) ))" || echo "Ethernet$(( 56 + 4*($1-3) ))"; }
LEAF_SPINE_PORTS=$(for i in $(seq 1 "$SPINES"); do spine_port "$i"; done)

BFD_PROFILE='bfd
 profile FAST
  receive-interval 300
  transmit-interval 300
  detect-multiplier 3
 exit
exit'

bfd_lines() {  # peer-group...
  local g
  for g in "$@"; do
    if [[ $BFD == on ]]; then echo " neighbor $g bfd profile FAST"; else echo " no neighbor $g bfd"; fi
  done
}

wait_sonic() {
  local n=$1
  echo -n "waiting for $n "
  until docker exec "$LAB-$n" bash -c 'supervisorctl status orchagent | grep -q RUNNING && vtysh -c "show version" >/dev/null 2>&1 && [ "$(redis-cli -n 4 keys "PORT|Ethernet*" | wc -l)" -ge 32 ]' 2>/dev/null; do
    echo -n .; sleep 3
  done
  echo " up"
  # sonic-vs starts lldpd without its control socket; restart so lldpcli works
  docker exec "$LAB-$n" supervisorctl restart lldpd >/dev/null
  # sonic-vs ships bfdd but doesn't run it
  docker exec "$LAB-$n" bash -c 'pgrep -x bfdd >/dev/null || /usr/lib/frr/bfdd -P 0 -d 2>/dev/null'
}

apply_frr() {  # node, file
  docker cp "$2" "$LAB-$1:/tmp/frr-lab.conf"
  docker exec "$LAB-$1" vtysh -f /tmp/frr-lab.conf 2>&1 >/dev/null \
    | grep -vE '^\[[0-9]+\|[a-z]+\] (done|sending configuration)|Waiting for children' || true
}

# ---------- spines ----------
spine() {  # node, loopback-octet
  local n=$1 lo=10.0.0.$2
  wait_sonic "$n"
  docker exec "$LAB-$n" bash -c "
    config loopback add Loopback0 2>/dev/null || true
    config interface ip add Loopback0 $lo/32 2>/dev/null || true
    for p in Ethernet0 Ethernet4; do
      config interface ipv6 enable use-link-local-only \$p
      config interface startup \$p
    done"

  local to_leaf
  if [[ $MODE == traditional ]]; then to_leaf="route-map TO-LEAF permit 20"   # relay everything
  else to_leaf="no route-map TO-LEAF permit 20"; fi                           # loopbacks only

  cat > "$TMP/$n.conf" <<EOF
hostname $n
$BFD_PROFILE
!
ip prefix-list LOOPBACKS seq 10 permit 10.0.0.0/24 ge 32
!
route-map TO-LEAF permit 10
 match ip address prefix-list LOOPBACKS
!
$to_leaf
!
router bgp 65100
 bgp router-id $lo
 no bgp ebgp-requires-policy
 bgp bestpath as-path multipath-relax
 neighbor FABRIC peer-group
 neighbor FABRIC remote-as external
 neighbor Ethernet0 interface peer-group FABRIC
 neighbor Ethernet4 interface peer-group FABRIC
$(bfd_lines FABRIC)
 address-family ipv4 unicast
  network $lo/32
  neighbor FABRIC route-map TO-LEAF out
  maximum-paths 16
 exit-address-family
EOF
  apply_frr "$n" "$TMP/$n.conf"
}

# ---------- leaves ----------
leaf() {  # node, asn, loopback-octet, rr1-link-octet, rr2-link-octet, gpu-third-octet-base
  local n=$1 asn=$2 lo=10.0.0.$3 l1=$4 l2=$5 base=$6
  wait_sonic "$n"
  local gpu_ifs="" gpu_static="" fabric_nbrs="" p
  for s in 1 2 3 4; do
    local port=Ethernet$((16 + 4*(s-1))) o=$((base + s))
    gpu_ifs+="config interface ip add $port 10.100.$o.0/31 2>/dev/null; config interface startup $port; "
    gpu_static+="ip route 10.100.$o.1/32 $port"$'\n'
  done
  for p in $LEAF_SPINE_PORTS; do fabric_nbrs+=" neighbor $p interface peer-group FABRIC"$'\n'; done
  docker exec "$LAB-$n" bash -c "
    config loopback add Loopback0 2>/dev/null || true
    config interface ip add Loopback0 $lo/32 2>/dev/null || true
    for p in $(echo $LEAF_SPINE_PORTS); do
      config interface ipv6 enable use-link-local-only \$p
      config interface startup \$p
    done
    config interface ip add Ethernet8  10.2.$l1.0/31 2>/dev/null; config interface startup Ethernet8
    config interface ip add Ethernet12 10.2.$l2.0/31 2>/dev/null; config interface startup Ethernet12
    $gpu_ifs"

  local to_spine_gpu from_spine_gpu rsc_state
  if [[ $MODE == strict ]]; then to_spine_gpu="no route-map TO-SPINE permit 20"
  else to_spine_gpu="route-map TO-SPINE permit 20
 match ip address prefix-list MY-GPU"; fi
  if [[ $MODE == traditional ]]; then
    from_spine_gpu="route-map FROM-SPINE permit 20
 match ip address prefix-list GPU"
    rsc_state=" neighbor 10.0.0.101 shutdown
 neighbor 10.0.0.102 shutdown"
  else
    from_spine_gpu="no route-map FROM-SPINE permit 20"
    rsc_state=" no neighbor 10.0.0.101 shutdown
 no neighbor 10.0.0.102 shutdown"
  fi

  cat > "$TMP/$n.conf" <<EOF
hostname $n
$BFD_PROFILE
!
ip route 10.0.0.101/32 10.2.$l1.1
ip route 10.0.0.102/32 10.2.$l2.1
$gpu_static
ip prefix-list LOOPBACKS seq 10 permit 10.0.0.0/24 ge 32
ip prefix-list MY-LO seq 10 permit $lo/32
ip prefix-list GPU seq 10 permit 10.100.0.0/16 ge 32
ip prefix-list MY-GPU seq 10 permit 10.100.$base.0/17 ge 32
!
! GPU host routes are statics (real GPUs + scale.sh injected); advertise only our own
route-map GPU-HOSTS permit 10
 match ip address prefix-list MY-GPU
!
route-map TO-SPINE permit 10
 match ip address prefix-list MY-LO
$to_spine_gpu
!
route-map FROM-SPINE permit 10
 match ip address prefix-list LOOPBACKS
$from_spine_gpu
!
route-map TO-RSC permit 10
 match ip address prefix-list MY-GPU
 set ip next-hop $lo
!
route-map FROM-RSC permit 10
 match ip address prefix-list GPU
!
router bgp $asn
 bgp router-id $lo
 no bgp ebgp-requires-policy
 bgp bestpath as-path multipath-relax
 neighbor FABRIC peer-group
 neighbor FABRIC remote-as external
$fabric_nbrs neighbor RSC peer-group
 neighbor RSC remote-as 65500
 neighbor RSC ebgp-multihop 2
 neighbor RSC update-source Loopback0
 no neighbor RSC enforce-first-as
 neighbor 10.0.0.101 peer-group RSC
 neighbor 10.0.0.102 peer-group RSC
$rsc_state
$(bfd_lines FABRIC RSC)
 address-family ipv4 unicast
  network $lo/32
  redistribute static route-map GPU-HOSTS
  neighbor FABRIC route-map FROM-SPINE in
  neighbor FABRIC route-map TO-SPINE out
  neighbor RSC route-map FROM-RSC in
  neighbor RSC route-map TO-RSC out
  maximum-paths 16
 exit-address-family
EOF
  apply_frr "$n" "$TMP/$n.conf"
}

# ---------- route servers: only the BFD toggle (rest comes from rr/rrN.conf) ----------
rr_bfd() {
  local n=$1
  docker exec "$LAB-$n" sh -c 'pgrep -x bfdd >/dev/null || /usr/lib/frr/bfdd -d -A 127.0.0.1 2>/dev/null' || true
  if [[ $BFD == on ]]; then
    docker exec "$LAB-$n" vtysh -c 'conf t' -c 'router bgp 65500' -c 'neighbor LEAF bfd profile FAST' >/dev/null 2>&1
  else
    docker exec "$LAB-$n" vtysh -c 'conf t' -c 'router bgp 65500' -c 'no neighbor LEAF bfd' >/dev/null 2>&1
  fi
}

echo "MODE=$MODE BFD=$BFD SPINES=$SPINES"
for i in $(seq 1 "$SPINES"); do spine "spine$i" "$i"; done
leaf leaf1 65001 11 11 12 0
leaf leaf2 65002 12 21 22 128
rr_bfd rr1
rr_bfd rr2
# re-send updates so a policy change (e.g. switching MODE) takes effect on a running lab
for n in $(seq -f 'spine%g' 1 "$SPINES") leaf1 leaf2; do
  docker exec "$LAB-$n" vtysh -c 'clear bgp ipv4 unicast * soft' >/dev/null
done
echo "done. run ./compare.sh in ~30s"
