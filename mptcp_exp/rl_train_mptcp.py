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
# High-level: choose a PROPORTIONAL split profile across paths.
# Each profile says how to weigh {cheap, medium, expensive} paths.
# (continuous ratios, learned via a small set of discrete profiles)
ACTIONS_PATH = ['sw', 'direct']    # kept for backward-compat policy field
PROFILES = [                        # path-ratio profiles (cheap:mid:expensive)
    (0.6, 0.3, 0.1),               # 0: prefer cheap paths
    (0.4, 0.4, 0.2),               # 1: balanced
    (0.2, 0.3, 0.5),               # 2: accept expensive (when theyre fast)
]
ACTIONS_CWND = [2.0, 1.0, 0.5, 0.25]   # low-level: cwnd multiplier
BOTTLENECK_BW = 100.0
N_SUB = 3                          # subflows per connection in sim
EPISODES = 200
INNER = 20
ALPHA = 0.3
GAMMA = 0.9
# path economics (same as scheduler)
PATH_COST = {'direct': 0.1, 'sw1': 0.0, 'sw2': 1.0, 'sw3': 0.2}
COST_WEIGHT = 0.5


def env_fingerprint():
    import hashlib
    payload = (N_STATES, tuple(PROFILES), tuple(ACTIONS_CWND),
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
    # HIGH: choose a proportional-split PROFILE (action = profile index)
    Q_path = [[0.0] * len(PROFILES) for _ in range(N_STATES)]
    Q_cwnd = [[0.0] * len(ACTIONS_CWND) for _ in range(N_STATES)]
    eps = 0.3

    for episode in range(EPISODES):
        rnd = random.Random(episode)

        # ---- train HIGH (Q_path) while LOW (Q_cwnd) frozen ----
        for _ in range(INNER):
            c = rnd.randint(0, N_STATES - 1)
            if rnd.random() < eps:
                a = rnd.randint(0, len(PROFILES) - 1)
            else:
                a = max(range(len(PROFILES)), key=lambda i: Q_path[c][i])
            profile = PROFILES[a]           # (cheap, mid, expensive) ratios
            # weighted average cost of the split
            cheap, mid, exp = profile
            split_cost = (cheap * 0.0 +      # cheap path (wifi)
                          mid * 0.2 +        # mid path (fiber)
                          exp * 1.0)         # expensive path (cellular)
            share = 0.8                      # traffic on the bottleneck
            # fixed cwnd multiplier (low level frozen)
            util, delay, loss = step_sim(c, share, 1.0)
            r = util - 0.4 * (delay / 40.0) - 1.5 * loss - COST_WEIGHT * split_cost
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

    pol_path = [max(range(len(PROFILES)), key=lambda i: Q_path[c][i])
                for c in range(N_STATES)]
    pol_cwnd = [max(range(len(ACTIONS_CWND)), key=lambda i: Q_cwnd[c][i])
                for c in range(N_STATES)]
    return Q_path, Q_cwnd, pol_path, pol_cwnd


def main():
    print '=== Hierarchical Q-learning (MPTCP proportional split + cwnd) ==='
    Qp, Qc, pp, pc = train()
    print 'Q_path (state -> profile):'
    for c in range(N_STATES):
        print '  state', c, ['%.2f' % v for v in Qp[c]]
    print 'Q_cwnd:'
    for c in range(N_STATES):
        print '  state', c, ['%.2f' % v for v in Qc[c]]
    print 'policy_path (state->profile):', [PROFILES[i] for i in pp]
    print 'policy_cwnd (state->mult):', [ACTIONS_CWND[i] for i in pc]

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       'policy_mptcp.json')
    with open(out, 'w') as f:
        json.dump({
            'profiles': PROFILES,
            'policy_path': pp,               # state -> profile index
            'actions_cwnd': ACTIONS_CWND,
            'policy_cwnd': pc,
            'Q_path': Qp,                    # for real-env fine-tune init
            'Q_cwnd': Qc,
            'env_fingerprint': env_fingerprint(),
        }, f, indent=2)
    print 'policy exported to', out
    print 'env fingerprint:', env_fingerprint()


if __name__ == '__main__':
    main()
