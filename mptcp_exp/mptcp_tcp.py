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
        self.delays_ms = []

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

    def recv_loop(self, duration, idle_after_close=2.0):
        """Accept subflow connections and read data concurrently, reorder by
        DSN. Keeps reading even if a subflow connects late or never (e.g. a
        path was cut), so the remaining subflows are still delivered. Uses
        select() so reads happen immediately when data arrives (no 1s accept
        stall starving the read path)."""
        ctl = threading.Thread(target=self._run_control)
        ctl.daemon = True
        ctl.start()
        end = time.time() + duration
        last_data_at = time.time()
        accepted_any = False
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
                        accepted_any = True
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
                last_data_at = time.time()
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
            if (accepted_any and not conns and
                    time.time() - last_data_at >= idle_after_close):
                break
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
            if len(payload) >= 10 and payload[:2] == b'TS':
                try:
                    sent_at = struct.unpack('!d', payload[2:10])[0]
                    delay_ms = max(0.0, (time.time() - sent_at) * 1000.0)
                    self.delays_ms.append(delay_ms)
                except Exception:
                    pass
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
        delays = sorted(self.delays_ms)
        def _pct(p):
            if not delays:
                return 0.0
            idx = int(round((len(delays) - 1) * p))
            return delays[max(0, min(len(delays) - 1, idx))]
        return {'received': self.received,
                'ordered': len(self.ordered),
                'next_dsn': self.next_dsn,
                'dup': self.dup,
                'per_sub': self.per_sub,
                'in_buf': len(self.buf),
                'delay_mean_ms': (sum(delays) / len(delays)) if delays else 0.0,
                'delay_p95_ms': _pct(0.95),
                'delay_p99_ms': _pct(0.99)}


