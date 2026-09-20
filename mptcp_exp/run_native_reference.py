#!/usr/bin/env python3
"""Native Linux MPTCP reference on the existing 16-host BMv2 topology.

Independent units are full runs. TCP is an initial-direct-path reference;
MPTCP uses the same initial path plus advertised switch paths. This suite
does not conflate results with historical application-overlay measurements.
"""
import argparse
import hashlib
import json
import platform
import random
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / 'results' / 'native_mptcp'


def run(*args, check=True):
    p = subprocess.run([str(x) for x in args], capture_output=True, text=True, timeout=20)
    if check and p.returncode:
        raise RuntimeError('%s: %s' % (' '.join(map(str, args)), p.stderr))
    return p.stdout


def thresholds(high=False):
    for i, value in enumerate((5, 10, 20) if high else (50, 100, 200)):
        p = subprocess.run(['chroot', '/p4root', '/usr/local/bin/simple_switch_CLI', '--thrift-ip', '127.0.0.1', '--thrift-port', str(9090 + i)],
                           input='register_write ecn_thresh 0 %d\n' % value,
                           capture_output=True, text=True, timeout=10)
        if p.returncode or 'Error' in p.stdout:
            raise RuntimeError(p.stdout + p.stderr)


def configure(flows):
    nodes = sorted({f[k] for f in flows for k in ('src', 'dst')})
    for node in nodes:
        ns = 'ns-h%d' % node
        run('ip', '-n', ns, 'mptcp', 'endpoint', 'flush')
        run('ip', '-n', ns, 'mptcp', 'limits', 'set', 'subflows', '4', 'add_addr_accepted', '4')
        run('ip', 'netns', 'exec', ns, 'sysctl', '-w', 'net.mptcp.enabled=1',
            'net.ipv4.tcp_congestion_control=cubic', 'net.ipv4.tcp_ecn=1')
    registered = set()
    for f in flows:
        ns = 'ns-h%d' % f['dst']
        for path in f['paths']:
            if path['path'] == 'direct' or (ns, path['dst_ip']) in registered:
                continue
            run('ip', '-n', ns, 'mptcp', 'endpoint', 'add', path['dst_ip'],
                'dev', path['dst_dev'], 'signal')
            registered.add((ns, path['dst_ip']))


