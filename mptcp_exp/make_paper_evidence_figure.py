#!/usr/bin/env python3
"""Build the corrected paper-evidence figure from unfiltered repetitions."""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import Patch
from matplotlib.ticker import NullFormatter

from audit_panel_alignment import require_matplotlib_panel_alignment

plt.rcParams['font.family'] = 'sans-serif'
plt.rcParams['font.sans-serif'] = ['Arial', 'DejaVu Sans', 'Liberation Sans']
plt.rcParams['svg.fonttype'] = 'none'
plt.rcParams['pdf.fonttype'] = 42
plt.rcParams.update({'svg.fonttype': 'none', 'pdf.fonttype': 42})
plt.rcParams["font.size"] = 7
plt.rcParams["axes.linewidth"] = 0.8
plt.rcParams["axes.spines.top"] = False
plt.rcParams["axes.spines.right"] = False
plt.rcParams["legend.frameon"] = False

HERE = Path(__file__).resolve().parent
RES = HERE / "results"
OUT = RES / "paper_evidence"
OUT.mkdir(parents=True, exist_ok=True)
cc = json.loads((RES / "t5_cc.json").read_text(encoding="utf-8"))
dyn = json.loads((RES / "dynamic_adaptation.json").read_text(encoding="utf-8"))
stages = {s: json.loads((RES / f"fig8_stage{s}.json").read_text(encoding="utf-8"))
          for s in (0, 1, 3, 15)}

COL = {"local": "#767676", "rl": "#0F4D92", "offline": "#8FAFD2",
       "lia": "#42949E", "olia": "#9A4D8E", "fixed": "#B64342",
       "aimd": "#D79B45", "missing": "#B64342", "dup": "#8FAFD2"}

fig, axs = plt.subplots(2, 2, figsize=(7.09, 5.35), constrained_layout=True)
ax_a, ax_b, ax_c, ax_d = axs.ravel()

# a: throughput-latency operating points. Each faint point is one independent run.
mode_order = ["local", "rl_offline", "rl", "lia", "olia", "fixed", "aimd"]
labels = {"local": "Local", "rl_offline": "Residual (offline)",
          "rl": "Residual (fine-tuned)", "lia": "LIA-inspired",
          "olia": "OLIA-inspired", "fixed": "Fixed cwnd", "aimd": "AIMD"}
colors = {"local": COL["local"], "rl_offline": COL["offline"], "rl": COL["rl"],
          "lia": COL["lia"], "olia": COL["olia"], "fixed": COL["fixed"],
          "aimd": COL["aimd"]}
for mode in mode_order:
    x = np.array([sum(r) for r in cc[mode]["per_rep"]], float)
    y = np.array([r["delay_p95_ms"] for r in cc[mode]["metrics_per_rep"]], float)
    if np.any(y <= 0):
        raise ValueError("p95 latency must be positive for the log axis")
    ax_a.scatter(x, y, s=10, color=colors[mode], alpha=0.28, linewidths=0)
    ax_a.plot(x.mean(), y.mean(), "o", ms=5, color=colors[mode],
              label=labels[mode])
ax_a.set_yscale("log")
ax_a.set_yticks([300, 500, 1000, 3000, 7000])
ax_a.set_yticklabels(["300", "500", "1,000", "3,000", "7,000"])
ax_a.yaxis.set_minor_formatter(NullFormatter())
ax_a.set_xlabel("Aggregate throughput (segment/s)")
ax_a.set_ylabel("p95 application latency (ms)")
ax_a.set_title("Throughput–latency operating points", loc="left", fontsize=8)
key_pos = [(0.61, 0.96), (0.61, 0.89), (0.61, 0.82), (0.61, 0.75),
           (0.82, 0.96), (0.82, 0.89), (0.82, 0.82)]
key_text = ["Local", "Residual off.", "Residual tuned", "LIA",
            "OLIA", "Fixed", "AIMD"]
for mode, txt, (kx, ky) in zip(mode_order, key_text, key_pos):
    ax_a.text(kx, ky, txt, transform=ax_a.transAxes, color=colors[mode],
              fontsize=5.8, ha="left", va="top", fontweight="bold")

# b/c: dynamic phases, mean ± sample SD across complete runs (n=3).
phases = ["normal", "congested", "recovery"]
phase_labels = ["Normal", "High ECN", "Recovery"]
x = np.arange(3)
for mode in ("local", "rl"):
    rows = dyn["modes"][mode]["per_rep"]
    vals = np.array([[r["phases"][p]["throughput_seg_s"] for p in phases]
                     for r in rows])
    ax_b.plot(x, vals.mean(0), "o-", color=COL[mode], lw=1.4, ms=4,
              label="Local" if mode == "local" else "Residual Q")
    ax_b.fill_between(x, vals.mean(0) - vals.std(0, ddof=1),
                      vals.mean(0) + vals.std(0, ddof=1),
                      color=COL[mode], alpha=0.16, linewidth=0)
    shares = np.array([[r["phases"][p]["path_share"].get("direct", 0) for p in phases]
                       for r in rows]) * 100
    ax_c.plot(x, shares.mean(0), "o-", color=COL[mode], lw=1.4, ms=4,
              label="Local" if mode == "local" else "Residual Q")
    ax_c.fill_between(x, shares.mean(0) - shares.std(0, ddof=1),
                      shares.mean(0) + shares.std(0, ddof=1),
                      color=COL[mode], alpha=0.16, linewidth=0)
