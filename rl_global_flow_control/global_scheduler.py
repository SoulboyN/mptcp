#!/usr/bin/env python2
"""
global_scheduler.py -- software-side global scheduler combining:
  * RL policy (Q-learning, loaded from policy.json) for global rate choice
  * DCQCN-style quantized congestion response on top of ECN signals
  * credit-aware pacing (a token allowance per flow based on receiver grants)

This runs in the CONTROL PLANE (host-side), reading ECN/congestion state
from the BMv2 switch and writing rate decisions back. It is pure software;
the data plane (simple_router_global.p4) only forwards and marks ECN.
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
        self.actions = cfg['actions']       # e.g. [1.2, 1.0, 0.7, 0.4]
        self.policy = cfg['policy']         # state(0..2) -> action index
        self.flows = flows if flows else range(1, 17)
        # per-flow current rate multiplier (start neutral)
        self.rate_mult = {f: 1.0 for f in self.flows}

        # DCQCN state: after a quantization (rate cut) we hold, then slowly
        # recover toward the RL target.
        self.cut_until = time.time()
        self.recovery_step = 0.05

    # ---- congestion state from ECN counts ----
    def congestion_state(self, ecn_ratio):
        """Map measured ECN-marking ratio to coarse state 0/1/2."""
        if ecn_ratio < 0.1:
            return 0
        if ecn_ratio < 0.4:
            return 1
        return 2

    # ---- RL decision ----
    def rl_rate(self, state):
        """Return a rate multiplier from the learned policy."""
        idx = self.policy[state]
        return self.actions[idx]

    # ---- DCQCN quantized reaction on top ----
    def decide(self, state, last_cut_at):
        """Combine RL target with DCQCN quantized reaction.

        If the network is congested (state>=1) AND we just got a fresh
        ECN signal (last_cut_at recent), apply a quantized cut; otherwise
        follow the RL multiplier and slowly recover.
        """
        rl_mul = self.rl_rate(state)
        now = time.time()
        # quantized cut: state>=1 means ECN present; cut hard once
        if state >= 1 and (now - last_cut_at) < 0.05:
            cut = 0.5 if state == 1 else 0.25
            for f in self.flows:
                self.rate_mult[f] = max(0.2, self.rate_mult[f] * cut)
        else:
            # move toward RL target in small steps (recovery)
            for f in self.flows:
                target = rl_mul
                cur = self.rate_mult[f]
                if cur < target:
                    cur = min(target, cur + self.recovery_step)
                elif cur > target:
                    cur = max(target, cur - self.recovery_step)
                self.rate_mult[f] = cur
        return self.rate_mult

    # ---- export decisions for the driver ----
    def pacing_sleep(self, base_sleep, flow):
        """Map a rate multiplier to a sender sleep interval (inverse)."""
        return base_sleep / max(self.rate_mult.get(flow, 1.0), 0.1)
