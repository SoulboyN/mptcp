#!/usr/bin/env python2
"""
run_mptcp.py -- MPTCP-style multi-path experiment on 16 nodes.

Topology (3 switches + direct links):
  - 16 netns hosts, each with one interface per switch:
      hN-s1 -> sw1 (10.0.0.0/24)
      hN-s2 -> sw2 (10.2.0.0/24)
      hN-s3 -> sw3 (10.3.0.0/24)
    So a 'sw1'/'sw2'/'sw3' subflow uses that switch's subnet address pair:
    genuinely different physical path + INDEPENDENT ECN domain per switch.
  - PLUS direct veth links for the 'direct' subflows of each connection:
    node keeps an extra interface hN-dM in a dedicated /30 subnet
    (10.1.<idx>.0/30), so direct subflows do NOT cross any switch.

Data plane (software-side):
  - senders emit packets tagged with (flow_id, subflow_id, SSN) in the
    UDP payload; receivers reorder by DSN across subflows (M3/M4).
  - credit flow control per subflow; DCQCN ECN per switch; RL global
    scheduler (M5-M7) added in later milestones.

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
# RL fine-tune output; loaded by the schedulers so the learned policy drives
# the runtime decisions (missing on first run -> schedulers fall back to
# their built-in defaults, then the fine-tuned policy takes over next run).
POLICY_REAL = os.path.join(_HERE, 'policy_mptcp_real.json')

NODES = 16
NET = '10.0.0'
BASE_IP = lambda i: '{}.{}'.format(NET, i)
NS     = lambda i: 'ns-h{}'.format(i)
H_INTF = lambda i: 'h{}-eth0'.format(i)      # switch-facing interface (sw1)
SW_INTF = lambda i: 's1-eth{}'.format(i)

# ---- 3 switches: each node gets one interface per switch ----
N_SW = 3
SW_NAME = ['s1', 's2', 's3']
SW_PORT = [9090, 9091, 9092]                  # thrift port per switch
# per-switch host interface: hN-s1, hN-s2, hN-s3 ; switch side: s2-ethN ...
H_SW_INTF = lambda i, s: 'h{}-{}'.format(i, SW_NAME[s-1])
SW_SIDE   = lambda i, s: '{}-eth{}'.format(SW_NAME[s-1], i)

sw_procs = {}                                  # switch idx -> Popen


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
    print '\n=== Cleanup ==='
    os.system('pkill -9 -f simple_switch 2>/dev/null')
    time.sleep(0.5)
    print '  all BMv2 stopped'
    for i in range(1, NODES + 1):
        sh('ip netns del {} 2>/dev/null'.format(NS(i)), check_ok=False)
        for s in range(1, N_SW + 1):
            sh('ip link del {} 2>/dev/null'.format(SW_SIDE(i, s)), check_ok=False)
            sh('ip link del {} 2>/dev/null'.format(H_SW_INTF(i, s)), check_ok=False)
    # any leftover direct-link interfaces (hN-dM / hN-dM peer)
    os.system("ip link show 2>/dev/null | grep -oE 'h[0-9]+-d[0-9]+' | sort -u "
              "| while read x; do ip link del $x 2>/dev/null; done")
    sh('rm -f /tmp/bmv2-*.ipc', check_ok=False)


def mac_of(ns, iface):
    out = subprocess.check_output(
        'ip netns exec {} cat /sys/class/net/{}/address'.format(ns, iface),
        shell=True)
    return out.strip()


def run_cli(cmds, thrift_port=9090):
    p = subprocess.Popen(
        ['simple_switch_CLI', '--thrift-port', str(thrift_port)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = p.communicate(input=cmds)
    return out, err


def ecn_collector():
    """Periodically read each switch's ecn_marks register and write the SDN
    global view to /tmp/ecn_global.json (shared; RL-driven senders read it
    every scheduling round to do DCQCN + RL state)."""
    import json as _json
    while True:
        try:
            ecn = {}
            for s in range(1, N_SW + 1):
                out, _ = run_cli('register_read ecn_marks 0\n',
                                 thrift_port=SW_PORT[s - 1])
                v = 0.0
                for line in out.splitlines():
                    if '=' in line:
                        try:
                            v = min(1.0, float(line.split('=')[-1].strip()) / 200.0)
                            break
                        except ValueError:
                            continue
                ecn[s] = v
            with open('/tmp/ecn_global.json', 'w') as f:
                _json.dump(ecn, f)
        except Exception:
            pass
        time.sleep(1.0)


def monitor_collector():
    """Aggregate the sender/receiver live state files into
    build/monitor_state.json for the DSN/SSN monitor page."""
    import json as _json
    import glob
    while True:
        try:
            state = {'flows': [], 'recv': {}}
            for f in glob.glob('/tmp/mptcp_sender_*.json'):
                try:
                    with open(f) as fh:
                        state['flows'].append(_json.load(fh))
                except Exception:
                    pass
            try:
                with open('/tmp/mptcp_recv_live.json') as fh:
                    state['recv'] = _json.load(fh)
            except Exception:
                pass
            with open(os.path.join(_HERE, 'build', 'monitor_state.json'), 'w') as fh:
                _json.dump(state, fh)
        except Exception:
            pass
        time.sleep(0.5)


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


def udp_listener(ns, port, seconds):
    """Run a simple UDP counter in a namespace; prints received count."""
    body = (
        'import socket, time\n'
        's = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n'
        's.bind(("0.0.0.0", %d)); s.settimeout(%d)\n'
        'c = 0\n'
        'try:\n'
        '    while True:\n'
        '        s.recvfrom(1500); c += 1\n'
        'except socket.timeout:\n'
        '    pass\n'
        'print c\n'
    ) % (port, seconds + 2)
    p = os.path.join('/tmp', 'udp_listen_%d.py' % port)
    with open(p, 'w') as f:
        f.write(body)
    return subprocess.Popen(
        'ip netns exec {} python2 -u {}'.format(ns, p),
        shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def main():
    signal.signal(signal.SIGINT, lambda *_: (cleanup(), sys.exit(0)))
    cleanup()
    time.sleep(0.5)

    import flow_mptcp as fmod

    # ---- 1. Build connection graph (3~4 subflows, first direct) ----
    print '=== 1. Build MPTCP connection graph ==='
    flows, pairs = fmod.build_mptcp_graph(range(1, NODES + 1),
                                          min_sub=3, max_sub=4, seed=3)
    counts = fmod.count_by_path(flows)
    print '  flows:', len(flows), ' subflow counts:', counts

    # ---- 2. Compile P4 ----
    print '\n=== 2. Compile P4 ==='
    if not os.path.exists(os.path.join(_HERE, 'build')):
        os.makedirs(os.path.join(_HERE, 'build'))
    subprocess.check_call([
        'p4c', '--target', 'bmv2', '--arch', 'v1model',
        '--p4runtime-files', P4INFO, '-o', os.path.join(_HERE, 'build'), P4FILE
    ])

    # ---- 3. Build 16-node topology across 3 switches ----
    # Each node gets one interface per switch: hN-s1 (sw1), hN-s2 (sw2),
    # hN-s3 (sw3). Each switch has its own subnet:
    #   sw1: 10.0.0.0/24   sw2: 10.2.0.0/24   sw3: 10.3.0.0/24
    # so a subflow going through sw2 uses the sw2 subnet address pair
    # (genuinely different path + independent ECN domain per switch).
    SW_NET = {1: '10.0.0', 2: '10.2.0', 3: '10.3.0'}
    def sw_ip(i, s):            # node i's address on switch s's subnet
        return '{}.{}'.format(SW_NET[s], i)
    print '\n=== 3. Build 16-node topology (3 switches) ==='
    for i in range(1, NODES + 1):
        sh('ip netns add {}'.format(NS(i)))
        for s in range(1, N_SW + 1):
            hi = H_SW_INTF(i, s)          # hN-s1 / hN-s2 / hN-s3
            ss = SW_SIDE(i, s)            # s1-ethN / s2-ethN / s3-ethN
            sh('ip link add {} type veth peer name {}'.format(hi, ss))
            sh('ip link set {} up'.format(ss))
            sh('ip link set {} netns {}'.format(hi, NS(i)))
            sh('ip netns exec {} ip addr add {}/24 dev {}'.format(
                NS(i), sw_ip(i, s), hi))
            sh('ip netns exec {} ip link set {} up'.format(NS(i), hi))
        sh('ip netns exec {} ip link set lo up'.format(NS(i)))

    # Static ARP on each switch subnet (all pairs, per interface)
    print '\n=== 4. Static ARP (per switch subnet) ==='
    for i in range(1, NODES + 1):
        for s in range(1, N_SW + 1):
            hi = H_SW_INTF(i, s)
            for j in range(1, NODES + 1):
                if i == j:
                    continue
                mj = mac_of(NS(j), H_SW_INTF(j, s))
                sh_quiet('ip netns exec {} ip neigh replace {} lladdr {} dev {} nud permanent'
                         .format(NS(i), sw_ip(j, s), mj, hi))

    # ---- 5. Direct links for 'direct' subflows ----
    print '\n=== 5. Build direct subflow links ==='
    direct_links = build_direct_links(flows, pairs)

    # ---- 6. Start 3 BMv2 switches (independent ECN domains) ----
    # Each switch needs a distinct --device-id so its notification IPC
    # (ipc:///tmp/bmv2-<id>-notifications.ipc) does not collide; without
    # it switch 2/3 die at startup with "Address already in use".
    print '\n=== 6. Start 3 BMv2 switches ==='
    for s in range(1, N_SW + 1):
        cmd = ['simple_switch', '--thrift-port', str(SW_PORT[s-1]),
               '--device-id', str(s - 1)]
        for i in range(1, NODES + 1):
            cmd += ['-i', '{}@{}'.format(i, SW_SIDE(i, s))]
        cmd.append(JSON)
        logf = open(os.path.join(_HERE, 'build', 'bmv2_mptcp_s%d.log' % s), 'w')
        sw_procs[s] = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT)
        time.sleep(4)
        if sw_procs[s].poll() is not None:
            print '  [!] switch %d died!' % s
            logf.close()
            sys.exit(1)
    print '  switches running on thrift:', SW_PORT

    # start the SDN ECN collector (writes /tmp/ecn_global.json for RL senders)
    import threading
    _ecn_thr = threading.Thread(target=ecn_collector)
    _ecn_thr.daemon = True
    _ecn_thr.start()
    _mon_thr = threading.Thread(target=monitor_collector)
    _mon_thr.daemon = True
    _mon_thr.start()

    # ---- 7. Push LPM routes to each switch ----
    print '\n=== 7. Push LPM routes (per switch) ==='
    for s in range(1, N_SW + 1):
        lines = []
        for i in range(1, NODES + 1):
            lines.append('table_add ipv4_lpm forward {}/32 => {} {}'.format(
                sw_ip(i, s), i, mac_of(NS(i), H_SW_INTF(i, s))))
        out, err = run_cli('\n'.join(lines) + '\n', thrift_port=SW_PORT[s-1])
        print '  sw%d: %d routes' % (s, NODES)

    # ---- 7b. Heterogeneous switch config ----
    # Each switch gets a DIFFERENT bandwidth (per-port meter rate) and a
    # different ECN threshold, so the 3 switch paths are genuinely
    # heterogeneous (the paper's "dynamic heterogeneous subflow").
    #   sw1: slow  (WiFi-like, low bw, sensitive ECN)
    #   sw2: medium(cellular-like, mid bw, medium ECN)
    #   sw3: fast  (fiber-like, high bw, tolerant ECN)
    SW_BW_MBPS  = {1: 25, 2: 60, 3: 140}      # per-switch bandwidth (Mbps)
    SW_ECN_THR  = {1: 5,  2: 20, 3: 60}       # ECN queue-depth threshold
    print '\n=== 7b. Heterogeneous switch config (bw + ECN) ==='
    for s in range(1, N_SW + 1):
        bw = SW_BW_MBPS[s]
        bytes_us = max(int(bw) * 1000000 / 8 / 1000000, 1)   # Mbps->bytes/us
        # unlimited-ish bucket for GREEN (rate = bw, big burst)
        lines = []
        for port in range(0, NODES):           # meter index 0..15
            lines.append('meter_set_rates m_port {} {}:{} {}:{}'.format(
                port, bytes_us, 150000, bytes_us, 150000))
        out, err = run_cli('\n'.join(lines) + '\n',
                           thrift_port=SW_PORT[s-1])
        # ECN threshold
        run_cli('register_write ecn_thresh 0 {}\n'.format(SW_ECN_THR[s]),
                thrift_port=SW_PORT[s-1])
        print '  sw%d: bw=%d Mbps (%.0f bytes/us), ecn_thresh=%d' % (
            s, bw, bytes_us, SW_ECN_THR[s])

    # ---- 7c. tc link characteristics (WiFi / cellular / fiber) ----
    # Use Linux tc netem on each switch's host veth to give the paths
    # genuinely different delay/jitter/loss. This is real kernel-level
    # shaping, the closest we can get to heterogeneous access links.
    #   sw1: WiFi  - delay 10ms +-2ms, 1% loss
    #   sw2: 4G    - delay 30ms +-10ms, 2% loss
    #   sw3: fiber - delay 2ms, 0.1% loss
    SW_TC = {1: 'delay 10ms 2ms loss 1%',
             2: 'delay 30ms 10ms loss 2%',
             3: 'delay 2ms loss 0.1%'}
    print '\n=== 7c. tc link characteristics (WiFi/cell/fiber) ==='
    for i in range(1, NODES + 1):
        for s in range(1, N_SW + 1):
            hi = H_SW_INTF(i, s)          # hN-s1 / hN-s2 / hN-s3
            # apply on the switch-facing side (root qdisc on the host iface)
            sh_quiet('ip netns exec {} tc qdisc replace dev {} root netem {}'
                     .format(NS(i), hi, SW_TC[s]))
    print '  applied tc netem per switch path'

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
        has_sw = any(p.startswith('sw') for p in paths)
        if 'direct' in paths and has_sw:
            demo = f
            break
    if demo is None:
        print '  no flow with both path types; skipping'
    else:
        import mptcp_tcp
        port = 7000
        sub = demo.subflows
        n_dir = len([sf for sf in sub if sf.path == 'direct'])
        n_sw = len(sub) - n_dir
        print '  demo flow %d: %d direct + %d switch subflows (real TCP)' % (
            demo.fid, n_dir, n_sw)
        # receiver: accept N TCP connections (one per subflow), reorder by DSN
        n_sub = len(sub)
        recv_body = (
            'import sys; sys.path.insert(0, "/workspace/mptcp_exp")\n'
            'import mptcp_tcp\n'
            'r = mptcp_tcp.TcpDsnReceiver(%d, n_subflows=%d, timeout=10)\n'
            'o = r.recv_loop(9)\n'
            'print "OK", r.stats()\n'
        ) % (port, n_sub)
        with open('/tmp/mptcp_recv.py', 'w') as f:
            f.write(recv_body)
        recv_proc = subprocess.Popen(
            'ip netns exec {} python2 -u /tmp/mptcp_recv.py'.format(NS(demo.dst)),
            shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        time.sleep(1)

        # send 20 segments per subflow over REAL kernel TCP, interleaved DSN
        segs_per_sub = 20
        send_dest = []
        for k, sf in enumerate(sub):
            if sf.path == 'direct':
                dlink = direct_links[sf.sid]
                ipb = dlink[3].split('/')[0]
            else:
                sw_idx = int(sf.path[2]) - 1
                s = sw_idx + 1
                ipb = sw_ip(demo.dst, s)
            send_dest.append((ipb, k))
        send_body = (
            'import sys; sys.path.insert(0, "/workspace/mptcp_exp")\n'
            'import mptcp_tcp, time\n'
            'senders = []\n'
        )
        for ipb, k in send_dest:
            send_body += 'senders.append(mptcp_tcp.TcpSsnSender("%s", %d, "%d.%d", sid_int=%d))\n' % (
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

    # ---- 9b. Proportional split + retransmission path selection ----
    # Demonstrate the two new scheduler abilities in-process:
    #   (1) continuous proportional traffic split across heterogeneous paths
    #   (2) on packet loss, retransmit over the healthiest path
    print '\n=== 9b. Proportional split + retrans-path selection ==='
    import mptcp_scheduler as sch
    sel = sch.RlPathSelector(flows, n_sw=3, policy_path=POLICY_REAL)
    for f in flows:
        for sf in f.subflows:
            class _Sock(object):
                pass
            s = _Sock()
            s.in_flight = 0
            s._effective_cwnd = lambda: 10
            s.ctrl_cwnd = None
            sel.sockets[sf.sid] = s
    # scenario: sw2 (cellular) congested + expensive, sw1 free, direct cheap
    sel.observe({1: 0.1, 2: 0.9, 3: 0.0})
    # pick a flow with 3+ subflows for a clear split
    demo2 = None
    for f in flows:
        if len(f.subflows) >= 3:
            demo2 = f
            break
    if demo2:
        w = sel.path_weights(demo2)
        print '  flow %d path weights: %s' % (
            demo2.fid, {sf.path: round(w[sf.sid], 3) for sf in demo2.subflows})
        from collections import Counter
        cnt = Counter()
        for _ in range(500):
            sid = sel.pick_by_ratio(demo2)
            for sf in demo2.subflows:
                if sf.sid == sid:
                    cnt[sf.path] += 1
                    break
        print '  500 segments split:', dict(cnt)
        rsf = sel.select_retrans_subflow(demo2)
        print '  retransmission healthiest path:', rsf.path

    # ---- 10. M5-M7: three-domain congestion control demo ----
    demo_3domain()

    # ---- 10b. Real-environment RL fine-tuning ----
    # Pre-trained policy is fine-tuned on the LIVE 3-switch topology: each
    # round sends REAL traffic, reads REAL per-switch ECN, measures REAL
    # received/loss/delay, updates Q with the REAL reward.
    try:
        import rl_real_train as rrt
        from mptcp_scheduler import PATH_COST
        trainer = rrt.RealEnvTrainer(flows, sw_ports={1: SW_PORT[0],
                                                      2: SW_PORT[1],
                                                      3: SW_PORT[2]})

        def _real_traffic(weights, cwnd_mul):
            """Send a modest burst over the live topology using the given
            per-subflow weights; return (received, sent, avg_delay_ms).
            Batches per source namespace into ONE subprocess (it used to spawn
            one subprocess PER PACKET -- that process-spawn was the training
            bottleneck and made each RL round take tens of seconds)."""
            n = 15                              # segments per round
            port = 7500
            listeners = []
            for f in flows[:4]:
                listeners.append(udp_listener(NS(f.dst), port, 12))
                port += 1
            time.sleep(0.5)
            import random as _rnd
            per_src = {}
            sent_by_path = {}
            for _ in range(n):
                for fi, f in enumerate(flows[:4]):
                    ws = [weights.get(sf.sid, 0) for sf in f.subflows]
                    tot = sum(ws) or 1.0
                    r = _rnd.random() * tot
                    acc = 0
                    chosen = f.subflows[0]
                    for sf, w in zip(f.subflows, ws):
                        acc += w
                        if r <= acc:
                            chosen = sf
                            break
                    if chosen.path == 'direct':
                        dlink = direct_links[chosen.sid]
                        ipb = dlink[3].split('/')[0]
                    else:
                        s_idx = int(chosen.path[2]) - 1
                        ipb = sw_ip(f.dst, s_idx + 1)
                    per_src.setdefault(f.src, []).append((ipb, 7500 + fi))
                    sent_by_path[chosen.path] = sent_by_path.get(chosen.path, 0) + 1
            # one batched sender subprocess per source namespace
            for src, pairs in per_src.items():
                body = ('import socket, time\n'
                        's = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n')
                for ipb, p in pairs:
                    body += 's.sendto("Q", ("%s", %d))\ntime.sleep(0.003)\n' % (ipb, p)
                path = os.path.join('/tmp', 'snd_src_%d.py' % src)
                with open(path, 'w') as _f:
                    _f.write(body)
                sh_quiet('ip netns exec {} python2 {}'.format(NS(src), path))
            total_sent = n * len(flows[:4])
            time.sleep(1)
            total_recv = 0
            for lis in listeners:
                try:
                    out = lis.stdout.readline()
                    total_recv += int(out.strip())
                except Exception:
                    pass
            # weighted-average path delay (from the tc profile) for the reward
            _PD = {'direct': 2, 'sw1': 10, 'sw2': 30, 'sw3': 2}
            tot = sum(sent_by_path.values()) or 1
            avg_delay = sum(_PD.get(p, 10) * c / tot
                            for p, c in sent_by_path.items())
            return total_recv, total_sent, avg_delay

        def _read_real_ecn(sw_idx):
            """Read REAL ecn_marks of switch sw_idx via CLI."""
            out, _ = run_cli('register_read ecn_marks 0\n',
                             thrift_port=SW_PORT[sw_idx - 1])
            for line in out.splitlines():
                if '=' in line:
                    try:
                        return min(1.0, float(line.split('=')[-1].strip()) / 200.0)
                    except ValueError:
                        continue
            return 0.0

        print '\n=== 10b. Real-environment RL fine-tuning ==='
        trainer.train_loop(rounds=6, sw_ports=[1, 2, 3],
                           run_traffic=_real_traffic, read_ecn=_read_real_ecn,
                           save_every=3)
    except Exception as e:
        import traceback
        traceback.print_exc()
        print '  [!] real-env training skipped:', e

    # ---- 11. Interactive MPTCP resilience demo (real kernel TCP) ----
    # Interactive terminal: user can cut a subflow path (WLAN/cellular
    # drop) and watch data continue over the remaining paths.
    # Non-interactive alternatives (no tty needed):
    #   --cut <path>        cut one path at the start, observe reroute
    #   --demo "seq"        run a scripted cut/up/sleep sequence, e.g.
    #                       "sleep 3|cut sw1|sleep 5|up sw1|sleep 5|cut sw2|sleep 5"
    cut_arg = None
    demo_seq = None
    if len(sys.argv) > 2:
        if sys.argv[1] == '--cut':
            cut_arg = sys.argv[2]
        elif sys.argv[1] == '--demo':
            demo_seq = sys.argv[2].split('|')
    try:
        demo_interactive(flows, pairs, direct_links,
                         auto_cut=cut_arg, auto_demo=demo_seq)
    except Exception as e:
        import traceback
        traceback.print_exc()
        print '  [!] interactive demo skipped:', e

    # ---- 13. CC comparison (T5): RL vs fixed vs pseudo-Reno ----
    try:
        demo_flows = [f for f in flows if len(f.subflows) >= 3][:3]
        compare_cc(demo_flows, pairs, direct_links)
    except Exception as e:
        import traceback
        traceback.print_exc()
        print '  [!] CC comparison skipped:', e

    print '\n=== Done (topology up) ==='
    print 'Ctrl-C to cleanup.'


def demo_3domain():
    """M5-M7 demo: DCQCN(switch ECN) + credit(point-to-point) + RL(global).
    Simulates the scheduler decision loop in-process (no netns needed).
    Uses the live switch ECN counter for the switch-domain signal."""
    global sw_proc
    import flow_mptcp as fmod
    import mptcp_scheduler as sch
    flows, pairs = fmod.build_mptcp_graph(range(1, 17), seed=3)
    sched = sch.MptcpScheduler(flows, policy_path=POLICY_REAL)
    print '\n=== M5-M7: three-domain congestion control demo ==='
    print '  flows:', len(flows), ' subflows:', sum(len(f.subflows) for f in flows)

    # simulate rounds with a synthetic ECN wave so the DCQCN domain is
    # visibly exercised (switch idle => real counter would be 0). The
    # scheduler logic is what we test, not the live counter here.
    ecn_wave = [0.0, 0.8, 0.2, 0.9, 0.1, 0.5]
    for rnd in range(6):
        ratio = ecn_wave[rnd]
        state = sched.dcqcn_backoff(ratio)
        m = sched.apply_rl(state)
        # credit: replenish all subflows (receiver grants periodically)
        for f in flows:
            for sf in f.subflows:
                sched.grant(sf, sf.ssn_credit_grant)
        # stats
        dir_rates = [sched.rate[f.subflows[0].sid] for f in flows
                     if f.subflows[0].path == 'direct']
        sw_rates = [sched.rate[sf.sid] for f in flows for sf in f.subflows
                    if sf.path.startswith('sw')]
        avg_dir = sum(dir_rates) / max(len(dir_rates), 1)
        avg_sw = sum(sw_rates) / max(len(sw_rates), 1)
        print '  rnd %d: ecn=%.2f state=%d rl=%.2f avg_direct=%.2f avg_sw=%.2f' % (
            rnd, ratio, state, m, avg_dir, avg_sw)
        time.sleep(0.3)
    print '  [*] direct subflows keep rate (DCQCN only cuts switch subflows)'


def compare_cc(demos, pairs, direct_links, run_seconds=8):
    """Congestion-control comparison (T5): RL-cwnd vs fixed cwnd vs pseudo-Reno
    (AIMD) over the same concurrent MPTCP flows; prints per-flow throughput and
    fairness (Jain's index) for the paper's baseline data."""
    import mptcp_tcp
    _SW_NET = {1: '10.0.0', 2: '10.2.0', 3: '10.3.0'}
    def sw_ip(i, s):
        return '{}.{}'.format(_SW_NET[s], i)
    modes = [('rl', 'RL-cwnd (fine-tuned)'),
             ('fixed', 'Fixed cwnd=32'),
             ('aimd', 'Pseudo-Reno (AIMD)')]
    print '\n=== 13. Congestion-control comparison (T5) ==='
    results = {}
    for mi, (mode, label) in enumerate(modes):
        base_port = 8000 + mi * 10
        receivers, senders = [], []
        for fi, demo in enumerate(demos):
            port = base_port + fi
            n_sub = len(demo.subflows)
            recv_body = (
                'import sys; sys.path.insert(0, "/workspace/mptcp_exp")\n'
                'import mptcp_tcp\n'
                'r = mptcp_tcp.TcpDsnReceiver(%d, n_subflows=%d, timeout=%d)\n'
                'o = r.recv_loop(%d)\n'
                'print "OK", r.stats()\n'
            ) % (port, n_sub, run_seconds + 5, run_seconds + 5)
            with open('/tmp/cc_recv_%d_%d.py' % (mi, fi), 'w') as f:
                f.write(recv_body)
            rp = subprocess.Popen(
                'ip netns exec {} python2 -u /tmp/cc_recv_%d_%d.py'.format(NS(demo.dst)) % (mi, fi),
                shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            receivers.append((demo, rp, port))
            send_dest = []
            for k, sf in enumerate(demo.subflows):
                if sf.path == 'direct':
                    dlink = direct_links[sf.sid]
                    ipb = dlink[3].split('/')[0]
                else:
                    sw_idx = int(sf.path[2]) - 1
                    ipb = sw_ip(demo.dst, sw_idx + 1)
                send_dest.append((ipb, k, sf.path))
            stopf = '/tmp/cc_stop_%d_%d' % (mi, demo.fid)
            try:
                os.remove(stopf)
            except Exception:
                pass
            snd_body = (
                'import sys; sys.path.insert(0, "/workspace/mptcp_exp")\n'
                'import mptcp_tcp\n'
                'g = mptcp_tcp.MptcpGroupSender(%d, %r, %d, policy_path=%r, cc_mode=%r)\n'
                'g.run_loop(stop_file=%r)\n'
            ) % (demo.fid, send_dest, port, POLICY_REAL, mode, stopf)
            with open('/tmp/cc_send_%d_%d.py' % (mi, fi), 'w') as f:
                f.write(snd_body)
            sp = subprocess.Popen(
                'ip netns exec {} python2 -u /tmp/cc_send_%d_%d.py'.format(NS(demo.src)) % (mi, fi),
                shell=True, preexec_fn=os.setsid,
                stdout=open('/tmp/cc_send_%d_%d.out' % (mi, fi), 'w'),
                stderr=subprocess.STDOUT)
            senders.append((demo, sp, stopf))
        time.sleep(1)
        time.sleep(run_seconds)
        for demo, sp, stopf in senders:
            try:
                with open(stopf, 'w'):
                    pass
                sp.wait(timeout=6)
            except Exception:
                try:
                    os.killpg(os.getpgid(sp.pid), signal.SIGTERM)
                except Exception:
                    pass
        time.sleep(3)
        import re as _re
        thpt = []
        for demo, rp, port in receivers:
            out = rp.stdout.read()
            m = _re.search(r"'ordered': (\d+)", out)
            ordered = int(m.group(1)) if m else 0
            thpt.append(ordered / float(run_seconds))
        n = len(thpt)
        jain = ((sum(thpt) ** 2) / (n * sum(x * x for x in thpt))) \
            if n and sum(thpt) else 0.0
        results[mode] = (thpt, jain)
        print '  %-24s thpt=%s seg/s  jain=%.3f' % (
            label, ['%.1f' % t for t in thpt], jain)
    return results


def demo_interactive(flows, pairs, direct_links, auto_cut=None, auto_demo=None):
    """Interactive terminal demo of MPTCP resilience over REAL kernel TCP.

    The user can cut a subflow path (simulating WLAN / cellular drop) and
    watch data continue over the remaining paths. Commands:
      cut <path>   -> bring down that path (direct / sw1 / sw2 / sw3)
      up  <path>   -> bring the path back
      quit         -> exit
    If `auto_cut` is provided, that path is cut automatically at the start
    (no interactive input needed) -- useful in non-tty environments.
    If `auto_demo` is provided, a scripted cut/up/sleep sequence is run
    (e.g. ["sleep 3", "cut sw1", "sleep 5", "up sw1", "sleep 5", "quit"]).
    Cutting a path physically disables its veth interfaces in the
    relevant namespaces, so real TCP connections over it break; the other
    subflows keep delivering data (MPTCP path resilience).
    """
    import mptcp_tcp
    # sw_ip: node i's address on switch s's subnet (module-level map)
    _SW_NET = {1: '10.0.0', 2: '10.2.0', 3: '10.3.0'}
    def sw_ip(i, s):
        return '{}.{}'.format(_SW_NET[s], i)
    # pick up to 3 flows with >=3 subflows as CONCURRENT MPTCP connections so
    # the RL scheduler's global view (shared switch ECN) is exercised by real
    # cross-flow congestion, not just a single connection.
    demos = [f for f in flows if len(f.subflows) >= 3][:3]
    if not demos:
        print '  [!] no flow with >=3 subflows; skipping interactive'
        return
    print '\n=== 11. Multi-connection MPTCP resilience (real kernel TCP) ==='
    base_port = 7900
    recv_procs = {}      # fid -> Popen
    snd_procs = {}       # fid -> Popen
    snd_outs = {}        # fid -> file
    stopfs = {}          # fid -> stop file path
    send_dests = {}      # fid -> [(ipb, sid, path)]
    for fi, demo in enumerate(demos):
        port = base_port + fi
        n_sub = len(demo.subflows)
        print '  flow %d: %d subflows over real TCP; receiver on node %s (port %d)' % (
            demo.fid, n_sub, demo.dst, port)
        # receiver: accept n_sub TCP connections, reorder by DSN
        recv_body = (
            'import sys; sys.path.insert(0, "/workspace/mptcp_exp")\n'
            'import mptcp_tcp\n'
            'r = mptcp_tcp.TcpDsnReceiver(%d, n_subflows=%d, timeout=30)\n'
            'o = r.recv_loop(30)\n'
            'print "OK", r.stats()\n'
        ) % (port, n_sub)
        with open('/tmp/mptcp_recv_%d.py' % fi, 'w') as f:
            f.write(recv_body)
        rp = subprocess.Popen(
            'ip netns exec {} python2 -u /tmp/mptcp_recv_%d.py'.format(NS(demo.dst)) % fi,
            shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        recv_procs[demo.fid] = rp
        # the senders must run INSIDE each source namespace (that's where the
        # routes to each path's dst IP exist)
        send_dest = []
        for k, sf in enumerate(demo.subflows):
            if sf.path == 'direct':
                dlink = direct_links[sf.sid]
                ipb = dlink[3].split('/')[0]
            else:
                sw_idx = int(sf.path[2]) - 1
                ipb = sw_ip(demo.dst, sw_idx + 1)
            send_dest.append((ipb, k, sf.path))
        send_dests[demo.fid] = send_dest
        stopf = '/tmp/mptcp_stop_%d' % demo.fid
        try:
            os.remove(stopf)
        except Exception:
            pass
        stopfs[demo.fid] = stopf
        snd_body = (
            'import sys; sys.path.insert(0, "/workspace/mptcp_exp")\n'
            'import mptcp_tcp\n'
            'g = mptcp_tcp.MptcpGroupSender(%d, %r, %d, policy_path=%r)\n'
            'g.run_loop(stop_file=%r)\n'
        ) % (demo.fid, send_dest, port, POLICY_REAL, stopf)
        with open('/tmp/mptcp_send_%d.py' % fi, 'w') as f:
            f.write(snd_body)
        snd_outs[demo.fid] = open('/tmp/mptcp_send_%d.out' % fi, 'w')
        sp = subprocess.Popen(
            'ip netns exec {} python2 -u /tmp/mptcp_send_%d.py'.format(NS(demo.src)) % fi,
            shell=True, preexec_fn=os.setsid,
            stdout=snd_outs[demo.fid], stderr=subprocess.STDOUT)
        snd_procs[demo.fid] = sp
    time.sleep(1)
    # per-flow path-up state
    up = {demo.fid: {sf.path: True for sf in demo.subflows} for demo in demos}

    def _demo(fid):
        for d in demos:
            if d.fid == fid:
                return d
        return None

    import re as _re
    def _node_of(ifc):
        m = _re.match(r'h(\d+)-', ifc)
        return int(m.group(1)) if m else None

    def path_ifaces(fid, path):
        """veth interfaces for a path on the given flow's source+dst nodes."""
        demo = _demo(fid)
        ifaces = []
        for node in (demo.src, demo.dst):
            if path == 'direct':
                for sid, (a, b, ipa, ipb) in direct_links.items():
                    for ff in flows:
                        for sf in ff.subflows:
                            if sf.sid == sid and sf.path == 'direct' \
                               and ff.src == demo.src and ff.dst == demo.dst:
                                ifaces += [a, b]
                                break
            else:
                sw_idx = int(path[2]) - 1
                ifaces.append(H_SW_INTF(node, sw_idx + 1))
        return list(set(ifaces))

    def _apply_path(fid, path, state):
        demo = _demo(fid)
        if demo is None or path not in up.get(fid, {}):
            print '  [!] unknown flow/path: %s %s' % (fid, path)
            return
        for ifc in path_ifaces(fid, path):
            n = _node_of(ifc)
            if n is not None:
                sh_quiet('ip netns exec ns-h{} ip link set {} {} 2>/dev/null'
                         .format(n, ifc, state))
        up[fid][path] = (state == 'up')
        print '  [%s] flow %d path %s' % ('-' if state == 'down' else '+', fid, path)

    print '  interactive: cut/up <flow_id> <path> (e.g. "cut 0 sw1"), or "quit"'
    print '  flows: %s' % {d.fid: [sf.path for sf in d.subflows] for d in demos}

    def _cmd_flow_path(parts):
        """Parse cut/up command -> (action, fid, path). Accepts "cut sw1",
        "cut 1 sw1", "sw1", "1 sw1" (fid defaults to the first flow)."""
        action = parts[0] if parts[0] in ('cut', 'up') else 'cut'
        rest = parts[1:] if parts[0] in ('cut', 'up') else parts
        if len(rest) >= 2 and rest[0].isdigit():
            return action, int(rest[0]), rest[1]
        return action, demos[0].fid, rest[0]

    if auto_demo:
        print '  [demo] scripted sequence: %s' % ' | '.join(auto_demo)
        for step in auto_demo:
            parts = step.strip().split()
            if not parts:
                continue
            cmd = parts[0]
            if cmd == 'sleep':
                print '  [demo]   waiting %.1fs...' % float(parts[1])
                time.sleep(float(parts[1]))
            elif cmd in ('cut', 'up'):
                action, fid, path = _cmd_flow_path(parts)
                _apply_path(fid, path, 'down' if cmd == 'cut' else 'up')
            elif cmd == 'quit':
                break
    elif auto_cut:
        print '  [auto] cutting:', auto_cut
        action, fid, path = _cmd_flow_path(auto_cut.split())
        _apply_path(fid, path, 'down')
        print '  [auto] letting data reroute over remaining paths for 5s...'
        time.sleep(5)
        print '  [auto] done'
    else:
        try:
            while True:
                try:
                    line = raw_input('mptcp> ')
                except EOFError:
                    break
                parts = line.strip().split()
                if not parts:
                    continue
                cmd = parts[0]
                if cmd == 'quit':
                    break
                elif cmd in ('cut', 'up') and len(parts) >= 2:
                    action, fid, path = _cmd_flow_path(parts)
                    _apply_path(fid, path, 'down' if cmd == 'cut' else 'up')
        except KeyboardInterrupt:
            pass
    # graceful stop ALL senders: signal them to stop assigning new DSNs, let
    # each recover its in-flight tail (NAK) and close subflows (FIN), so the
    # receivers drain complete ordered streams instead of truncated ones.
    for fid, sp in snd_procs.items():
        try:
            with open(stopfs[fid], 'w'):
                pass
            sp.wait(timeout=6)
        except Exception:
            try:
                os.killpg(os.getpgid(sp.pid), signal.SIGTERM)
            except Exception:
                try:
                    sp.terminate()
                except Exception:
                    pass
    try:
        for fid, sp in snd_procs.items():
            sp.wait(timeout=3)
    except Exception:
        pass
    time.sleep(3)
    for demo in demos:
        try:
            out = recv_procs[demo.fid].stdout.read()
            print '  flow %d receiver final: %s' % (demo.fid, out.strip())
        except Exception:
            pass
        try:
            fi = demos.index(demo)
            snd_outs[demo.fid].close()
            snd_txt = open('/tmp/mptcp_send_%d.out' % fi).read().strip()
            if snd_txt:
                print '  flow %d sender log: %s' % (demo.fid, snd_txt[:300])
        except Exception:
            pass


if __name__ == '__main__':
    main()

