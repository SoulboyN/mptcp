#!/usr/bin/env python2
"""
tcp_stack.py -- a minimal custom TCP implementation for the MPTCP experiment.

Purpose
-------
The paper topic is "SDN-global-aware MPTCP dynamic heterogeneous subflow
congestion control". The core innovation is RL-driven congestion-window
control on per-subflow custom TCP connections. This module provides the
transport layer: a simplified TCP state machine with sequence numbers, ACK
feedback, RTO-based retransmission, and a congestion window (cwnd).

What is implemented
-------------------
  - Connection state machine: CLOSED -> SYN_SENT -> ESTABLISHED -> (FIN)
  - Sequence numbering: snd_nxt (next to send), snd_una (oldest unacked),
    rcv_nxt (next expected), rcv_buf (reorder buffer)
  - ACK feedback: receiver sends cumulative ACK; sender advances snd_una
  - RTO timer: retransmit unacked segments on timeout (exponential backoff)
  - Congestion control: cwnd / ssthresh / in_flight; send only if
    in_flight < cwnd; slow start (cwnd += 1/seg per ACK) and congestion
    avoidance (cwnd += 1/cwnd per ACK). The RL controller can override
    cwnd directly (see RlCwndController in mptcp_scheduler.py) -- this is
    the paper's core "SDN global awareness -> RL congestion-window control"
    mechanism: the scheduler is the SDN controller with the global view.

This is a *simplified* TCP: it runs over the experiment's UDP carrier and
focuses on the scheduling/congestion-control aspects, not on full RFC 793.
"""

import time
import random
import struct

# ---- segment flags ----
SYN = 0x02
ACK = 0x10
FIN = 0x01
PSH = 0x08

# ---- segment header over UDP payload: ----
#   [flow_id:2][subflow_id:2][flags:1][seq:4][ack:4][len:2][data...]
_HDR = '!HHBIIH'


def pack_seg(flow_id, subflow_id, flags, seq, ack, data=b''):
    return struct.pack(_HDR, flow_id, subflow_id, flags, seq, ack,
                       len(data)) + data


def unpack_seg(buf):
    hlen = struct.calcsize(_HDR)
    if len(buf) < hlen:
        return None
    flow_id, subflow_id, flags, seq, ack, dlen = struct.unpack(
        _HDR, buf[:hlen])
    data = buf[hlen:hlen + dlen]
    return {'flow': flow_id, 'sub': subflow_id, 'flags': flags,
            'seq': seq, 'ack': ack, 'data': data}


