#!/usr/bin/env python2
"""
mptcp_tcp.py -- REAL kernel-TCP transport for the MPTCP experiment.

Each subflow is a genuine kernel TCP connection (SOCK_STREAM): the kernel
handles the 3-way handshake, ACK, retransmission and congestion window.
On top of TCP we carry a small app-layer header with DSN so the receiver
can reorder ACROSS subflows (kernel TCP only orders within one connection).

Segment (TCP payload):
  [2B flow_id][2B subflow_id][8B DSN][payload...]
  DSN = data sequence (connection-wide) -- used for cross-subflow reorder
  SSN = kernel TCP already numbers bytes; we also echo a per-subflow
        index in the header for stats (optional).

Note: we deliberately do NOT use kernel MPTCP; each subflow is an ordinary
TCP connection and we emulate the MPTCP data-plane (DSN mapping/reorder) in
the application, which is the paper's focus.
"""

import socket
import struct
import time

HDR = struct.Struct('>HHQ')   # flow_id, subflow_id, dsn


def pack_seg(flow_id, subflow_id, dsn, payload=b''):
    return HDR.pack(flow_id, subflow_id, dsn) + payload


def unpack_seg(data):
    if len(data) < HDR.size:
        return None
    fid, sid, dsn = HDR.unpack(data[:HDR.size])
    return (fid, sid, dsn, data[HDR.size:])


class TcpDsnReceiver(object):
    """Accepts one TCP connection per subflow on a port; reorders by DSN."""

    def __init__(self, port, n_subflows=1, timeout=15):
        self.port = port
        self.n_subflows = n_subflows
        self.timeout = timeout
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(('0.0.0.0', port))
        self.srv.listen(n_subflows)
        self.srv.settimeout(timeout)
        self.buf = {}
        self.next_dsn = 0
        self.received = 0
        self.dup = 0
        self.per_sub = {}
        self.ordered = []

    def recv_loop(self, duration):
        """Accept subflow connections, read until end-of-file or timeout,
        reorder by DSN."""
        end = time.time() + duration
        conns = []
        # accept connections (one per subflow)
        while time.time() < end and len(conns) < self.n_subflows:
            try:
                c, addr = self.srv.accept()
                c.settimeout(2)
                conns.append(c)
            except socket.timeout:
                break
        # read from all connections
        while time.time() < end:
            got = False
            for c in conns:
                try:
                    data = c.recv(4096)
                except socket.timeout:
                    continue
                except socket.error:
                    continue
                if not data:
                    continue
                got = True
                self._process(data)
            if not got:
                time.sleep(0.05)
        for c in conns:
            c.close()
        self.srv.close()
        return self.ordered

    def _process(self, data):
        # data may contain multiple or partial segments; for this demo we
        # send one segment per send(), so treat each recv() as one segment.
        seg = unpack_seg(data)
        if seg is None:
            return
        fid, sid, dsn, payload = seg
        self.received += 1
        self.per_sub[sid] = self.per_sub.get(sid, 0) + 1
        if dsn in self.buf:
            self.dup += 1
            return
        self.buf[dsn] = payload
        while self.next_dsn in self.buf:
            self.ordered.append((self.next_dsn, self.buf.pop(self.next_dsn)))
            self.next_dsn += 1

    def stats(self):
        return {'received': self.received,
                'ordered': len(self.ordered),
                'next_dsn': self.next_dsn,
                'dup': self.dup,
                'per_sub': self.per_sub,
                'in_buf': len(self.buf)}


class TcpSsnSender(object):
    """Sends one DSN-tagged segment over a REAL kernel TCP connection."""

    def __init__(self, dst_ip, port, subflow_id, sid_int=0):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(10)
        self.sock.connect((dst_ip, port))
        self.sid = subflow_id
        self.sid_int = sid_int

    def send_seg(self, flow_id, dsn, payload=b'Q' * 100):
        self.sock.send(pack_seg(flow_id, self.sid_int, dsn, payload))

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


def demo_send_recv(dst_ip, port, n_subflows, n_seg, sleep=0.01):
    """In-process demo: n_subflows real TCP senders -> one receiver."""
    # receiver thread
    import threading
    recv = TcpDsnReceiver(port, n_subflows=n_subflows, timeout=8)
    thr = threading.Thread(target=recv.recv_loop, args=(6,))
    thr.daemon = True
    thr.start()
    time.sleep(0.5)
    # senders
    senders = []
    for s in range(n_subflows):
        snd = TcpSsnSender(dst_ip, port, '%d' % s, sid_int=s)
        senders.append(snd)
    # interleave DSN across subflows so receiver must reorder
    for i in range(n_seg):
        for s, snd in enumerate(senders):
            dsn = i * n_subflows + s
            snd.send_seg(1, dsn, payload=b'P%03d' % dsn)
            time.sleep(sleep)
    time.sleep(1)
    for snd in senders:
        snd.close()
    thr.join(timeout=3)
    return recv


if __name__ == '__main__':
    print '=== demo: %d subflows over real kernel TCP ===' % 3
    r = demo_send_recv('127.0.0.1', 7800, 3, 5)
    print 'stats:', r.stats()
