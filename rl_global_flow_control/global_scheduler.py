#!/usr/bin/env python2
"""
global_scheduler.py -- software-side hierarchical global scheduler:
  * HIGH-LEVEL RL (Q_tree): which flows to activate this round
    (learned activation priority, paper-inspired two-level structure)
  * LOW-LEVEL RL (Q_flow): rate multiplier for the active flows
  * DCQCN-style quantized congestion response on top of ECN signals
  * credit-aware pacing

Runs in the CONTROL PLANE (host-side); reads ECN/congestion from the BMv2
switch and writes pacing decisions back. Pure software; data plane only
forwards and marks ECN.
"""

import json
import os
import time


class GlobalScheduler(object):
    def __init__(self, policy_path=None, flows=None):
        if policy_path is None:
            policy_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       'policy.json')
        with open(policy_path) as f:
            cfg = json.load(f)
        # low-level (rate): existing Q-learning policy
        self.actions_lo = cfg['actions_lo']          # [1.2, 1.0, 0.7, 0.4]
        self.policy_flow = cfg['policy_flow']        # congestion -> action idx
        # high-level (activation): paper-style selection policy
        self.actions_hi = cfg['actions_hi']          # ['all','half','quarter']
        self.policy_tree = cfg['policy_tree']        # hi-state -> action idx
        self.flows = flows if flows else range(1, 17)
        self.n_flows = len(self.flows)

        # per-flow current rate multiplier (start neutral)
        self.rate_mult = {f: 1.0 for f in self.flows}
        # which flows are currently activated (start: all)
        self.active = list(self.flows)

        # DCQCN state
        self.cut_until = time.time()
        self.recovery_step = 0.05

    # ---- congestion state from ECN counts ----
    def congestion_state(self, ecn_ratio):
        if ecn_ratio < 0.1:
            return 0
        if ecn_ratio < 0.4:
            return 1
        return 2

    # ---- high-level: which flows to activate this round ----
    def hi_state(self, congestion, work_ratio):
        """High-level state index = congestion*2 + work_level."""
        work_level = 0 if work_ratio < 0.5 else 1
        return congestion * 2 + work_level

    def select_active(self, congestion, work_ratio):
        """Return the list of flows activated this round (learned)."""
        s = self.hi_state(congestion, work_ratio)
        a = self.policy_tree[s]
        n = self.n_flows
        if self.actions_hi[a] == 'all':
            return list(self.flows)
        if self.actions_hi[a] == 'half':
            step = max(2, n // 2)
            return [f for f in self.flows if (f % step) in (0, 1)]
        # quarter
        step = max(4, n // 2)
        return [f for f in self.flows if f % step == 0]

    # ---- low-level: rate multiplier from congestion ----
    def rl_rate(self, state):
        idx = self.policy_flow[state]
        return self.actions_lo[idx]

    # ---- combine: activate + rate + DCQCN quantized cut ----
    def decide(self, state, last_cut_at, work_ratio=1.0):
        """High-level selects active flows, low-level sets their rate,
        DCQCN applies a quantized cut if ECN just fired."""
        self.active = self.select_active(state, work_ratio)
        rl_mul = self.rl_rate(state)
        now = time.time()
        # quantized cut (DCQCN): congested + fresh ECN -> hard cut
        if state >= 1 and (now - last_cut_at) < 0.05:
            cut = 0.5 if state == 1 else 0.25
            for f in self.flows:
                self.rate_mult[f] = max(0.2, self.rate_mult[f] * cut)
        else:
            # recover toward RL target in small steps
            for f in self.flows:
                target = rl_mul
                cur = self.rate_mult[f]
                if cur < target:
                    cur = min(target, cur + self.recovery_step)
                elif cur > target:
                    cur = max(target, cur - self.recovery_step)
                self.rate_mult[f] = cur
        return self.active, self.rate_mult

    # ---- pacing for the driver ----
    def pacing_sleep(self, base_sleep, flow):
        # inactive flows pause (no traffic); active flows pace by multiplier
        if flow not in self.active:
            return None
        return base_sleep / max(self.rate_mult.get(flow, 1.0), 0.1)
