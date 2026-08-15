#!/usr/bin/env python2
"""
flow_mptcp.py -- MPTCP-style data model with DSN/SSN two-dimensional flow
control.

  * Flow  : a logical connection (src->dst) with ONE DSN (data sequence)
            sequence covering the whole byte stream, and a reorder buffer.
  * Subflow: one path (direct or via switch) with its OWN SSN (subflow
            sequence). Traffic control acts on both axes:
              - SSN axis (within a subflow): credit + rate pacing
              - DSN axis (across subflows):  RL picks which subflow gets
                the next DSN segment (path selection).

Each connection gets 3..4 random subflows; the first is a DIRECT link
(no switch), the rest go through the BMv2 switch. Directional: A->B and
B->A are separate Flows for bidirectional traffic.
"""

import random

# path constants
PATH_DIRECT = 'direct'
# multiple switches -> genuinely heterogeneous switch paths
PATH_SWITCHES = ['sw1', 'sw2', 'sw3']


class Subflow(object):
    """One path of a flow = one SSN sequence + one physical path."""
    def __init__(self, sid, src, dst, path):
        self.sid = sid
        self.src = src
        self.dst = dst
        self.path = path               # 'direct' or 'sw'
        self.rate = 1.0                # RL low-level rate multiplier
        self.active = True
        # SSN-axis state (within this subflow)
        self.ssn_next = 0              # next SSN to transmit
        self.ssn_acked = 0             # highest SSN acknowledged
        self.ssn_unacked = 0           # outstanding (pseudo-cwnd)
        self.ssn_credit = 0            # current credit allowance
        self.ssn_credit_grant = 20     # receiver's grant per round
        self.packets_sent = 0
        self.packets_lost = 0
        # congestion signals
        self.ecn = 0.0                 # switch ECN ratio (sw subflows)
        self.delay = 0.0
        self.rtt = 0.0

    def can_send(self):
        return self.active and self.ssn_credit > 0

    def consume_credit(self):
        self.ssn_credit -= 1
        self.ssn_unacked += 1

    def ssn_state(self):
        return (self.ssn_next, self.ssn_unacked, self.ssn_credit,
                self.ecn, self.delay)

    def __repr__(self):
        return 'Subflow(%s:%d->%d %s r=%.2f ssn=%d c=%d)' % (
            self.sid, self.src, self.dst, self.path, self.rate,
            self.ssn_next, self.ssn_credit)


class Flow(object):
    """A logical connection src->dst with a DSN sequence + subflows."""
    def __init__(self, fid, src, dst, n_subflows=1, direct_ok=True):
        self.fid = fid
        self.src = src
        self.dst = dst
        self.subflows = []
        sw_idx = 0
        for k in range(n_subflows):
            if direct_ok and k == 0:
                path = PATH_DIRECT
            else:
                # assign each switch subflow to a DIFFERENT switch
                # (round-robin over sw1/sw2/sw3) -> genuinely heterogeneous
                path = PATH_SWITCHES[sw_idx % len(PATH_SWITCHES)]
                sw_idx += 1
            self.subflows.append(
                Subflow('%d.%d' % (fid, k), src, dst, path))
        # DSN-axis state (whole connection)
        self.dsn_next = 0
        self.dsn_received = 0
        self.reorder_buf = {}          # dsn -> payload (out-of-order held)

    def add_subflow(self, path=None):
        if path is None:
            path = PATH_SWITCHES[0]
        sf = Subflow('%d.%d' % (self.fid, len(self.subflows)),
                     self.src, self.dst, path)
        self.subflows.append(sf)
        return sf

    def dsn_state(self):
        return (self.dsn_next, self.dsn_received, len(self.reorder_buf),
                self.dsn_next - self.dsn_received)

    def __repr__(self):
        return 'Flow(%d:%d->%d %d sf)' % (
            self.fid, self.src, self.dst, len(self.subflows))


def build_mptcp_graph(nodes, min_sub=3, max_sub=4, min_dst=1, max_dst=3,
                      seed=1, direct_ok=True):
    """Randomly give every node 1..max_dst destinations; each connection
    gets min_sub..max_sub subflows (first one direct if allowed).
    Returns (flows, pairs) with pairs[(src,dst)] = Flow."""
    rnd = random.Random(seed)
    flows = []
    pairs = {}
    fid = 0
    for src in nodes:
        others = [d for d in nodes if d != src]
        for dst in rnd.sample(others, rnd.randint(min_dst, max_dst)):
            n_sub = rnd.randint(min_sub, max_sub)
            f = Flow(fid, src, dst, n_subflows=n_sub, direct_ok=direct_ok)
            flows.append(f)
            pairs[(src, dst)] = f
            fid += 1
    return flows, pairs


def count_by_path(flows):
    """Count subflows by path for diagnostics. Returns a dict."""
    counts = {PATH_DIRECT: 0}
    for sw in PATH_SWITCHES:
        counts[sw] = 0
    for f in flows:
        for sf in f.subflows:
            counts[sf.path] = counts.get(sf.path, 0) + 1
    return counts


if __name__ == '__main__':
    flows, pairs = build_mptcp_graph(range(1, 17), seed=3)
    counts = count_by_path(flows)
    print 'flows:', len(flows), ' subflow counts:', counts
    for f in flows[:5]:
        print ' ', f, '->', [sf.path for sf in f.subflows]
