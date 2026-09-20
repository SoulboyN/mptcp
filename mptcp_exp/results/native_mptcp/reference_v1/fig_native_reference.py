#!/usr/bin/env python3
"""Native MPTCP reference: static cost and blackhole-recovery benefit."""
import csv
import math
import statistics
import sys
from pathlib import Path

import matplotlib as mpl
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parent
SKILL_SCRIPTS = Path(r'C:/Users/ning/.agents/skills/nature-figure/scripts')
sys.path.insert(0, str(SKILL_SCRIPTS))
from audit_panel_alignment import require_matplotlib_panel_alignment

mpl.rcParams.update({
    'font.family': 'sans-serif',
    'font.sans-serif': ['Arial', 'Helvetica', 'DejaVu Sans', 'sans-serif'],
    'svg.fonttype': 'none', 'pdf.fonttype': 42,
    'font.size': 7.5, 'axes.labelsize': 7.5, 'axes.titlesize': 8,
    'xtick.labelsize': 7.5, 'ytick.labelsize': 7.5,
    'axes.spines.right': False, 'axes.spines.top': False,
    'axes.linewidth': 0.7, 'legend.frameon': False,
})

COLORS = {'tcp': '#8A8F98', 'mptcp': '#2878B5'}
T95 = {3: 4.303, 5: 2.776}


def load_rows():
    with (ROOT / 'source_data.csv').open(newline='') as handle:
        return list(csv.DictReader(handle))


def values(rows, scenario, protocol, metric):
    chosen = sorted((row for row in rows if row['scenario'] == scenario and
                     row['protocol'] == protocol), key=lambda row: int(row['rep']))
    return [float(row[metric]) for row in chosen]


def mean_ci(data):
    mean = statistics.mean(data)
    half = T95[len(data)] * statistics.stdev(data) / math.sqrt(len(data))
    return mean, mean - half, mean + half


def paired_panel(ax, tcp, mptcp, ylabel, title, log=False):
    if log:
        assert min(tcp + mptcp) > 0, 'log-scaled metrics must be strictly positive'
    for left, right in zip(tcp, mptcp):
        ax.plot([0, 1], [left, right], color='#CBD0D6', lw=0.65, zorder=1)
    ax.scatter([0] * len(tcp), tcp, s=18, facecolor=COLORS['tcp'],
               edgecolor='white', linewidth=0.45, zorder=2)
    ax.scatter([1] * len(mptcp), mptcp, s=18, facecolor=COLORS['mptcp'],
               edgecolor='white', linewidth=0.45, zorder=2)
    for x, data in enumerate((tcp, mptcp)):
        mean, low, high = mean_ci(data)
        ax.errorbar(x, mean, yerr=[[mean-low], [high-mean]], fmt='D', ms=3.5,
                    color='#20242A', capsize=2.4, lw=0.9, zorder=3)
    if log:
        ax.set_yscale('log')
    ax.set_xlim(-0.35, 1.35)
    ax.set_xticks([0, 1], ['TCP', 'MPTCP'])
    ax.set_ylabel(ylabel)
    ax.set_title(title, pad=5, fontweight='bold')
    ax.grid(axis='y', color='#E5E8EB', lw=0.45, zorder=0)


def main():
    rows = load_rows()
    fig, axes = plt.subplots(1, 3, figsize=(7.20, 2.50))
    fig.subplots_adjust(left=0.085, right=0.985, bottom=0.20, top=0.80, wspace=0.42)
    paired_panel(axes[0], values(rows, 'static', 'tcp', 'throughput_seg_s'),
                 values(rows, 'static', 'mptcp', 'throughput_seg_s'),
                 'Aggregate throughput (segment/s)', 'Static throughput', log=True)
    paired_panel(axes[1], values(rows, 'static', 'tcp', 'mean_flow_delay_ms'),
                 values(rows, 'static', 'mptcp', 'mean_flow_delay_ms'),
                 'Ordered delivery delay (ms)', 'Static delay', log=True)
    paired_panel(axes[2], values(rows, 'fault', 'tcp', 'recovery_max_gap_s'),
                 values(rows, 'fault', 'mptcp', 'recovery_max_gap_s'),
                 'Maximum delivery gap (s)', 'Primary-path blackhole')
    for label, ax in zip('abc', axes):
        ax.annotate(label, xy=(0, 1), xycoords='axes fraction', xytext=(-20, 8),
                    textcoords='offset points', fontsize=8, fontweight='bold',
                    va='bottom', ha='left', annotation_clip=False)
    fig.canvas.draw()
    base = ROOT / 'fig_native_reference'
    require_matplotlib_panel_alignment(
        fig, json_out=str(base) + '.alignment.json',
        overlay_svg=str(base) + '.alignment.svg', tolerance_pt=1.5,
        gutter_tolerance_pt=1.5, require_panel_labels=True, strict=True)
    fig.savefig(str(base) + '.svg', bbox_inches='tight')
    fig.savefig(str(base) + '.pdf', bbox_inches='tight')
    fig.savefig(str(base) + '.tiff', dpi=600, bbox_inches='tight')
    fig.savefig(str(base) + '.png', dpi=600, bbox_inches='tight')


if __name__ == '__main__':
    main()
