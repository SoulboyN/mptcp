#!/usr/bin/env python2
"""
rl_train.py -- offline Q-learning for a GLOBAL flow-control scheduler.

Idea
----
We train a tiny policy in a *flow-level simulator* (a stand-in for the
real BMv2 network) so the RL loop converges quickly. The learned Q-table
maps a global congestion state to a per-flow rate multiplier.

State  = coarse congestion level across the network (0..2):
         0 = low (mean ECN-marking rate < 0.1)
         1 = moderate
         2 = congested (mean ECN-marking rate >= 0.4)
Action = one shared rate multiplier applied to every flow this round:
         0: x1.2   (aggressive)
         1: x1.0   (hold)
         2: x0.7   (back off)
         3: x0.4   (strong back off)
Reward = throughput_utilization - 0.4*mean_delay - 1.5*loss_rate
         (throughput utilization rewards high goodput; delay and loss are
          the "special-scenario" penalties we want to minimize)

The simulator is a tiny fluid model: each flow gets a share of the
bottleneck link; if aggregate demand exceeds capacity, the excess becomes
loss, ECN marks rise, and queues grow (delay rises). The learned policy is
exactly what a real control plane would use, and here we export it as
policy.json (a list of per-state action indices).
"""

import json
import random
import os

# ---------------- tiny flow-level simulator ----------------

BOTTLENECK_BW = 100.0   # units of bandwidth
BASE_RATE     = 10.0    # per-flow base rate (so 8 flows oversubscribe)

def simulate(policy, steps=400, n_flows=8, seed=1):
    """Run the simulator; returns aggregate stats over the last window."""
    rnd = random.Random(seed)
    # per-flow rates start at base
    rates = [BASE_RATE] * n_flows
    ema_ecn = 0.0
    ema_delay = 0.0
    ema_loss = 0.0
    # collect rewards during training (Q-learning uses per-step reward)
    state_hist = []
    for t in range(steps):
        demand = sum(rates)
        util = min(demand, BOTTLENECK_BW) / BOTTLENECK_BW
        loss = max(0.0, demand - BOTTLENECK_BW) / max(demand, 1e-9)
        # ECN marks and delay grow with oversubscription
        ecn = min(1.0, max(0.0, loss * 2.0 + rnd.gauss(0, 0.03)))
        delay = 5.0 + 40.0 * max(0.0, util - 0.6) + rnd.gauss(0, 1.0)
        delay = max(1.0, delay)

        ema_ecn = 0.8 * ema_ecn + 0.2 * ecn
        ema_delay = 0.8 * ema_delay + 0.2 * delay
        ema_loss = 0.8 * ema_loss + 0.2 * loss

        # coarse state
        if ema_ecn < 0.1:
            s = 0
        elif ema_ecn < 0.4:
            s = 1
        else:
            s = 2

        # choose action
        a = policy[s]
        mul = [1.2, 1.0, 0.7, 0.4][a]
        # apply with small per-flow jitter
        rates = [min(BASE_RATE * 1.8, max(BASE_RATE * 0.2, r * mul)) for r in rates]

        # reward: throughput util, penalize delay and loss
        r = util - 0.4 * (delay / 40.0) - 1.5 * loss
        state_hist.append((s, a, r))
    return state_hist, ema_ecn, ema_delay, ema_loss


# ---------------- Q-learning ----------------

def train():
    random.seed(7)
    n_states = 3
    n_actions = 4
    Q = [[0.0] * n_actions for _ in range(n_states)]
    alpha = 0.3
    gamma = 0.9
    eps = 0.3
    policy = [2, 1, 3]  # fallback: low->x1.0, mid->x0.7, high->x0.4

    for episode in range(300):
        # random initial policy for exploration is embedded via eps-greedy
        rnd = random.Random(episode)
        for step in range(40):
            s = rnd.randint(0, 2)
            if rnd.random() < eps:
                a = rnd.randint(0, 3)
            else:
                a = max(range(n_actions), key=lambda i: Q[s][i])
            # reward from a one-step sim snapshot with this (s, a)
            mul = [1.2, 1.0, 0.7, 0.4][a]
            demand = 8 * BASE_RATE * mul
            util = min(demand, BOTTLENECK_BW) / BOTTLENECK_BW
            loss = max(0.0, demand - BOTTLENECK_BW) / max(demand, 1e-9)
            # congested state -> high delay penalty
            delay = 5.0 + (s * 25.0)
            r = util - 0.4 * (delay / 40.0) - 1.5 * loss
            # next state follows congestion after this action
            if s == 0:
                s_next = 0 if util < 0.6 else (1 if util < 0.9 else 2)
            elif s == 1:
                s_next = 1 if loss < 0.1 else 2
            else:
                s_next = 2 if loss > 0.2 else 1
            Q[s][a] = Q[s][a] + alpha * (r + gamma * max(Q[s_next]) - Q[s][a])

        # slowly anneal epsilon
        eps = max(0.05, eps * 0.995)

    # extract greedy policy
    policy = [max(range(n_actions), key=lambda i: Q[s][i]) for s in range(n_states)]
    return Q, policy


def main():
    print '=== Training Q-learning global scheduler ==='
    Q, policy = train()
    print 'Q table:'
    for s in range(3):
        print '  state', s, ['%.2f' % v for v in Q[s]]
    print 'learned policy (state -> action):', policy
    print 'action legend: [x1.2, x1.0, x0.7, x0.4]'

    # evaluate learned policy vs fixed policies in the simulator
    results = {}
    for name, pol in [('learned', policy),
                      ('fixed x0.7', [2, 2, 2]),
                      ('fixed x1.0', [1, 1, 1])]:
        hist, ecn, delay, loss = simulate(pol, steps=500, n_flows=8, seed=3)
        avg_reward = sum(x[2] for x in hist) / len(hist)
        results[name] = (avg_reward, ecn, delay, loss)
        print '  %-12s avg_reward=%.3f  ecn=%.2f  delay=%.1f  loss=%.2f' % (
            name, avg_reward, ecn, delay, loss)

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'policy.json')
    with open(out, 'w') as f:
        json.dump({'actions': [1.2, 1.0, 0.7, 0.4], 'policy': policy}, f)
    print 'policy exported to', out


if __name__ == '__main__':
    main()
