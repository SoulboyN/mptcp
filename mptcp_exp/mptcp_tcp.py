#!/usr/bin/env python2
"""
mptcp_tcp.py -- REAL kernel-TCP transport for the MPTCP experiment.

Each subflow is a genuine kernel TCP connection (SOCK_STREAM): the kernel
handles the 3-way handshake, ACK, retransmission and congestion window.
On top of TCP we carry a small app-layer header with DSN so the receiver
can reorder ACROSS subflows (kernel TCP only orders within one connection).

Segment (TCP payload):
  [2B flow_id][2B subflow_id][8B DSN][2B payload_len][payload...]
  DSN = data sequence (connection-wide) -- used for cross-subflow reorder
  payload_len lets the receiver split a byte stream into segments even when
  TCP coalesces or splits them across recv boundaries.

Note: we deliberately do NOT use kernel MPTCP; each subflow is an ordinary
TCP connection and we emulate the MPTCP data-plane (DSN mapping/reorder) in
the application, which is the paper's focus.
"""

import select
import socket
import struct
import threading
import time

HDR = struct.Struct('>HHQH')   # flow_id, subflow_id, dsn, payload_len


def pack_seg(flow_id, subflow_id, dsn, payload=b''):
    return HDR.pack(flow_id, subflow_id, dsn, len(payload)) + payload


def unpack_seg(data):
    """Return (fid, sid, dsn, payload) only when a COMPLETE segment is
    present in data (header + declared payload); None if the segment is
    still partial (TCP can deliver a header and its payload separately)."""
    if len(data) < HDR.size:
        return None
    fid, sid, dsn, plen = HDR.unpack(data[:HDR.size])
    if len(data) < HDR.size + plen:
        return None
    payload = data[HDR.size:HDR.size + plen]
    return (fid, sid, dsn, payload)


class TcpDsnReceiver(object):
    """Accepts one TCP connection per subflow on a port; reorders by DSN."""

    def __init__(self, port, n_subflows=1, timeout=15, control_port=None):
        self.port = port
        self.n_subflows = n_subflows
        self.timeout = timeout
        self.control_port = control_port or port
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(('0.0.0.0', port))
        self.srv.listen(n_subflows)
        self.srv.settimeout(timeout)
        self.buf = {}
        self.seen = set()          # every DSN ever processed (dup protection)
        self.next_dsn = 0
        self._ctl_lock = threading.Lock()
        self.received = 0
        self.dup = 0
        self.per_sub = {}
        self.ordered = []

    def _run_control(self):
        """UDP control server: replies to NAK probes with the smallest missing
        DSN plus per-subflow received counts, so the sender can retransmit the
        gap on healthy subflows and detect silently-stuck subflows."""
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.bind(('0.0.0.0', self.control_port))
            s.settimeout(1)
            while True:
                try:
                    data, addr = s.recvfrom(64)
                except socket.timeout:
                    continue
                with self._ctl_lock:
                    nxt = self.next_dsn
                    per_sub = dict(self.per_sub)
                payload = struct.pack('>QH', nxt, len(per_sub))
                for sid, cnt in sorted(per_sub.items()):
                    payload += struct.pack('>HI', sid, cnt)
                s.sendto(payload, addr)
        except Exception:
            pass
        finally:
            try:
                s.close()
            except Exception:
                pass

    def recv_loop(self, duration):
        """Accept subflow connections and read data concurrently, reorder by
        DSN. Keeps reading even if a subflow connects late or never (e.g. a
        path was cut), so the remaining subflows are still delivered. Uses
        select() so reads happen immediately when data arrives (no 1s accept
        stall starving the read path)."""
        ctl = threading.Thread(target=self._run_control)
        ctl.daemon = True
        ctl.start()
        end = time.time() + duration
        self.srv.setblocking(0)
        conns = []              # each entry: [socket, partial-buffer]
        while time.time() < end:
            rlist = [self.srv] + [pair[0] for pair in conns]
            try:
                readable, _, _ = select.select(rlist, [], [], 0.5)
            except socket.error:
                break
            for s in readable:
                if s is self.srv:
                    try:
                        c, _ = s.accept()
                        c.setblocking(0)
                        conns.append([c, b''])
                    except socket.error:
                        pass
                    continue
                pair = next((p for p in conns if p[0] is s), None)
                if pair is None:
                    continue
                try:
                    data = s.recv(4096)
                except socket.error:
                    conns.remove(pair)
                    s.close()
                    continue
                if not data:
                    conns.remove(pair)
                    s.close()
                    continue
                pair[1] = self._drain(pair[1] + data)
            time.sleep(0.01)
        for pair in conns:
            pair[0].close()
        self.srv.close()
        return self.ordered

    def _drain(self, buf):
        """Parse as many complete segments as possible from buf (TCP may
        coalesce or split segments across recv boundaries); return leftover."""
        off = 0
        while True:
            seg = unpack_seg(buf[off:])
            if seg is None:
                break               # header/payload incomplete -> wait for more
            fid, sid, dsn, payload = seg
            self.received += 1
            self.per_sub[sid] = self.per_sub.get(sid, 0) + 1
            if dsn in self.seen:
                self.dup += 1          # retransmission arrived after delivery
            else:
                self.seen.add(dsn)
                self.buf[dsn] = payload
                with self._ctl_lock:
                    while self.next_dsn in self.buf:
                        self.ordered.append((self.next_dsn, self.buf.pop(self.next_dsn)))
                        self.next_dsn += 1
            off += HDR.size + len(payload)
        return buf[off:]

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
        self.dst_ip = dst_ip
        self.port = port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(3)
        self.sock.connect((dst_ip, port))
        self.sid = subflow_id
        self.sid_int = sid_int

    def send_seg(self, flow_id, dsn, payload=b'Q' * 100):
        self.sock.sendall(pack_seg(flow_id, self.sid_int, dsn, payload))

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


