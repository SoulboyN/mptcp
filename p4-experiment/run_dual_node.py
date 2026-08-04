#!/usr/bin/env python2
"""
Dual-node IPv4 communication through a P4 BMv2 switch.
Topo: h1 (10.0.0.1) --- [BMv2 port1 | port2] --- h2 (10.0.0.2)

Works with the p4lang/p4app Docker image.
Usage: docker exec p4app bash -c 'cd /workspace && python2 run_dual_node.py'
"""

import subprocess
import time
import os
import sys
import signal

P4FILE = 'simple_router.p4'
JSON   = 'build/simple_router.json'
P4INFO = 'build/simple_router.p4info.txt'

SW_INTF1 = 's1-eth1'
SW_INTF2 = 's1-eth2'
H_INTF1  = 'h1-eth0'
H_INTF2  = 'h2-eth0'
NS1      = 'ns-h1'
NS2      = 'ns-h2'
H1_IP    = '10.0.0.1'
H2_IP    = '10.0.0.2'
PREFIX   = 24

sw_proc = None


def sh(cmd, check_ok=True):
    print '[+]', cmd if len(cmd) < 140 else cmd[:140] + '...'
    rc = subprocess.call(cmd, shell=True)
    if check_ok and rc != 0:
        print '[!] FAIL (rc={})'.format(rc)
    return rc


def cleanup():
    global sw_proc
    print '\n=== Cleanup ==='
    if sw_proc and sw_proc.poll() is None:
        sw_proc.terminate()
        sw_proc.wait()
        print '  BMv2 stopped'
    for ns in [NS1, NS2]:
        sh('ip netns del {} 2>/dev/null'.format(ns), check_ok=False)
    for iface in [SW_INTF1, SW_INTF2, H_INTF1, H_INTF2]:
        sh('ip link del {} 2>/dev/null'.format(iface), check_ok=False)


def mac_of(ns, iface):
    out = subprocess.check_output(
        'ip netns exec {} cat /sys/class/net/{}/address'.format(ns, iface),
        shell=True)
    return out.strip()


def main():
    global sw_proc
    signal.signal(signal.SIGINT, lambda *_: (cleanup(), sys.exit(0)))

    cleanup()
    time.sleep(0.5)

    # === 1. Compile P4 ================================================
    print '=== 1. Compile P4 ==='
    if not os.path.exists('build'):
        os.makedirs('build')
    subprocess.check_call([
        'p4c', '--target', 'bmv2', '--arch', 'v1model',
        '--p4runtime-files', P4INFO, '-o', 'build/', P4FILE
    ])

    # === 2. Create veth pairs =========================================
    print '\n=== 2. Create veth pairs ==='
    sh('ip link add {} type veth peer name {}'.format(H_INTF1, SW_INTF1))
    sh('ip link add {} type veth peer name {}'.format(H_INTF2, SW_INTF2))
    sh('ip link set {} up'.format(SW_INTF1))
    sh('ip link set {} up'.format(SW_INTF2))

    # Create host namespaces
    sh('ip netns add {}'.format(NS1))
    sh('ip netns add {}'.format(NS2))
    sh('ip link set {} netns {}'.format(H_INTF1, NS1))
    sh('ip link set {} netns {}'.format(H_INTF2, NS2))

    # Configure IPs
    sh('ip netns exec {} ip addr add {}/{} dev {}'.format(NS1, H1_IP, PREFIX, H_INTF1))
    sh('ip netns exec {} ip addr add {}/{} dev {}'.format(NS2, H2_IP, PREFIX, H_INTF2))
    sh('ip netns exec {} ip link set {} up'.format(NS1, H_INTF1))
    sh('ip netns exec {} ip link set {} up'.format(NS2, H_INTF2))
    sh('ip netns exec {} ip link set lo up'.format(NS1))
    sh('ip netns exec {} ip link set lo up'.format(NS2))

    h1_mac = mac_of(NS1, H_INTF1)
    h2_mac = mac_of(NS2, H_INTF2)
    print '  h1 MAC: {}   h2 MAC: {}'.format(h1_mac, h2_mac)

    # Static ARP using the REAL MACs
    sh('ip netns exec {} ip neigh replace {} lladdr {} dev {} nud permanent'
       .format(NS1, H2_IP, h2_mac, H_INTF1))
    sh('ip netns exec {} ip neigh replace {} lladdr {} dev {} nud permanent'
       .format(NS2, H1_IP, h1_mac, H_INTF2))

    # === 3. Start BMv2 ================================================
    print '\n=== 3. Start BMv2 ==='
    cmd = [
        'simple_switch', '--log-console', '--thrift-port', '9090',
        '-i', '1@{}'.format(SW_INTF1),
        '-i', '2@{}'.format(SW_INTF2),
        JSON,
    ]
    sw_proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    time.sleep(3)
    if sw_proc.poll() is not None:
        print '  [!] BMv2 died!'
        sys.exit(1)
    print '  BMv2 PID={}'.format(sw_proc.pid)

    # === 4. Push forwarding rules =====================================
    # IMPORTANT: use REAL MAC addresses so kernel accepts the forwarded packets
    print '\n=== 4. Push forwarding rules ==='
    rules = '\n'.join([
        'table_add ipv4_lpm forward {}/32 => 1 {}'.format(H1_IP, h1_mac),
        'table_add ipv4_lpm forward {}/32 => 2 {}'.format(H2_IP, h2_mac),
        '',
    ])
    print rules
    p = subprocess.Popen(
        ['simple_switch_CLI', '--thrift-port', '9090'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    out, err = p.communicate(input=rules)
    print out.rstrip()
    if err and err.strip():
        print err.rstrip()

    # === 5. Ping test (with tcpdump capture) ==========================
    # Capture on BOTH switch ports so the full path (h1->sw->h2 and back)
    # is visible in Wireshark.
    CAPTURE = 'ping_capture.pcap'
    print '\n=== 5. Ping test ==='
    if os.path.exists(CAPTURE):
        os.remove(CAPTURE)
    cap1 = subprocess.Popen(
        'tcpdump -i {} -w /tmp/cap1.pcap icmp'.format(SW_INTF1),
        shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    cap2 = subprocess.Popen(
        'tcpdump -i {} -w /tmp/cap2.pcap icmp'.format(SW_INTF2),
        shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    time.sleep(1)

    print '--- h1 -> h2 ---'
    subprocess.call('ip netns exec {} ping -c 3 {}'.format(NS1, H2_IP), shell=True)
    print '\n--- h2 -> h1 ---'
    subprocess.call('ip netns exec {} ping -c 3 {}'.format(NS2, H1_IP), shell=True)

    time.sleep(1)
    cap1.terminate()
    cap1.wait()
    cap2.terminate()
    cap2.wait()
    # merge both ports into one pcap
    subprocess.call(
        'mergecap -w {} /tmp/cap1.pcap /tmp/cap2.pcap'.format(CAPTURE),
        shell=True)
    print '[+] Capture saved to: {}/{} (open with Wireshark on Windows)'.format(os.getcwd(), CAPTURE)

    # === 6. Cleanup ===================================================
    cleanup()
    print 'Success!\n'


if __name__ == '__main__':
    main()