class TcpSocket(object):
    """A single TCP connection = one subflow."""

    def __init__(self, flow_id, subflow_id, src, dst, path, mss=1400):
        self.flow_id = flow_id
        self.subflow_id = subflow_id
        self.src = src
        self.dst = dst
        self.path = path            # 'direct' or 'sw'
        self.mss = mss

        # connection state
        self.state = 'CLOSED'       # CLOSED -> SYN_SENT -> ESTABLISHED
        self.snd_nxt = random.randint(0, 10000)      # next seq to send
        self.snd_una = self.snd_nxt                  # oldest unacked
        self.rcv_nxt = 0                             # next expected seq
        self.rcv_buf = {}                            # seq -> data (reorder)

        # congestion window (the RL control target)
        self.cwnd = 10
        self.ssthresh = 65535
        self.in_flight = 0
        self.rtt_est = 0.05
        self.rto = 1.0
        self.rto_base = 1.0

        # segments waiting for ACK: seq -> (sent_time, data)
        self.unacked = {}
        self.retrans_q = []

        # stats
        self.bytes_sent = 0
        self.bytes_acked = 0
        self.retrans = 0
        self.dup_acks = 0

        # external interface: the scheduler can set the target cwnd directly
        self.ctrl_cwnd = None        # RL-set cwnd override (None = algorithm)
        # retransmission path: the scheduler sets this to a HEALTHIER path
        # (see RlPathSelector.select_retrans_subflow) so a dropped segment
        # is retransmitted over a different, more reliable path.
        self.retrans_to = None

    # ---- helpers ----
    def _effective_cwnd(self):
        if self.ctrl_cwnd is not None:
            return max(1, int(self.ctrl_cwnd))
        return max(1, int(self.cwnd))

    def can_send(self):
        return (self.state == 'ESTABLISHED'
                and self.in_flight < self._effective_cwnd())

    # ---- connection setup ----
    def start(self):
        """Initiate the connection (send SYN)."""
        self.state = 'SYN_SENT'
        return pack_seg(self.flow_id, self.subflow_id, SYN,
                        self.snd_nxt, 0)

    def on_syn_ack(self, seg):
        self.rcv_nxt = seg['seq'] + 1
        self.snd_una = self.snd_nxt = seg['ack']
        self.state = 'ESTABLISHED'
        return pack_seg(self.flow_id, self.subflow_id, ACK,
                        self.snd_nxt, self.rcv_nxt)

    def on_syn(self, seg):
        self.rcv_nxt = seg['seq'] + 1
        self.state = 'ESTABLISHED'
        # SYN-ACK
        return pack_seg(self.flow_id, self.subflow_id, SYN | ACK,
                        self.snd_nxt, self.rcv_nxt)

    # ---- data send ----
    def send(self, data):
        """Wrap one segment. Caller paces (scheduler controls timing)."""
        seg = pack_seg(self.flow_id, self.subflow_id, PSH | ACK,
                       self.snd_nxt, self.rcv_nxt, data)
        self.unacked[self.snd_nxt] = (time.time(), data)
        self.in_flight += 1
        self.snd_nxt += len(data)
        self.bytes_sent += len(data)
        return seg

    # ---- receive ----
    def on_seg(self, seg):
        """Process an incoming segment; returns (ack_to_send, deliverable)."""
        out = []
        if seg['flags'] & ACK:
            self._on_ack(seg['ack'])
        if seg['data']:
            self._receive_data(seg['seq'], seg['data'])
        # deliver in-order data
        while self.rcv_nxt in self.rcv_buf:
            out.append(self.rcv_buf.pop(self.rcv_nxt))
            self.rcv_nxt += len(out[-1]) if isinstance(out[-1], bytes) else 0
        ack = pack_seg(self.flow_id, self.subflow_id, ACK,
                       self.snd_nxt, self.rcv_nxt)
        return ack, out

    def _on_ack(self, ack_num):
        # remove acknowledged segments
        for seq in [s for s in self.unacked if s + len(self.unacked[s][1]) <= ack_num]:
            self.bytes_acked += len(self.unacked[seq][1])
            self.in_flight = max(0, self.in_flight - 1)
            del self.unacked[seq]
            # ACK-based cwnd growth (slow start / congestion avoidance)
            self._cc_on_ack()
        if self.unacked:
            # not fully acked -> could be dup ack
            pass

    def _cc_on_ack(self):
        """Classic cwnd update; RL can override via ctrl_cwnd."""
        if self.ctrl_cwnd is not None:
            return                       # RL owns cwnd
        if self.cwnd < self.ssthresh:
            self.cwnd += 1               # slow start
        else:
            self.cwnd += 1.0 / max(self.cwnd, 1)   # congestion avoidance

    def _receive_data(self, seq, data):
        if seq == self.rcv_nxt:
            self.rcv_buf[seq] = data
        elif seq > self.rcv_nxt:
            self.rcv_buf[seq] = data     # out of order, hold

    # ---- retransmission ----
    def on_timeout(self, now):
        """RTO timeout: retransmit oldest unacked, back off RTO. The
        scheduler may set self.retrans_to to a healthier path; the caller
        sends the returned segment over that path."""
        if not self.unacked:
            return None
        oldest_seq = min(self.unacked)
        _, data = self.unacked[oldest_seq]
        self.retrans += 1
        self.rto = min(self.rto * 2, 10.0)
        self.in_flight = max(0, self.in_flight - 1)
        return pack_seg(self.flow_id, self.subflow_id, PSH | ACK,
                        oldest_seq, self.rcv_nxt, data)

    def check_rto(self, now):
        """Return a segment to retransmit if RTO expired, else None."""
        if not self.unacked:
            return None
        oldest_seq = min(self.unacked)
        t0, data = self.unacked[oldest_seq]
        if now - t0 >= self.rto:
            return self.on_timeout(now)
        return None

    def stats(self):
        return {'cwnd': self._effective_cwnd(), 'in_flight': self.in_flight,
                'rtt': self.rtt_est, 'retrans': self.retrans,
                'bytes_sent': self.bytes_sent,
                'bytes_acked': self.bytes_acked, 'path': self.path}
