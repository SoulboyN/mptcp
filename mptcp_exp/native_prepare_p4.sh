#!/bin/bash
set -euo pipefail
out=/workspace/mptcp_exp/results/native_mptcp
# Thrift AI_ADDRCONFIG requires a non-loopback address in the management ns.
if ! ip link show native-mgmt >/dev/null 2>&1; then
    ip link add native-mgmt type dummy
    ip addr add 192.0.2.1/32 dev native-mgmt
    ip link set native-mgmt up
fi
if [[ ! -f /p4root/.runtime-ready ]]; then
    mkdir -p /p4root
    tar -xf "$out/p4-runtime.tar" -C /p4root
    touch /p4root/.runtime-ready
fi
if ! mountpoint -q /p4root; then
    mount --bind /p4root /p4root
fi
mkdir -p /run/netns
for dir in proc sys dev run tmp workspace; do
    mkdir -p "/p4root/$dir"
    mount --bind "/$dir" "/p4root/$dir"
done
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
chroot /p4root /bin/bash -c 'command -v simple_switch; command -v p4c; python2 --version'
if [[ ! -f /tmp/native-topology.pid ]] || ! kill -0 "$(cat /tmp/native-topology.pid)" 2>/dev/null; then
    # Explicit experiment control marker, never user data.
    rm -f /tmp/native_topology_stop
    : > "$out/topology.log"
    chroot /p4root python2 -u /workspace/mptcp_exp/native_topology_bridge.py > "$out/topology.log" 2>&1 &
    echo "$!" > /tmp/native-topology.pid
fi
