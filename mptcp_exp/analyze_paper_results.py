#!/usr/bin/env python3
"""Aggregate the corrected experiments into paper-facing source data.

The independent unit is one complete experiment repetition.  No repetitions
are excluded.  Small-n uncertainty is reported descriptively as mean, sample
SD and a two-sided 95% t interval; paired intervals are used only where the
same repetition index contains both methods.
"""
from __future__ import annotations

import csv
import json
import math
from pathlib import Path

HERE = Path(__file__).resolve().parent
RES = HERE / "results"
OUT = RES / "paper_evidence"
OUT.mkdir(parents=True, exist_ok=True)

T975 = {2: 12.706, 3: 4.303, 4: 3.182, 5: 2.776}


def mean_sd_ci(values):
    vals = [float(x) for x in values]
    n = len(vals)
    mean = sum(vals) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in vals) / (n - 1)) if n > 1 else 0.0
    half = T975.get(n, 1.96) * sd / math.sqrt(n) if n > 1 else 0.0
    return {"n": n, "mean": mean, "sd": sd,
            "ci95_low": mean - half, "ci95_high": mean + half}


def paired_effect(a, b):
    """Effect a-b for matched repetition indices."""
    return mean_sd_ci([float(x) - float(y) for x, y in zip(a, b)])


cc = json.loads((RES / "t5_cc.json").read_text(encoding="utf-8"))
dyn = json.loads((RES / "dynamic_adaptation.json").read_text(encoding="utf-8"))
stages = {s: json.loads((RES / f"fig8_stage{s}.json").read_text(encoding="utf-8"))
          for s in (0, 1, 3, 15)}

# Rebuild the legacy aggregate from the corrected stage files.  Keeping this
# file authoritative prevents old plotting scripts from silently mixing the
# pre-fix fault experiment with the new deterministic 800-segment workload.
ablation_aggregate = []
for stage in (0, 1, 3, 15):
    source = stages[stage]
    rows = []
    for original in source["per_rep"]:
        row = dict(original)
        row["unique_delivered"] = row["ordered"] + row["in_buf"]
        row["missing_segments"] = row["target_segments"] - row["unique_delivered"]
        rows.append(row)
    block = dict(source)
    block["stage"] = stage
    block["per_rep"] = rows
    block["mean"] = dict(source["mean"])
    block["mean"]["unique_delivered"] = sum(r["unique_delivered"] for r in rows) / len(rows)
    block["mean"]["missing_segments"] = sum(r["missing_segments"] for r in rows) / len(rows)
    ablation_aggregate.append(block)

(RES / "fig8_ablation.json").write_text(
    json.dumps(ablation_aggregate, indent=2, ensure_ascii=False), encoding="utf-8")

summary = {"replication": {
    "cc": "n=5 independent complete runs per mode; within-repetition mode order randomized",
    "resilience": "n=5 independent complete runs per recovery stage",
    "dynamic": "n=3 independent complete dynamic runs per mode",
    "exclusions": "none",
}}

# Congestion-control comparison.
summary["congestion_control"] = {}
for mode, block in cc.items():
    totals = [sum(rep) for rep in block["per_rep"]]
    summary["congestion_control"][mode] = {
        "label": block["label"],
        "total_throughput_seg_s": mean_sd_ci(totals),
        "delay_mean_ms": mean_sd_ci([r["delay_mean_ms"] for r in block["metrics_per_rep"]]),
        "delay_p95_ms": mean_sd_ci([r["delay_p95_ms"] for r in block["metrics_per_rep"]]),
        "ecn_mean": mean_sd_ci([r["ecn_mean"] for r in block["metrics_per_rep"]]),
        "jain_from_flow_means": block["jain"],
        "connect_failures_total": sum(r["connect_failures"] for r in block["metrics_per_rep"]),
    }

local_total = [sum(x) for x in cc["local"]["per_rep"]]
rl_total = [sum(x) for x in cc["rl"]["per_rep"]]
local_delay = [x["delay_mean_ms"] for x in cc["local"]["metrics_per_rep"]]
rl_delay = [x["delay_mean_ms"] for x in cc["rl"]["metrics_per_rep"]]
summary["congestion_control"]["paired_rl_minus_local"] = {
    "throughput_seg_s": paired_effect(rl_total, local_total),
    "delay_mean_ms": paired_effect(rl_delay, local_delay),
    "mean_relative_throughput_change": (sum(rl_total) / sum(local_total) - 1.0),
    "mean_relative_delay_change": (sum(rl_delay) / sum(local_delay) - 1.0),
}

