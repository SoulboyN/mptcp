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

import json
import os
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
                    buf = self.buf
                payload = struct.pack('>QH', nxt, len(per_sub))
                for sid, cnt in sorted(per_sub.items()):
                    payload += struct.pack('>HI', sid, cnt)
                # SACK bitmap: which of [nxt, nxt+128) are missing (not in buf)
                lo = hi = 0
                for i in range(128):
                    if (nxt + i) not in buf:
                        if i < 64:
                            lo |= (1 << i)
                        else:
                            hi |= (1 << (i - 64))
                payload += struct.pack('>QQ', lo, hi)
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
            self._ticks = getattr(self, '_ticks', 0) + 1
            if self._ticks % 100 == 0:
                with self._ctl_lock:
                    _st = {'next_dsn': self.next_dsn,
                           'per_sub': dict(self.per_sub),
                           'in_buf': len(self.buf),
                           'ordered': len(self.ordered)}
                try:
                    with open('/tmp/mptcp_recv_live.json', 'w') as _f:
                        json.dump(_st, _f)
                except Exception:
                    pass
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
            if dsn in self.seen:
                self.dup += 1          # retransmission arrived after delivery
            else:
                self.seen.add(dsn)
                # count ONLY first-time DSNs per subflow so the sender's
                # recv_count -> in_flight is accurate (not inflated by retrans)
                self.per_sub[sid] = self.per_sub.get(sid, 0) + 1
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
    """One subflow socket plus its app-layer window state (SSN/credit axis):
    send/recv counts, RL-set cwnd, receiver-granted credit cap. The RL
    scheduler and the sender's window check drive these fields."""

    def __init__(self, dst_ip, port, subflow_id, sid_int=0, path='direct'):
        self.dst_ip = dst_ip
        self.port = port
        self.sid = subflow_id
        self.sid_int = sid_int
        self.path = path
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(3)
        self.sock.connect((dst_ip, port))
        # app-layer window state (SSN/credit axis)
        self.send_count = 0        # new DSNs sent on this subflow (stats)
        self.recv_count = 0        # receiver-reported received count (stats)
        self.cwnd = 16             # RL-set in-flight cap
        self.credit_limit = 20     # receiver-granted in-flight cap (ssn_credit_grant)
        self.assigned = []         # DSNs assigned here not yet ordered by recv
        self.recent = []           # recent DSNs for go-back-N replay

    @property
    def in_flight(self):
        # number of DSNs assigned to this subflow that the receiver has not
        # yet ordered (next_dsn >= dsn); retransmission crosses subflows, so
        # counting via assigned-DSNs is accurate (not per-subflow recv count)
        return len(self.assigned)

    def _effective_cwnd(self):
        return max(1, int(self.cwnd))

    def can_send(self):
        return self.in_flight < min(self._effective_cwnd(), self.credit_limit)

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

    def __init__(self, flow_id, dests, port, retry_interval=2.0, control_port=None,
                 policy_path=None, cc_mode='rl', fixed_cwnd=32):
        # dests: list of (dst_ip, sid_int, path)
        self.flow_id = flow_id
        self.port = port
        self.control_port = control_port or port
        self.cc_mode = cc_mode              # 'rl' | 'fixed' | 'aimd'
        self.fixed_cwnd = fixed_cwnd
        self.senders = []              # live subflow sockets
        self.dead = []                 # (dst_ip, sid_int, path) awaiting reconnect
        self.retry_interval = retry_interval
        self.max_dsn = 0               # highest DSN assigned so far
        self._last_retry = time.time()
        self._lock = threading.Lock()
        self._recv_counts = {}         # sid_int -> receiver received count
        self._recv_next = 0            # receiver next_dsn (for tail recovery)
        self._sack_missing = 0         # 128-bit missing-DSN bitmap from recv
        self._rl_counter = 0
        self._rl_state = 0
        for ipb, sid, path in dests:
            s = self._connect(ipb, sid, path)
            if s is None:
                print '  [sender] subflow %d connect failed' % sid
                self.dead.append((ipb, sid, path))
            else:
                self.senders.append(s)
        import mptcp_scheduler as sch
        self.scheduler = sch.RlScheduler(self.senders, policy_path=policy_path)
        ctl = threading.Thread(target=self._nak_loop)
        ctl.daemon = True
        ctl.start()

    def _connect(self, ipb, sid, path='direct'):
        try:
            return TcpSsnSender(ipb, self.port, '%d.%d' % (self.flow_id, sid),
                                sid_int=sid, path=path)
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
                       + [d[0] for d in self.dead])
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
                    lo, hi = struct.unpack('>QQ', data[off:off + 16])
                    sack = (hi << 64) | lo
                except Exception:
                    continue
                with self._lock:
                    self._recv_counts = dict(rcv)
                    self._recv_next = nxt
                    self._sack_missing = sack
                    # keep recv_count for stats; prune the subflow's assigned
                    # DSNs once the receiver has ordered them (nxt = next_dsn),
                    # so in_flight stays accurate while window-throttled.
                    for s in self.senders:
                        s.recv_count = rcv.get(s.sid_int, s.recv_count)
                        if s.assigned:
                            s.assigned = [d for d in s.assigned if d >= nxt]
                self._check_stalls(rcv)
                if nxt < maxd:
                    self._retransmit_range(nxt, maxd)
                break

    def _check_stalls(self, rcv):
        """Drop live subflows that delivered nothing new since the last poll
        while the sender is STILL feeding them (their DSNs are being silently
        swallowed by a dead connection). A subflow with no in-flight data is
        just window-throttled by RL/cwnd -- it must NOT be flagged."""
        with self._lock:
            live = list(self.senders)
        for s in live:
            sid = s.sid_int
            cnt = rcv.get(sid, 0)
            prev = self._last_rcv.get(sid, cnt)
            if s.in_flight <= 0:                 # not being fed right now
                self._stall_rounds[sid] = 0
                continue
            if cnt > prev:
                self._stall_rounds[sid] = 0
            else:
                self._stall_rounds[sid] = self._stall_rounds.get(sid, 0) + 1
            if self._stall_rounds[sid] >= 2:
                self._drop(s)
                self._stall_rounds[sid] = 0
        self._last_rcv = dict(rcv)

    def _retransmit_range(self, start, end):
        """Retransmit the gap; uses the receiver's 128-bit SACK bitmap to send
        ONLY the DSNs that are actually missing (avoids re-sending segments
        already delivered -> much lower dup). Beyond the bitmap window it
        falls back to an interval retransmit (bounded by NAK_BATCH)."""
        with self._lock:
            sack = self._sack_missing
        n = 0
        for i in range(min(128, end - start)):
            if sack & (1 << i):
                if self._retransmit(start + i):
                    n += 1
                time.sleep(self.NAK_PACE)
        limit = min(end, start + self.NAK_BATCH)
        for dsn in range(start + 128, limit):
            if self._retransmit(dsn):
                n += 1
            time.sleep(self.NAK_PACE)
        if n:
            print '  [sender] NAK: recovered %d missing DSN(s) from %d on healthy subflows' % (
                n, start)

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
            self.dead.append((s.dst_ip, s.sid_int, s.path))
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
            ipb, sid, path = self.dead.pop(0)
        s = self._connect(ipb, sid, path)
        if s is not None:
            with self._lock:
                self.senders.append(s)
            print '  [sender] subflow %d reconnected' % sid
        else:
            with self._lock:
                self.dead.append((ipb, sid, path))

    def send_next(self, dsn):
        """Assign dsn to a live subflow chosen by the RL scheduler; the send
        is gated by the subflow's window (in_flight < min(cwnd, credit)). If
        the chosen subflow fails, retransmit on a healthy one. Returns True
        if a subflow accepted it."""
        self._try_reconnect()
        self._rl_counter += 1
        if self._rl_counter % 20 == 0:
            self._rl_step()
        with self._lock:
            if not self.senders:
                return False
            live = list(self.senders)
        s = self._pick_subflow(live)
        if s is None:
            return False                    # windows full -> wait next round
        self.max_dsn = dsn + 1
        try:
            self._send_on(s, dsn)
            s.send_count += 1
            s.assigned.append(dsn)
            return True
        except Exception:
            self._drop(s)
            return self._retransmit(dsn)

    def _pick_subflow(self, live):
        """Prefer the RL-preferred (lowest pressure) subflow, skipping any
        whose window is full (cwnd or receiver-credit cap)."""
        weights = self.scheduler.path_weights()
        for sf in sorted(live, key=lambda sf: -weights.get(sf.sid, 0.0)):
            if sf.can_send():
                return sf
        return None

    def _rl_step(self):
        """One scheduling round by the selected CC mode: 'rl' reads global ECN
        and lets RlScheduler set cwnd/path from the policy; 'fixed' keeps a
        constant cwnd; 'aimd' does additive-increase / multiplicative-decrease
        on observed loss (pseudo-Reno). Exports sender state for the monitor."""
        try:
            import json as _json
            sw_ecn = {}
            try:
                with open('/tmp/ecn_global.json') as _f:
                    for k, v in _json.load(_f).items():
                        sw_ecn[int(k)] = float(v)
            except Exception:
                pass
            state = 0
            if self.cc_mode == 'fixed':
                for sf in list(self.senders):
                    sf.cwnd = self.fixed_cwnd
                print '  [cc] fixed cwnd=%d' % self.fixed_cwnd
            elif self.cc_mode == 'aimd':
                state = self._cc_aimd_step()
            else:
                with self._lock:
                    per_sub = dict(self._recv_counts)
                state, cwnds, _ = self.scheduler.step(sw_ecn, per_sub)
                self._rl_state = state
                print '  [rl] state=%d cwnd=%s' % (
                    state, {k: int(v) for k, v in cwnds.items()})
            with self._lock:
                live = list(self.senders)
            try:
                with open('/tmp/mptcp_sender_%d.json' % self.flow_id, 'w') as _f:
                    _json.dump({
                        'flow_id': self.flow_id,
                        'dsn_next': self.max_dsn,
                        'state': state,
                        'ecn': sw_ecn,
                        'subflows': {sf.sid_int: {
                            'path': sf.path, 'send': sf.send_count,
                            'ssn': sf.send_count,     # app-layer SSN (DSS axis)
                            'recv': sf.recv_count, 'cwnd': sf.cwnd,
                            'inflight': sf.in_flight,
                            'credit': sf.credit_limit,
                        } for sf in live},
                    }, _f)
            except Exception:
                pass
        except Exception as e:
            print '  [cc] step failed: %s' % e

    def _cc_aimd_step(self):
        """Additive-increase / multiplicative-decrease on observed loss
        (pseudo-Reno): if the receiver reports a gap, halve cwnd; else +1."""
        with self._lock:
            nxt = self._recv_next
            maxd = self.max_dsn
        for sf in list(self.senders):
            if maxd - nxt > 8:
                sf.cwnd = max(4, int(sf.cwnd / 2))
            else:
                sf.cwnd = min(64, int(sf.cwnd) + 1)
        print '  [cc] aimd cwnd=%s' % {s.sid_int: s.cwnd for s in self.senders}
        # coarse state for the monitor (based on gap size)
        return 2 if maxd - nxt > 8 else (1 if maxd - nxt > 2 else 0)

    def run_loop(self, stop_file=None, settle=3.0, cmd_file=None):
        """Main send loop with graceful shutdown: keep sending until stop_file
        appears (or KeyboardInterrupt); then stop assigning new DSNs, let the
        NAK thread + active tail recovery fill the remaining gap, and close
        the subflows (FIN) so the receiver drains a complete ordered stream.
        (Replaces a hard SIGTERM kill, which truncated the tail -> in_buf.)
        If cmd_file is given, it is a control channel for dynamic subflow
        management (MPTCP ADD_ADDR / REMOVE_ADDR simulation): lines like
        'add <sid> <dst_ip> <path>' or 'remove <sid>' are executed."""
        dsn = 0
        try:
            while True:
                if stop_file and os.path.exists(stop_file):
                    break
                if cmd_file and os.path.exists(cmd_file):
                    try:
                        with open(cmd_file) as _f:
                            lines = _f.read().strip().splitlines()
                        os.remove(cmd_file)
                        for line in lines:
                            self._exec_cmd(line)
                    except Exception:
                        pass
                self.send_next(dsn)
                dsn += 1
                time.sleep(0.01)
        except KeyboardInterrupt:
            pass
        if stop_file:
            deadline = time.time() + settle
            while time.time() < deadline:
                with self._lock:
                    nxt = self._recv_next
                    maxd = self.max_dsn
                if nxt >= maxd:
                    break                      # tail fully recovered
                self._retransmit_remaining()
                time.sleep(0.3)
        for s in list(self.senders):
            try:
                s.close()
            except Exception:
                pass
        print '  [sender] gracefully closed after %d DSNs (recv next=%d)' % (
            dsn, self._recv_next)

    def _exec_cmd(self, line):
        """Execute one control command: 'add <sid> <dst_ip> <path>' or
        'remove <sid>' -- dynamic subflow management (ADD_ADDR/REMOVE_ADDR)."""
        parts = line.split()
        if not parts:
            return
        if parts[0] == 'add' and len(parts) >= 4:
            self.add_subflow(parts[2], int(parts[1]), parts[3])
        elif parts[0] == 'remove' and len(parts) >= 2:
            self.remove_subflow(int(parts[1]))

    def add_subflow(self, dst_ip, sid, path):
        """Dynamically add a subflow (MPTCP ADD_ADDR simulation). Returns the
        new subflow or None on connect failure. The RlScheduler sees it next
        round (subflows list is shared)."""
        s = self._connect(dst_ip, sid, path)
        if s is None:
            print '  [sender] add subflow %d (%s) connect failed' % (sid, path)
            return None
        with self._lock:
            self.senders.append(s)
        print '  [sender] ADD_ADDR: subflow %d (%s) added' % (sid, path)
        return s

    def remove_subflow(self, sid):
        """Gracefully remove a subflow (MPTCP REMOVE_ADDR simulation)."""
        with self._lock:
            s = next((x for x in self.senders if x.sid_int == sid), None)
            if s is None:
                print '  [sender] remove subflow %d: not live' % sid
                return False
            self.senders.remove(s)
        try:
            s.close()
        except Exception:
            pass
        print '  [sender] REMOVE_ADDR: subflow %d (%s) removed' % (sid, s.path)
        return True

    def _retransmit_remaining(self):
        """Retransmit the gap [recv_next, max_dsn) on healthy subflows during
        graceful tail recovery (does NOT assign new DSNs)."""
        with self._lock:
            nxt = self._recv_next
            maxd = self.max_dsn
        if nxt < maxd:
            self._retransmit_range(nxt, maxd)


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
