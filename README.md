# sonic-rr: HPN Pivot lab

A containerlab reproduction of the BGP Optimization in an AI cluster design Alibaba Cloud presented at the
OCP Global Summit 2026. Leaves learn GPU service routes from a route-server cluster (FRR) over
multihop eBGP, with a loopback next hop resolved recursively over a SONiC spine underlay, instead
of learning every GPU route from every spine. A pytest suite compares it with a traditional
leaf/spine fabric.

This is an independent lab built from the public talk. It is not affiliated with or endorsed by
Alibaba, and it only approximates their design.

![topology](topology.svg)

## Prerequisites
- Linux host with Docker and [containerlab](https://containerlab.dev) (tested: containerlab 0.82, Docker 29)
- Python 3.11+ (for Ansible and pytest)
- About 6 GB of free RAM for the default lab (each SONiC-VS container uses ~370 MB)
- The `docker-sonic-vs` image from the SONiC build server (see below; about 900 MB)

## Lab shape: [ansible/group_vars/all.yml](ansible/group_vars/all.yml)
```yaml
spines: 4
leaves: 4             # one GPU rail per leaf: every server gets one GPU NIC per leaf
route_servers: 2
servers: 4
mode: pivot           # pivot | strict | traditional
bfd: true
```
Change the counts, then run `deploy.yml` and `configure.yml`. Ansible reaches every node with
`docker exec` (the `community.docker.docker` connection), so no SSH is needed.

| Node | Image | Role |
|---|---|---|
| spine1..N | docker-sonic-vs | AS 65100, BGP unnumbered to every leaf |
| leaf1..N | docker-sonic-vs | AS 65000+L, all route policy lives here |
| rr1..N | quay.io/frrouting/frr:10.2.1 | AS 65500 route servers (RSC-BGP), leaf peers only |
| srv1..N | gpu-server (ubuntu 24.04) | one GPU NIC per leaf (ethL → leafL), no BGP |

| Addressing | |
|---|---|
| Loopbacks | spine N `10.0.0.N`, leaf L `10.0.0.(10+L)`, rr R `10.0.0.(100+R)` |
| GPU on rail L, server S | `10.(100+L).S.1/31`, leaf side `.0` (srv4 on leaf2 = 10.102.4.1) |
| Leaf L ↔ rr R link | `10.2.L.(2R-2)/31`, rr side `.(2R-1)` |
| Leaf ports | spines first (Ethernet0..), then route servers, then GPU servers |

## Build and deploy
```bash
# one time: python tools (pytest, ansible) and images
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
export PATH=$PWD/.venv/bin:$PATH
curl -fL -o images/docker-sonic-vs.gz \
  "https://sonic-build.azurewebsites.net/api/sonic/artifacts?branchName=master&platform=vs&target=target/docker-sonic-vs.gz"
docker load < images/docker-sonic-vs.gz
docker pull quay.io/frrouting/frr:10.2.1

# lab
ansible-playbook ansible/deploy.yml       # build server image, generate topo.clab.yml, containerlab deploy
ansible-playbook ansible/configure.yml    # configure every node (about 20 s)
./compare.sh                              # BGP summaries, GPU route counts, pings
```

## Modes
```bash
ansible-playbook ansible/configure.yml -e mode=pivot        # default: leaves learn GPU routes only from rr1/rr2
ansible-playbook ansible/configure.yml -e mode=traditional  # spines relay every GPU route to every leaf
ansible-playbook ansible/configure.yml -e mode=strict       # spines get loopbacks only: cross-rail blackholes
ansible-playbook ansible/configure.yml -e bfd=false         # BFD off
```
The playbook soft-clears BGP at the end, so switching modes on a running lab takes effect right away.

## Route injection
```bash
ansible-playbook ansible/scale.yml -e scale_action=add -e scale_count=10000 -e scale_leaf=leaf2
ansible-playbook ansible/scale.yml -e scale_action=del -e scale_count=10000 -e scale_leaf=leaf2
```
Adds blackhole GPU /32s in `10.(100+L).128.0/17` behind one leaf, applied as one burst.

## Tests (pytest)
```bash
.venv/bin/pytest            # traditional vs pivot: BGP paths on leaf1, routes re-sent after a spine1 BGP restart
```
[tests/test_bgp_paths.py](tests/test_bgp_paths.py) reconfigures the lab through the playbooks and shows
progress live (`log_cli` in [pytest.ini](pytest.ini)). It reads no BGP state with vtysh: leaf1 streams
every route its peers send it over **BMP** (BGP Monitoring Protocol, FRR's `bgpd_bmp` module) to a
small collector inside the test ([tests/bmp.py](tests/bmp.py)). Which leaves stream, and where to, is
set by `bmp_leaves` / `bmp_collector` / `bmp_port` in group_vars.

## BFD
Every BGP session runs BFD with a `FAST` profile (300 ms tx/rx, multiplier 3, so ~900 ms detection):
- leaf↔spine: single-hop over the unnumbered links (`neighbor FABRIC bfd profile FAST`)
- leaf↔RSC: multihop, loopback↔loopback (`neighbor RSC bfd profile FAST`; FRR picks multihop
  automatically because the session uses `ebgp-multihop`)
- RRs: `neighbor LEAF bfd profile FAST`, `bfdd=yes` in [ansible/files/daemons](ansible/files/daemons)

sonic-vs ships `bfdd` but doesn't start it; configure.yml starts it. Check with `show bfd peers brief`
in vtysh. Test: `docker pause clab-sonic-rr-rr1` makes leaf1 drop the rr1 session in about 0.8 s
(versus the 180 s BGP hold timer without BFD). Routes stay via rr2, with no traffic loss.

## Review and troubleshooting
```bash
docker exec -it clab-sonic-rr-leaf1 vtysh          # FRR shell on a switch
docker exec -it clab-sonic-rr-leaf1 bash           # SONiC shell (show ip route, show interfaces status)
docker exec -it clab-sonic-rr-rr1 vtysh            # route server
docker exec -it clab-sonic-rr-srv1 bash            # GPU server
```
| What | Command (in vtysh) |
|---|---|
| FRR config | `show running-config` (BGP only: `show running-config bgpd`) |
| BGP peers | `show bgp ipv4 unicast summary` |
| Paths for one GPU | `show bgp ipv4 unicast 10.102.4.1/32` |
| What a leaf sends a spine | `show bgp ipv4 unicast neighbors Ethernet0 advertised-routes` |
| What a leaf took from an RR | `show bgp ipv4 unicast neighbors 10.0.0.101 routes` |
| Policy hit counts | `show route-map TO-SPINE` |

LLDP (the CDP equivalent) runs on the SONiC nodes. Run `lldpcli` inside the container:
```bash
docker exec clab-sonic-rr-leaf1 lldpcli show neighbors summary
```
SONiC's `show lldp table` doesn't work in sonic-vs: its wrapper calls `sudo docker exec lldp ...`,
and sonic-vs runs everything in one container. The RRs and servers don't run LLDP.

GPU server data-plane tests:
```bash
ping 10.101.3.1                                  # same rail (rail 1), works in every mode
ping -I 10.101.1.1 10.102.2.1                    # cross rail via a spine (fails in strict mode)
traceroute -n -s 10.101.1.1 10.102.2.1
```

## Teardown
```bash
containerlab destroy -t topo.clab.yml --cleanup
```

## License
[MIT](LICENSE)
