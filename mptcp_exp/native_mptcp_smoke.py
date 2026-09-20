#!/usr/bin/env python3
"""Two-path native MPTCP acceptance check, not a paper performance benchmark."""
import argparse
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve()
OUT = HERE.parent / 'results' / 'native_mptcp'


def run(*args):
    return subprocess.check_output(args, stderr=subprocess.STDOUT, text=True)


def worker(role):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 262)
    sock.settimeout(20)
    if role == 'server':
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(('0.0.0.0', 18462))
        sock.listen(1)
        conn, _ = sock.accept()
        received = 0
        while True:
            data = conn.recv(65536)
            if not data:
                break
            received += len(data)
        conn.sendall(str(received).encode() + b'\n')
        conn.close()
        print(json.dumps({'received_bytes': received}))
    else:
        sock.connect(('10.90.1.2', 18462))
        samples, sent = [], 0
        for _ in range(80):
            sock.sendall(b'X' * 16384)
            sent += 16384
            info = sock.getsockopt(284, 1, 256)
            # Linux UAPI mptcp_info starts with four uint8 fields; first is
            # number of additional subflows (initial subflow excluded).
            samples.append({'t': time.monotonic(), 'info_hex': info.hex(),
                            'additional_subflows': info[0]})
            time.sleep(0.1)
        sock.shutdown(socket.SHUT_WR)
        reply = sock.recv(100)
        result = {'sent_bytes': sent, 'received_bytes': int(reply.strip()),
                  'samples': samples,
                  'max_additional_subflows': max(s['additional_subflows'] for s in samples)}
        print(json.dumps(result))
    sock.close()


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    namespaces = ('nm-smoke-a', 'nm-smoke-b')
    existing = run('ip', 'netns', 'list')
    if any(n in existing for n in namespaces):
        raise RuntimeError('Smoke namespace already exists; inspect before retrying')
    created, server = [], None
    try:
        for ns in namespaces:
            run('ip', 'netns', 'add', ns)
            created.append(ns)
            run('ip', '-n', ns, 'link', 'set', 'lo', 'up')
            run('ip', 'netns', 'exec', ns, 'sysctl', '-w', 'net.mptcp.enabled=1')
            run('ip', '-n', ns, 'mptcp', 'limits', 'set', 'subflows', '4', 'add_addr_accepted', '4')
        for i in (1, 2):
            a, b = 'nm-a%d' % i, 'nm-b%d' % i
            run('ip', 'link', 'add', a, 'type', 'veth', 'peer', 'name', b)
            for ns, dev, last in ((namespaces[0], a, 1), (namespaces[1], b, 2)):
                run('ip', 'link', 'set', dev, 'netns', ns)
                run('ip', '-n', ns, 'addr', 'add', '10.90.%d.%d/24' % (i, last), 'dev', dev)
                run('ip', '-n', ns, 'link', 'set', dev, 'up')
        run('ip', '-n', namespaces[1], 'mptcp', 'endpoint', 'add', '10.90.2.2',
            'dev', 'nm-b2', 'signal')
        server = subprocess.Popen(['ip', 'netns', 'exec', namespaces[1], sys.executable,
                                   str(HERE), '--worker', 'server'], stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True)
        time.sleep(0.5)
        client = subprocess.run(['ip', 'netns', 'exec', namespaces[0], sys.executable,
                                 str(HERE), '--worker', 'client'], capture_output=True,
                                text=True, timeout=25)
        sout, serr = server.communicate(timeout=5)
        result = {'kind': 'native_protocol_smoke_only', 'kernel': run('uname', '-r').strip(),
                  'client_returncode': client.returncode, 'server_returncode': server.returncode,
                  'client_stderr': client.stderr, 'server_stderr': serr, 'server_stdout': sout}
        if client.returncode == 0:
            result['client'] = json.loads(client.stdout)
        else:
            result['client_stdout'] = client.stdout
        c = result.get('client', {})
        result['passed'] = (client.returncode == 0 and server.returncode == 0 and
                            c.get('max_additional_subflows', 0) >= 1 and
                            c.get('sent_bytes') == c.get('received_bytes'))
        (OUT / 'native_smoke.json').write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps({k: v for k, v in result.items() if k != 'client'}, indent=2))
        return 0 if result['passed'] else 1
    finally:
        if server is not None and server.poll() is None:
            server.kill()
            server.wait()
        for ns in reversed(created):
            subprocess.run(['ip', 'netns', 'del', ns], check=False)


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--worker', choices=['server', 'client'])
    args = p.parse_args()
    if args.worker:
        worker(args.worker)
    else:
        raise SystemExit(main())
