# -*- coding: utf-8 -*-
"""论文数据图(鲜明多色风):读 results/*.json 真实数据,带均值±std 误差棒。
生成 4 张数据图:fig_resilience(图8 断链韧性)/ fig_cc_compare(图9 五模式)/
fig_residual_policy(图10 残差策略)/ fig_cwnd_ecn(图6 部署 cwnd)。"""
import os
import json
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

plt.rcParams['font.sans-serif'] = ['Microsoft YaHei', 'SimHei']
plt.rcParams['axes.unicode_minus'] = False
plt.rcParams['font.size'] = 9

HERE = os.path.dirname(os.path.abspath(__file__))
RES = os.path.join(HERE, 'results')
OUT = os.path.join(HERE, 'images')
os.makedirs(OUT, exist_ok=True)

# 鲜明多色调色板(模式/系列色)
C_RL    = '#e6194B'   # 洋红
C_LIA   = '#3cb44b'   # 绿
C_OLIA  = '#4363d8'   # 蓝
C_FIXED = '#f58231'   # 橙
C_AIMD  = '#911eb4'   # 紫
C_DIR   = '#0082c8'   # 直连蓝
C_SW    = '#e6194B'   # 交换机红


# ============ 图 8:断链韧性(受控消融,读 results/fig8_stage*.json) ============
import glob as _glob
abl_files = sorted(_glob.glob(os.path.join(RES, 'fig8_stage*.json')))
if abl_files:
    stage_names = [u'无恢复', u'+go-back-N', u'+NAK/SACK', u'+停滞检测']
    in_buf_mean, in_buf_std, ord_mean, ord_std, dup_mean = [], [], [], [], []
    for f in abl_files:
        d = json.load(open(f, encoding='utf-8'))
        ib = [r['in_buf'] for r in d['per_rep']]
        od = [r['ordered'] for r in d['per_rep']]
        dp = [r['dup'] for r in d['per_rep']]
        in_buf_mean.append(np.mean(ib)); in_buf_std.append(np.std(ib))
        ord_mean.append(np.mean(od)); ord_std.append(np.std(od))
        dup_mean.append(np.mean(dp))
    x = np.arange(len(abl_files))
    fig, ax1 = plt.subplots(figsize=(5.6, 3.4))
    bars = ax1.bar(x, in_buf_mean, 0.5, yerr=in_buf_std, capsize=4,
                   color='#e6194B', edgecolor='#a80033', label=u'乱序堆积 in_buf')
    ax1.set_ylabel(u'in_buf (段)')
    ax1.set_ylim(0, max(in_buf_mean) * 1.7 + 1500)
    ax1.set_xticks(x)
    ax1.set_xticklabels(stage_names[:len(abl_files)], fontsize=8.5)
    ax2 = ax1.twinx()
    ax2.errorbar(x, ord_mean, yerr=ord_std, fmt='s--', color='#4363d8',
                 lw=1.6, ms=6, capsize=3, label=u'按序交付 ordered')
    ax2.set_ylabel(u'ordered (段)')
    ax2.set_ylim(0, max(ord_mean) * 1.3 + 1500)
    for i, v in enumerate(in_buf_mean):
        ax1.text(i, v + 800, '%.0f' % v, ha='center', fontsize=8.5, color='#a80033')
    l1, lab1 = ax1.get_legend_handles_labels()
    l2, lab2 = ax2.get_legend_handles_labels()
    ax1.legend(l1 + l2, lab1 + lab2, fontsize=8, loc='upper right')
    ax1.set_title(u'断链韧性:逐层启用恢复,缺口被补回、堆积归零', fontsize=10.5)
    fig.tight_layout()
    fig.savefig(os.path.join(OUT, 'fig_resilience.png'), dpi=300)
    plt.close()
    print('  fig_resilience: in_buf mean', [round(v, 0) for v in in_buf_mean],
          'ordered mean', [round(v, 0) for v in ord_mean])

