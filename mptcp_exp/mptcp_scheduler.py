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
            if 'policy_cwnd' in cfg:                 # rl_real_train output
                self.actions_lo = cfg.get('actions_cwnd', ACTIONS_LO)
                self.policy_lo = cfg.get('policy_cwnd', POLICY_LO)
            else:                                     # legacy rate policy
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


# ---- RL-driven congestion-window control (paper core innovation) ----
# Instead of the fixed slow-start/CA growth in tcp_stack, the scheduler
# (the SDN global controller) picks a cwnd-adjustment ACTION from a coarse
# global state. This is the "SDN global traffic awareness -> dynamic
# heterogeneous subflow congestion control" mechanism.

# cwnd actions: multiply the window by this factor
CWND_ACTIONS = [2.0, 1.0, 0.5, 0.25]
# policy: state (0=idle,1=busy,2=congested) -> action idx
CWND_POLICY = [0, 2, 3]


class RlCwndController(object):
    """Per-round controller that sets each subflow's cwnd via its socket."""

    def __init__(self, flows, actions=CWND_ACTIONS, policy=CWND_POLICY,
                 min_cwnd=4, max_cwnd=256, policy_path=None):
        self.flows = flows
        self.actions = actions
        self.policy = policy
        self.min_cwnd = min_cwnd
        self.max_cwnd = max_cwnd
        # load learned policy if available (rl_train_mptcp.py output)
        if policy_path and os.path.exists(policy_path):
            import json as _json
            try:
                with open(policy_path) as f:
                    cfg = _json.load(f)
                self.actions = cfg.get('actions_cwnd', actions)
                self.policy = cfg.get('policy_cwnd', policy)
            except Exception:
                pass
        # subflow string sid -> tcp socket (set by caller)
        self.sockets = {}

    def congestion_state(self, ecn_ratio, avg_inflight_ratio):
        """Coarse global state from SDN-wide signals: ECN (shared) + in-flight
        pressure (per-path). 0=idle 1=busy 2=congested."""
        if ecn_ratio >= 0.4 or avg_inflight_ratio >= 0.8:
            return 2
        if ecn_ratio >= 0.1 or avg_inflight_ratio >= 0.5:
            return 1
        return 0

    def cwnd_action_for(self, state):
        return self.actions[self.policy[state]]

    def apply(self, ecn_ratio, avg_inflight_ratio):
        """Decide a cwnd multiplier per state, apply to all subflow sockets.
        Direct subflows use the same multiplier but their cwnd is not cut by
        ECN (ECN only feeds the global state; per-path in-flight pressure
        differentiates them). Returns (state, multiplier)."""
        state = self.congestion_state(ecn_ratio, avg_inflight_ratio)
        mul = self.cwnd_action_for(state)
        for f in self.flows:
            for sf in f.subflows:
                sock = self.sockets.get(sf.sid)
                if sock is None:
                    continue
                new_cwnd = max(self.min_cwnd,
                               min(self.max_cwnd, sock.cwnd * mul))
                sock.ctrl_cwnd = new_cwnd
        return state, mul

    def avg_inflight_ratio(self):
        """Fraction of in-flight / cwnd across subflows (0..~1)."""
        vals = []
        for f in self.flows:
            for sf in f.subflows:
                sock = self.sockets.get(sf.sid)
                if sock is None:
                    continue
                c = sock._effective_cwnd()
                vals.append(float(sock.in_flight) / max(c, 1))
        return sum(vals) / max(len(vals), 1) if vals else 0.0


# ---- HIGH-LEVEL RL: per-segment path selection (paper's hierarchical DRL) --
# The upper-level policy decides which SUBFLOW gets the next DSN segment,
# i.e. "dynamic heterogeneous subflow path selection" driven by the SDN
# global view. With 3 switches there are genuinely independent path
# domains: direct, sw1, sw2, sw3 -- each switch has its OWN ECN signal.
# The lower level (RlCwndController) still owns congestion-window control.

PATH_ACTIONS = ['sw', 'direct']           # choose path type
# state (0=idle,1=busy,2=congested) -> prefer path
PATH_POLICY = ['sw', 'sw', 'direct']      # idle/busy -> spread on switch;
                                          # congested -> fall back to direct

# ---- path cost (per unit traffic) ----
# Simulate heterogeneous access economics: cellular is expensive, WiFi
# free, fiber cheap, direct link cheap. RL reward subtracts cost so the
# policy learns to prefer cheap paths when performance is comparable.
PATH_COST = {'direct': 0.1, 'sw1': 0.0, 'sw2': 1.0, 'sw3': 0.2}
COST_WEIGHT = 0.5


