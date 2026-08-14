#!/usr/bin/env python2
"""
mptcp_scheduler.py -- three-domain congestion control for MPTCP:
  * DCQCN domain (shared switch): switch ECN marks -> all SWITCH subflows
    that traverse it back off. Direct subflows are NOT affected.
  * Credit domain (point-to-point): each subflow (direct OR switch) sends
    only within a receiver-granted credit allowance.
  * RL domain (global): a simple Q table picks a rate multiplier from the
    aggregate congestion state (ECN + credit pressure), so the scheduler
    balances path usage (DSN assignment) and rate.

This is software-side (control plane); the data plane only forwards and
marks ECN. Credit is tracked in-memory (receiver grants via a lightweight
ACK channel simulated here).
"""

import json
import os
import time

# RL: coarse state = (ecn_level 0/1/2) -> rate multiplier
ACTIONS_LO = [1.0, 0.7, 0.4]
POLICY_LO = [0, 1, 2]          # state 0->x1.0, 1->x0.7, 2->x0.4


class MptcpScheduler(object):
    def __init__(self, flows, policy_path=None):
        self.flows = flows
        if policy_path and os.path.exists(policy_path):
            with open(policy_path) as f:
                cfg = json.load(f)
            self.actions_lo = cfg.get('actions_lo', ACTIONS_LO)
            self.policy_lo = cfg.get('policy_flow', POLICY_LO)
        else:
            self.actions_lo = ACTIONS_LO
            self.policy_lo = POLICY_LO
        # per-subflow rate and credit
        self.rate = {}
        self.credit = {}
        for f in flows:
            for sf in f.subflows:
                self.rate[sf.sid] = 1.0
                self.credit[sf.sid] = sf.ssn_credit_grant
        self.recovery_step = 0.05

    # ---- DCQCN domain: switch ECN only affects switch subflows ----
    def dcqcn_backoff(self, ecn_ratio):
        """Apply a quantized backoff to SWITCH subflows based on ECN.
        Direct subflows keep their rate (not affected by switch congestion)."""
        state = 0 if ecn_ratio < 0.1 else (1 if ecn_ratio < 0.4 else 2)
        for f in self.flows:
            for sf in f.subflows:
                if sf.path == 'sw':
                    sf.ecn = ecn_ratio
                    if state == 1:
                        self.rate[sf.sid] = max(0.3, self.rate[sf.sid] * 0.7)
                    elif state == 2:
                        self.rate[sf.sid] = max(0.2, self.rate[sf.sid] * 0.4)
                    else:
                        self._recover(sf.sid, self.actions_lo[self.policy_lo[0]])
        return state

    def _recover(self, sid, target):
        cur = self.rate[sid]
        if cur < target:
            self.rate[sid] = min(target, cur + self.recovery_step)
        elif cur > target:
            self.rate[sid] = max(target, cur - self.recovery_step)

    # ---- Credit domain: receiver grants, sender spends ----
    def grant(self, subflow, amount):
        self.credit[subflow.sid] = min(subflow.ssn_credit_grant,
                                       self.credit.get(subflow.sid, 0) + amount)

    def can_send(self, subflow):
        return subflow.active and self.credit.get(subflow.sid, 0) > 0

    def consume_credit(self, subflow):
        if self.credit.get(subflow.sid, 0) > 0:
            self.credit[subflow.sid] -= 1
            return True
        return False

    # ---- RL domain: aggregate rate from coarse state ----
    def rl_multiplier(self, state):
        return self.actions_lo[self.policy_lo[state]]

    def apply_rl(self, state):
        m = self.rl_multiplier(state)
        for f in self.flows:
            for sf in f.subflows:
                self._recover(sf.sid, m)
        return m

    def pacing_sleep(self, base_sleep, sid):
        return base_sleep / max(self.rate.get(sid, 1.0), 0.1)
