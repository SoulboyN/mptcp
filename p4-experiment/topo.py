#!/usr/bin/env python2
from mininet.net import Mininet
from mininet.node import Host
from mininet.log import setLogLevel, info
import os, time, subprocess

P4INFO = 'build/simple_router.p4info.txt'
P4_FILE = 'simple_router.p4'
JSON = 'build/simple_router.json'

def start_bmv2():
    cmd = [
        'simple_switch',
        '--log-console',
        '--thrift-port', '9090',
        '-i', '1@s1-eth1',
        '-i', '2@s1-eth2',
        JSON
    ]
    proc = subprocess.Popen(cmd)
    return proc

def run():
    setLogLevel('info')

    info('*** Compiling P4 program\n')
    subprocess.call(['mkdir', '-p', 'build'])
    subprocess.call([
        'p4c', '--target', 'bmv2', '--arch', 'v1model',
        '--p4runtime-files', P4INFO,
        '-o', 'build/',
        P4_FILE
    ])
    time.sleep(1)

    info('*** Starting BMv2 switch\n')
    sw_proc = start_bmv2()
    time.sleep(2)

    info('*** Creating Mininet network\n')
    net = Mininet(controller=None)
    s1 = net.addSwitch('s1', failMode='standalone')
    h1 = net.addHost('h1', ip='10.0.0.1/24')
    h2 = net.addHost('h2', ip='10.0.0.2/24')
    net.addLink(h1, s1)
    net.addLink(h2, s1)
    net.start()
    time.sleep(1)

    info('*** Pushing table entries\n')
    cmd_txt = """
table_add ipv4_lpm forward 10.0.0.1/32 => 1 00:00:00:00:00:01
table_add ipv4_lpm forward 10.0.0.2/32 => 2 00:00:00:00:00:02
"""
    p = subprocess.Popen(
        ["simple_switch_CLI", "--thrift-port", "9090"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    out, err = p.communicate(input=cmd_txt.encode())
    print(out.decode())
    if err:
        print(err.decode())

    info('*** Ping test\n')
    net.ping([h1, h2])

    info('*** Stopping\n')
    net.stop()
    sw_proc.terminate()
    sw_proc.wait()

if __name__ == '__main__':
    run()
