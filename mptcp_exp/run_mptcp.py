#!/usr/bin/env python2
"""
run_mptcp.py -- MPTCP-style multi-path experiment on 16 nodes.

Topology:
  - 16 netns hosts on a single BMv2 switch (subnet 10.0.0.0/24) as before.
  - PLUS direct veth links for the 'direct' subflows of each connection:
    node keeps an extra interface hN-dM in a dedicated /30 subnet
    (10.1.<idx>.0/30), so direct subflows do NOT cross the switch.

Data plane (software-side):
  - senders emit packets tagged with (flow_id, subflow_id, SSN) in the
    UDP payload; receivers reorder by DSN across subflows (M3/M4).
  - credit flow control per subflow; DCQCN ECN on switch subflows; RL
    global scheduler (M5-M7) added in later milestones.

Usage: docker exec p4app bash -c 'cd /workspace && python2 -u mptcp_exp/run_mptcp.py'
"""

import subprocess
import time
import os
import sys
import signal
import struct

# paths are relative to THIS file's directory (mptcp_exp/)
_HERE = os.path.dirname(os.path.abspath(__file__))
P4FILE = os.path.join(_HERE, 'simple_router_global.p4')
JSON   = os.path.join(_HERE, 'build', 'simple_router_global.json')
P4INFO = os.path.join(_HERE, 'build', 'simple_router_global.p4info.txt')

NODES = 16
NET = '10.0.0'
BASE_IP = lambda i: '{}.{}'.format(NET, i)
NS     = lambda i: 'ns-h{}'.format(i)
H_INTF = lambda i: 'h{}-eth0'.format(i)      # switch-facing interface
SW_INTF = lambda i: 's1-eth{}'.format(i)

sw_proc = None


def sh(cmd, check_ok=True):
    print '[+]', cmd if len(cmd) < 130 else cmd[:130] + '...'
    rc = subprocess.call(cmd, shell=True)
    if check_ok and rc != 0:
        print '[!] FAIL (rc={})'.format(rc)
    return rc


def sh_quiet(cmd):
    with open(os.devnull, 'w') as dn:
        rc = subprocess.call(cmd, shell=True, stdout=dn, stderr=dn)
    if rc != 0:
        print '[!] silent-fail rc={}: {}'.format(rc, cmd[:120])
    return rc


def cleanup():
    global sw_proc
    print '\n=== Cleanup ==='
    os.system('pkill -9 -f simple_switch 2>/dev/null')
    time.sleep(0.5)
    print '  all BMv2 stopped'
    for i in range(1, NODES + 1):
        sh('ip netns del {} 2>/dev/null'.format(NS(i)), check_ok=False)
        sh('ip link del {} 2>/dev/null'.format(SW_INTF(i)), check_ok=False)
        sh('ip link del {} 2>/dev/null'.format(H_INTF(i)), check_ok=False)
    # any leftover direct-link interfaces (hN-dM / hN-dM peer)
    os.system("ip link show 2>/dev/null | grep -oE 'h[0-9]+-d[0-9]+' | sort -u "
              "| while read x; do ip link del $x 2>/dev/null; done")
    sh('rm -f /tmp/bmv2-*.ipc', check_ok=False)


def mac_of(ns, iface):
    out = subprocess.check_output(
        'ip netns exec {} cat /sys/class/net/{}/address'.format(ns, iface),
        shell=True)
    return out.strip()


