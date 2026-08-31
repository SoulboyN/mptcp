#!/usr/bin/env python2
"""
rl_train_mptcp.py -- offline Q-learning for the RESIDUAL congestion control.

Paper topic: "SDN-global-aware MPTCP dynamic heterogeneous subflow
congestion control". The core mechanism is RL as the SDN global controller
that REFINES existing local mechanisms (DCQCN switch-ECN response, Credit)
with a small learned residual.

Design (quantized residual RL):
  state          : congestion quantized into 5 levels (0..4) from ECN/occ
  base cwnd      : computed by the LOCAL mechanism (DCQCN) per subflow
  residual action: quantized multiplier {x0.5, x1.0, x1.5} on the base
  final cwnd     = clamp(base_cwnd * residual, min, max)
So RL learns only "how much MORE/LESS aggressive than the base to be" per
congestion state -- fast, stable, and interpretable.

Exported to policy_mptcp.json (Q_residual + policy_residual + actions).
"""

import json
import os
import random

# ---- quantization / residual design (must match RlScheduler in mptcp_scheduler) ----
N_STATES = 5                          # congestion levels 0..4
STATE_ECN = [0.02, 0.1, 0.25, 0.5]    # 5-level quantizer boundaries
DCQCN_MUL = [1.0, 0.8, 0.6, 0.4, 0.25]  # DCQCN base cwnd multiplier per level
BASE_CWND = 32.0
ACTIONS_RESIDUAL = [0.5, 0.75, 1.0, 1.25, 1.5]    # quantized residual actions
EPISODES = 300
INNER = 20
ALPHA = 0.3
GAMMA = 0.9


def env_fingerprint():
    import hashlib
    payload = (N_STATES, tuple(STATE_ECN), tuple(DCQCN_MUL), BASE_CWND,
               tuple(ACTIONS_RESIDUAL), EPISODES)
    return hashlib.sha1(repr(payload)).hexdigest()[:16]


def step_sim(congestion, res_mul):
    """Fluid-model reward for residual action res_mul at congestion level 0..4.
    base cwnd shrinks with congestion (DCQCN); the residual multiplies it.
    util saturates at full load; overload creates loss; an aggressive residual
    at higher congestion inflates queueing delay strongly, so the model learns
    a DIFFERENT residual per state (grow to saturate when idle, be conservative
    when congested) instead of always picking an extreme."""
    base = BASE_CWND * DCQCN_MUL[congestion]
    cw = base * res_mul
    demand = 95.0 * (cw / BASE_CWND)          # demand vs bottleneck 100
    cap = 100.0
    load = demand / cap
    util = min(load, 1.0)
    loss = max(0.0, load - 1.0)
    delay = 5.0 + congestion * 20.0 + res_mul * congestion * 25.0
    return util, delay, loss


def train():
    random.seed(7)
    # Q: state x residual action
    Q_res = [[0.0] * len(ACTIONS_RESIDUAL) for _ in range(N_STATES)]
    eps = 0.3
    for episode in range(EPISODES):
        rnd = random.Random(episode)
        for _ in range(INNER):
            c = rnd.randint(0, N_STATES - 1)
            if rnd.random() < eps:
                a = rnd.randint(0, len(ACTIONS_RESIDUAL) - 1)
            else:
                a = max(range(len(ACTIONS_RESIDUAL)),
                        key=lambda i: Q_res[c][i])
            util, delay, loss = step_sim(c, ACTIONS_RESIDUAL[a])
            r = util - 0.4 * (delay / 40.0) - 1.5 * loss
            s_next = max(0, min(N_STATES - 1, c + (1 if loss > 0.1 else 0)))
            Q_res[c][a] += ALPHA * (r + GAMMA * max(Q_res[s_next]) - Q_res[c][a])
        eps = max(0.05, eps * 0.995)
    pol = [max(range(len(ACTIONS_RESIDUAL)), key=lambda i: Q_res[c][i])
           for c in range(N_STATES)]
    return Q_res, pol


def main():
    print '=== Offline residual Q-learning (5 states x 5 residual actions) ==='
    Qr, pr = train()
    print 'Q_residual (state -> x0.5 / x0.75 / x1.0 / x1.25 / x1.5):'
    for c in range(N_STATES):
        print '  state', c, ['%.2f' % v for v in Qr[c]]
    print 'policy_residual (state->action):',
    print [ACTIONS_RESIDUAL[i] for i in pr]

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       'policy_mptcp.json')
    with open(out, 'w') as f:
        json.dump({
            'actions_residual': ACTIONS_RESIDUAL,
            'policy_residual': pr,
            'Q_residual': Qr,
            # path selection stays heuristic (inverse pressure) -- keep a
            # matching entry so RlPathSelector loads consistently
            'actions_path': ['sw', 'direct'],
            'policy_path': [0, 0, 0, 0, 0],
            'env_fingerprint': env_fingerprint(),
        }, f, indent=2)
    print 'policy exported to', out
    print 'env fingerprint:', env_fingerprint()


if __name__ == '__main__':
    main()
