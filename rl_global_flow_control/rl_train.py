#!/usr/bin/env python2
"""
rl_train.py -- hierarchical Q-learning for global flow control.

Design (inspired by "AllReduce Scheduling with Hierarchical DRL", Wei et al.),
kept model-free Q-learning like the original experiment:

  High-level  Q_tree : WHICH flows to activate this round.
      state  = (congestion 0..2, remaining-work level 0/1)   -> 6 states
      action = {activate all, activate half, activate quarter}
      reward = throughput density + stage bonus (done -> +5)
  Low-level   Q_flow : WHAT rate multiplier for the active flows.
      state  = congestion (0..2)
      action = rate multiplier (x1.2 / x1.0 / x0.7 / x0.4)
      reward = util - 0.4*delay - 1.5*loss   (unchanged from original)

Training alternates per Algorithm 1 of the paper: freeze Q_flow while
training Q_tree, then freeze Q_tree while training Q_flow, repeat.

The result is exported to policy.json with both greedy policies and the
environment fingerprint (persisted; reused if the env hasn't changed).
"""

import json
import os
import random

# ---------------- environment / training parameters ----------------
# (these define the "environment"; changing any invalidates a saved policy)

BOTTLENECK_BW = 100.0   # units of bandwidth
BASE_RATE     = 10.0    # per-flow base rate

N_FLOWS      = 8        # number of flow pairs in the demo
N_STATES_LO  = 3        # low-level states: congestion 0/1/2
ACTIONS_LO   = [1.2, 1.0, 0.7, 0.4]          # low-level rate multipliers
REWARD_DELAY_COEF = 0.4
REWARD_LOSS_COEF  = 1.5

# high-level: activation tiers (fraction of flows to activate)
ACTIONS_HI = ['all', 'half', 'quarter']
STAGE_BONUS = 5.0        # stage reward when all work done (paper-style)

EPISODES = 300
ALT_INNER = 20           # alternating-training inner rounds (J and K)


def env_fingerprint():
    import hashlib
    payload = (N_FLOWS, N_STATES_LO, tuple(ACTIONS_LO),
               tuple(ACTIONS_HI), REWARD_DELAY_COEF, REWARD_LOSS_COEF,
               STAGE_BONUS, EPISODES, BOTTLENECK_BW, BASE_RATE)
    return hashlib.sha1(repr(payload)).hexdigest()[:16]


# ---------------- tiny flow-level simulator ----------------

def step_sim(congestion, n_active):
    """One round of the fluid simulator given the coarse congestion level
    and how many flows are active. Returns (util, delay, loss)."""
    rnd = random.Random()
    demand = n_active * BASE_RATE * 1.0        # nominal demand of active flows
    util = min(demand, BOTTLENECK_BW) / BOTTLENECK_BW
    loss = max(0.0, demand - BOTTLENECK_BW) / max(demand, 1e-9)
    delay = 5.0 + congestion * 25.0 + rnd.gauss(0, 1.0)
    delay = max(1.0, delay)
    return util, delay, loss


def hi_state(congestion, work_ratio):
    """High-level state index: (congestion, work level)."""
    work_level = 0 if work_ratio < 0.5 else 1   # 0 = near done, 1 = plenty
    return congestion * 2 + work_level


def hi_n_active(action_idx):
    """Map a high-level action to how many flows it activates."""
    if action_idx == 0:      # all
        return N_FLOWS
    if action_idx == 1:      # half
        return max(1, N_FLOWS / 2)
    return max(1, N_FLOWS / 4)   # quarter


# ---------------- hierarchical Q-learning ----------------

