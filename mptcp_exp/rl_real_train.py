#!/usr/bin/env python2
"""
rl_real_train.py -- real-environment (slow) fine-tuning for the RESIDUAL RL
congestion control.

Pre-trained offline (rl_train_mptcp.py) then FINE-TUNED on the LIVE 3-switch
BMv2 topology where the reward comes from ACTUAL ECN marks, actual loss,
actual delay. The learned policy is a RESIDUAL over the DCQCN local mechanism:
  final cwnd = clamp( DCQCN_base_cwnd(ecn) x residual_action, min, max )
with the residual action quantized to {x0.5, x1.0, x1.5}.
"""

import json
import os
import time
import random

_HERE = os.path.dirname(os.path.abspath(__file__))
import rl_train_mptcp as rtm

# ---- quantization / residual design (must match RlScheduler + offline) ----
N_STATES = 5
STATE_ECN = [0.02, 0.1, 0.25, 0.5]
DCQCN_MUL = [1.0, 0.8, 0.6, 0.4, 0.25]
BASE_CWND = 32.0
ACTIONS_RESIDUAL = [0.5, 0.75, 1.0, 1.25, 1.5]


def load_policy(path=None):
    if path is None:
        path = os.path.join(_HERE, 'policy_mptcp.json')
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return None


def read_real_ecn(thrift_port, port_idx, budget):
    """Read the REAL ecn_marks register of one switch via CLI."""
    import subprocess
    p = subprocess.Popen(
        ['simple_switch_CLI', '--thrift-port', str(thrift_port)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, _ = p.communicate(input='register_read ecn_marks {}\n'.format(port_idx))
    for line in out.splitlines():
        if '=' in line:
            try:
                return min(1.0, float(line.split('=')[-1].strip()) / budget)
            except ValueError:
                continue
    return 0.0


def congestion_state(ecn_agg, avg_occ):
    """Quantize the aggregate congestion signal into 5 levels (0..4)."""
    sig = max(ecn_agg, avg_occ)
    for i, thr in enumerate(STATE_ECN):
        if sig < thr:
            return i
    return 4


class RealEnvTrainer(object):
    """Fine-tunes the residual policy against the live 3-switch topology."""

    def __init__(self, flows, sw_ports):
        self.flows = flows
        self.sw_ports = sw_ports            # {sw_idx: thrift_port}
        # warm-start the residual Q table from the offline pre-training
        pol = load_policy() or {}
        Qr = pol.get('Q_residual')
        if Qr and len(Qr) == N_STATES and len(Qr[0]) == len(ACTIONS_RESIDUAL):
            self.Q_res = [list(r) for r in Qr]
        else:
            self.Q_res = [[0.0] * len(ACTIONS_RESIDUAL) for _ in range(N_STATES)]

    def decide_actions(self, ecn_by_sw, avg_occ):
        """Greedy residual action for the current (quantized) state.
        Returns (state, residual_mul)."""
        ecn_agg = max(ecn_by_sw.values()) if ecn_by_sw else 0.0
        state = congestion_state(ecn_agg, avg_occ)
        a = max(range(len(ACTIONS_RESIDUAL)), key=lambda i: self.Q_res[state][i])
        return state, ACTIONS_RESIDUAL[a]

    def update(self, state, res_idx, reward, alpha=0.3, gamma=0.9):
        self.Q_res[state][res_idx] += alpha * (
            reward + gamma * max(self.Q_res[state]) - self.Q_res[state][res_idx])

    def train_loop(self, rounds, sw_ports, run_traffic, read_ecn,
                   save_every=5, out_path=None):
        """Fine-tune the residual policy on the LIVE topology.

        run_traffic(path_weights, cwnd_mul) -> (received, sent, delay):
            callback that sends real traffic; cwnd_mul scales the burst so the
            residual action really affects how much is sent.
        read_ecn(sw_idx) -> ecn_ratio: callback reading a switch's ecn_marks.
        Each round:
          - read real ECN -> quantized state
          - pick a residual action; cwnd_mul = DCQCN(state) * residual
          - run_traffic -> real received/sent/delay
          - reward = util - delay - loss - cost ; Q update ; periodic save
        """
        from mptcp_scheduler import PATH_COST, COST_WEIGHT
        for rnd in range(rounds):
            ecn_by_sw = {}
            for s in sw_ports:
                ecn_by_sw[s] = read_ecn(s)
            ecn_agg = max(ecn_by_sw.values()) if ecn_by_sw else 0.0
            # low occupancy placeholder: the fine-tune state is dominated by
            # the REAL per-switch ECN (the trainer has no in-flight feedback)
            occs = [0.1]
            avg_occ = sum(occs) / len(occs)

            state, res_mul = self.decide_actions(ecn_by_sw, avg_occ)
            res_idx = ACTIONS_RESIDUAL.index(res_mul)
            # effective window = DCQCN base x residual, normalized
            cwnd_mul = DCQCN_MUL[state] * res_mul

            # proportional weights (inverse cost) for the traffic burst
            weights = {}
            for f in self.flows:
                for sf in f.subflows:
                    weights[sf.sid] = 1.0 / max(PATH_COST.get(sf.path, 1.0), 0.1)
            tw = sum(weights.values())
            norm = {k: v / tw for k, v in weights.items()} if tw else {}

            recv, sent, delay = run_traffic(norm, cwnd_mul)
            util = recv / max(sent, 1)
            loss = (sent - recv) / max(sent, 1)
            split_cost = sum(norm.get(sf.sid, 0) * PATH_COST.get(sf.path, 0)
                             for f in self.flows for sf in f.subflows)
            reward = util - 0.4 * (delay / 100.0) - 1.5 * loss - COST_WEIGHT * split_cost

            self.update(state, res_idx, reward)

            if (rnd + 1) % save_every == 0 or rnd == rounds - 1:
                p = self.save(out_path)
                print '  [train rnd %d] ecn=%s state=%d res=x%.2f cwnd_mul=%.2f recv=%d/%d delay=%.0fms reward=%.3f -> %s' % (
                    rnd + 1, {k: round(v, 2) for k, v in ecn_by_sw.items()},
                    state, res_mul, cwnd_mul, recv, sent, delay, reward, p)
        return self

    def save(self, path=None):
        if path is None:
            path = os.path.join(_HERE, 'policy_mptcp_real.json')
        with open(path, 'w') as f:
            json.dump({
                'Q_residual': self.Q_res,
                'actions_residual': ACTIONS_RESIDUAL,
                # greedy residual policy for the runtime RlScheduler
                'policy_residual': [max(range(len(ACTIONS_RESIDUAL)),
                                        key=lambda i: self.Q_res[s][i])
                                    for s in range(N_STATES)],
                # path selection stays heuristic (inverse pressure)
                'actions_path': ['sw', 'direct'],
                'policy_path': [0, 0, 0, 0, 0],
            }, f, indent=2)
        return path
