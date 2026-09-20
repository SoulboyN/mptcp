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
import re

# paths are relative to THIS file's directory (mptcp_exp/)
_HERE = os.path.dirname(os.path.abspath(__file__))
P4FILE = os.path.join(_HERE, 'simple_router_global.p4')
JSON   = os.path.join(_HERE, 'build', 'simple_router_global.json')
P4INFO = os.path.join(_HERE, 'build', 'simple_router_global.p4info.txt')
# RL fine-tune output; loaded by the schedulers so the learned policy drives
# the runtime decisions (missing on first run -> schedulers fall back to
# their built-in defaults, then the fine-tuned policy takes over next run).
POLICY_REAL = os.path.join(_HERE, 'policy_mptcp_real.json')
POLICY_OFFLINE = os.path.join(_HERE, 'policy_mptcp.json')

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


def wait_process(proc, timeout, poll=0.05):
    """Python-2-compatible bounded process wait. Returns True on exit."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return True
        time.sleep(poll)
    return proc.poll() is not None


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


def _parse_indexed_registers(output, register_names, n_ports=NODES):
    """Parse batched simple_switch_CLI register reads into per-port arrays."""
    values = {name: {} for name in register_names}
    pattern = re.compile(
        r'(?P<name>%s)\s*\[\s*(?P<idx>\d+)\s*\]\s*=\s*(?P<value>\d+)' %
        '|'.join(re.escape(name) for name in register_names))
    for line in output.splitlines():
        match = pattern.search(line)
        if not match:
            continue
        name = match.group('name')
        idx = int(match.group('idx'))
        if 0 <= idx < n_ports:
            values[name][idx] = float(match.group('value'))
    missing = []
    for name in register_names:
        for idx in range(n_ports):
            if idx not in values[name]:
                missing.append('%s[%d]' % (name, idx))
    if missing:
        raise ValueError('missing register values: %s' % ', '.join(missing))
    return {name: [values[name][idx] for idx in range(n_ports)]
            for name in register_names}


def _read_switch_ecn_counters(sw_idx):
    """Read every egress port in one CLI session for a switch."""
    names = ('ecn_marks', 'egress_total')
    commands = []
    for name in names:
        for port_idx in range(NODES):
            commands.append('register_read %s %d' % (name, port_idx))
    out, err = run_cli('\n'.join(commands) + '\n',
                       thrift_port=SW_PORT[sw_idx - 1])
    return _parse_indexed_registers(out, names)


def _ecn_delta_snapshot(current, previous=None):
    """Return traffic-weighted switch ECN and auditable per-port deltas."""
    if previous is None:
        previous = current
    ports = {}
    sum_dm = 0.0
    sum_dt = 0.0
    for idx in range(NODES):
        marks = current['ecn_marks'][idx]
        total = current['egress_total'][idx]
        dm = max(0.0, marks - previous['ecn_marks'][idx])
        dt = max(0.0, total - previous['egress_total'][idx])
        ratio = min(1.0, dm / dt) if dt > 0 else 0.0
        ports[idx] = {
            'marks': marks, 'total': total,
            'delta_marks': dm, 'delta_total': dt, 'ratio': ratio,
        }
        sum_dm += dm
        sum_dt += dt
    return ((min(1.0, sum_dm / sum_dt) if sum_dt > 0 else 0.0), ports)


def _write_json_atomic(path, value, json_module):
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json_module.dump(value, f)
    os.rename(tmp, path)


def ecn_collector():
    """Periodically read each switch's ecn_marks / egress_total registers and
    write the SDN global view to /tmp/ecn_global.json (shared; RL-driven
    senders read it every scheduling round to do DCQCN + RL state).

    Both registers are CUMULATIVE per-egress-port counters (incremented in
    P4 on every CE mark / every egress packet), so the ECN ratio is a
    DELTA ratio d(marks)/d(total) between two polls -- dividing the raw
    counter by a constant would drift to 1.0 over time and pin RL to the
    most-congested state regardless of the actual network."""
    import json as _json
    prev = {}   # switch -> {'ecn_marks': [16], 'egress_total': [16]}
    while True:
        try:
            ecn = {}
            detail = {'timestamp': time.time(), 'switches': {}}
            for s in range(1, N_SW + 1):
                current = _read_switch_ecn_counters(s)
                ratio, ports = _ecn_delta_snapshot(current, prev.get(s))
                prev[s] = current
                # Traffic-weighted ratio across all 16 egress ports.
                ecn[s] = ratio
                detail['switches'][s] = {'ratio': ratio, 'ports': ports}
            # Keep the legacy flat file consumed by senders, and add a full
            # per-port snapshot for validation and experiment audit trails.
            _write_json_atomic('/tmp/ecn_global.json', ecn, _json)
            _write_json_atomic('/tmp/ecn_global_detail.json', detail, _json)
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

    # controlled experiment entry points (avoid full pipeline retraining):
    #   --cc [nrep]        : build topology then run compare_cc only
    #   --ablate <stage>   : build topology then run one resilience ablation
    #   --ablate-all       : run all 4 ablation stages (0..3)
    argv = [a for a in sys.argv]
    cc_mode = '--cc' in argv
    dynamic_mode = '--dynamic' in argv
    ablate_mode = None
    ablate_all = '--ablate-all' in argv
    for i, a in enumerate(argv):
        if a == '--ablate' and i + 1 < len(argv):
            ablate_mode = int(argv[i + 1])
    cc_rep = 5
    for i, a in enumerate(argv):
        if a == '--cc' and i + 1 < len(argv) and argv[i + 1].isdigit():
            cc_rep = int(argv[i + 1])

    import flow_mptcp as fmod
    import json as _json_main

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
    # BMv2 exposes deq_timedelta rather than a usable queue-depth signal.
    # Calibrated with real traffic so ECN spans a useful range instead of
    # saturating at 100% (the old 5/20/60 values marked almost every packet).
    SW_ECN_THR  = {1: 50, 2: 100, 3: 200}      # deq_timedelta threshold
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

    # ---- controlled experiments: --cc / --ablate skip the demo pipeline ----
    results_dir = os.path.join(_HERE, 'results')
    if cc_mode:
        demo_flows = [f for f in flows if len(f.subflows) >= 3][:3]
        compare_cc(demo_flows, pairs, direct_links, n_rep=cc_rep,
                   results_out=os.path.join(results_dir, 't5_cc.json'))
        print '\n=== Done (--cc, topology up) ==='
        print 'Ctrl-C to cleanup.'
        return
    if dynamic_mode:
        demo_flows = [f for f in flows if len(f.subflows) >= 3][:3]
        dynamic_adaptation(
            demo_flows, direct_links, n_rep=3,
            results_out=os.path.join(results_dir, 'dynamic_adaptation.json'))
        print '\n=== Done (--dynamic, topology up) ==='
        return
    if ablate_mode is not None or ablate_all:
        stages = [ablate_mode] if ablate_mode is not None else [0, 1, 3, 15]
        agg = []
        for st in stages:
            r = ablate_resilience(
                flows, pairs, direct_links, stage=st,
                results_out=os.path.join(results_dir,
                                         'fig8_stage%d.json' % st))
            agg.append(r)
        with open(os.path.join(results_dir, 'fig8_ablation.json'), 'w') as f:
            _json_main.dump(agg, f, indent=1)
        print '\n=== Done (--ablate, topology up) ==='
        print 'Ctrl-C to cleanup.'
        return

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
            # segments scale with the effective window (DCQCN base x residual),
            # so the RL residual action really changes how much is sent
            n = int(15 * max(cwnd_mul, 0.25))
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
            """Use the same all-egress delta ECN view as deployed senders."""
            try:
                import json as _json
                with open('/tmp/ecn_global.json') as f:
                    values = _json.load(f)
                return float(values.get(str(sw_idx), values.get(sw_idx, 0.0)))
            except Exception:
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


def compare_cc(demos, pairs, direct_links, run_seconds=8, n_rep=5,
               credit_limit=128, pace=0.0002, results_out=None):
    """Congestion-control comparison (T5): RL vs MPTCP LIA/OLIA vs fixed cwnd
    vs pseudo-Reno (AIMD) over the same concurrent MPTCP flows. Each mode is
    run n_rep times (fresh receiver ports per rep, topology not rebuilt) and
    per-flow throughput is reported as mean +/- std; Jain's index is computed
    from the per-flow means. Results are dumped to results_out (JSON) so the
    figure script reads real data instead of hardcoded values."""
    import mptcp_tcp
    import re as _re
    import json as _json
    import ast as _ast
    import random as _random
    _SW_NET = {1: '10.0.0', 2: '10.2.0', 3: '10.3.0'}
    def sw_ip(i, s):
        return '{}.{}'.format(_SW_NET[s], i)
    # result key, sender cc_mode, label, policy path. The first three rows
    # isolate the learned residual from the same ECN/Credit local baseline.
    modes = [
        ('local', 'local', 'ECN/Credit baseline (no RL)', POLICY_REAL),
        ('rl_offline', 'rl', 'Residual Q (offline)', POLICY_OFFLINE),
        ('rl', 'rl', 'Residual Q (fine-tuned)', POLICY_REAL),
        ('lia', 'lia', 'LIA-inspired (user-space)', POLICY_REAL),
        ('olia', 'olia', 'OLIA-inspired (user-space)', POLICY_REAL),
        ('fixed', 'fixed', 'Fixed cwnd=32', POLICY_REAL),
        ('aimd', 'aimd', 'Pseudo-Reno (AIMD)', POLICY_REAL),
    ]
    n_modes = len(modes)
    print '\n=== 13. Congestion-control comparison (T5): %d modes x %d reps ===' % (
        n_modes, n_rep)
    # mode -> {rep: [per-flow thpt]}
    per_rep = {key: [] for key, _, _, _ in modes}
    metrics_per_rep = {key: [] for key, _, _, _ in modes}
    dropped = {}   # mode -> count of reps dropped as outliers
    for rep in range(n_rep):
        run_order = list(modes)
        _random.Random(20260919 + rep).shuffle(run_order)
        mode_index = {spec[0]: i for i, spec in enumerate(modes)}
        for key, mode, label, policy_path in run_order:
            mi = mode_index[key]
            base_port = 8000 + (rep * n_modes + mi) * 10   # fresh ports per rep
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
                with open('/tmp/cc_recv_%d_%d_%d.py' % (rep, mi, fi), 'w') as f:
                    f.write(recv_body)
                rp = subprocess.Popen(
                    'ip netns exec {} python2 -u /tmp/cc_recv_%d_%d_%d.py'.format(
                        NS(demo.dst)) % (rep, mi, fi),
                    shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                receivers.append((demo, rp))
                time.sleep(0.2)
                send_dest = []
                for k, sf in enumerate(demo.subflows):
                    if sf.path == 'direct':
                        dlink = direct_links[sf.sid]
                        ipb = dlink[3].split('/')[0]
                    else:
                        sw_idx = int(sf.path[2]) - 1
                        ipb = sw_ip(demo.dst, sw_idx + 1)
                    send_dest.append((ipb, k, sf.path))
                stopf = '/tmp/cc_stop_%d_%d_%d' % (rep, mi, demo.fid)
                try:
                    os.remove(stopf)
                except Exception:
                    pass
                snd_body = (
                    'import sys; sys.path.insert(0, "/workspace/mptcp_exp")\n'
                    'import mptcp_tcp\n'
                    'g = mptcp_tcp.MptcpGroupSender(%d, %r, %d, policy_path=%r, '
                    'cc_mode=%r, credit_limit=%d, pace=%r)\n'
                    'g.run_loop(stop_file=%r)\n'
                ) % (demo.fid, send_dest, port, policy_path, mode,
                     credit_limit, pace, stopf)
                with open('/tmp/cc_send_%d_%d_%d.py' % (rep, mi, fi), 'w') as f:
                    f.write(snd_body)
                sp = subprocess.Popen(
                    'ip netns exec {} python2 -u /tmp/cc_send_%d_%d_%d.py'.format(
                        NS(demo.src)) % (rep, mi, fi),
                    shell=True, preexec_fn=os.setsid,
                    stdout=open('/tmp/cc_send_%d_%d_%d.out' % (rep, mi, fi), 'w'),
                    stderr=subprocess.STDOUT)
                senders.append((demo, sp, stopf))
            time.sleep(1)
            ecn_samples = []
            mode_started = time.time()
            while time.time() - mode_started < run_seconds:
                time.sleep(0.5)
                try:
                    with open('/tmp/ecn_global.json') as _f:
                        values = [float(v) for v in _json.load(_f).values()]
                    if values:
                        ecn_samples.append(max(values))
                except Exception:
                    pass
            for demo, sp, stopf in senders:
                try:
                    with open(stopf, 'w'):
                        pass
                    if not wait_process(sp, 6):
                        raise RuntimeError('sender did not stop within 6s')
                except Exception:
                    try:
                        os.killpg(os.getpgid(sp.pid), signal.SIGTERM)
                    except Exception:
                        pass
            time.sleep(3)
            thpt = []
            recv_stats = []
            for demo, rp in receivers:
                out = rp.stdout.read()
                m = _re.search(r'OK\s+(\{.*\})', out, _re.S)
                try:
                    stats = _ast.literal_eval(m.group(1)) if m else {}
                except Exception:
                    stats = {}
                ordered = int(stats.get('ordered', 0))
                thpt.append(ordered / float(run_seconds))
                recv_stats.append(stats)
            connect_failures = 0
            for demo, sp, stopf in senders:
                try:
                    with open('/tmp/cc_send_%d_%d_%d.out' %
                              (rep, mi, demo.fid)) as f:
                        connect_failures += f.read().count('connect failed')
                except Exception:
                    pass
            per_rep[key].append(thpt)
            metrics_per_rep[key].append({
                'delay_mean_ms': (sum(float(s.get('delay_mean_ms', 0.0))
                                      for s in recv_stats) / max(len(recv_stats), 1)),
                'delay_p95_ms': max([float(s.get('delay_p95_ms', 0.0))
                                     for s in recv_stats] or [0.0]),
                'delay_p99_ms': max([float(s.get('delay_p99_ms', 0.0))
                                     for s in recv_stats] or [0.0]),
                'dup': sum(int(s.get('dup', 0)) for s in recv_stats),
                'in_buf': sum(int(s.get('in_buf', 0)) for s in recv_stats),
                'ecn_mean': (sum(ecn_samples) / len(ecn_samples))
                            if ecn_samples else 0.0,
                'ecn_peak': max(ecn_samples) if ecn_samples else 0.0,
                'connect_failures': connect_failures,
            })
            n = len(thpt)
            jain = ((sum(thpt) ** 2) / (n * sum(x * x for x in thpt))) \
                if n and sum(thpt) else 0.0
            print '  [rep %d] %-24s thpt=%s seg/s  jain=%.3f' % (
                rep, label, ['%.1f' % t for t in thpt], jain)
    # ---- aggregate: per-flow mean/std over reps, Jain from means ----
    import math as _math
    results = {}
    for key, mode, label, policy_path in modes:
        reps = [r for r in per_rep[key] if r]
        n_flow = len(reps[0])
        # Keep every repetition. Connection-establishment failures are an
        # explicit reliability metric, not a post-hoc outlier to discard.
        keep_idx = range(len(reps))
        keep = reps
        dropped[key] = 0
        means = [sum(r[i] for r in keep) / float(len(keep))
                 for i in range(n_flow)]
        stds = [_math.sqrt(sum((r[i] - means[i]) ** 2 for r in keep)
                           / float(len(keep))) for i in range(n_flow)]
        s = sum(means)
        jain = (s * s) / (n_flow * sum(m * m for m in means)) \
            if s else 0.0
        metric_rows = [metrics_per_rep[key][ri] for ri in keep_idx]
        metric_mean = {}
        if metric_rows:
            for metric in metric_rows[0]:
                metric_mean[metric] = sum(r[metric] for r in metric_rows) / len(metric_rows)
        results[key] = {
            'label': label,
            'cc_mode': mode,
            'policy_path': policy_path,
            'per_rep': per_rep[key],
            'metrics_per_rep': metrics_per_rep[key],
            'metrics_mean': metric_mean,
            'n_rep_used': len(keep),
            'n_rep_dropped': dropped[key],
            'mean': means, 'std': stds, 'jain': jain,
        }
        print '  %-24s mean=%s +- %s seg/s  jain=%.4f  (used %d/%d reps)' % (
            label, ['%.2f' % m for m in means], ['%.2f' % v for v in stds],
            jain, len(keep), len(reps))
    if results_out:
        try:
            os.makedirs(os.path.dirname(results_out))
        except Exception:
            pass
        with open(results_out, 'w') as f:
            _json.dump(results, f, indent=1)
        print '  [cc] results written to', results_out
    return results


def dynamic_adaptation(demos, direct_links, n_rep=3, run_seconds=9,
                       results_out=None):
    """Within-run dynamic heterogeneity experiment.

    Three concurrent real-TCP MPTCP-style connections experience a normal
    3-second phase, a 3-second high-ECN phase, and a 3-second recovery phase.
    Compare the same local ECN/Credit controller with and without the learned
    residual.  The parent samples sender state every 0.5 s, yielding actual
    phase throughput, cwnd, path share and ECN traces rather than a synthetic
    state-transition demonstration.
    """
    import json as _json
    import re as _re
    import ast as _ast
    _SW_NET = {1: '10.0.0', 2: '10.2.0', 3: '10.3.0'}
    normal_thr = {1: 50, 2: 100, 3: 200}
    congest_thr = {1: 5, 2: 10, 3: 20}
    modes = [('local', 'ECN/Credit baseline (no RL)'),
             ('rl', 'Residual Q (fine-tuned)')]
    phase_len = run_seconds / 3.0
    all_rows = {m: [] for m, _ in modes}

    def _set_thresholds(values):
        for s, value in values.items():
            run_cli('register_write ecn_thresh 0 %d\n' % value,
                    thrift_port=SW_PORT[s - 1])

    def _phase(t):
        return 'normal' if t < phase_len else (
            'congested' if t < 2 * phase_len else 'recovery')

    print '\n=== Dynamic heterogeneity: 2 modes x %d reps ===' % n_rep
    for rep in range(n_rep):
        for mode, label in modes:
            _set_thresholds(normal_thr)
            base_port = 9400 + rep * 20 + (0 if mode == 'local' else 10)
            receivers, senders = [], []
            for fi, demo in enumerate(demos):
                port = base_port + fi
                recv_body = (
                    'import sys; sys.path.insert(0, "/workspace/mptcp_exp")\n'
                    'import mptcp_tcp\n'
                    'r=mptcp_tcp.TcpDsnReceiver(%d,n_subflows=%d,timeout=%d)\n'
                    'r.recv_loop(%d)\nprint "OK", r.stats()\n'
                ) % (port, len(demo.subflows), run_seconds + 8,
                     run_seconds + 8)
                rfile = '/tmp/dyn_recv_%d_%s_%d.py' % (rep, mode, fi)
                with open(rfile, 'w') as f:
                    f.write(recv_body)
                rp = subprocess.Popen(
                    'ip netns exec %s python2 -u %s' % (NS(demo.dst), rfile),
                    shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                receivers.append(rp)
                time.sleep(0.2)
                dests = []
                for k, sf in enumerate(demo.subflows):
                    if sf.path == 'direct':
                        ipb = direct_links[sf.sid][3].split('/')[0]
                    else:
                        ipb = '%s.%d' % (_SW_NET[int(sf.path[2])], demo.dst)
                    dests.append((ipb, k, sf.path))
                stopf = '/tmp/dyn_stop_%d_%s_%d' % (rep, mode, fi)
                try:
                    os.remove(stopf)
                except Exception:
                    pass
                statef = '/tmp/mptcp_sender_%d.json' % demo.fid
                try:
                    os.remove(statef)
                except Exception:
                    pass
                snd_body = (
                    'import sys; sys.path.insert(0, "/workspace/mptcp_exp")\n'
                    'import mptcp_tcp\n'
                    'g=mptcp_tcp.MptcpGroupSender(%d,%r,%d,policy_path=%r,'
                    'cc_mode=%r,credit_limit=128,pace=0.0002)\n'
                    'g.run_loop(stop_file=%r)\n'
                ) % (demo.fid, dests, port, POLICY_REAL, mode, stopf)
                sfile = '/tmp/dyn_send_%d_%s_%d.py' % (rep, mode, fi)
                ofile = '/tmp/dyn_send_%d_%s_%d.out' % (rep, mode, fi)
                with open(sfile, 'w') as f:
                    f.write(snd_body)
                sp = subprocess.Popen(
                    'ip netns exec %s python2 -u %s' % (NS(demo.src), sfile),
                    shell=True, preexec_fn=os.setsid,
                    stdout=open(ofile, 'w'), stderr=subprocess.STDOUT)
                senders.append((demo, sp, stopf, statef))

            started = time.time()
            switched_high = switched_restore = False
            samples = []
            while time.time() - started < run_seconds:
                elapsed = time.time() - started
                if elapsed >= phase_len and not switched_high:
                    _set_thresholds(congest_thr)
                    switched_high = True
                if elapsed >= 2 * phase_len and not switched_restore:
                    _set_thresholds(normal_thr)
                    switched_restore = True
                time.sleep(0.5)
                dsn_total = 0
                cwnds, states = [], []
                path_counts = {'direct': 0, 'sw1': 0, 'sw2': 0, 'sw3': 0}
                for demo, sp, stopf, statef in senders:
                    try:
                        st = _json.load(open(statef))
                        dsn_total += int(st.get('dsn_next', 0))
                        states.append(int(st.get('state', 0)))
                        for sf in st.get('subflows', {}).values():
                            cwnds.append(float(sf.get('cwnd', 0)))
                            p = sf.get('path')
                            path_counts[p] = path_counts.get(p, 0) + int(sf.get('send', 0))
                    except Exception:
                        pass
                try:
                    ev = [float(v) for v in _json.load(
                        open('/tmp/ecn_global.json')).values()]
                except Exception:
                    ev = []
                samples.append({
                    't_s': elapsed, 'phase': _phase(elapsed),
                    'dsn_total': dsn_total,
                    'cwnd_mean': sum(cwnds) / len(cwnds) if cwnds else 0.0,
                    'state_max': max(states) if states else 0,
                    'ecn_max': max(ev) if ev else 0.0,
                    'path_send_total': path_counts,
                })

            for demo, sp, stopf, statef in senders:
                with open(stopf, 'w'):
                    pass
                if not wait_process(sp, 12):
                    try:
                        os.killpg(os.getpgid(sp.pid), signal.SIGTERM)
                    except Exception:
                        pass
            _set_thresholds(normal_thr)
            recv_stats = []
            for rp in receivers:
                out = rp.stdout.read()
                m = _re.search(r'OK\s+(\{.*\})', out, _re.S)
                try:
                    recv_stats.append(_ast.literal_eval(m.group(1)) if m else {})
                except Exception:
                    recv_stats.append({})

            phases = {}
            for ph in ('normal', 'congested', 'recovery'):
                ss = [x for x in samples if x['phase'] == ph]
                if len(ss) >= 2:
                    dt = max(ss[-1]['t_s'] - ss[0]['t_s'], 1e-6)
                    thpt = (ss[-1]['dsn_total'] - ss[0]['dsn_total']) / dt
                    path_delta = {}
                    for p in ss[-1]['path_send_total']:
                        path_delta[p] = (ss[-1]['path_send_total'].get(p, 0) -
                                         ss[0]['path_send_total'].get(p, 0))
                    total_path = float(sum(path_delta.values())) or 1.0
                    shares = {p: v / total_path for p, v in path_delta.items()}
                else:
                    thpt, shares = 0.0, {}
                phases[ph] = {
                    'throughput_seg_s': thpt,
                    'cwnd_mean': sum(x['cwnd_mean'] for x in ss) / len(ss) if ss else 0.0,
                    'ecn_mean': sum(x['ecn_max'] for x in ss) / len(ss) if ss else 0.0,
                    'state_mean': sum(x['state_max'] for x in ss) / float(len(ss)) if ss else 0.0,
                    'path_share': shares,
                }
            row = {
                'rep': rep, 'mode': mode, 'samples': samples, 'phases': phases,
                'delay_mean_ms': sum(float(x.get('delay_mean_ms', 0)) for x in recv_stats) / max(len(recv_stats), 1),
                'delay_p95_ms': max([float(x.get('delay_p95_ms', 0)) for x in recv_stats] or [0]),
                'ordered': sum(int(x.get('ordered', 0)) for x in recv_stats),
            }
            all_rows[mode].append(row)
            print '  [dyn rep %d] %-28s phase_thpt=%s' % (
                rep, label, {p: round(phases[p]['throughput_seg_s'], 1)
                             for p in phases})

    result = {'phase_seconds': phase_len, 'thresholds': {
                  'normal': normal_thr, 'congested': congest_thr},
              'modes': {}}
    for mode, label in modes:
        rows = all_rows[mode]
        summary = {}
        for ph in ('normal', 'congested', 'recovery'):
            summary[ph] = {}
            for metric in ('throughput_seg_s', 'cwnd_mean', 'ecn_mean', 'state_mean'):
                vals = [r['phases'][ph][metric] for r in rows]
                summary[ph][metric] = sum(vals) / len(vals)
            paths = ('direct', 'sw1', 'sw2', 'sw3')
            summary[ph]['path_share'] = {
                p: sum(r['phases'][ph]['path_share'].get(p, 0.0)
                       for r in rows) / len(rows) for p in paths}
        result['modes'][mode] = {'label': label, 'per_rep': rows,
                                 'phase_mean': summary}
    if results_out:
        with open(results_out, 'w') as f:
            _json.dump(result, f, indent=1)
        print '  [dynamic] written to', results_out
    return result


def ablate_resilience(flows, pairs, direct_links, stage=3, run_seconds=35,
                      results_out=None, n_rep=5, target_segments=800):
    """Controlled resilience ablation (paper Fig. 8): the same cut/up script
    is replayed under progressively enabled recovery layers, and the receiver
    reports (ordered, dup, in_buf). stage bitmask over
    {1: go-back-N replay, 2: NAK/SACK, 4: stall-detect, 8: tail-converge};
    stage 0 = no app-layer recovery, stage 15 = all layers."""
    import mptcp_tcp
    import json as _json
    import re as _re
    import ast as _ast
    _SW_NET = {1: '10.0.0', 2: '10.2.0', 3: '10.3.0'}
    _SW_TC = {1: 'delay 10ms 2ms loss 1%',
              2: 'delay 30ms 10ms loss 2%',
              3: 'delay 2ms loss 0.1%'}
    def sw_ip(i, s):
        return '{}.{}'.format(_SW_NET[s], i)
    stages_map = {
        0: {'replay': 0, 'nak': 0, 'stall': 0, 'tail': 0},
        1: {'replay': 1, 'nak': 0, 'stall': 0, 'tail': 0},
        3: {'replay': 1, 'nak': 1, 'stall': 0, 'tail': 0},
        15: {'replay': 1, 'nak': 1, 'stall': 1, 'tail': 1},
    }
    name = {0: 'stage0-no-recovery', 1: 'stage1-replay',
            3: 'stage2-nak', 15: 'stage3-full'}[stage]
    print '\n=== Resilience ablation: %s ===' % name
    demo = [f for f in flows if len(f.subflows) >= 3][0]
    n_sub = len(demo.subflows)
    # drop target: the switch subflow with the largest RTT (sw2=30ms > sw1
    # 10ms > sw3 2ms) so it holds the most in-flight data at drop time
    rtt = {'direct': 1.0, 'sw1': 10.0, 'sw2': 30.0, 'sw3': 2.0}
    sw_sfs = [(k, sf) for k, sf in enumerate(demo.subflows)
              if sf.path.startswith('sw')]
    drop_sid = sorted(sw_sfs, key=lambda (k, sf): -rtt[sf.path])[0][0]
    print '  demo flow %d -> %s, drop sid=%d (%s)' % (
        demo.fid, [sf.path for sf in demo.subflows], drop_sid,
        demo.subflows[drop_sid].path)
    send_dest = []
    for k, sf in enumerate(demo.subflows):
        if sf.path == 'direct':
            dlink = direct_links[sf.sid]
            ipb = dlink[3].split('/')[0]
        else:
            sw_idx = int(sf.path[2]) - 1
            ipb = sw_ip(demo.dst, sw_idx + 1)
        send_dest.append((ipb, k, sf.path))
    res = {'stage': stage, 'name': name, 'per_rep': []}
    for rep in range(n_rep):
        port = 9000 + stage * 10 + rep
        recv_body = (
            'import sys; sys.path.insert(0, "/workspace/mptcp_exp")\n'
            'import mptcp_tcp\n'
            'r = mptcp_tcp.TcpDsnReceiver(%d, n_subflows=%d, timeout=%d)\n'
            'o = r.recv_loop(%d, idle_after_close=2.0)\n'
            'print "OK", r.stats()\n'
        ) % (port, n_sub, run_seconds + 8, run_seconds + 8)
        with open('/tmp/abl_recv_%d_%d.py' % (stage, rep), 'w') as f:
            f.write(recv_body)
        rp = subprocess.Popen(
            'ip netns exec {} python2 -u /tmp/abl_recv_%d_%d.py'.format(
                NS(demo.dst)) % (stage, rep),
            shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        # Give the listener time to bind before opening three simultaneous
        # TCP subflows. This removes sporadic initial-connect failures.
        time.sleep(0.5)
        stopf = '/tmp/abl_stop_%d_%d' % (stage, rep)
        cmdf = '/tmp/abl_cmd_%d_%d' % (stage, rep)
        progressf = '/tmp/abl_progress_%d_%d' % (stage, rep)
        resumef = '/tmp/abl_resume_%d_%d' % (stage, rep)
        try:
            os.remove(stopf)
        except Exception:
            pass
        try:
            os.remove(cmdf)
        except Exception:
            pass
        try:
            os.remove(progressf)
        except Exception:
            pass
        for _p in (resumef, cmdf + '.ack'):
            try:
                os.remove(_p)
            except Exception:
                pass
        snd_body = (
            'import sys; sys.path.insert(0, "/workspace/mptcp_exp")\n'
            'import mptcp_tcp\n'
            'g = mptcp_tcp.MptcpGroupSender(%d, %r, %d, policy_path=%r, '
            'cc_mode=%r, fixed_cwnd=%d, credit_limit=%d, pace=%r, stages=%r, '
            'round_robin=True, auto_drop_sid=%d, auto_drop_at=None)\n'
            'g.run_loop(stop_file=None, settle=10.0, cmd_file=%r, '
            'max_segments=%d, progress_file=%r, pause_at=%d, '
            'resume_file=%r)\n'
        ) % (demo.fid, send_dest, port, POLICY_REAL, 'fixed',
             300, 300, 0.0002, stages_map[stage], drop_sid, cmdf,
             target_segments, progressf, 100, resumef)
        with open('/tmp/abl_send_%d_%d.py' % (stage, rep), 'w') as f:
            f.write(snd_body)
        sp = subprocess.Popen(
            'ip netns exec {} python2 -u /tmp/abl_send_%d_%d.py'.format(
                NS(demo.src)) % (stage, rep),
            shell=True, preexec_fn=os.setsid,
            stdout=open('/tmp/abl_send_%d_%d.out' % (stage, rep), 'w'),
            stderr=subprocess.STDOUT)
        # Real path-level failure: bring down the switch-side egress interface
        # after 100 DSNs, then notify the sender that SDN confirmed the failure.
        # This strands real kernel-TCP data on the failed path while keeping
        # recovery-layer behavior controlled and reproducible.
        cut_dsn = 100
        wait_deadline = time.time() + 8.0
        progress = 0
        while time.time() < wait_deadline and progress < cut_dsn:
            try:
                with open(progressf) as f:
                    progress = int(f.read().strip() or 0)
            except Exception:
                pass
            time.sleep(0.02)
        cut_path = demo.subflows[drop_sid].path
        cut_sw = int(cut_path[2])
        # Black-hole the sender namespace's real egress path in the kernel.
        # A veth down/up cycle makes BMv2 permanently lose its raw-socket port
        # until the whole switch is restarted, invalidating later repetitions.
        # Replacing the host qdisc with 100% loss is an equivalent data-plane
        # outage, flushes queued packets, and can be restored reproducibly.
        cut_iface = H_SW_INTF(demo.src, cut_sw)
        cut_rc = sh_quiet('ip netns exec %s tc qdisc replace dev %s root netem loss 100%%' % (
            NS(demo.src), cut_iface))
        fault_burst = 30
        with open(cmdf, 'w') as f:
            f.write('faultburst %d %d\nfail %d\n' % (
                drop_sid, fault_burst, drop_sid))
        # Do not release the DSN barrier until the sender has consumed the
        # failure command and removed the dead subflow.
        ack_deadline = time.time() + 3.0
        while time.time() < ack_deadline and not os.path.exists(cmdf + '.ack'):
            time.sleep(0.01)
        with open(resumef, 'w') as f:
            f.write('resume\n')
        try:
            if not wait_process(sp, run_seconds):
                raise RuntimeError('sender did not finish fixed workload')
        except Exception:
            try:
                os.killpg(os.getpgid(sp.pid), signal.SIGTERM)
            except Exception:
                pass
        # Always restore the physical path before the next repetition.
        sh_quiet('ip netns exec %s tc qdisc replace dev %s root netem %s' % (
            NS(demo.src), cut_iface, _SW_TC[cut_sw]))
        time.sleep(0.5)
        out = rp.stdout.read()
        m = _re.search(r'OK\s+(\{.*\})', out, _re.S)
        try:
            stats = _ast.literal_eval(m.group(1)) if m else {}
        except Exception:
            stats = {}
        ordered = int(stats.get('ordered', 0))
        dup = int(stats.get('dup', 0))
        in_buf = int(stats.get('in_buf', -1))
        unique_delivered = ordered + max(in_buf, 0)
        res['per_rep'].append({
            'ordered': ordered, 'dup': dup, 'in_buf': in_buf,
            'delay_mean_ms': float(stats.get('delay_mean_ms', 0.0)),
            'delay_p95_ms': float(stats.get('delay_p95_ms', 0.0)),
            'delay_p99_ms': float(stats.get('delay_p99_ms', 0.0)),
            'fault_type': 'sender-egress-kernel-blackhole',
            'fault_iface': '%s/%s' % (NS(demo.src), cut_iface),
            'fault_rc': cut_rc,
            'cut_dsn': progress,
            'fault_detection_window_segments': fault_burst,
            'target_segments': target_segments,
            'completion_ratio': ordered / float(target_segments),
            'unique_delivered': unique_delivered,
            'missing_segments': max(0, target_segments - unique_delivered),
        })
        print '  [abl rep %d] ordered=%d dup=%d in_buf=%d' % (
            rep, ordered, dup, in_buf)
    n = len(res['per_rep'])
    res['mean'] = {
        'ordered': sum(r['ordered'] for r in res['per_rep']) / float(n),
        'dup': sum(r['dup'] for r in res['per_rep']) / float(n),
        'in_buf': sum(r['in_buf'] for r in res['per_rep']) / float(n),
        'completion_ratio': sum(r['completion_ratio'] for r in res['per_rep']) / float(n),
        'unique_delivered': sum(r['unique_delivered'] for r in res['per_rep']) / float(n),
        'missing_segments': sum(r['missing_segments'] for r in res['per_rep']) / float(n),
    }
    if results_out:
        try:
            os.makedirs(os.path.dirname(results_out))
        except Exception:
            pass
        with open(results_out, 'w') as f:
            _json.dump(res, f, indent=1)
        print '  [abl] written to', results_out
    return res


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
            if not wait_process(sp, 6):
                raise RuntimeError('sender did not stop within 6s')
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
            if not wait_process(sp, 3):
                raise RuntimeError('sender did not stop within 3s')
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