for ax in (ax_b, ax_c):
    ax.set_xticks(x, phase_labels)
    ax.set_xlim(-0.1, 2.45)
ax_b.text(2.08, np.mean([r["phases"]["recovery"]["throughput_seg_s"]
                         for r in dyn["modes"]["local"]["per_rep"]]),
          "Local", color=COL["local"], fontsize=6, va="center")
ax_b.text(2.08, np.mean([r["phases"]["recovery"]["throughput_seg_s"]
                         for r in dyn["modes"]["rl"]["per_rep"]]),
          "Residual Q", color=COL["rl"], fontsize=6, va="center")
ax_c.text(2.08, 100 * np.mean([r["phases"]["recovery"]["path_share"].get("direct", 0)
                               for r in dyn["modes"]["local"]["per_rep"]]),
          "Local", color=COL["local"], fontsize=6, va="center")
ax_c.text(2.08, 100 * np.mean([r["phases"]["recovery"]["path_share"].get("direct", 0)
                               for r in dyn["modes"]["rl"]["per_rep"]]),
          "Residual Q", color=COL["rl"], fontsize=6, va="center")
ax_b.set_ylabel("Aggregate throughput (segment/s)")
ax_b.set_title("Response to a within-run ECN shift", loc="left", fontsize=8)
ax_c.set_ylabel("Direct-path traffic share (%)")
ax_c.set_title("Traffic shifts away from ECN paths", loc="left", fontsize=8)

# d: fault-recovery ablation; missing and duplicate segments are distinct costs.
stage_order = [0, 1, 3, 15]
stage_labels = ["None", "Replay", "Replay + NAK", "Full"]
missing_runs, dup_runs = [], []
for s in stage_order:
    rows = stages[s]["per_rep"]
    missing_runs.append(np.array([r["target_segments"] - r["ordered"] - r["in_buf"]
                                  for r in rows], float))
    dup_runs.append(np.array([r["dup"] for r in rows], float))
width = 0.36
xx = np.arange(4)
ax_d.bar(xx - width / 2, [v.mean() for v in missing_runs], width,
         color=COL["missing"], linewidth=0, label="Missing")
ax_d.bar(xx + width / 2, [v.mean() for v in dup_runs], width,
         color=COL["dup"], linewidth=0, label="Duplicate")
for i, vals in enumerate(missing_runs):
    jitter = np.linspace(-0.055, 0.055, len(vals))
    ax_d.scatter(np.full(len(vals), xx[i] - width / 2) + jitter, vals,
                 s=8, color="#6E2020", zorder=3, linewidths=0)
for i, vals in enumerate(dup_runs):
    jitter = np.linspace(-0.055, 0.055, len(vals))
    ax_d.scatter(np.full(len(vals), xx[i] + width / 2) + jitter, vals,
                 s=8, color="#365F88", zorder=3, linewidths=0)
ax_d.set_xticks(xx, stage_labels, rotation=15, ha="right", rotation_mode="anchor")
ax_d.set_ylabel("Segments per 800-segment workload")
ax_d.set_title("Recovery completeness and overhead", loc="left", fontsize=8)
ax_d.legend(fontsize=6, handletextpad=1.6)
ax_d.grid(axis="y", ls=":", lw=0.4, alpha=0.45)

for label, ax in zip("abcd", (ax_a, ax_b, ax_c, ax_d)):
    ax.text(-0.13, 1.05, label, transform=ax.transAxes, fontsize=9,
            fontweight="bold", va="bottom", ha="left")

fig.suptitle("Residual control lowers latency while layered recovery restores completeness",
             fontsize=9, fontweight="bold")
fig.canvas.draw()
base = OUT / "fig_paper_evidence"
require_matplotlib_panel_alignment(
    fig, axes=[ax_a, ax_b, ax_c, ax_d], panel_ids=list("abcd"),
    row_groups=[["a", "b"], ["c", "d"]],
    column_groups=[["a", "c"], ["b", "d"]],
    json_out=str(base) + ".alignment.json",
    overlay_svg=str(base) + ".alignment.svg",
    tolerance_pt=1.5, gutter_tolerance_pt=1.5,
    require_panel_labels=False, strict=True)
fig.savefig(str(base) + ".svg")
fig.savefig(str(base) + ".pdf")
fig.savefig(str(base) + ".tiff", dpi=600)
fig.savefig(str(base) + ".png", dpi=300)
plt.close(fig)
print(base)
