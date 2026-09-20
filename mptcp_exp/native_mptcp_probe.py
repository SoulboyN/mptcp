#!/usr/bin/env python3
"""Read-only native MPTCP capability probe; never labels TCP fallback MPTCP."""
import argparse
import json
import platform
import socket
import subprocess
from pathlib import Path


def command(args):
    try:
        p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           universal_newlines=True, timeout=10)
        return {'returncode': p.returncode, 'output': p.stdout.strip()}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {'error': str(exc)}


def probe():
    result = {'kernel': platform.release(), 'platform': platform.platform(),
              'protocol': 262, 'socket_supported': False}
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM, 262):
            result['socket_supported'] = True
    except OSError as exc:
        result['socket_error'] = {'errno': exc.errno, 'message': str(exc)}
    for key in ('enabled', 'scheduler', 'pm_type', 'path_manager'):
        path = Path('/proc/sys/net/mptcp') / key
        result[key] = path.read_text().strip() if path.exists() else None
    result['ip_version'] = command(['ip', '-Version'])
    result['path_manager_tool'] = command(['ip', 'mptcp', 'limits', 'show'])
    result['tcp_congestion_control'] = command(['sysctl', '-n', 'net.ipv4.tcp_congestion_control'])
    result['note'] = ('Capability only. Experiments must additionally verify successful '
                      'MPTCP negotiation and at least two established kernel subflows.')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = probe()
    text = json.dumps(result, indent=2)
    print(text)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + '\n', encoding='utf-8')
    raise SystemExit(0 if result['socket_supported'] else 2)
