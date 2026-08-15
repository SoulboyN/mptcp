#!/usr/bin/env python2
"""
rl_real_train.py -- real-environment (slow) RL training for the MPTCP
experiment.

Why real training
-----------------
The thesis topic is "SDN global traffic awareness -> MPTCP dynamic
heterogeneous subflow congestion control". To make the learned policy
RELIABLE, we do not trust only the simplified offline simulator: we
pre-train there, then FINE-TUNE on the REAL 3-switch BMv2 network where
the reward comes from ACTUAL ECN marks, actual loss, actual delay.

Flow
----
1. Load a pre-trained policy (rl_train_mptcp.py output, or start fresh).
2. For each training round:
     a. decide actions with the current policy (path selection + cwnd)
     b. run real traffic on the live 3-switch topology
     c. read REAL per-switch ECN counters (register ecn_marks via CLI)
     d. measure REAL received/loss/delay at the receivers
     e. compute reward = util - loss - delay ; update Q tables
     f. save the policy periodically
3. Stop when loss is low and reward plateaus (or after N rounds).

Because simple_switch is single-threaded, keep per-round traffic modest
so real ECN/loss signals are meaningful rather than CPU-overload drops.
"""

import json
import os
import time
import random

# reuse the offline trainer's structure
_HERE = os.path.dirname(os.path.abspath(__file__))
import rl_train_mptcp as rtm

# cwnd multiplier actions (low level), path preference (high level)
CWND_ACTIONS = [2.0, 1.0, 0.5, 0.25]
PATH_ACTIONS = ['sw', 'direct']


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


class RealEnvTrainer(object):
    """Drives RL fine-tuning against the live 3-switch topology."""

    def __init__(self, flows, sw_ports, min_cwnd=4, max_cwnd=128):
        self.flows = flows
        self.sw_ports = sw_ports            # {sw_idx: thrift_port}
        self.min_cwnd = min_cwnd
        self.max_cwnd = max_cwnd
        # RL tables (pre-trained from offline, else fresh)
        pol = load_policy() or {}
        self.Q_path = [[0.0] * len(PATH_ACTIONS) for _ in range(3)]
        self.Q_cwnd = [[0.0] * len(CWND_ACTIONS) for _ in range(3)]
        # per-subflow state
        self.cwnd = {}
        self.in_flight = {}
        self.loss = {}
        self.delay = {}
        for f in flows:
            for sf in f.subflows:
                self.cwnd[sf.sid] = 10
                self.in_flight[sf.sid] = 0
                self.loss[sf.sid] = 0.0
                self.delay[sf.sid] = 0.0

    def decide_actions(self, ecn_by_sw, avg_occ):
        """High: path type; Low: cwnd multiplier. Returns (state, cwnd)."""
        ecn_agg = max(ecn_by_sw.values()) if ecn_by_sw else 0.0
        if ecn_agg >= 0.4 or avg_occ >= 0.8:
            state = 2
        elif ecn_agg >= 0.1 or avg_occ >= 0.5:
            state = 1
        else:
            state = 0
        # greedy from Q tables (or learned policy)
        path_idx = max(range(len(PATH_ACTIONS)),
                       key=lambda i: self.Q_path[state][i])
        cwnd_idx = max(range(len(CWND_ACTIONS)),
                       key=lambda i: self.Q_cwnd[state][i])
        return state, PATH_ACTIONS[path_idx], CWND_ACTIONS[cwnd_idx]

    def update(self, state, path_idx, cwnd_idx, reward, alpha=0.3, gamma=0.9):
        """Q update from a real-world reward."""
        self.Q_path[state][path_idx] += alpha * (
            reward + gamma * max(self.Q_path[state]) - self.Q_path[state][path_idx])
        self.Q_cwnd[state][cwnd_idx] += alpha * (
            reward + gamma * max(self.Q_cwnd[state]) - self.Q_cwnd[state][cwnd_idx])

    def save(self, path=None):
        if path is None:
            path = os.path.join(_HERE, 'policy_mptcp_real.json')
        with open(path, 'w') as f:
            json.dump({
                'Q_path': self.Q_path,
                'Q_cwnd': self.Q_cwnd,
                'actions_path': PATH_ACTIONS,
                'actions_cwnd': CWND_ACTIONS,
            }, f, indent=2)
        return path