def trial(flows, scenario, protocol, rep, root):
    directory = root / ('%s_%s_%02d' % (scenario, protocol, rep))
    directory.mkdir(parents=True, exist_ok=False)
    duration = 12 if scenario == 'fault' else (9 if scenario == 'dynamic' else 8)
    start = time.monotonic() + 4
    processes = []
    statuses, events, snapshots = [], [], []
    selected = flows[:1] if scenario == 'fault' else flows
    thresholds(False)
    try:
        for i, flow in enumerate(selected):
            direct = next(p for p in flow['paths'] if p['path'] == 'direct')
            port = 19000 + rep * 10 + i
            for role, node in (('server', flow['dst']), ('client', flow['src'])):
                output = directory / ('%d_%s.json' % (i, role))
                argv = ['ip', 'netns', 'exec', 'ns-h%d' % node, sys.executable,
                        str(HERE / 'native_stream.py'), role, '--protocol', protocol,
                        '--source', direct['src_ip'], '--dest', direct['dst_ip'],
                        '--port', str(port), '--start', str(start), '--seconds', str(duration),
                        '--output', str(output)]
                if scenario == 'fault':
                    argv += ['--target', '800', '--rate', '100', '--fault-at', '2']
                log = open(directory / ('%d_%s.log' % (i, role)), 'w')
                proc = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT)
                processes.append((proc, log, output))
                if role == 'server':
                    deadline = time.monotonic() + 2
                    while not Path(str(output) + '.ready').exists():
                        if proc.poll() is not None or time.monotonic() > deadline:
                            raise RuntimeError('Server failed to become ready')
                        time.sleep(.02)
        if time.monotonic() >= start:
            raise RuntimeError('Preparation exceeded start barrier')
        high = restored = faulted = False
        while time.monotonic() < start + duration:
            elapsed = time.monotonic() - start
            if scenario == 'dynamic' and elapsed >= 3 and not high:
                thresholds(True)
                events.append({'t': time.monotonic() - start, 'event': 'high_ecn'})
                high = True
            if scenario == 'dynamic' and elapsed >= 6 and not restored:
                thresholds(False)
                events.append({'t': time.monotonic() - start, 'event': 'normal_ecn'})
                restored = True
            if scenario == 'fault' and elapsed >= 2 and not faulted:
                run('ip', 'netns', 'exec', 'ns-h%d' % selected[0]['src'], 'tc', 'qdisc',
                    'replace', 'dev', direct['src_dev'], 'root', 'netem', 'loss', '100%')
                events.append({'t': time.monotonic() - start, 'event': 'direct_blackhole'})
                faulted = True
            if scenario == 'fault' and elapsed >= 6 and not restored:
                run('ip', 'netns', 'exec', 'ns-h%d' % selected[0]['src'], 'tc', 'qdisc',
                    'del', 'dev', direct['src_dev'], 'root')
                events.append({'t': time.monotonic() - start, 'event': 'direct_restored'})
                restored = True
            if elapsed >= 0:
                sockets = [run('ip', 'netns', 'exec', 'ns-h%d' % flow['src'],
                               'ss', '-tin', check=False) for flow in selected]
                established = []
                for i, output in enumerate(sockets):
                    peer_port = 19000 + rep * 10 + i
                    established.append(sum(1 for line in output.splitlines()
                        if line.startswith('ESTAB') and
                        re.search(r':%d(?:\s|$)' % peer_port, line.split()[4])))
                snapshots.append({'t': elapsed, 'sockets_by_flow': sockets,
                                  'established_subflows_by_flow': established})
                try:
                    snapshots[-1]['ecn_telemetry'] = json.loads(Path('/tmp/ecn_global.json').read_text())
                except (OSError, ValueError):
                    snapshots[-1]['ecn_telemetry'] = None
            time.sleep(.25)
        for proc, log, output in processes:
            try:
                rc = proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                rc = proc.wait()
            log.close()
            statuses.append({'file': output.name, 'returncode': rc,
                             'result': json.loads(output.read_text()) if output.exists() else None})
    finally:
        for proc, log, output in processes:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            log.close()
        if scenario == 'fault':
            run('ip', 'netns', 'exec', 'ns-h%d' % selected[0]['src'], 'tc', 'qdisc',
                'del', 'dev', direct['src_dev'], 'root', check=False)
        thresholds(False)
    clients = [r['result'] for r in statuses if 'client' in r['file']]
    servers = [r['result'] for r in statuses if 'server' in r['file']]
    # additional_subflows also includes SYN-SENT attempts: never sufficient alone.
    negotiated = bool(clients) and all(c and any(s.get('additional_subflows', 0) >= 1
        and not s.get('fallback') for s in c['samples']) for c in clients)
    negotiated = negotiated and all(any(row['established_subflows_by_flow'][i] >= 2
        for row in snapshots) for i in range(len(selected)))
    result = {'scenario': scenario, 'protocol': protocol, 'rep': rep, 'events': events,
              'processes': statuses, 'snapshots': snapshots,
              'multipath_verified': negotiated if protocol == 'mptcp' else None,
              'all_complete': all(r['returncode'] == 0 and r['result'] and
                                  r['result']['complete'] for r in statuses)}
    if all(s for s in servers):
        result['aggregate_throughput_seg_s'] = sum(s.get('throughput_seg_s', 0) for s in servers)
        result['mean_flow_delay_ms'] = sum(s.get('delay_mean_ms') or 0 for s in servers) / len(servers)
        result['mean_flow_p95_ms'] = sum(s.get('delay_p95_ms') or 0 for s in servers) / len(servers)
    (directory / 'run.json').write_text(json.dumps(result, indent=2))
    print('%s %s rep=%d complete=%s multipath=%s throughput=%.1f' %
          (scenario, protocol, rep, result['all_complete'], result['multipath_verified'],
           result.get('aggregate_throughput_seg_s', 0)), flush=True)
    return result


def main(args):
    flows = json.loads((OUT / 'topology.json').read_text())
    configure(flows)
    root = OUT / args.name
    root.mkdir(parents=True, exist_ok=False)
    metadata = {'kernel': platform.release(), 'cc': 'cubic', 'ecn': 1,
                'python': sys.version, 'started_unix': time.time(),
                'source_sha256': {name: hashlib.sha256((HERE / name).read_bytes()).hexdigest()
                    for name in ('native_stream.py', 'run_native_reference.py',
                                 'native_topology_bridge.py', 'run_mptcp.py',
                                 'simple_router_global.p4')},
                'scheduler': Path('/proc/sys/net/mptcp/scheduler').read_text().strip(),
                'flows': flows, 'independent_unit': 'complete experimental run',
                'exclusions': 'none; protocol failures are retained',
                'scope': 'native protocol reference; not paired with historical prototype',
                'results': []}
    scenarios = [args.scenario] if args.scenario else ['static', 'dynamic', 'fault']
    for scenario in scenarios:
        repeats = args.reps or (3 if scenario == 'dynamic' else 5)
        for rep in range(1, repeats + 1):
            modes = ['tcp', 'mptcp']
            random.Random(20260920 + rep).shuffle(modes)
            for mode in modes:
                metadata['results'].append(trial(flows, scenario, mode, rep, root))
                (root / 'results.json').write_text(json.dumps(metadata, indent=2))


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--name', default='reference_v1')
    p.add_argument('--scenario', choices=['static', 'dynamic', 'fault'])
    p.add_argument('--reps', type=int)
    main(p.parse_args())
