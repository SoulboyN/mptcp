#!/usr/bin/env python2
"""
mptcp_io.py -- DSN/SSN tagged UDP transport for the MPTCP experiment.

Packet format (UDP payload):
  [4B magic][2B flow_id][2B subflow_id][8B DSN][4B SSN][payload...]
  DSN = data sequence (connection-wide byte offset / segment index)
  SSN = subflow sequence (per-subflow segment index)

Sender assigns SSN per subflow and DSN across the flow. Receiver holds
out-of-order segments keyed by DSN and delivers an ordered stream.
"""

import socket
import struct
import time

MAGIC = 0x4d504354   # 'MPCT'
HDR = struct.Struct('>IHHII')


def pack_seg(flow_id, subflow_id, dsn, ssn, payload=b''):
    return HDR.pack(MAGIC, flow_id, subflow_id, dsn, ssn) + payload


def unpack_seg(data):
    if len(data) < HDR.size:
        return None
    magic, fid, sid, dsn, ssn = HDR.unpack(data[:HDR.size])
    if magic != MAGIC:
        return None
    return (fid, sid, dsn, ssn, data[HDR.size:])


class DsnReceiver(object):
    """Receiver: accepts segments from any subflow, reorders by DSN."""
    def __init__(self, port, timeout=10):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(('0.0.0.0', port))
        self.sock.settimeout(timeout)
        self.buf = {}          # dsn -> payload
        self.next_dsn = 0      # next expected DSN (contiguous)
        self.received = 0
        self.dup = 0
        self.per_sub = {}      # sid -> count
        self.ordered = []      # delivered (dsn, payload)

    def recv_loop(self, duration):
        end = time.time() + duration
        while time.time() < end:
            try:
                data, _ = self.sock.recvfrom(1500)
            except socket.timeout:
                break
            seg = unpack_seg(data)
            if seg is None:
                continue
            fid, sid, dsn, ssn, payload = seg
            self.received += 1
            self.per_sub[sid] = self.per_sub.get(sid, 0) + 1
            if dsn in self.buf:
                self.dup += 1
                continue
            self.buf[dsn] = payload
            # deliver contiguous prefix
            while self.next_dsn in self.buf:
                self.ordered.append((self.next_dsn, self.buf.pop(self.next_dsn)))
                self.next_dsn += 1
        return self.ordered

    def stats(self):
        return {'received': self.received,
                'ordered': len(self.ordered),
                'next_dsn': self.next_dsn,
                'dup': self.dup,
                'per_sub': self.per_sub,
                'in_buf': len(self.buf)}


class SsnSender(object):
    """Sender: assigns SSN per subflow; DSN handed out by the caller."""
    def __init__(self, dst_ip, port, subflow_id, sid_int=0, base_sleep=0.002):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.dst = (dst_ip, port)
        self.sid = subflow_id
        self.sid_int = sid_int
        self.base_sleep = base_sleep
        self.ssn = 0

    def send_seg(self, flow_id, dsn, payload=b'Q' * 100):
        data = pack_seg(flow_id, self.sid_int, dsn, self.ssn, payload)
        self.sock.sendto(data, self.dst)
        self.ssn += 1

    def send_n(self, flow_id, dsn_start, n, sleep=None):
        for i in range(n):
            self.send_seg(flow_id, dsn_start + i)
            time.sleep(self.base_sleep if sleep is None else sleep)
        return self.ssn
