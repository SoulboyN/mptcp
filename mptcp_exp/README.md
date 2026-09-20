# 实验实现与复现入口

本目录包含两套互相独立、不可混合统计的实验：

1. **用户态 MPTCP 语义原型**：16 个命名空间、3 台 BMv2 交换机、direct 路径与 25/60/140 Mbps 异构交换路径；研究 P4 遥测、ECN/Credit、受约束残差 Q 控制和分层恢复。
2. **Linux 原生 MPTCP 参考实验**：在 Linux 6.8 内核上使用原生 MPTCP 和 Cubic，测量有序字节流交付，用于外部行为参考。

原型记录逐子流到达时延，原生实验记录有序字节流交付时延。二者定义不同，不能合并或作配对统计。

## 主要入口

| 文件 | 用途 |
|---|---|
| `run_mptcp.py` | 原型的拓扑、拥塞控制、动态 ECN 和恢复消融入口 |
| `mptcp_tcp.py` | 多子流发送、DSN 重排、窗口控制与分层恢复 |
| `mptcp_scheduler.py` | ECN/Credit 与残差 Q 调度 |
| `simple_router_global.p4` | P4 转发、ECN 标记和计量 |
| `policy_mptcp_real.json` | 正式实验使用的残差策略 |
| `analyze_paper_results.py` | 生成原型统计汇总和 Source Data |
| `make_paper_evidence_figure.py` | 生成论文主证据图 |
| `run_native_reference.py` | 原生 TCP/MPTCP 正式对照套件 |
| `analyze_native_reference.py` | 生成原生参考统计和 Source Data |
| `prepare_native_vm.sh` | 准备原生 MPTCP 虚拟机环境 |

设计细节见 [`MPTCP_DESIGN.md`](MPTCP_DESIGN.md)，结果导航见 [`results/README.md`](results/README.md)。

## 原型实验

运行环境为带 P4/BMv2、Mininet 和 Python 2 的实验容器：

```bash
docker start p4app
docker exec p4app bash -lc "cd /workspace && python2 -u mptcp_exp/run_mptcp.py --help"
```

正式结果包含：

- 7 种拥塞控制模式的比较；
- 正常、高 ECN、恢复三阶段动态实验；
- 无恢复、replay、NAK/SACK、完整恢复四级消融。

## Linux 原生 MPTCP 参考

原生套件需要支持 MPTCP 协议 262 的 Linux 内核、KVM/QEMU、BMv2 和网络命名空间。正式环境为 Linux `6.8.0-138-generic`。运行前请阅读脚本参数，避免在未准备的宿主机上直接启动网络拓扑：

```bash
python3 mptcp_exp/run_native_reference.py --help
```

正式结果位于 [`results/native_mptcp/reference_v1/`](results/native_mptcp/reference_v1/)。

## 结果重建

```bash
python3 mptcp_exp/analyze_paper_results.py
python3 mptcp_exp/make_paper_evidence_figure.py
python3 mptcp_exp/analyze_native_reference.py
```

图形 QA 由 `audit_panel_alignment.py` 生成。仓库保存 PNG、SVG 和 PDF；投稿用 TIFF 可从矢量图重新导出。
