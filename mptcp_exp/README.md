# MPTCP 多路径实验(DSN/SSN 二维流量控制)

在 16 节点自由通讯基础上,升级为 MPTCP 风格的多路径:每对流随机 3~4 条子流
(1 条直连不走交换机,其余经交换机),用 DSN/SSN 二维结构做流量控制,
DCQCN + Credit + RL 三域协同拥塞控制。

详细设计见 `MPTCP_DESIGN.md`。

## 文件说明

| 文件 | 作用 |
|---|---|
| `flow_mptcp.py` | 数据模型:Flow(DSN 序列 + 乱序缓冲)/ Subflow(SSN 序列 + path=direct\|sw1\|sw2\|sw3);随机 3~4 子流其中 1 条直连 |
| `run_mptcp.py` | 主脚本:16 命名空间 + 3 台 BMv2(独立子网/thrift/ECN)+ 直连 veth;异构配置;多里程碑演示 + 真实训练 |
| `tcp_stack.py` | 自定义 TCP:握手/序号/ACK/RTO 重传/cwnd(可被 RL 覆盖)+ 重传路径 |
| `mptcp_io.py` | DSN/SSN 标记的传输:发送端按 SSN 编号,接收端按 DSN 重组 |
| `mptcp_scheduler.py` | 多交换机感知调度:DCQCN(每交换机独立 ECN)+ Credit + RL(路径比例/cwnd/成本) |
| `rl_train_mptcp.py` | 离线分层 Q-learning(路径比例 profile + cwnd),交替冻结训练 |
| `rl_real_train.py` | 真实环境训练:读真实 ECN/丢包/时延做奖励,微调策略 |
| `simple_router_global.p4` | 数据面:转发 + ECN 标记 + meter 限速 |
| `MPTCP_DESIGN.md` | 设计蓝图 |

## 运行

```bash
docker start p4app
docker exec p4app bash -c "cd /workspace && python2 -u mptcp_exp/run_mptcp.py"
```

脚本自动:编译 P4 → 建 16 命名空间 + **3 台交换机** → 建直连 veth → 验证直连绕过交换机 →
M3/M4 多子流 DSN 重组 → M5-M7 三域拥塞 → **9b 比例分流 + 重传选路** → **10b 真实环境 RL 训练**。

## 已实现里程碑

| 里程碑 | 内容 | 实测 |
|---|---|---|
| M1 | DSN/SSN 模型 + 随机 3~4 子流(1直连) | 29 流:29直连+sw1 29+sw2 29+sw3 21 |
| M2 | 3 台交换机 + 直连双路径拓扑 | 直连 ping 绕过交换机 |
| M3-M4 | SSN 发送 + DSN 重组 | 60段跨3子流全按DSN有序 |
| M5-M7 | DCQCN+Credit+RL 三域调度 | ECN高时交换机子流降速,直连保持 |
| 异构 | 3 交换机不同带宽/ECN阈值/tc链路 | sw1=25M/ecn5/WiFi, sw2=60M/ecn20/蜂窝, sw3=140M/ecn60/光纤 |
| 成本感知 | PATH_COST 进奖励与选路 | sw2(蜂窝,贵)仅分 2% 流量 |
| 比例分流 | 连续比例按路径特征分配 | 500段:direct 267/sw1 223/sw2 10 |
| 重传选路 | 丢包重传走最健康路径 | retrans→direct(ECN/占用最低) |
| 真实训练 | 3 交换机真实 ECN/丢包/时延驱动 RL | recv 60/60, reward 0.905, 存 policy_mptcp_real.json |

## 三域拥塞控制(核心)

```
拥塞域① 交换机(共享):DCQCN — ECN标记 → 经交换机子流统一降速
拥塞域② 节点汇聚:     Credit(按子流)— 防某子流独占缓冲
拥塞域③ 直连(点对点):  Credit — 接收方授信,额度内发
全局:                 RL — 看ECN+credit+成本,做比例分流/cwnd/路径
```

## 待办

- M8:DSN/SSN 二维实时监控页
- 完整多流演示 + 真实 ECN 计数接入调度器
