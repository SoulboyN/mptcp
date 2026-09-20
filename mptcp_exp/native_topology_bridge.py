#!/usr/bin/env python2
"""Reuse the exact P4 topology in a guest chroot; publish native endpoint map."""
import json
import os
import sys
import time
import subprocess
import run_mptcp as runner

OUT = '/workspace/mptcp_exp/results/native_mptcp'


def checked_cli(cmds, thrift_port=9090):
    proc = subprocess.Popen(['/usr/local/bin/simple_switch_CLI', '--thrift-ip',
                             '127.0.0.1', '--thrift-port', str(thrift_port)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    out, err = proc.communicate(cmds)
    if proc.returncode or 'Error' in out or 'Could not connect' in out:
        raise RuntimeError(out + err)
    return out, err


def hold_topology(demos, pairs, direct_links, **kwargs):
    records = []
    for flow in demos:
        paths = []
        for sf in flow.subflows:
            if sf.path == 'direct':
                a, b, ipa, ipb = direct_links[sf.sid]
                paths.append(dict(path='direct', src_ip=ipa.split('/')[0],
                                  dst_ip=ipb.split('/')[0], src_dev=a, dst_dev=b))
            else:
                sw = int(sf.path[2])
                subnet = {1: '10.0.0', 2: '10.2.0', 3: '10.3.0'}[sw]
                paths.append(dict(path=sf.path, src_ip='%s.%d' % (subnet, flow.src),
                                  dst_ip='%s.%d' % (subnet, flow.dst),
                                  src_dev='h%d-s%d' % (flow.src, sw),
                                  dst_dev='h%d-s%d' % (flow.dst, sw)))
        records.append(dict(fid=flow.fid, src=flow.src, dst=flow.dst, paths=paths))
    with open(OUT + '/topology.json', 'w') as f:
        json.dump(records, f, indent=2)
    print('NATIVE_TOPOLOGY_READY')
    while not os.path.exists('/tmp/native_topology_stop'):
        time.sleep(1)


if __name__ == '__main__':
    runner.compare_cc = hold_topology
    runner.run_cli = checked_cli
    sys.argv = [__file__, '--cc', '1']
    try:
        runner.main()
    finally:
        runner.cleanup()