class TcpSsnSender(object):
    """One subflow socket plus its app-layer window state (SSN/credit axis):
    send/recv counts, RL-set cwnd, receiver-granted credit cap. The RL
    scheduler and the sender's window check drive these fields."""

    def __init__(self, dst_ip, port, subflow_id, sid_int=0, path='direct',
                 cwnd=16, credit_limit=20):
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
        self.cwnd = cwnd           # RL-set in-flight cap
        self.credit_limit = credit_limit  # receiver-granted in-flight cap
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
    NAK_POLL = 0.3               # seconds between NAK/SACK polls
    NAK_PACE = 0.003             # seconds between retransmitted DSNs
    PAYLOAD_BYTES = 1200         # meaningful link load; below Ethernet MTU

    # RTT per path (matches run_mptcp.py's tc netem: sw1 WiFi 10ms, sw2 cell
    # 30ms, sw3 fiber 2ms, direct unshaped). Used by LIA/OLIA coupled CC.
    PATH_RTT = {'direct': 1.0, 'sw1': 10.0, 'sw2': 30.0, 'sw3': 2.0}

    def __init__(self, flow_id, dests, port, retry_interval=2.0, control_port=None,
                 policy_path=None, cc_mode='rl', fixed_cwnd=32,
                 credit_limit=20, pace=0.01, cc_period=0.3,
                 stages=None, round_robin=False,
                 auto_drop_sid=None, auto_drop_at=None):
        # dests: list of (dst_ip, sid_int, path)
        self.flow_id = flow_id
        self.port = port
        self.control_port = control_port or port
        self.cc_mode = cc_mode              # 'rl' | 'local' | 'fixed' | 'lia' | 'olia' | 'aimd'
        self.fixed_cwnd = fixed_cwnd
        self.credit_limit = credit_limit    # receiver-granted in-flight cap
        self.pace = pace                    # run_loop poll when window full (s)
        self.cc_period = cc_period          # CC scheduling period (s)
        self.round_robin = round_robin      # ablation: pick subflows in turn
        self.auto_drop_sid = auto_drop_sid  # ablation: sid to kill at auto_drop_at
        self.auto_drop_at = auto_drop_at    # seconds after run_loop start
        self._dropped_auto = False
        self.no_reconnect = set()           # sids held down by the ablation
        # resilience-layer switches for the ablation study (stage0..stage3)
        self.stages = {'replay': 1, 'nak': 1, 'stall': 1, 'tail': 1}
        if stages:
            self.stages.update(stages)
        self.senders = []              # live subflow sockets
        self.dead = []                 # (dst_ip, sid_int, path) awaiting reconnect
        self.retry_interval = retry_interval
        self.max_dsn = 0               # highest DSN assigned so far
        self._last_retry = time.time()
        self._lock = threading.Lock()
        self._recv_counts = {}         # sid_int -> receiver received count
        self._recv_total = 0           # receiver unique-received watermark
        self._recv_next = 0            # receiver next_dsn (for tail recovery)
        self._sack_missing = 0         # 128-bit missing-DSN bitmap from recv
        self._recv_deltas = {}         # sid_int -> delivered delta per NAK poll
        self._prev_send = {}           # sid_int -> send_count at last CC step
        self._rl_counter = 0
        self._rl_state = 0
        self._rr = 0
        self._last_cc_t = time.time()
        self._last_cc_print = 0.0      # throttle CC console prints
        self._recv_epoch = 0           # bumped each NAK response received
        self._cc_epoch_done = -1       # last CC step's recv_epoch
        for ipb, sid, path in dests:
            s = self._connect(ipb, sid, path)
            if s is None:
                print '  [sender] subflow %d connect failed' % sid
                self.dead.append((ipb, sid, path))
            else:
                self.senders.append(s)
        import mptcp_scheduler as sch
        self.scheduler = sch.RlScheduler(self.senders, policy_path=policy_path)
        if self.cc_mode == 'local':
            # Local ECN/Credit baseline without a learned residual. This is
            # the causal control needed to isolate the Q-table contribution.
            self.scheduler.policy_residual = [2, 2, 2, 2, 2]
        ctl = threading.Thread(target=self._nak_loop)
        ctl.daemon = True
        ctl.start()
        dg = threading.Thread(target=self._diag_loop)
        dg.daemon = True
        dg.start()

    def _connect(self, ipb, sid, path='direct'):
        try:
            return TcpSsnSender(ipb, self.port, '%d.%d' % (self.flow_id, sid),
                                sid_int=sid, path=path,
                                cwnd=16, credit_limit=self.credit_limit)
        except Exception:
            return None

    def _payload(self, dsn):
        # All namespaces share the host clock, so this embedded timestamp
        # yields end-to-end application delivery latency for every segment.
        head = b'TS' + struct.pack('!d', time.time()) + (b'I%03d' % dsn)
        return head + (b'Q' * max(0, self.PAYLOAD_BYTES - len(head)))

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
        nak_prev_nxt = None
        nak_prev_maxd = None
        nak_prev_total = None
        nak_stable = 0
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
                    self._recv_total = sum(rcv.values())
                    self._recv_next = nxt
                    self._sack_missing = sack
                    # Prune a subflow's assigned DSNs once the receiver has
                    # RECEIVED them (unique-count watermark = sum of per_sub),
                    # NOT once it has ordered them (next_dsn). next_dsn stalls
                    # on a reorder gap, and coupling the window to it froze the
                    # whole connection when one subflow went slow/lossy; the
                    # received watermark advances with any delivery, so an
                    # out-of-order backlog keeps the window flowing.
                    for s in self.senders:
                        s.recv_count = rcv.get(s.sid_int, s.recv_count)
                        if s.assigned:
                            s.assigned = [d for d in s.assigned
                                          if d >= self._recv_total]
                with self._lock:
                    # deltas must be computed against the PREVIOUS poll's
                    # _last_rcv; _check_stalls below replaces _last_rcv with the
                    # current rcv, so do this first.
                    self._recv_deltas = {
                        sid: cnt - self._last_rcv.get(sid, 0)
                        for sid, cnt in rcv.items()}
                self._check_stalls(rcv)
                with self._lock:
                    self._recv_epoch += 1
                # Reordering across 1/10/30-ms paths temporarily looks like a
                # loss.  Require the same gap and send watermark to persist
                # for three polls before retransmitting; otherwise normal
                # in-flight packets are duplicated by an eager NAK.
                rcv_total = sum(rcv.values())
                if (nxt == nak_prev_nxt and maxd == nak_prev_maxd and
                        rcv_total == nak_prev_total):
                    nak_stable += 1
                else:
                    nak_stable = 0
                nak_prev_nxt, nak_prev_maxd, nak_prev_total = (
                    nxt, maxd, rcv_total)
                if (self.stages.get('nak', 1) and nxt < maxd and
                        nak_stable >= 6):
                    self._retransmit_range(nxt, maxd)
                    nak_stable = 0
                break

    def _diag_loop(self):
        """Per-second sender-state log (one line each): per-subflow window
        state + receiver next_dsn + SACK missing count, to /tmp. Lets a stalled
        flow be diagnosed after the fact (which subflow blocks next_dsn)."""
        t0 = time.time()
        while True:
            time.sleep(1.0)
            with self._lock:
                live = [(s.sid_int, s.path, int(s.cwnd), len(s.assigned),
                         s.send_count, s.recv_count) for s in self.senders]
                dead = [(d[1], d[2]) for d in self.dead]
                nxt = self._recv_next
                rtot = self._recv_total
                maxd = self.max_dsn
                sack = self._sack_missing
            import math
            miss = bin(sack).count('1')
            f = '/tmp/mptcp_sender_%d_live.log' % self.flow_id
            try:
                with open(f, 'a') as _f:
                    _f.write('t=%5.1f dsn=%d rcv=%d next=%d miss=%d live=%s dead=%s\n' % (
                        time.time() - t0, maxd, rtot, nxt, miss,
                        live, [('%d.%s' % (self.flow_id, sid)) for sid, _ in dead]))
            except Exception:
                pass

    def _check_stalls(self, rcv):
        """Drop live subflows that delivered nothing new since the last poll
        while the sender is STILL feeding them (their DSNs are being silently
        swallowed by a dead connection). A subflow with no in-flight data is
        just window-throttled by RL/cwnd -- it must NOT be flagged. Gated by
        the 'stall' stage for the resilience ablation study."""
        if not self.stages.get('stall', 1):
            self._last_rcv = dict(rcv)
            return
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
        # Do not blindly retransmit beyond the receiver's 128-bit SACK
        # horizon.  Those DSNs are unknown, not missing; treating them as
        # losses caused hundreds of avoidable duplicates.  Once the current
        # holes are filled, the next poll advances the horizon naturally.
        if n:
            print '  [sender] NAK: recovered %d missing DSN(s) from %d on healthy subflows' % (
                n, start)

    def _retransmit(self, dsn):
        """Deliver dsn on a healthy subflow; prefer the least-loaded (fewest
        assigned) so retransmits spread instead of piling onto one slow/lossy
        path, and drop any that fail."""
        with self._lock:
            if not self.senders:
                return False
            targets = sorted(self.senders, key=lambda s: len(s.assigned))
        for s in targets:
            try:
                self._send_on(s, dsn)
                return True
            except Exception:
                self._drop(s)
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
        # sitting in the kernel TCP buffer and lost on the RST). Gated by the
        # 'replay' stage for the resilience ablation study.
        if self.stages.get('replay', 1) and recent:
            n = 0
            for dsn in recent:
                if self._retransmit(dsn):
                    n += 1
                time.sleep(self.NAK_PACE)
            print '  [sender]   replayed %d in-flight DSN(s) of subflow %d' % (
                n, s.sid_int)
        # ablated subflows stay down (no reconnect) so the recovery layers
        # must actively fill the gap, not wait for the path to come back
        if self.auto_drop_sid is not None and s.sid_int == self.auto_drop_sid:
            try:
                self.dead.remove((s.dst_ip, s.sid_int, s.path))
            except Exception:
                pass
            self.no_reconnect.add(s.sid_int)

    def _try_reconnect(self):
        with self._lock:
            if not self.dead:
                return
            if time.time() - self._last_retry < self.retry_interval:
                return
            # reconnect only subflows NOT held down by the ablation
            candidates = [d for d in self.dead if d[1] not in self.no_reconnect]
            if not candidates:
                return
            self._last_retry = time.time()
            ipb, sid, path = candidates.pop(0)
            self.dead.remove((ipb, sid, path))
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
        now = time.time()
        self._rl_counter += 1
        if self.cc_mode in ('rl', 'local'):
            # time-based: RL reads the global ECN view, refreshed independently
            if now - self._last_cc_t >= self.cc_period:
                self._last_cc_t = now
                self._rl_step()
        else:
            # loss-based CC (lia/olia/aimd) must not step faster than the NAK
            # feedback loop: _recv_deltas refreshes once per NAK poll, and a
            # step between polls sees sent>0 but recv_delta=0 -> spurious
            # "congestion" that halves cwnd to the floor.
            with self._lock:
                ep = self._recv_epoch
            if ep != self._cc_epoch_done:
                self._cc_epoch_done = ep
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
        whose window is full (cwnd or receiver-credit cap). In round-robin
        mode (used by the resilience ablation) subflows are picked in turn so
        every path carries data and a path cut actually strands in-flight
        DSNs."""
        if self.round_robin:
            for _ in range(len(live)):
                s = live[self._rr % len(live)]
                self._rr += 1
                if s.can_send():
                    return s
            return None
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
                self._dbg('  [cc] fixed cwnd=%d' % self.fixed_cwnd)
            elif self.cc_mode in ('lia', 'olia'):
                state = self._cc_lia_step()
            elif self.cc_mode == 'aimd':
                state = self._cc_aimd_step()
            else:
                with self._lock:
                    per_sub = dict(self._recv_counts)
                state, cwnds, _ = self.scheduler.step(sw_ecn, per_sub)
                self._rl_state = state
                self._dbg('  [rl] state=%d cwnd=%s' % (
                    state, {k: int(v) for k, v in cwnds.items()}))
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

    def _dbg(self, msg):
        """Throttled console print for per-step CC messages (they fire every
        cc_period when pacing is removed; printing every step floods logs)."""
        now = time.time()
        if now - self._last_cc_print >= 2.0:
            self._last_cc_print = now
            print msg

    def _cc_lia_step(self):
        """MPTCP LIA / OLIA coupled congestion control (RFC 6356).

        Each scheduling step: a subflow that delivered less than half of what
        it sent since the last NAK poll (recv_delta vs send delta) is treated
        as congested and its cwnd is halved; otherwise the window grows with
        the RFC 6356 coupled increase
            alpha = cwnd_total * max_i(cwnd_i/rtt_i^2) / (sum_i cwnd_i/rtt_i)^2
            cwnd_i += min(alpha / cwnd_total, 1 / cwnd_i)
        so a fast path's share throttles the whole connection's growth (the
        signature of MPTCP coupled congestion control). OLIA adds an
        opportunistic boost for the best path (largest cwnd/rtt)."""
        with self._lock:
            live = list(self.senders)
            recv_d = dict(self._recv_deltas)
        if not live:
            return 0
        cw = {s.sid_int: max(1.0, float(s.cwnd)) for s in live}
        tot = float(sum(cw.values())) or 1.0
        rtt = lambda s: self.PATH_RTT.get(s.path, 1.0)
        share = lambda s: cw[s.sid_int] / rtt(s)
        best_share = max(share(s) for s in live)
        alpha = (tot * max(cw[s.sid_int] / (rtt(s) ** 2) for s in live)
                 / (sum(share(s) for s in live) ** 2 or 1.0))
        congested = 0
        for s in live:
            sid = s.sid_int
            sent_d = s.send_count - self._prev_send.get(sid, s.send_count)
            self._prev_send[sid] = s.send_count
            if sent_d > 0 and recv_d.get(sid, 0) < 0.5 * sent_d:
                s.cwnd = max(4.0, s.cwnd / 2)          # congested subflow
                congested += 1
                continue
            inc = min(alpha / tot, 1.0 / cw[sid])
            if self.cc_mode == 'olia' and share(s) >= best_share - 1e-9:
                inc = max(inc, 1.0 / cw[sid])           # best-path boost
            s.cwnd = min(128.0, s.cwnd + inc)
        self._dbg('  [cc] %s cwnd=%s' % (
            self.cc_mode, {s.sid_int: int(s.cwnd) for s in live}))
        return 2 if congested else (1 if any(s.cwnd > 20 for s in live) else 0)

    def _cc_aimd_step(self):
        """Pseudo-Reno (AIMD) on per-subflow delivered rate: a subflow whose
        delivered rate is < half its send rate since the last NAK poll is
        congested -> cwnd halved; otherwise cwnd grows by 1 (additive
        increase). Same congestion signal as LIA, so loss (not reorder) drives
        the decrease."""
        with self._lock:
            live = list(self.senders)
            recv_d = dict(self._recv_deltas)
        congested = 0
        for s in live:
            sid = s.sid_int
            sent_d = s.send_count - self._prev_send.get(sid, s.send_count)
            self._prev_send[sid] = s.send_count
            if sent_d > 0 and recv_d.get(sid, 0) < 0.5 * sent_d:
                s.cwnd = max(4, int(s.cwnd / 2))
                congested += 1
            else:
                s.cwnd = min(64, int(s.cwnd) + 1)
        self._dbg('  [cc] aimd cwnd=%s' % {s.sid_int: s.cwnd for s in live})
        return 2 if congested else (1 if any(s.cwnd > 20 for s in live) else 0)

    def run_loop(self, stop_file=None, settle=3.0, cmd_file=None,
                 max_segments=None, progress_file=None, pause_at=None,
                 resume_file=None):
        """Main send loop with graceful shutdown: keep sending until stop_file
        appears (or KeyboardInterrupt); then stop assigning new DSNs, let the
        NAK thread + active tail recovery fill the remaining gap, and close
        the subflows (FIN) so the receiver drains a complete ordered stream.
        (Replaces a hard SIGTERM kill, which truncated the tail -> in_buf.)
        If cmd_file is given, it is a control channel for dynamic subflow
        management (MPTCP ADD_ADDR / REMOVE_ADDR simulation): lines like
        'add <sid> <dst_ip> <path>' or 'remove <sid>' are executed.
        DSN advances only when a subflow accepted it (no holes when the window
        is full); when all windows are full we poll at self.pace so the send
        rate is governed by in_flight < min(cwnd, credit), not a fixed timer."""
        dsn = 0
        paused_once = False
        _t0 = time.time()
        if cmd_file:
            cmd_thr = threading.Thread(target=self._command_loop,
                                       args=(cmd_file,))
            cmd_thr.daemon = True
            cmd_thr.start()
        try:
            while True:
                if stop_file and os.path.exists(stop_file):
                    break
                if max_segments is not None and dsn >= max_segments:
                    break
                # Deterministic experiment barrier.  The controller can pause
                # assignment at an exact DSN, inject a physical link fault,
                # wait until the sender has closed the failed socket, and only
                # then release the workload.  This prevents a short workload
                # from being queued entirely in the kernel before the fault.
                if (pause_at is not None and not paused_once and
                        dsn >= pause_at and resume_file):
                    paused_once = True
                    if progress_file:
                        try:
                            with open(progress_file, 'w') as _f:
                                _f.write(str(dsn))
                        except Exception:
                            pass
                    while not os.path.exists(resume_file):
                        time.sleep(0.01)
                    # A fault-detection-window command may have assigned DSNs
                    # directly to the now-black-holed subflow while this main
                    # loop was paused.  Continue after that reserved range.
                    with self._lock:
                        dsn = max(dsn, self.max_dsn)
                # ablation: kill a subflow at a fixed time to simulate a path
                # drop (its in-flight window is recovered only if the replay/
                # nak/tail layers are enabled for this stage)
                if (self.auto_drop_sid is not None and self.auto_drop_at is not None
                        and not self._dropped_auto
                        and time.time() - _t0 >= self.auto_drop_at):
                    self._dropped_auto = True
                    with self._lock:
                        tgt = next((s for s in self.senders
                                    if s.sid_int == self.auto_drop_sid), None)
                    if tgt is not None:
                        # force RST (linger=0) so in-flight data in the kernel
                        # buffer is genuinely lost, like a real path drop;
                        # a graceful close() would drain it and hide the gap
                        try:
                            import struct as _st
                            tgt.sock.setsockopt(
                                socket.SOL_SOCKET, socket.SO_LINGER,
                                _st.pack('ii', 1, 0))
                        except Exception:
                            pass
                        self._drop(tgt)
                        print '  [abl] dropped subflow %d (t=%.1fs, assigned=%d)' % (
                            self.auto_drop_sid, time.time() - _t0,
                            len(getattr(tgt, 'assigned', [])))
                if self.send_next(dsn):
                    dsn += 1
                    if progress_file and dsn % 10 == 0:
                        try:
                            with open(progress_file, 'w') as _f:
                                _f.write(str(dsn))
                        except Exception:
                            pass
                else:
                    time.sleep(self.pace)
        except KeyboardInterrupt:
            pass
        if stop_file or max_segments is not None:
            if self.stages.get('tail', 1):
                deadline = time.time() + settle
                while time.time() < deadline:
                    with self._lock:
                        nxt = self._recv_next
                        maxd = self.max_dsn
                    if nxt >= maxd:
                        break                  # tail fully recovered
                    self._retransmit_remaining()
                    time.sleep(0.3)
            elif self.stages.get('nak', 1):
                # NAK-only ablation: keep healthy sockets open long enough for
                # the background SACK loop to repair a persistent gap.  This
                # is passive convergence; the full stage below additionally
                # performs active tail recovery.
                deadline = time.time() + settle
                while time.time() < deadline:
                    with self._lock:
                        if self._recv_next >= self.max_dsn:
                            break
                    time.sleep(0.1)
        for s in list(self.senders):
            try:
                s.close()
            except Exception:
                pass
        print '  [sender] gracefully closed after %d DSNs (recv next=%d)' % (
            dsn, self._recv_next)

    def _command_loop(self, cmd_file):
        """Process SDN control commands even if the send thread is blocked."""
        while True:
            if os.path.exists(cmd_file):
                try:
                    with open(cmd_file) as _f:
                        lines = _f.read().strip().splitlines()
                    os.remove(cmd_file)
                    for line in lines:
                        self._exec_cmd(line)
                    with open(cmd_file + '.ack', 'w') as _f:
                        _f.write('ok')
                except Exception:
                    pass
            time.sleep(0.05)

    def _exec_cmd(self, line):
        """Execute ADD/REMOVE or a controller-confirmed path failure."""
        parts = line.split()
        if not parts:
            return
        if parts[0] == 'add' and len(parts) >= 4:
            self.add_subflow(parts[2], int(parts[1]), parts[3])
        elif parts[0] == 'remove' and len(parts) >= 2:
            self.remove_subflow(int(parts[1]))
        elif parts[0] == 'fail' and len(parts) >= 2:
            sid = int(parts[1])
            with self._lock:
                s = next((x for x in self.senders if x.sid_int == sid), None)
            if s is not None:
                assigned = len(s.assigned)
                # Abort rather than gracefully close: a normal close keeps
                # unacknowledged TCP data in the kernel and may retransmit it
                # after the experiment restores the path, masking the fault.
                try:
                    import struct as _st
                    s.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                                      _st.pack('ii', 1, 0))
                except Exception:
                    pass
                self._drop(s)
                print '  [abl] controller confirmed failed subflow %d (assigned=%d)' % (
                    sid, assigned)
        elif parts[0] == 'faultburst' and len(parts) >= 3:
            # Model a bounded controller/path-failure detection delay.  The
            # kernel qdisc is already a 100% black hole, but the sender has not
            # yet learned that fact, so these new DSNs are accepted by TCP and
            # then lost.  This makes the ablation independent of host timing.
            sid, count = int(parts[1]), int(parts[2])
            with self._lock:
                s = next((x for x in self.senders if x.sid_int == sid), None)
            sent = 0
            while s is not None and sent < count:
                with self._lock:
                    dsn = self.max_dsn
                    self.max_dsn += 1
                try:
                    self._send_on(s, dsn)
                    s.send_count += 1
                    s.assigned.append(dsn)
                    sent += 1
                except Exception:
                    break
            print '  [abl] fault-detection window assigned %d DSN(s) to subflow %d' % (
                sent, sid)

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
