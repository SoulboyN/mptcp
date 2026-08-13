#!/usr/bin/env python2
"""
global_scheduler.py -- hierarchical global scheduler over dynamic flows.

Controls an arbitrary set of flows (from flow.py), not a fixed 16-pair set:
  * HIGH-LEVEL (Q_tree): which flows/subflows to activate this round.
  * LOW-LEVEL  (Q_flow): rate multiplier for each active flow.
  * DCQCN quantized cut on top of ECN signals.
  * MPTCP-friendly: operates per Subflow; a Flow with multiple subflows
    would have each subflow's rate controlled independently (reserved).

The scheduler does NOT touch the data plane; it reads ECN counts from the
BMv2 switch and returns (active_flows, rate_multipliers) that the driver
uses to pace each sender.
"""

import json
import os
import time


class GlobalScheduler(object):
    def __init__(self, policy_path=None, flows=None, nodes=None):
        if policy_path is None:
            policy_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       'policy.json')
        with open(policy_path) as f:
            cfg = json.load(f)
        self.actions_lo = cfg['actions_lo']          # [1.2, 1.0, 0.7, 0.4]
        self.policy_flow = cfg['policy_flow']        # congestion -> action idx
        self.actions_hi = cfg['actions_hi']          # ['all','half','quarter']
        self.policy_tree = cfg['policy_tree']        # hi-state -> action idx
        # flows: dict {(src,dst): Flow} from flow.build_connection_graph
        self.flows = flows if flows else {}
        self.nodes = nodes if nodes else range(1, 17)
        # per-subflow rate multiplier
        self.rate_mult = {}
        for f in self.flows.values():
            for sf in f.subflows:
                self.rate_mult[sf.sid] = 1.0

        self.cut_until = time.time()
        self.recovery_step = 0.05

    # ---- congestion state ----
    def congestion_state(self, ecn_ratio):
        if ecn_ratio < 0.1:
            return 0
        if ecn_ratio < 0.4:
            return 1
        return 2

    # ---- high-level: which flows to activate ----
    def hi_state(self, congestion, work_ratio):
        work_level = 0 if work_ratio < 0.5 else 1
        return congestion * 2 + work_level

    def select_active(self, congestion, work_ratio):
        """Return the list of subflow ids to activate this round."""
        s = self.hi_state(congestion, work_ratio)
        a = self.policy_tree[s]
        all_sf = [sf.sid for f in self.flows.values() for sf in f.subflows]
        n = len(all_sf)
        if self.actions_hi[a] == 'all':
            return list(all_sf)
        # sample a subset deterministically
        step = max(2, n // (2 if self.actions_hi[a] == 'half' else 4))
        return [sid for i, sid in enumerate(all_sf) if i % step == 0]

    # ---- low-level: rate ----
    def rl_rate(self, state):
        idx = self.policy_flow[state]
        return self.actions_lo[idx]

    # ---- combine ----
    def decide(self, state, last_cut_at, work_ratio=1.0):
        active = self.select_active(state, work_ratio)
        rl_mul = self.rl_rate(state)
        now = time.time()
        if state >= 1 and (now - last_cut_at) < 0.05:
            cut = 0.5 if state == 1 else 0.25
            for sid in self.rate_mult:
                self.rate_mult[sid] = max(0.2, self.rate_mult[sid] * cut)
        else:
            for sid in self.rate_mult:
                cur = self.rate_mult[sid]
                if cur < rl_mul:
                    cur = min(rl_mul, cur + self.recovery_step)
                elif cur > rl_mul:
                    cur = max(rl_mul, cur - self.recovery_step)
                self.rate_mult[sid] = cur
        return active, self.rate_mult

    # ---- pacing for the driver ----
    def pacing_sleep(self, base_sleep, subflow_id):
        if subflow_id not in self.rate_mult:
            return base_sleep
        return base_sleep / max(self.rate_mult.get(subflow_id, 1.0), 0.1)