# ============ 图 9:五模式吞吐对比(读 t5_cc.json) ============
cc_path = os.path.join(RES, 't5_cc.json')
if os.path.exists(cc_path):
    cc = json.load(open(cc_path, encoding='utf-8'))
    order = ['rl', 'lia', 'olia', 'fixed', 'aimd']
    labels = [u'RL-cwnd', u'MPTCP LIA', u'MPTCP OLIA', u'固定 cwnd=32', u'伪 Reno']
    cols = [C_RL, C_LIA, C_OLIA, C_FIXED, C_AIMD]
    flow_names = [u'流 1', u'流 2', u'流 3']
    nf = len(cc['rl']['mean'])
    fig, ax = plt.subplots(figsize=(6.2, 3.4))
    for i, (mo, lab, col) in enumerate(zip(order, labels, cols)):
        m = cc[mo]['mean']
        s = cc[mo]['std']
        xs = [i * (nf + 1.2) + k for k in range(nf)]
        ax.bar(xs, m, 0.85, yerr=s, capsize=3, color=col, label=u'%s' % lab)
        for xi, mi in zip(xs, m):
            ax.text(xi, mi + 2, '%.0f' % mi, ha='center', fontsize=7.5)
    ax.set_xticks([i * (nf + 1.2) + 1 for i in range(len(order))])
    ax.set_xticklabels(labels, fontsize=8.5)
    ax.set_ylabel(u'吞吐 (seg/s)')
    ax.set_ylim(0, max(cc['fixed']['mean']) * 1.25 + 20)
    ax.grid(axis='y', ls=':', lw=0.5, alpha=0.6)
    ax.legend(fontsize=8, loc='upper left', ncol=2)
    jain_txt = '  '.join(u'%s Jain=%.3f' % (labels[i], cc[o]['jain'])
                         for i, o in enumerate(order))
    ax.set_title(u'五类拥塞控制的吞吐对比(均值±std,N=5)\n' + jain_txt, fontsize=10)
    fig.tight_layout()
    fig.savefig(os.path.join(OUT, 'fig_cc_compare.png'), dpi=300)
    plt.close()
    print('  fig_cc_compare: 5 modes mean/std 写图')

# ============ 图 10:残差策略(读 policy_mptcp.json) ============
pol_path = os.path.join(HERE, 'policy_mptcp.json')
if os.path.exists(pol_path):
    pol = json.load(open(pol_path, encoding='utf-8'))
    acts = pol['actions_residual']          # [0.5,0.75,1.0,1.25,1.5]
    pol_idx = pol['policy_residual']        # [2,3,4,0,0]
    residual = [acts[a] for a in pol_idx]   # [1.0,1.25,1.5,0.5,0.5]
    states = [u'状态0\n(空闲)', u'状态1', u'状态2', u'状态3', u'状态4\n(拥塞)']
    x = np.arange(5)
    fig, ax = plt.subplots(figsize=(5.0, 3.1))
    cols = [C_LIA if r > 1 else (C_DIR if r == 1 else C_SW) for r in residual]
    ax.bar(x, residual, 0.6, color=cols, edgecolor='#333', lw=0.8)
    ax.set_xticks(x); ax.set_xticklabels(states, fontsize=8.5)
    ax.set_ylabel(u'残差动作乘数')
    ax.axhline(1.0, color='#888', ls='--', lw=1)
    ax.text(4.6, 1.02, u'维持基线 ×1.0', fontsize=7, color='#555')
    for i, v in enumerate(residual):
        ax.text(i, v + 0.03, u'×%.2f' % v, ha='center', fontsize=9, fontweight='bold')
    ax.set_ylim(0, 1.75)
    ax.set_title(u'残差策略:低拥塞激进、高拥塞保守', fontsize=10.5)
    fig.tight_layout()
    fig.savefig(os.path.join(OUT, 'fig_residual_policy.png'), dpi=300)
    plt.close()
    print('  fig_residual_policy: residual', residual)

# ============ 图 6:部署 cwnd 随拥塞档(基线×残差) ============
if os.path.exists(pol_path):
    pol = json.load(open(pol_path, encoding='utf-8'))
    acts = pol['actions_residual']
    pol_idx = pol['policy_residual']
    residual = [acts[a] for a in pol_idx]
    DCQCN_MUL = [1.0, 0.8, 0.6, 0.4, 0.25]   # 交换机路径基线乘数(每档)
    BASE = 32.0
    sw_cwnd = [round(BASE * dc * r) for dc, r in zip(DCQCN_MUL, residual)]
    dir_cwnd = [round(BASE * r) for r in residual]
    x = np.arange(5)
    fig, ax = plt.subplots(figsize=(5.2, 3.1))
    w = 0.36
    ax.bar(x - w/2, sw_cwnd, w, color=C_SW, edgecolor='#a80033', lw=0.8, label=u'交换机子流(DCQCN 式降速×残差)')
    ax.bar(x + w/2, dir_cwnd, w, color=C_DIR, edgecolor='#00609e', lw=0.8, label=u'直连子流(残差)')
    ax.set_xticks(x); ax.set_xticklabels(states, fontsize=8.5)
    ax.set_ylabel(u'部署 cwnd')
    for i in range(5):
        ax.text(i - w/2, sw_cwnd[i] + 1.5, str(sw_cwnd[i]), ha='center', fontsize=8)
        ax.text(i + w/2, dir_cwnd[i] + 1.5, str(dir_cwnd[i]), ha='center', fontsize=8)
    ax.legend(fontsize=8, loc='upper right')
    ax.set_title(u'各拥塞档下子流部署 cwnd(基线×残差)', fontsize=10.5)
    fig.tight_layout()
    fig.savefig(os.path.join(OUT, 'fig_cwnd_ecn.png'), dpi=300)
    plt.close()
    print('  fig_cwnd_ecn: sw', sw_cwnd, 'direct', dir_cwnd)

print('figures written to', OUT)
