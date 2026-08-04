#!/usr/bin/env python2
"""
16-node IPv4 forwarding through a P4 BMv2 switch, with per-destination
token-bucket rate limiting (v1model meter).

Topo: ns-h1..ns-h16 (10.0.0.1..16/24) -- veth -- [BMv2 ports 1..16]

Usage: docker exec p4app bash -c 'cd /workspace && python2 run_16node.py'
"""

import subprocess
import time
import os
import sys
import signal

P4FILE = 'simple_router16.p4'
JSON   = 'build/simple_router16.json'
P4INFO = 'build/simple_router16.p4info.txt'

NODES = 16
NET    = '10.0.0'
BASE_IP = lambda i: '{}.{}'.format(NET, i)   # i in 1..16
NS     = lambda i: 'ns-h{}'.format(i)
H_INTF = lambda i: 'h{}-eth0'.format(i)
SW_INTF = lambda i: 's1-eth{}'.format(i)

sw_proc = None


def sh(cmd, check_ok=True):
    print '[+]', cmd if len(cmd) < 140 else cmd[:140] + '...'
    rc = subprocess.call(cmd, shell=True)
    if check_ok and rc != 0:
        print '[!] FAIL (rc={})'.format(rc)
    return rc


def sh_quiet(cmd):
    """Run a command silently (no echo); returns rc."""
    with open(os.devnull, 'w') as devnull:
        rc = subprocess.call(cmd, shell=True, stdout=devnull, stderr=devnull)
    if rc != 0:
        print '[!] silent-fail rc={}: {}'.format(rc, cmd[:120])
    return rc


def cleanup():
    global sw_proc
    print '\n=== Cleanup ==='
    if sw_proc and sw_proc.poll() is None:
        sw_proc.terminate()
        sw_proc.wait()
        print '  BMv2 stopped'
    for i in range(1, NODES + 1):
        sh('ip netns del {} 2>/dev/null'.format(NS(i)), check_ok=False)
        sh('ip link del {} 2>/dev/null'.format(SW_INTF(i)), check_ok=False)
        sh('ip link del {} 2>/dev/null'.format(H_INTF(i)), check_ok=False)
    # Remove stale BMv2 IPC sockets that block startup
    sh('rm -f /tmp/bmv2-*.ipc', check_ok=False)

def mac_of(ns, iface):
    out = subprocess.check_output(
        'ip netns exec {} cat /sys/class/net/{}/address'.format(ns, iface),
        shell=True)
    return out.strip()


def push_table_entries():
    """Build CLI text that adds 16 LPM routes with real MACs."""
    lines = []
    for i in range(1, NODES + 1):
        ip = BASE_IP(i)
        mac = mac_of(NS(i), H_INTF(i))
        lines.append('table_add ipv4_lpm forward {}/32 => {} {}'.format(ip, i, mac))
    lines.append('')
    return '\n'.join(lines)


def push_meter_config(limited_dest, rate_bps, burst_bytes):
    """
    Configure per-destination meters with meter_set_rates.
    Rate unit is bytes/microsecond in BMv2 CLI, so convert from bps:
    bytes/us = bps / 8 / 1e6.
    This BMv2 expects 2 (rate:burst) pairs (GREEN/YELLOW).
    Returns CLI text.
    """
    rate_bytes_per_us = max(int(rate_bps) / 8 / 1000000, 1)
    lines = []
    for i in range(1, NODES + 1):
        idx = i - 1   # meter index is 0-based, P4 uses last_octet-1
        if i == limited_dest:
            r = rate_bytes_per_us
            b = burst_bytes
        else:
            r = 1000000000   # effectively unlimited
            b = 1000000000
        lines.append('meter_set_rates {} {} {}:{} {}:{}'.format(
            'm_dst', idx, r, b, r, b))
    return '\n'.join(lines)