# Recovery ablation.  Unique delivered = ordered + currently buffered unique
# DSNs. Missing is relative to the fixed 800-segment workload.
summary["resilience"] = {}
for stage, block in stages.items():
    unique = [r["ordered"] + r["in_buf"] for r in block["per_rep"]]
    missing = [r["target_segments"] - u for r, u in zip(block["per_rep"], unique)]
    summary["resilience"][str(stage)] = {
        "name": block["name"],
        "ordered": mean_sd_ci([r["ordered"] for r in block["per_rep"]]),
        "unique_delivered": mean_sd_ci(unique),
        "missing_segments": mean_sd_ci(missing),
        "duplicates": mean_sd_ci([r["dup"] for r in block["per_rep"]]),
        "delay_p95_ms": mean_sd_ci([r["delay_p95_ms"] for r in block["per_rep"]]),
    }

# Dynamic phases and whole-run latency.
summary["dynamic"] = {}
for mode in ("local", "rl"):
    rows = dyn["modes"][mode]["per_rep"]
    phases = {}
    for phase in ("normal", "congested", "recovery"):
        phases[phase] = {
            "throughput_seg_s": mean_sd_ci([r["phases"][phase]["throughput_seg_s"] for r in rows]),
            "cwnd_mean": mean_sd_ci([r["phases"][phase]["cwnd_mean"] for r in rows]),
            "ecn_mean": mean_sd_ci([r["phases"][phase]["ecn_mean"] for r in rows]),
            "direct_path_share": mean_sd_ci([r["phases"][phase]["path_share"].get("direct", 0) for r in rows]),
        }
    summary["dynamic"][mode] = {
        "label": dyn["modes"][mode]["label"],
        "phases": phases,
        "whole_run_delay_mean_ms": mean_sd_ci([r["delay_mean_ms"] for r in rows]),
        "whole_run_delay_p95_ms": mean_sd_ci([r["delay_p95_ms"] for r in rows]),
    }

(OUT / "paper_evidence_summary.json").write_text(
    json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

with (OUT / "source_data_paper.csv").open("w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["experiment", "condition", "rep", "phase", "metric", "value", "unit"])
    for mode, block in cc.items():
        if mode == "paired_rl_minus_local":
            continue
        for i, rep in enumerate(block["per_rep"]):
            w.writerow(["cc", mode, i + 1, "all", "total_throughput", sum(rep), "segment/s"])
            for metric in ("delay_mean_ms", "delay_p95_ms", "ecn_mean", "ecn_peak", "dup"):
                w.writerow(["cc", mode, i + 1, "all", metric,
                            block["metrics_per_rep"][i][metric], "ms" if "delay" in metric else "ratio_or_count"])
    for stage, block in stages.items():
        for i, r in enumerate(block["per_rep"]):
            unique = r["ordered"] + r["in_buf"]
            for metric, value, unit in (
                ("ordered", r["ordered"], "segment"),
                ("unique_delivered", unique, "segment"),
                ("missing", r["target_segments"] - unique, "segment"),
                ("duplicates", r["dup"], "segment"),
                ("delay_p95_ms", r["delay_p95_ms"], "ms")):
                w.writerow(["resilience", stage, i + 1, "all", metric, value, unit])
    for mode in ("local", "rl"):
        for r in dyn["modes"][mode]["per_rep"]:
            for phase in ("normal", "congested", "recovery"):
                p = r["phases"][phase]
                w.writerow(["dynamic", mode, r["rep"] + 1, phase,
                            "throughput", p["throughput_seg_s"], "segment/s"])
                w.writerow(["dynamic", mode, r["rep"] + 1, phase,
                            "direct_path_share", p["path_share"].get("direct", 0), "fraction"])
                w.writerow(["dynamic", mode, r["rep"] + 1, phase,
                            "cwnd_mean", p["cwnd_mean"], "segment"])

print(OUT / "paper_evidence_summary.json")
print(OUT / "source_data_paper.csv")
