#!/usr/bin/env python3
"""Native TCP/MPTCP byte-stream workload, 1200 B payload and 14 B record header.

Latency is ordered application delivery since sendall submission, not the
prototype's per-subflow arrival latency. All samples and failures are retained.
"""
import argparse
import array
import json
import socket
import struct
import threading
import time
from pathlib import Path

HEADER = struct.Struct('>HHQH')
RECORD = 1214


def dump(path, data):
    tmp = Path(str(path) + '.tmp')
    tmp.write_text(json.dumps(data))
    tmp.replace(path)


def mptcp_info(sock):
    info = sock.getsockopt(284, 1, 256)
    # Linux v6.8 UAPI: six uint8 fields, two bytes padding, then flags.
    flags = struct.unpack_from('=I', info, 8)[0] if len(info) >= 12 else None
    return dict(additional_subflows=info[0], flags=flags,
                fallback=bool(flags & 1) if flags is not None else None,
                total_subflows=info[80] if len(info) >= 81 else None,
                bytes_retrans=struct.unpack_from('=Q', info, 48)[0] if len(info) >= 56 else None,
                raw_hex=info.hex())


def main(args):
    output = Path(args.output)
    data = {'role': args.role, 'protocol': args.protocol, 'payload_bytes': 1200,
            'record_bytes': RECORD, 'start': args.start, 'seconds': args.seconds,
            'latency_definition': 'ordered application record delivery minus sendall submission',
            'samples': [], 'complete': False}
    proto = 262 if args.protocol == 'mptcp' else socket.IPPROTO_TCP
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM, proto)
    sock.settimeout(25)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    stop = threading.Event()
    monitor = None
    listener = None
    try:
        if args.role == 'server':
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(('0.0.0.0', args.port))
            sock.listen(1)
            dump(str(output) + '.ready', {'ready': True})
            conn, address = sock.accept()
            # MP_JOIN SYNs still need the listening port after initial accept.
            listener = sock
            sock = conn
            sock.settimeout(25)
            data['peer'] = address
        else:
            if args.protocol == 'tcp':
                sock.bind((args.source, 0))
            sock.connect((args.dest, args.port))

        def sample():
            while not stop.wait(0.25):
                row = {'t': time.monotonic() - args.start}
                if args.protocol == 'mptcp':
                    try:
                        row.update(mptcp_info(sock))
                    except OSError as exc:
                        row['error'] = str(exc)
                data['samples'].append(row)

        monitor = threading.Thread(target=sample, daemon=True)
        monitor.start()
        if args.role == 'client':
            while time.monotonic() < args.start:
                time.sleep(0.005)
            sent = 0
            while time.monotonic() < args.start + args.seconds:
                if args.target and sent >= args.target:
                    break
                payload = b'TS' + struct.pack('!d', time.monotonic()) + b'X' * 1190
                sock.sendall(HEADER.pack(1, 0, sent, 1200) + payload)
                sent += 1
                if args.rate:
                    delay = args.start + sent / args.rate - time.monotonic()
                    if delay > 0:
                        time.sleep(delay)
            data['sent_records'] = sent
            data['send_elapsed_s'] = time.monotonic() - args.start
            sock.shutdown(socket.SHUT_WR)
            reply = sock.recv(128)
            data['receiver_records'] = int(reply.strip())
            data['complete'] = data['receiver_records'] == sent and (not args.target or sent == args.target)
        else:
            buf, count, in_window = b'', 0, 0
            phase_counts = [0, 0, 0]
            delays = array.array('d')
            last = None
            max_gap = 0.0
            first_after_fault = None
            last_write = 0.0
            while True:
                chunk = sock.recv(256 * 1024)
                if not chunk:
                    break
                buf += chunk
                limit = len(buf) // RECORD * RECORD
                for pos in range(0, limit, RECORD):
                    fid, sid, seq, length = HEADER.unpack_from(buf, pos)
                    if length != 1200 or seq != count:
                        raise RuntimeError('Record corruption or ordering failure')
                    now = time.monotonic()
                    stamp = struct.unpack_from('!d', buf, pos + HEADER.size + 2)[0]
                    elapsed = now - args.start
                    if last is not None:
                        max_gap = max(max_gap, elapsed - last)
                    if args.fault_at >= 0 and elapsed >= args.fault_at and first_after_fault is None:
                        first_after_fault = elapsed
                    count += 1
                    last = elapsed
                    if 0 <= elapsed < args.seconds:
                        in_window += 1
                        phase_counts[min(2, int(elapsed / (args.seconds / 3)))] += 1
                        delays.append((now - stamp) * 1000)
                buf = buf[limit:]
                if time.monotonic() - last_write >= 0.25:
                    dump(str(output) + '.progress', {'records': count, 't': last})
                    last_write = time.monotonic()
            if buf:
                raise RuntimeError('Truncated final record')
            sock.sendall(str(count).encode() + b'\n')
            ordered = sorted(delays)
            data.update(received_records=count, records_in_window=in_window,
                        throughput_seg_s=in_window / args.seconds,
                        phase_throughput_seg_s=[n / (args.seconds / 3) for n in phase_counts],
                        last_delivery_s=last, complete=True,
                        max_delivery_gap_s=max_gap,
                        first_delivery_after_fault_s=first_after_fault,
                        delay_mean_ms=sum(ordered) / len(ordered) if ordered else None,
                        delay_p95_ms=ordered[round((len(ordered) - 1) * .95)] if ordered else None,
                        delay_p99_ms=ordered[round((len(ordered) - 1) * .99)] if ordered else None)
    except Exception as exc:
        data['error'] = '%s: %s' % (type(exc).__name__, exc)
    finally:
        stop.set()
        if monitor:
            monitor.join(1)
        sock.close()
        if listener is not None:
            listener.close()
        dump(output, data)
    return 0 if data['complete'] else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('role', choices=['server', 'client'])
    parser.add_argument('--protocol', choices=['tcp', 'mptcp'], required=True)
    parser.add_argument('--source', default='0.0.0.0')
    parser.add_argument('--dest', default='127.0.0.1')
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--start', type=float, required=True)
    parser.add_argument('--seconds', type=float, default=9)
    parser.add_argument('--target', type=int, default=0)
    parser.add_argument('--rate', type=float, default=0)
    parser.add_argument('--fault-at', type=float, default=-1)
    parser.add_argument('--output', required=True)
    raise SystemExit(main(parser.parse_args()))
