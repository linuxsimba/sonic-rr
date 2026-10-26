#!/usr/bin/env bash
# Configure the SONiC spines/leaves for the HPN Pivot lab.
#   MODE=strict (default) leaves advertise loopback only -> spines have no GPU routes
#                         (same-rail works, cross-rail blackholes at the spine)
#   MODE=pivot            leaves also advertise their OWN GPU /32s to spines
#                         (Alibaba HPN Pivot: spines hold one path per GPU)
set -euo pipefail
MODE=${MODE:-strict}
LAB=clab-sonic-rr
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

wait_sonic() {
  local n=$1
  echo -n "waiting for $n "
  until docker exec "$LAB-$n" bash -c 'supervisorctl status orchagent | grep -q RUNNING && vtysh -c "show version" >/dev/null 2>&1 && [ "$(redis-cli -n 4 keys "PORT|Ethernet*" | wc -l)" -ge 32 ]' 2>/dev/null; do
    echo -n .; sleep 3
  done
  echo " up"
  # sonic-vs starts lldpd without its control socket; restart so lldpcli works
  docker exec "$LAB-$n" supervisorctl restart lldpd >/dev/null
}

apply_frr() {  # node, file
  docker cp "$2" "$LAB-$1:/tmp/frr-lab.conf"
  docker exec "$LAB-$1" vtysh -f /tmp/frr-lab.conf
}

# ---------- spines ----------
spine() {  # node, loopback-octet
  local n=$1 lo=10.0.0.$2
  wait_sonic "$n"
  docker exec "$LAB-$n" bash -c "
    config loopback add Loopback0 2>/dev/null || true
    config interface ip add Loopback0 $lo/32
    for p in Ethernet0 Ethernet4; do
      config interface ipv6 enable use-link-local-only \$p
      config interface startup \$p
    done"
  cat > "$TMP/$n.conf" <<EOF
hostname $n
router bgp 65100
 bgp router-id $lo
 no bgp ebgp-requires-policy
 bgp bestpath as-path multipath-relax
 neighbor FABRIC peer-group
 neighbor FABRIC remote-as external
 neighbor Ethernet0 interface peer-group FABRIC
 neighbor Ethernet4 interface peer-group FABRIC
 address-family ipv4 unicast
  network $lo/32
  maximum-paths 16
 exit-address-family
EOF
  apply_frr "$n" "$TMP/$n.conf"
}

# ---------- leaves ----------
leaf() {  # node, asn, loopback-octet, rr1-link-octet, rr2-link-octet, gpu-third-octet-base
  local n=$1 asn=$2 lo=10.0.0.$3 l1=$4 l2=$5 base=$6
  wait_sonic "$n"
  local gpu_ifs="" gpu_static="" gpu_net=""
  for s in 1 2 3 4; do
    local port=Ethernet$((16 + 4*(s-1))) o=$((base + s))
    gpu_ifs+="config interface ip add $port 10.100.$o.0/31; config interface startup $port; "
    gpu_static+="ip route 10.100.$o.1/32 $port"$'\n'
    gpu_net+="  network 10.100.$o.1/32"$'\n'
  done
  docker exec "$LAB-$n" bash -c "
    config loopback add Loopback0 2>/dev/null || true
    config interface ip add Loopback0 $lo/32
    for p in Ethernet0 Ethernet4; do
      config interface ipv6 enable use-link-local-only \$p
      config interface startup \$p
    done
    config interface ip add Ethernet8  10.2.$l1.0/31; config interface startup Ethernet8
    config interface ip add Ethernet12 10.2.$l2.0/31; config interface startup Ethernet12
    $gpu_ifs"

  local spine_out="route-map TO-SPINE permit 10
 match ip address prefix-list MY-LO"
  if [[ $MODE == pivot ]]; then spine_out+="
route-map TO-SPINE permit 20
 match ip address prefix-list MY-GPU"
  else spine_out+="
no route-map TO-SPINE permit 20"
  fi

  cat > "$TMP/$n.conf" <<EOF
hostname $n
ip route 10.0.0.101/32 10.2.$l1.1
ip route 10.0.0.102/32 10.2.$l2.1
$gpu_static
ip prefix-list LOOPBACKS seq 10 permit 10.0.0.0/24 ge 32
ip prefix-list MY-LO seq 10 permit $lo/32
ip prefix-list GPU seq 10 permit 10.100.0.0/16 ge 32
ip prefix-list MY-GPU seq 10 permit 10.100.$base.0/24 ge 32
!
$spine_out
!
route-map FROM-SPINE permit 10
 match ip address prefix-list LOOPBACKS
!
route-map TO-RSC permit 10
 match ip address prefix-list GPU
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
 neighbor Ethernet0 interface peer-group FABRIC
 neighbor Ethernet4 interface peer-group FABRIC
 neighbor RSC peer-group
 neighbor RSC remote-as 65500
 neighbor RSC ebgp-multihop 2
 neighbor RSC update-source Loopback0
 no neighbor RSC enforce-first-as
 neighbor 10.0.0.101 peer-group RSC
 neighbor 10.0.0.102 peer-group RSC
 address-family ipv4 unicast
  network $lo/32
$gpu_net  neighbor FABRIC route-map FROM-SPINE in
  neighbor FABRIC route-map TO-SPINE out
  neighbor RSC route-map FROM-RSC in
  neighbor RSC route-map TO-RSC out
  maximum-paths 16
 exit-address-family
EOF
  apply_frr "$n" "$TMP/$n.conf"
}

echo "MODE=$MODE"
spine spine1 1
spine spine2 2
leaf leaf1 65001 11 11 12 0
leaf leaf2 65002 12 21 22 128
echo "done. run ./compare.sh in ~30s"