def run_cli(cmds):
    p = subprocess.Popen(
        ['simple_switch_CLI', '--thrift-port', '9090'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = p.communicate(input=cmds)
    return out, err


def main():
    global sw_proc
    signal.signal(signal.SIGINT, lambda *_: (cleanup(), sys.exit(0)))

    cleanup()
    time.sleep(0.5)

    # === 1. Compile ==================================================
    print '=== 1. Compile P4 ==='
    if not os.path.exists('build'):
        os.makedirs('build')
    subprocess.check_call([
        'p4c', '--target', 'bmv2', '--arch', 'v1model',
        '--p4runtime-files', P4INFO, '-o', 'build/', P4FILE
    ])

    # === 2. Build topology ===========================================
    print '\n=== 2. Build 16-node topology ==='
    for i in range(1, NODES + 1):
        sh('ip link add {} type veth peer name {}'.format(H_INTF(i), SW_INTF(i)))
        sh('ip link set {} up'.format(SW_INTF(i)))
        sh('ip netns add {}'.format(NS(i)))
        sh('ip link set {} netns {}'.format(H_INTF(i), NS(i)))
        sh('ip netns exec {} ip addr add {}/24 dev {}'.format(NS(i), BASE_IP(i), H_INTF(i)))
        sh('ip netns exec {} ip link set {} up'.format(NS(i), H_INTF(i)))
        sh('ip netns exec {} ip link set lo up'.format(NS(i)))
    print '  [*] {} namespaces + veth pairs created'.format(NODES)

    # Static ARP for all pairs (avoid ARP flooding the switch)
    print '\n=== 3. Static ARP (all pairs, silent) ==='
    for i in range(1, NODES + 1):
        for j in range(1, NODES + 1):
            if i == j:
                continue
            sh_quiet('ip netns exec {} ip neigh replace {} lladdr {} dev {} nud permanent'
                     .format(NS(i), BASE_IP(j), mac_of(NS(j), H_INTF(j)), H_INTF(i)))
    print '  [*] static ARP done'

    # === 4. Start BMv2 with 16 ports =================================
    print '\n=== 4. Start BMv2 (16 ports) ==='
    # NOTE: no --log-console here. That flag makes simple_switch log every
    # packet, which crushes throughput (a 20 Mbps UDP burst saturated it and
    # caused losses independent of the meter). Without it the switch keeps
    # up and the meter becomes the only drop point.
    cmd = ['simple_switch', '--thrift-port', '9090']
    for i in range(1, NODES + 1):
        cmd += ['-i', '{}@{}'.format(i, SW_INTF(i))]
    cmd.append(JSON)
    # Send switch logs to a file (NOT a PIPE): reading never happens, so a
    # PIPE would fill its 64KB buffer and deadlock the switch mid-run.
    logf = open('build/bmv2.log', 'w')
    sw_proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT)
    time.sleep(5)   # give BMv2 time to be fully ready
    if sw_proc.poll() is not None:
        print '  [!] BMv2 died!'
        logf.close()
        sys.exit(1)
    print '  BMv2 PID={}'.format(sw_proc.pid)

    # === 5. Push 16 routes ===========================================
    print '\n=== 5. Push 16 forwarding rules ==='
    out, err = run_cli(push_table_entries())
    print out.rstrip()
    if err and err.strip():
        print err.rstrip()

    # === 6. Connectivity check (full ping) ===========================
    print '\n=== 6. Connectivity check (ping all pairs) ==='
    ok = 0
    for i in range(1, NODES + 1):
        for j in range(1, NODES + 1):
            if i >= j:
                continue
            try:
                with open(os.devnull, 'w') as devnull:
                    out = subprocess.check_output(
                        'ip netns exec {} ping -c 1 -W 1 {}'.format(NS(i), BASE_IP(j)),
                        shell=True, stderr=devnull)
                if '1 received' in out:
                    ok += 1
            except subprocess.CalledProcessError:
                pass
    total = NODES * (NODES - 1) / 2
    print '  [*] {}/{} directional pairs ping OK'.format(ok, total)

    # === 7. Rate-limit demo ==========================================
    # Rate-limit ALL traffic TO host 2 to the BMv2 meter floor (1 byte/us =
    # 8 Mbps), leaving other destinations unlimited. Drive UDP at 20 Mbps
    # via iperf so the limited path visibly drops packets while the
    # unlimited path does not.
    print '\n=== 7. Traffic-control demo ==='
    # Meter floor is 1 byte/us = 8 Mbps. Rate-limit traffic to h2 to that
    # floor with a burst >= one packet (1500B), so low-rate flows pass and
    # high-rate flows get dropped. h16 stays unlimited as the control.
    print '  Rate-limit traffic to h2 (10.0.0.2) to 8 Mbps (meter floor)'
    rules = push_meter_config(limited_dest=2, rate_bps=8000000, burst_bytes=1500)
    out, err = run_cli(rules + '\n')
    print out.rstrip()

    def udp_flow(dst_ip, tag, n=200, interval=0.002, payload=b'Z'*1400):
        """Send n UDP packets from h1 to dst; count how many the receiver
        (a listener inside the destination namespace) gets."""
        import tempfile
        dst_ns = NS(2) if dst_ip == BASE_IP(2) else NS(16)
        listen_script = os.path.join(tempfile.gettempdir(), 'flow_listen.py')
        listen_body = (
            'import socket, time\n'
            's = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n'
            's.bind(("0.0.0.0", 9752))\n'
            's.settimeout(%s)\n'
            'c = 0\n'
            'try:\n'
            '    while True:\n'
            '        s.recvfrom(1500); c += 1\n'
            'except socket.timeout:\n'
            '    pass\n'
            'print c\n'
        ) % (interval * n + 3)
        with open(listen_script, 'w') as f:
            f.write(listen_body)
        # listener inside destination namespace
        listener = subprocess.Popen(
            'ip netns exec {} python2 -u {}'.format(dst_ns, listen_script),
            shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        time.sleep(0.5)
        # sender inside h1 namespace
        send_script = os.path.join(tempfile.gettempdir(), 'flow_send.py')
        send_body = (
            'import socket, time\n'
            's = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n'
            'p = %r\n'
            'for i in range(%d):\n'
            '    s.sendto(p, ("%s", 9752))\n'
            '    time.sleep(%s)\n'
        ) % (payload, n, dst_ip, repr(interval))
        with open(send_script, 'w') as f:
            f.write(send_body)
        subprocess.check_call(
            'ip netns exec {} python2 {}'.format(NS(1), send_script), shell=True)
        # listener prints one line ("c") when its socket times out; read it
        out = listener.stdout.readline()
        listener.wait()
        try:
            recv = int(out.strip())
        except (ValueError, IndexError):
            recv = 0
        loss = 100.0 * (n - recv) / n if n else 0
        print '  [*] {}: sent {}, received {} ({:.0f}% loss)'.format(
            tag, n, recv, loss)

    print '\n  --- sustained UDP flows h1 -> destinations (~15 Mbps) ---'
    # 300 packets of 1400B every 0.8ms ~= 15.7 Mbps: above the 8 Mbps
    # meter floor, so the limited path drops ~half while h16 passes all.
    udp_flow(BASE_IP(2), 'h1 -> h2   (RATE LIMITED to 8 Mbps)',
             n=300, interval=0.0008)
    udp_flow(BASE_IP(16), 'h1 -> h16  (NOT LIMITED)',
             n=300, interval=0.0008)

    print '\n=== Done ==='
    print 'Topology stays up for manual exploration. Ctrl-C to cleanup.'


if __name__ == '__main__':
    main()
