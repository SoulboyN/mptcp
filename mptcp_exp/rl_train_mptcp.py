#!/usr/bin/env python2
"""
rl_train_mptcp.py -- hierarchical Q-learning for the MPTCP experiment:
  HIGH level  Q_path : which PATH (subflow) gets the next DSN segment.
      state = congestion (0..2); action = {sw, direct}
      reward = throughput - delay - loss (path's contribution)
  LOW  level  Q_cwnd : cwnd multiplier for subflows.
      state = congestion (0..2); action = {x2, x1, x0.5, x0.25}
      reward = throughput - loss   (per-path rate vs drops)

Trained alternately per the paper's Algorithm 1: freeze Q_cwnd while
training Q_path, then freeze Q_path while training Q_cwnd. Exported to
policy_mptcp.json.

This is the "SDN global awareness -> hierarchical DRL (path selection +
congestion-window control)" mechanism of the thesis.
"""

import json
import os
import random

# ---- environment / training parameters ----
N_STATES = 3                       # congestion 0/1/2
ACTIONS_PATH = ['sw', 'direct']    # high-level: path type to prefer
ACTIONS_CWND = [2.0, 1.0, 0.5, 0.25]   # low-level: cwnd multiplier
BOTTLENECK_BW = 100.0
N_SUB = 3                          # subflows per connection in sim
EPISODES = 200
INNER = 20
ALPHA = 0.3
GAMMA = 0.9


def env_fingerprint():
    import hashlib
    payload = (N_STATES, tuple(ACTIONS_PATH), tuple(ACTIONS_CWND),
               EPISODES, BOTTLENECK_BW, N_SUB)
    return hashlib.sha1(repr(payload)).hexdigest()[:16]


def step_sim(congestion, path_share, cwnd_mul):
    """Fluid-model reward for choosing `path_share` (0..1 of traffic on the
    bottleneck path) with cwnd multiplier `cwnd_mul`."""
    rnd = random.Random()
    # demand scales with cwnd multiplier and how much goes on bottleneck
    demand = BOTTLENECK_BW * cwnd_mul * (0.4 + 0.6 * path_share)
    util = min(demand, BOTTLENECK_BW) / BOTTLENECK_BW
    loss = max(0.0, demand - BOTTLENECK_BW) / max(demand, 1e-9)
    delay = 5.0 + congestion * 25.0 + rnd.gauss(0, 1.0)
    delay = max(1.0, delay)
    return util, delay, loss


def train():
    random.seed(7)
    Q_path = [[0.0] * len(ACTIONS_PATH) for _ in range(N_STATES)]
    Q_cwnd = [[0.0] * len(ACTIONS_CWND) for _ in range(N_STATES)]
    eps = 0.3

    for episode in range(EPISODES):
        rnd = random.Random(episode)

        # ---- train HIGH (Q_path) while LOW (Q_cwnd) frozen ----
        for _ in range(INNER):
            c = rnd.randint(0, N_STATES - 1)
            if rnd.random() < eps:
                a = rnd.randint(0, len(ACTIONS_PATH) - 1)
            else:
                a = max(range(len(ACTIONS_PATH)), key=lambda i: Q_path[c][i])
            path = ACTIONS_PATH[a]
            share = 0.2 if path == 'direct' else 0.8   # direct unshares bottleneck
            # fixed cwnd multiplier (low level frozen)
            util, delay, loss = step_sim(c, share, 1.0)
            # reward includes path COST (heterogeneous access economics):
            # sw2 (cellular) is expensive, sw1 (wifi) free, direct cheap.
            path_cost = {'direct': 0.1, 'sw1': 0.0, 'sw2': 1.0, 'sw3': 0.2}
            cost_penalty = 0.5 * path_cost.get(path, 0.0)
            r = util - 0.4 * (delay / 40.0) - 1.5 * loss - cost_penalty
            s_next = 2 if loss > 0.2 else (1 if share > 0.5 else 0)
            Q_path[c][a] += ALPHA * (r + GAMMA * max(Q_path[s_next]) - Q_path[c][a])

        # ---- train LOW (Q_cwnd) while HIGH (Q_path) frozen ----
        for _ in range(INNER):
            c = rnd.randint(0, N_STATES - 1)
            if rnd.random() < eps:
                a = rnd.randint(0, len(ACTIONS_CWND) - 1)
            else:
                a = max(range(len(ACTIONS_CWND)), key=lambda i: Q_cwnd[c][i])
            mul = ACTIONS_CWND[a]
            # fixed path share (high level frozen)
            util, delay, loss = step_sim(c, 0.5, mul)
            r = util - 1.5 * loss
            s_next = 2 if loss > 0.2 else (1 if util > 0.8 else 0)
            Q_cwnd[c][a] += ALPHA * (r + GAMMA * max(Q_cwnd[s_next]) - Q_cwnd[c][a])

        eps = max(0.05, eps * 0.995)

    pol_path = [max(range(len(ACTIONS_PATH)), key=lambda i: Q_path[c][i])
                for c in range(N_STATES)]
    pol_cwnd = [max(range(len(ACTIONS_CWND)), key=lambda i: Q_cwnd[c][i])
                for c in range(N_STATES)]
    return Q_path, Q_cwnd, pol_path, pol_cwnd


def main():
    print '=== Hierarchical Q-learning (MPTCP path + cwnd) ==='
    Qp, Qc, pp, pc = train()
    print 'Q_path:'
    for c in range(N_STATES):
        print '  state', c, ['%.2f' % v for v in Qp[c]]
    print 'Q_cwnd:'
    for c in range(N_STATES):
        print '  state', c, ['%.2f' % v for v in Qc[c]]
    print 'policy_path (state->path):', [ACTIONS_PATH[i] for i in pp]
    print 'policy_cwnd (state->mult):', [ACTIONS_CWND[i] for i in pc]

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       'policy_mptcp.json')
    with open(out, 'w') as f:
        json.dump({
            'actions_path': ACTIONS_PATH,
            'policy_path': pp,
            'actions_cwnd': ACTIONS_CWND,
            'policy_cwnd': pc,
            'env_fingerprint': env_fingerprint(),
        }, f, indent=2)
    print 'policy exported to', out
    print 'env fingerprint:', env_fingerprint()


if __name__ == '__main__':
    main()
