# P4 遥测驱动的 MPTCP 控制与恢复实验

本仓库保存论文当前使用的实验代码、正式结果和可复核图件。仓库已清理早期两节点、16 节点 token-bucket 和独立全局流控原型；当前唯一维护的实验实现位于 [`mptcp_exp/`](mptcp_exp/)。

## 最新结论

- 残差 Q 控制器选择了低时延、低吞吐的工作点。与无 RL 的 ECN/Credit 基线相比，微调残差 Q 的平均吞吐下降 40.1%，平均时延下降 62.8%；后者置信区间跨 0，应表述为趋势而非显著优势。
- 动态 ECN 实验表明控制闭环会降低窗口、调整路径份额并在拥塞解除后恢复，但当前状态轨迹不足以证明 Q 表发生了多状态学习切换。
- 分层恢复中，NAK/SACK 和完整恢复均实现 5/5 次、800/800 段完整交付；完整恢复降低尾部时延，但产生更多重复段。
- Linux 原生 MPTCP 在本实验的强异构路径上承受明显吞吐和有序交付时延代价，但将主路径黑洞下的最大交付中断从 6.40 s 缩短至 0.71 s。

完整统计口径、置信区间、限制和允许使用的论文表述见 [`EXPERIMENT_RERUN_REPORT_20260919.md`](EXPERIMENT_RERUN_REPORT_20260919.md)。

## 仓库结构

```text
.
├── EXPERIMENT_RERUN_REPORT_20260919.md   # 当前总报告与证据边界
└── mptcp_exp/
    ├── README.md                         # 运行入口和代码导航
    ├── run_mptcp.py                      # 用户态 MPTCP 语义原型实验
    ├── run_native_reference.py           # Linux 原生 TCP/MPTCP 对照套件
    ├── analyze_paper_results.py          # 原型结果统计
    ├── analyze_native_reference.py       # 原生参考结果统计
    └── results/
        ├── README.md                     # 结果文件索引
        ├── paper_evidence/               # 论文主证据图、汇总和 Source Data
        └── native_mptcp/reference_v1/    # 原生 MPTCP 正式结果与图件
```

## 快速查看

1. 阅读[总实验报告](EXPERIMENT_RERUN_REPORT_20260919.md)。
2. 查看[论文主证据图](mptcp_exp/results/paper_evidence/fig_paper_evidence.png)及其[统计汇总](mptcp_exp/results/paper_evidence/paper_evidence_summary.json)。
3. 查看[Linux 原生 MPTCP 报告](mptcp_exp/results/native_mptcp/reference_v1/REPORT.md)及其[Source Data](mptcp_exp/results/native_mptcp/reference_v1/source_data.csv)。

## 数据范围

- 拥塞控制和故障恢复：每条件 `n=5`。
- 动态原型实验：每模式 `n=3`。
- 原生 Linux 参考：静态与故障场景每协议 `n=5`，动态场景每协议 `n=3`。
- 无事后排除；独立单位为一次完整运行。

本仓库不跟踪容器/虚拟机运行时、缓存、日志、pilot 数据、逐进程临时输出和可由 SVG/PDF/PNG 重建的 TIFF 文件。
