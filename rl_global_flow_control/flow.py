#!/usr/bin/env python2
"""
flow.py -- data model for the free-communication experiment.

Designed with MPTCP-style multi-path in mind: a logical Flow can be split
into multiple Subflows, each with its own rate/path. Today every flow has a
single subflow (single path); later the scheduler can attach more subflows
and use rate/flow-control metrics to pick paths.

Structure:
  Subflow: one (src,dst) path carrying a share of a Flow's data.
  Flow:    a logical end-to-end connection (src -> dst) = list of subflows.
           Direction is one-way; for bidirectional traffic two Flows are
           created (one each way).

The experiment builds a random "connection graph": every node picks 1..3
random destinations and a Flow is created per (src,dst) pair.
"""

import random


class Subflow(object):
    """One path of a flow. Fields relevant to MPTCP path selection."""
    def __init__(self, sid, src, dst, rate=1.0):
        self.sid = sid          # subflow id (unique within the flow)
        self.src = src          # source node id (1..16)
        self.dst = dst          # destination node id
        self.rate = rate        # rate multiplier (RL low-level controls this)
        self.active = True      # whether it transmits this round
        # MPTCP-path placeholders (reserved for later):
        self.path = None        # path identifier when multi-path is used
        self.rtt = None         # measured path RTT
        self.ecn = 0.0          # measured path ECN marking ratio
        self.cwnd = None        # congestion-window-style metric (reserved)

    def __repr__(self):
        return 'Subflow(%d:%d->%d r=%.2f %s)' % (
            self.sid, self.src, self.dst, self.rate,
            'on' if self.active else 'off')


class Flow(object):
    """A logical connection src->dst, possibly over multiple subflows."""
    def __init__(self, fid, src, dst, n_subflows=1):
        self.fid = fid          # flow id
        self.src = src
        self.dst = dst
        self.subflows = [Subflow('%d.%d' % (fid, k), src, dst)
                         for k in range(n_subflows)]

    def add_subflow(self, dst):
        """Add a path to a new destination (reserved for MPTCP path mgmt)."""
        sf = Subflow('%d.%d' % (self.fid, len(self.subflows)), self.src, dst)
        self.subflows.append(sf)
        return sf

    @property
    def active_rate(self):
        return max((sf.rate for sf in self.subflows if sf.active), default=1.0)

    def __repr__(self):
        return 'Flow(%d:%d->%d %d sf)' % (
            self.fid, self.src, self.dst, len(self.subflows))


def build_connection_graph(nodes, min_dst=1, max_dst=3, seed=1):
    """Randomly give every node 1..max_dst distinct destinations.
    Returns (flows, pairs) where pairs is a dict {(src,dst): Flow} and flows
    is the list of all flows. Directional: if A->B and B->A are both picked,
    two separate Flows are created (bidirectional traffic)."""
    rnd = random.Random(seed)
    flows = []
    pairs = {}
    fid = 0
    for src in nodes:
        others = [d for d in nodes if d != src]
        k = rnd.randint(min_dst, max_dst)
        dests = rnd.sample(others, k)
        for dst in dests:
            f = Flow(fid, src, dst)
            flows.append(f)
            pairs[(src, dst)] = f
            fid += 1
    return flows, pairs


if __name__ == '__main__':
    # demo / sanity check
    nodes = range(1, 17)
    flows, pairs = build_connection_graph(nodes, seed=3)
    print 'total flows:', len(flows)
    for src in nodes:
        out = [(f.dst) for f in flows if f.src == src]
        print '  node %d -> %s' % (src, out)