class RlPathSelector(object):
    """Per-segment path selector over multiple switch ECN domains.
    Tracks per-switch ECN ratio (each switch independent) and per-subflow
    occupancy; picks the subflow with the least pressure among the
    policy-preferred path type."""

    def __init__(self, flows, actions=PATH_ACTIONS, policy=PATH_POLICY,
                 policy_path=None, n_sw=3):
        self.flows = flows
        self.actions = actions
        self.policy = policy
        self.n_sw = n_sw
        if policy_path and os.path.exists(policy_path):
            import json as _json
            try:
                with open(policy_path) as f:
                    cfg = _json.load(f)
                pp = cfg.get('policy_path', [])
                if pp:
                    self.actions = cfg.get('actions_path', actions)
                    self.policy = [self.actions[i] for i in pp]
            except Exception:
                pass
        self.sockets = {}                 # sid -> TcpSocket
        # per-switch ECN ratio (independent congestion domain each)
        self.sw_ecn = {s: 0.0 for s in range(1, n_sw + 1)}
        self.ecn_ratio = 0.0              # aggregate (worst switch)

    def congestion_state(self, ecn_ratio, avg_occ):
        if ecn_ratio >= 0.4 or avg_occ >= 0.8:
            return 2
        if ecn_ratio >= 0.1 or avg_occ >= 0.5:
            return 1
        return 0

    def preferred_path(self, state):
        return self.policy[state]

    def _path_pressure(self, sf):
        """Combined pressure of a path: its switch's ECN (if switch path),
        its own in-flight occupancy, PLUS its monetary cost. The cost term
        makes the selector prefer cheap paths when congestion is similar."""
        sock = self.sockets.get(sf.sid)
        occ = 0.0
        if sock is not None:
            c = sock._effective_cwnd()
            occ = float(sock.in_flight) / max(c, 1)
        cost = COST_WEIGHT * PATH_COST.get(sf.path, 0.0)
        if sf.path.startswith('sw'):
            s_idx = int(sf.path[2]) - 1      # 'sw1'->0
            ecn = self.sw_ecn.get(s_idx + 1, 0.0)
            return occ + 0.6 * ecn + cost    # ECN + cost pressure
        return occ + cost                    # direct has no ECN, only cost

    def select_subflow(self, flow, state):
        """Pick the subflow for the next DSN segment: prefer the policy's
        path type; among candidates pick the least-pressure one. A policy
        preference of 'sw' matches any sw1/sw2/sw3 subflow."""
        prefer = self.preferred_path(state)
        if prefer == 'sw':
            cand = [sf for sf in flow.subflows if sf.path.startswith('sw')]
        else:
            cand = [sf for sf in flow.subflows if sf.path == prefer]
        if not cand:
            cand = flow.subflows
        best = min(cand, key=self._path_pressure)
        return best

    # ---- continuous proportional traffic split ----
    def path_weights(self, flow):
        """Continuous per-path weights (inverse of pressure): a less
        pressured / cheaper / less congested path gets a larger share.
        Returns {subflow.sid: weight} for this flow's subflows."""
        pressures = {sf.sid: max(self._path_pressure(sf), 0.05)
                     for sf in flow.subflows}
        inv = {sid: 1.0 / p for sid, p in pressures.items()}
        total = sum(inv.values()) or 1.0
        return {sid: w / total for sid, w in inv.items()}

    def pick_by_ratio(self, flow):
        """Pick a subflow for the next DSN segment by the continuous
        proportional weights (weighted random). This is the "split traffic
        across heterogeneous paths by ratio" the RL policy drives."""
        import random
        weights = self.path_weights(flow)
        sids = list(weights.keys())
        probs = [weights[s] for s in sids]
        r = random.random() * sum(probs)
        acc = 0.0
        for sid, p in zip(sids, probs):
            acc += p
            if r <= acc:
                return sid
        return sids[-1]

    # ---- retransmission: pick the healthiest path ----
    def select_retrans_subflow(self, flow):
        """For a DROP/RETRANSMIT, choose the currently healthiest path:
        lowest pressure (ECN + occupancy + cost). Ignores the original
        path, so a retransmitted segment avoids the path that dropped."""
        if not flow.subflows:
            return None
        return min(flow.subflows, key=self._path_pressure)

    def observe(self, sw_ecn_map):
        """Update per-switch ECN (the SDN global view of each switch)."""
        for s, v in sw_ecn_map.items():
            if s in self.sw_ecn:
                self.sw_ecn[s] = v
        self.ecn_ratio = max(self.sw_ecn.values()) if self.sw_ecn else 0.0


