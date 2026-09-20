# 正式实验结果索引

这里只保留当前论文使用的正式结果。历史快照、pilot、运行日志和逐进程临时输出未纳入仓库。

## 用户态原型

| 文件 | 内容 |
|---|---|
| `t5_cc.json` | 7 种拥塞控制模式，每模式 5 次独立运行 |
| `dynamic_adaptation.json` | 无 RL 与残差 Q 的三阶段动态实验，每模式 3 次 |
| `fig8_stage{0,1,3,15}.json` | 四级故障恢复消融的逐轮结果 |
| `fig8_ablation.json` | 修正后的故障恢复汇总 |
| `paper_evidence/paper_evidence_summary.json` | 论文主证据的均值、标准差和 95% t 区间 |
| `paper_evidence/source_data_paper.csv` | 可复核长表 |
| `paper_evidence/fig_paper_evidence.{png,svg,pdf}` | 论文主证据图 |

## Linux 原生 MPTCP 参考

[`native_mptcp/reference_v1/`](native_mptcp/reference_v1/) 是唯一正式版本：

| 文件 | 内容 |
|---|---|
| `REPORT.md` | 实验设计、完整性核验、结果和限制 |
| `results.json` | 26 次正式运行的合并原始记录 |
| `summary.json` | 统计汇总与配对差值 |
| `source_data.csv` | 图表 Source Data |
| `fig_native_reference.{png,svg,pdf}` | 原生参考图 |
| `fig_native_reference.py` | 绘图源代码 |

`native_mptcp/topology.json` 与 `native_mptcp/vm_capability.json` 保存正式环境和拓扑核验信息。

## 解释限制

- 小样本结果主要用于效应量和机制证据，不作普遍优越性声明。
- 原型和原生实验的时延定义不同，不得合并。
- 动态原生实验与连接升速阶段重叠，只作为操纵核验。
- 原型的离线 Q 与微调 Q 贪心动作相同，不能宣称微调带来额外收益。