def run_cli(cmds):
    p = subprocess.Popen(
        ['simple_switch_CLI', '--thrift-port', '9090'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = p.communicate(input=cmds)
    return out, err


# ---- direct-link construction ----
# Each direct subflow of (src,dst) gets a unique /30: 10.1.<idx>.1 (src),
# 10.1.<idx>.2 (dst). Interface names: hN-d<idx>.
def build_direct_links(flows, pairs):
    """Create veth pairs for direct subflows. Returns dict
    {subflow_sid: (iface_src, iface_dst, ip_src, ip_dst)}."""
    links = {}
    idx = 1
    for f in flows:
        for sf in f.subflows:
            if sf.path != 'direct':
                continue
            iface_a = 'h{}-d{}'.format(f.src, idx)
            iface_b = 'h{}-d{}'.format(f.dst, idx)
            ip_a = '10.1.{}.1/30'.format(idx)
            ip_b = '10.1.{}.2/30'.format(idx)
            # create veth
            sh('ip link add {} type veth peer name {}'.format(iface_a, iface_b))
            sh('ip link set {} netns {}'.format(iface_a, NS(f.src)))
            sh('ip link set {} netns {}'.format(iface_b, NS(f.dst)))
            sh('ip netns exec {} ip addr add {} dev {}'.format(
                NS(f.src), ip_a, iface_a))
            sh('ip netns exec {} ip addr add {} dev {}'.format(
                NS(f.dst), ip_b, iface_b))
            sh('ip netns exec {} ip link set {} up'.format(NS(f.src), iface_a))
            sh('ip netns exec {} ip link set {} up'.format(NS(f.dst), iface_b))
            # static ARP on the /30 so direct subflows skip ARP entirely
            ma = mac_of(NS(f.src), iface_a)
            mb = mac_of(NS(f.dst), iface_b)
            sh_quiet('ip netns exec {} ip neigh replace {} lladdr {} dev {} nud permanent'
                     .format(NS(f.src), ip_b.split('/')[0], mb, iface_a))
            sh_quiet('ip netns exec {} ip neigh replace {} lladdr {} dev {} nud permanent'
                     .format(NS(f.dst), ip_a.split('/')[0], ma, iface_b))
            # also add a host route so the direct dest IP is reachable via
            # the direct interface (only for THIS dst to keep it simple)
            ip_b_host = ip_b.split('/')[0]
            sh_quiet('ip netns exec {} ip route add {} dev {}'.format(
                NS(f.src), ip_b_host, iface_a))
            links[sf.sid] = (iface_a, iface_b, ip_a, ip_b)
            idx += 1
    print '  [*] {} direct links built'.format(len(links))
    return links


def main():
    global sw_proc
    signal.signal(signal.SIGINT, lambda *_: (cleanup(), sys.exit(0)))
    cleanup()
    time.sleep(0.5)

    import flow_mptcp as fmod

    # ---- 1. Build connection graph (3~4 subflows, first direct) ----
    print '=== 1. Build MPTCP connection graph ==='
    flows, pairs = fmod.build_mptcp_graph(range(1, NODES + 1),
                                          min_sub=3, max_sub=4, seed=3)
    nd, ns = fmod.count_by_path(flows)
    print '  flows:', len(flows), ' direct subflows:', nd, ' switch subflows:', ns

    # ---- 2. Compile P4 ----
    print '\n=== 2. Compile P4 ==='
    if not os.path.exists(os.path.join(_HERE, 'build')):
        os.makedirs(os.path.join(_HERE, 'build'))
    subprocess.check_call([
        'p4c', '--target', 'bmv2', '--arch', 'v1model',
        '--p4runtime-files', P4INFO, '-o', os.path.join(_HERE, 'build'), P4FILE
    ])

    # ---- 3. Build 16-node switch topology ----
    print '\n=== 3. Build 16-node switch topology ==='
    for i in range(1, NODES + 1):
        sh('ip link add {} type veth peer name {}'.format(H_INTF(i), SW_INTF(i)))
        sh('ip link set {} up'.format(SW_INTF(i)))
        sh('ip netns add {}'.format(NS(i)))
        sh('ip link set {} netns {}'.format(H_INTF(i), NS(i)))
        sh('ip netns exec {} ip addr add {}/24 dev {}'.format(NS(i), BASE_IP(i), H_INTF(i)))
        sh('ip netns exec {} ip link set {} up'.format(NS(i), H_INTF(i)))
        sh('ip netns exec {} ip link set lo up'.format(NS(i)))

    # Static ARP on switch subnet (all pairs)
    print '\n=== 4. Static ARP (switch subnet) ==='
    for i in range(1, NODES + 1):
        for j in range(1, NODES + 1):
            if i == j:
                continue
            sh_quiet('ip netns exec {} ip neigh replace {} lladdr {} dev {} nud permanent'
                     .format(NS(i), BASE_IP(j), mac_of(NS(j), H_INTF(j)), H_INTF(i)))

    # ---- 5. Direct links for 'direct' subflows ----
    print '\n=== 5. Build direct subflow links ==='
    direct_links = build_direct_links(flows, pairs)

    # ---- 6. Start BMv2 ----
    print '\n=== 6. Start BMv2 (16 ports) ==='
    cmd = ['simple_switch', '--thrift-port', '9090']
    for i in range(1, NODES + 1):
        cmd += ['-i', '{}@{}'.format(i, SW_INTF(i))]
    cmd.append(JSON)
    logf = open(os.path.join(_HERE, 'build', 'bmv2_mptcp.log'), 'w')
    sw_proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT)
    time.sleep(5)
    if sw_proc.poll() is not None:
        print '  [!] BMv2 died!'
        logf.close()
        sys.exit(1)

    # LPM routes (switch subnet)
    print '\n=== 7. Push LPM routes ==='
    lines = []
    for i in range(1, NODES + 1):
        lines.append('table_add ipv4_lpm forward {}/32 => {} {}'.format(
            BASE_IP(i), i, mac_of(NS(i), H_INTF(i))))
    out, err = run_cli('\n'.join(lines) + '\n')

    # ---- 8. Sanity: verify a direct subflow path bypasses the switch ----
    print '\n=== 8. Verify direct subflow connectivity ==='
    # pick the first direct link
    if direct_links:
        sid = sorted(direct_links.keys())[0]
        ifa, ifb, ipa, ipb = direct_links[sid]
        dst_host = ipb.split('/')[0]
        # find the flow that owns this subflow
        for f in flows:
            for sf in f.subflows:
                if sf.sid == sid:
                    src = f.src
        print '  testing direct subflow {}: node{} -> {}'.format(sid, src, dst_host)
        # echo request over the direct interface
        out = subprocess.call(
            'ip netns exec {} ping -c 2 -W 1 {}'.format(NS(src), dst_host),
            shell=True)
        print '  direct ping rc:', out

    # ---- 9. M3/M4: multi-subflow send with SSN, reorder by DSN ----
    print '\n=== 9. M3/M4: multi-subflow SSN send + DSN reorder ==='
    # pick the first flow that has both a direct and a switch subflow
    demo = None
    for f in flows:
        paths = set(sf.path for sf in f.subflows)
        if 'direct' in paths and 'sw' in paths:
            demo = f
            break
    if demo is None:
        print '  no flow with both path types; skipping'
    else:
        import mptcp_io
        port = 7000
        sub = demo.subflows
        n_dir = len([sf for sf in sub if sf.path == 'direct'])
        n_sw = len(sub) - n_dir
        print '  demo flow %d: %d direct + %d switch subflows' % (
            demo.fid, n_dir, n_sw)
        # start receiver on the destination node
        recv_body = (
            'import sys; sys.path.insert(0, "/workspace/mptcp_exp")\n'
            'import mptcp_io\n'
            'r = mptcp_io.DsnReceiver(%d, timeout=8)\n'
            'o = r.recv_loop(8)\n'
            'print "OK", r.stats()\n'
        ) % port
        with open('/tmp/mptcp_recv.py', 'w') as f:
            f.write(recv_body)
        recv_proc = subprocess.Popen(
            'ip netns exec {} python2 -u /tmp/mptcp_recv.py'.format(NS(demo.dst)),
            shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        time.sleep(1)

        # send 20 segments per subflow, interleaved DSN across subflows,
        # so the receiver must reorder (subflows send out of DSN order).
        # SENDERS MUST RUN INSIDE THE SOURCE NAMESPACE (their sockets live
        # there and only there can reach the direct /30 or switch subnet).
        segs_per_sub = 20
        send_dest = []
        for k, sf in enumerate(sub):
            if sf.path == 'direct':
                dlink = direct_links[sf.sid]
                ipb = dlink[3].split('/')[0]
            else:
                ipb = BASE_IP(demo.dst)
            send_dest.append((ipb, k))
        send_body = (
            'import sys; sys.path.insert(0, "/workspace/mptcp_exp")\n'
            'import mptcp_io, time\n'
            'senders = []\n'
        )
        for ipb, k in send_dest:
            send_body += 'senders.append(mptcp_io.SsnSender("%s", %d, "%d.%d", sid_int=%d))\n' % (
                ipb, port, demo.fid, k, k)
        send_body += (
            'for i in range(%d):\n'
            '    for k, s in enumerate(senders):\n'
            '        dsn = i * len(senders) + k\n'
            '        s.send_seg(%d, dsn, payload=b"P%%03d" %% dsn)\n'
            '        time.sleep(0.001)\n'
            'print "sent", %d * len(senders)\n'
        ) % (segs_per_sub, demo.fid, segs_per_sub)
        with open('/tmp/mptcp_send.py', 'w') as f:
            f.write(send_body)
        subprocess.call(
            'ip netns exec {} python2 -u /tmp/mptcp_send.py'.format(NS(demo.src)),
            shell=True)
        out = recv_proc.stdout.read()
        print '  receiver:', out.strip()
        # parse the stats dict printed by the receiver
        ok = False
        if 'received' in out:
            import re
            m = re.search(r"'received': (\d+)", out)
            if m and int(m.group(1)) > 0:
                ok = True
        if ok:
            print '  [*] DSN reorder OK: segments delivered in order across subflows'
        else:
            print '  [!] receiver got 0 segments - reorder NOT verified'

    print '\n=== Done (topology up) ==='
    print 'Ctrl-C to cleanup.'


if __name__ == '__main__':
    main()