class MptcpGroupSender(object):
    """Fan-out sender with MPTCP-style resilience over REAL kernel TCP.

    - round-robins DSNs across the live subflows
    - when a subflow's send fails (path cut / broken connection), that DSN is
      retransmitted on a healthy subflow and the dead one is set aside
    - on path death the subflow's recent in-flight window is replayed on
      healthy subflows (go-back-N): data sitting in the kernel TCP buffer is
      lost on RST even though send() "succeeded", so replay recovers it
    - dead subflows are periodically reconnected, so a restored path resumes
      carrying data (e.g. after the interactive demo's "up <path>")
    """

    REPLAY_WIN = 20              # DSNs to replay per dead subflow
    NAK_BATCH = 500              # DSNs to retransmit per NAK round
    NAK_POLL = 0.7               # seconds between NAK polls
    NAK_PACE = 0.003             # seconds between retransmitted DSNs

    def __init__(self, flow_id, dests, port, retry_interval=2.0, control_port=None):
        # dests: list of (dst_ip, sid_int)
        self.flow_id = flow_id
        self.port = port
        self.control_port = control_port or port
        self.senders = []              # live subflow sockets
        self.dead = []                 # (dst_ip, sid_int) awaiting reconnect
        self.retry_interval = retry_interval
        self.max_dsn = 0               # highest DSN assigned so far
        self._last_retry = time.time()
        self._lock = threading.Lock()
        for ipb, sid in dests:
            s = self._connect(ipb, sid)
            if s is None:
                print '  [sender] subflow %d connect failed' % sid
                self.dead.append((ipb, sid))
            else:
                self.senders.append(s)
        ctl = threading.Thread(target=self._nak_loop)
        ctl.daemon = True
        ctl.start()

    def _connect(self, ipb, sid):
        try:
            return TcpSsnSender(ipb, self.port, '%d.%d' % (self.flow_id, sid),
                                sid_int=sid)
        except Exception:
            return None

    def _payload(self, dsn):
        return b'I%03d' % dsn

    def _send_on(self, s, dsn):
        s.send_seg(self.flow_id, dsn, payload=self._payload(dsn))
        recent = getattr(s, 'recent', None)
        if recent is None:
            s.recent = []
            recent = s.recent
        recent.append(dsn)
        if len(recent) > self.REPLAY_WIN:
            del recent[0]

    def _nak_loop(self):
        """Poll the receiver's UDP control port for its next_dsn (smallest
        missing DSN) + per-subflow received counts. Retransmit the gap on
        healthy subflows, and drop subflows whose receive count is not
        advancing (silently stuck after a path cut -- send() keeps succeeding
        into a dead connection)."""
        ctl = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        ctl.settimeout(1.5)
        self._last_rcv = {}         # sid -> received count at previous poll
        self._stall_rounds = {}     # sid -> consecutive non-advancing rounds
        while True:
            time.sleep(self.NAK_POLL)
            with self._lock:
                ips = ([s.dst_ip for s in self.senders]
                       + [ipb for ipb, _ in self.dead])
                maxd = self.max_dsn
            if not ips:
                continue
            for ip in ips:
                try:
                    ctl.sendto(b'NAK', (ip, self.control_port))
                    data, _ = ctl.recvfrom(64)
                    nxt, n = struct.unpack('>QH', data[:10])
                    rcv = {}
                    off = 10
                    for _ in range(n):
                        sid, cnt = struct.unpack('>HI', data[off:off + 6])
                        rcv[sid] = cnt
                        off += 6
                except Exception:
                    continue
                self._check_stalls(rcv)
                if nxt < maxd:
                    self._retransmit_range(nxt, maxd)
                break

    def _check_stalls(self, rcv):
        """Drop live subflows that delivered nothing new since the last poll
        (their DSNs are being silently swallowed by a dead connection)."""
        with self._lock:
            live = list(self.senders)
        for s in live:
            sid = s.sid_int
            cnt = rcv.get(sid, 0)
            prev = self._last_rcv.get(sid, cnt)
            if cnt > prev:
                self._stall_rounds[sid] = 0
            else:
                self._stall_rounds[sid] = self._stall_rounds.get(sid, 0) + 1
            if self._stall_rounds[sid] >= 2:
                self._drop(s)
                self._stall_rounds[sid] = 0
        self._last_rcv = dict(rcv)

    def _retransmit_range(self, start, end):
        limit = min(end, start + self.NAK_BATCH)
        n = 0
        for dsn in range(start, limit):
            if self._retransmit(dsn):
                n += 1
            time.sleep(self.NAK_PACE)
        if n:
            print '  [sender] NAK: recovered %d missing DSN(s) %d..%d on healthy subflows' % (
                n, start, limit - 1)

    def _retransmit(self, dsn):
        """Deliver dsn on a healthy subflow (round-robin); drop any that fail."""
        with self._lock:
            if not self.senders:
                return False
            targets = list(self.senders)
        s = targets[dsn % len(targets)]
        try:
            self._send_on(s, dsn)
            return True
        except Exception:
            self._drop(s)
            for t in targets:
                try:
                    self._send_on(t, dsn)
                    return True
                except Exception:
                    self._drop(t)
            return False

    def _drop(self, s):
        with self._lock:
            if s in self.senders:
                self.senders.remove(s)
            self.dead.append((s.dst_ip, s.sid_int))
        recent = list(getattr(s, 'recent', []))
        try:
            s.close()
        except Exception:
            pass
        print '  [sender] subflow %d lost; %d still up' % (
            s.sid_int, len(self.senders))
        # replay the subflow's in-flight window (its data may have been
        # sitting in the kernel TCP buffer and lost on the RST)
        if recent:
            n = 0
            for dsn in recent:
                if self._retransmit(dsn):
                    n += 1
                time.sleep(self.NAK_PACE)
            print '  [sender]   replayed %d in-flight DSN(s) of subflow %d' % (
                n, s.sid_int)

    def _try_reconnect(self):
        with self._lock:
            if not self.dead:
                return
            if time.time() - self._last_retry < self.retry_interval:
                return
            self._last_retry = time.time()
            ipb, sid = self.dead.pop(0)
        s = self._connect(ipb, sid)
        if s is not None:
            with self._lock:
                self.senders.append(s)
            print '  [sender] subflow %d reconnected' % sid
        else:
            with self._lock:
                self.dead.append((ipb, sid))

    def send_next(self, dsn):
        """Assign dsn to a live subflow; if that send fails, retransmit it on
        another healthy subflow. Returns True if a subflow accepted it."""
        self._try_reconnect()
        with self._lock:
            if not self.senders:
                return False
            s = self.senders[dsn % len(self.senders)]
        self.max_dsn = dsn + 1
        try:
            self._send_on(s, dsn)
            return True
        except Exception:
            self._drop(s)
            return self._retransmit(dsn)


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
