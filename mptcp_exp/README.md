# MPTCP 多路径实验(DSN/SSN 二维流量控制)

在 16 节点自由通讯基础上,升级为 MPTCP 风格的多路径:每对流随机 3~4 条子流
(1 条直连不走交换机,其余经交换机),用 DSN/SSN 二维结构做流量控制,
DCQCN + Credit + RL 三域协同拥塞控制。

详细设计见 `MPTCP_DESIGN.md`。

## 文件说明

| 文件 | 作用 |
|---|---|
| `flow_mptcp.py` | 数据模型:Flow(DSN 序列 + 乱序缓冲)/ Subflow(SSN 序列 + path=direct\|sw);随机 3~4 子流其中 1 条直连 |
| `run_mptcp.py` | 主脚本:16 命名空间 + BMv2 + 直连 veth(专用 /30 子网绕过交换机);LPM 路由;M2/M3-M4/M5-M7 演示 |
| `mptcp_io.py` | DSN/SSN 标记的 UDP 传输:发送端按 SSN 编号,接收端按 DSN 重组 |
| `mptcp_scheduler.py` | 三域拥塞控制:DCQCN(共享域)+ Credit(点对点)+ RL(全局) |
| `simple_router_global.p4` | 数据面:转发 + ECN 标记(复用实验三) |
| `MPTCP_DESIGN.md` | 设计蓝图 |

## 运行

```bash
docker start p4app
docker exec p4app bash -c "cd /workspace && python2 -u mptcp_exp/run_mptcp.py"
```

脚本自动:编译 P4 → 建 16 命名空间 + 交换机 → 建直连 veth → 验证直连绕过交换机 →
M3/M4 多子流 DSN 重组演示 → M5-M7 三域拥塞控制演示。

## 已实现里程碑

| 里程碑 | 内容 | 实测 |
|---|---|---|
| M1 | DSN/SSN 模型 + 随机 3~4 子流(1直连) | 29 流:29直连+79交换机子流 |
| M2 | 直连 + 交换机双路径拓扑 | 直连 ping 绕过交换机 |
| M3-M4 | SSN 发送 + DSN 重组 | 60段跨3子流全按DSN有序(ordered=60,dup=0) |
| M5-M7 | DCQCN+Credit+RL 三域调度 | ECN高时交换机子流→0.25,直连保持0.75+ |

## 三域拥塞控制(核心)

```
拥塞域① 交换机(共享):DCQCN — ECN标记 → 经交换机子流统一降速
拥塞域② 节点汇聚:     Credit(按子流)— 防某子流独占缓冲
拥塞域③ 直连(点对点):  Credit — 接收方授信,额度内发
全局:                 RL — 看ECN+credit+进度,做速率/路径/DSN分配
```

## 待办

- M8:DSN/SSN 二维实时监控页
- 完整多流演示 + 真实 ECN 计数接入调度器