def train(init_Q=None, alpha=0.3, eps=0.3, episodes=None):
    """Q-learning with optional warm start.

    init_Q: (Q_tree, Q_flow) to continue from (incremental fine-tuning).
            If None, train from scratch.
    alpha/eps: learning/exploration rates. For fine-tuning pass small alpha
            (e.g. 0.05) so the base policy is preserved and only adapted.
    episodes: override EPISODES (fewer rounds for fine-tuning).
    """
    random.seed(7)

    # Q tables: warm-start or fresh
    if init_Q is not None:
        Q_tree = [row[:] for row in init_Q[0]]
        Q_flow = [row[:] for row in init_Q[1]]
    else:
        Q_tree = [[0.0] * len(ACTIONS_HI) for _ in range(6)]
        Q_flow = [[0.0] * len(ACTIONS_LO) for _ in range(N_STATES_LO)]

    gamma = 0.9
    if episodes is None:
        episodes = EPISODES

    for episode in range(episodes):
        rnd = random.Random(episode)

        # ---- alternate: train Q_tree while Q_flow is frozen ----
        for _ in range(ALT_INNER):
            c = rnd.randint(0, 2)                      # congestion
            work = rnd.random()                        # remaining-work ratio
            s = hi_state(c, work)
            if rnd.random() < eps:
                a = rnd.randint(0, len(ACTIONS_HI) - 1)
            else:
                a = max(range(len(ACTIONS_HI)), key=lambda i: Q_tree[s][i])
            n_active = hi_n_active(a)
            util, delay, loss = step_sim(c, n_active)
            # dense throughput reward + stage bonus when all done
            done = (work <= 0.0)
            r = util + (STAGE_BONUS if done else 0.0)
            work_next = max(0.0, work - n_active * 0.02)
            s_next = hi_state(c, work_next)
            Q_tree[s][a] += alpha * (r + gamma * max(Q_tree[s_next]) - Q_tree[s][a])

        # ---- train Q_flow while Q_tree is frozen ----
        for _ in range(ALT_INNER):
            c = rnd.randint(0, 2)                      # congestion
            n_active = hi_n_active(rnd.randint(0, len(ACTIONS_HI) - 1))
            s = c
            if rnd.random() < eps:
                a = rnd.randint(0, len(ACTIONS_LO) - 1)
            else:
                a = max(range(len(ACTIONS_LO)), key=lambda i: Q_flow[s][i])
            mul = ACTIONS_LO[a]
            # scale demand by the chosen rate multiplier
            demand = n_active * BASE_RATE * mul
            util = min(demand, BOTTLENECK_BW) / BOTTLENECK_BW
            loss = max(0.0, demand - BOTTLENECK_BW) / max(demand, 1e-9)
            delay = 5.0 + c * 25.0
            r = util - REWARD_DELAY_COEF * (delay / 40.0) - REWARD_LOSS_COEF * loss
            # next congestion: high demand -> more congested
            s_next = 2 if util > 0.9 else (1 if util > 0.6 else 0)
            Q_flow[s][a] += alpha * (r + gamma * max(Q_flow[s_next]) - Q_flow[s][a])

        eps = max(0.05, eps * 0.995)

    # extract greedy policies
    policy_tree = [max(range(len(ACTIONS_HI)), key=lambda i: Q_tree[s][i])
                   for s in range(6)]
    policy_flow = [max(range(len(ACTIONS_LO)), key=lambda i: Q_flow[s][i])
                   for s in range(N_STATES_LO)]
    return Q_tree, Q_flow, policy_tree, policy_flow


def main():
    print '=== Hierarchical Q-learning (flow-activation + rate) ==='
    Q_tree, Q_flow, p_tree, p_flow = train()
    print 'Q_tree (hi: state=0..5 -> Q values):'
    for s in range(6):
        print '  state', s, ['%.2f' % v for v in Q_tree[s]]
    print 'Q_flow (lo: congestion -> Q values):'
    for s in range(N_STATES_LO):
        print '  state', s, ['%.2f' % v for v in Q_flow[s]]
    print 'policy_tree (state -> action):', p_tree
    print '  action legend:', ACTIONS_HI
    print 'policy_flow (congestion -> action):', p_flow
    print '  action legend:', ACTIONS_LO

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'policy.json')
    with open(out, 'w') as f:
        json.dump({
            'actions_hi': ACTIONS_HI,
            'policy_tree': p_tree,
            'actions_lo': ACTIONS_LO,
            'policy_flow': p_flow,
            'Q_tree': Q_tree,
            'Q_flow': Q_flow,
            'env_fingerprint': env_fingerprint(),
            'meta': {
                'n_flows': N_FLOWS,
                'n_states_lo': N_STATES_LO,
                'episodes': EPISODES,
                'alt_inner': ALT_INNER,
                'reward_delay_coef': REWARD_DELAY_COEF,
                'reward_loss_coef': REWARD_LOSS_COEF,
                'stage_bonus': STAGE_BONUS,
                'bottleneck_bw': BOTTLENECK_BW,
                'base_rate': BASE_RATE,
            },
        }, f, indent=2)
    print 'policy exported to', out
    print 'env fingerprint:', env_fingerprint()


if __name__ == '__main__':
    main()