class RlScheduler(object):
    """Embedded SDN-global scheduler (RESIDUAL + quantized) for a real sender.

    Every step:
      - quantizes the global congestion state into 5 levels (0..4)
      - computes a LOCAL-mechanism base cwnd per subflow (DCQCN: switch ECN
        shrinks the base on switch paths; direct keeps the full base)
      - applies the RL residual policy: a quantized residual action
        {x0.5, x1.0, x1.5} multiplies the base
      - produces path weights (inverse pressure) for subflow selection
    So RL only learns a small correction ON TOP of the existing DCQCN/Credit
    mechanisms -- "global RL refines local mechanisms" (the paper narrative).
    """

    BASE_CWND = 32
    MIN_CWND = 4
    MAX_CWND = 128
    # 5-level state quantization boundaries (ECN ratio / occupancy)
    STATE_ECN = [0.02, 0.1, 0.25, 0.5]
    # DCQCN local-mechanism base multiplier per state (switch paths)
    DCQCN_MUL = [1.0, 0.8, 0.6, 0.4, 0.25]
    # quantized residual actions (multipliers on the base)
    RESIDUAL = [0.5, 0.75, 1.0, 1.25, 1.5]
    DEFAULT_POLICY_RESIDUAL = [2, 2, 2, 2, 2]   # maintain the base (x1.0)

    def __init__(self, subflows, policy_path=None, base_cwnd=BASE_CWND, n_sw=3):
        self.subflows = subflows
        self.base_cwnd = base_cwnd
        self.actions_residual = list(self.RESIDUAL)
        self.policy_residual = list(self.DEFAULT_POLICY_RESIDUAL)
        if policy_path and os.path.exists(policy_path):
            try:
                cfg = json.load(open(policy_path))
                ar = cfg.get('actions_residual')
                if ar and len(ar) >= 5:
                    self.actions_residual = list(ar)
                pr = cfg.get('policy_residual')
                if pr and len(pr) == 5:
                    self.policy_residual = [int(x) for x in pr]
            except Exception:
                pass
        self._flow = type('_Flow', (), {'subflows': subflows})()
        self.sel = RlPathSelector([self._flow], policy_path=policy_path, n_sw=n_sw)
        for sf in subflows:
            self.sel.sockets[sf.sid] = sf
        self.sw_ecn = {s: 0.0 for s in range(1, n_sw + 1)}
        self.cwnd = {sf.sid: base_cwnd for sf in subflows}
        self.last_state = 0

    def congestion_state(self, ecn_agg, avg_occ):
        """Quantize the aggregate congestion signal into 5 levels (0..4)."""
        sig = max(ecn_agg, avg_occ)
        for i, thr in enumerate(self.STATE_ECN):
            if sig < thr:
                return i
        return 4

    def _base_cwnd(self, sf):
        """Local-mechanism base cwnd: DCQCN shrinks switch-path bases by that
        switch's ECN; direct paths keep the full base (credit domain)."""
        if sf.path.startswith('sw'):
            s_idx = int(sf.path[2])             # 'sw1' -> 1
            ecn = self.sw_ecn.get(s_idx, 0.0)
            for i, thr in enumerate(self.STATE_ECN):
                if ecn < thr:
                    return max(self.MIN_CWND, self.base_cwnd * self.DCQCN_MUL[i])
            return max(self.MIN_CWND, self.base_cwnd * self.DCQCN_MUL[4])
        return self.base_cwnd

    def step(self, sw_ecn, per_sub):
        """One scheduling round: quantize state, apply residual policy on the
        DCQCN base, produce path weights. Returns (state, {sid: cwnd}, weights)."""
        for sf in self.subflows:            # re-bind (reconnect swaps sockets)
            self.sel.sockets[sf.sid] = sf
        self.sel.observe(sw_ecn)
        self.sw_ecn.update(sw_ecn)
        for sf in self.subflows:
            sf.recv_count = per_sub.get(sf.sid_int, sf.recv_count)
        ecn_agg = max(self.sw_ecn.values()) if self.sw_ecn else 0.0
        occs = [float(sf.in_flight) / max(sf._effective_cwnd(), 1)
                for sf in self.subflows]
        avg_occ = sum(occs) / max(len(occs), 1)
        state = self.congestion_state(ecn_agg, avg_occ)
        self.last_state = state
        res_mul = self.actions_residual[self.policy_residual[state]]
        for sf in self.subflows:
            base = self._base_cwnd(sf)
            cw = max(self.MIN_CWND, min(self.MAX_CWND, base * res_mul))
            sf.cwnd = cw
            self.cwnd[sf.sid] = cw
        weights = self.sel.path_weights(self._flow)
        return state, dict(self.cwnd), weights

    def path_weights(self):
        return self.sel.path_weights(self._flow)
