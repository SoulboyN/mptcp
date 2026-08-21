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
| `mptcp_tcp.py` | **真实内核 TCP 子流传输 + DSN 重排**(帧头带 payload 长度);`MptcpGroupSender` 四层重传恢复 + **应用层 cwnd/credit 窗口**(RL 设置 cwnd,in_flight < min(cwnd, credit) 才发) |
| `mptcp_scheduler.py` | 多交换机感知调度:DCQCN(每交换机独立 ECN)+ Credit + RL(路径比例/cwnd/成本);`RlScheduler` 内嵌调度器驱动真实发送 |
| `rl_train_mptcp.py` | 离线分层 Q-learning(路径比例 profile + cwnd),交替冻结训练;导出 Q 表数值 |
| `rl_real_train.py` | 真实环境训练:读真实 ECN/丢包/时延做奖励,微调策略;从离线 Q **warm-start**,保存时导出 `policy_path`/`policy_cwnd` 供调度器加载 |
| `simple_router_global.p4` | 数据面:转发 + ECN 标记 + meter 限速 |
| `MPTCP_DESIGN.md` | 设计蓝图 |

## 运行

```bash
docker start p4app
docker exec p4app bash -c "cd /workspace && python2 -u mptcp_exp/run_mptcp.py"
```

脚本自动:编译 P4 → 建 16 命名空间 + **3 台交换机** → 建直连 veth → 验证直连绕过交换机 →
M3/M4 多子流 DSN 重组 → M5-M7 三域拥塞 → **9b 比例分流 + 重传选路** → **10b 真实环境 RL 训练** →
**11 断链重路由演示**。

非交互参数(无需 tty,Claude Code 里可直接跑):

```bash
docker exec p4app bash -c "cd /workspace && python2 -u mptcp_exp/run_mptcp.py --cut sw1"
docker exec p4app bash -c "cd /workspace && python2 -u mptcp_exp/run_mptcp.py --demo 'sleep 3|cut sw1|sleep 5|up sw1|sleep 5|cut sw2|sleep 5'"
```

交互模式(需 tty):`docker exec -it p4app bash -c "cd /workspace && python2 -u mptcp_exp/run_mptcp.py"`,
到 `mptcp>` 提示符后输入 `cut <path>` / `up <path>` / `quit`。cut/up 在 netns 内真正切换接口,`up`
后 ~2s 该子流自动重连恢复载流。

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
| 断链重传 | 四层恢复:发送失败即重传 / go-back-N 重放 / NAK(最小缺失 DSN) / 停滞检测(踢出静默卡死子流) | --demo 生命周期:in_buf 0,唯一 DSN 全部按序交付 |
| 断链重路由 | 交互 cut/up + `--cut`/`--demo` 自动演示;子流 ~2s 自动重连 | --cut sw1:received==ordered, in_buf 0 |
| RL 策略部署 | 离线存 Q → 真实微调 **warm-start**(不冷启动)→ 保存导出 `policy_path`/`policy_cwnd` → 调度器(RlPathSelector/RlCwndController/MptcpScheduler)加载并驱动决策 | 微调后 policy_cwnd [1,1,1]→[1,0,1](state1 cwnd×2);三调度器加载单测通过 |
| RL 驱动真实发送 | `MptcpGroupSender` 内嵌 `RlScheduler`:每轮读真实 ECN/credit/in_flight → 决策 cwnd + 路径权重;发送受 `in_flight < min(cwnd, credit)` 窗口约束;三域闭环(DCQCN 读真实 ecn_marks / Credit 接收方授信 / RL 全局) | step 11 实测:cwnd=16→4 时 inflight 跟随封顶(16→4);ECN 高时 state2 cwnd 减半;断链重传不回归 |
| 监控页 M8 | DSN/SSN 二维实时监控:`build/monitor.html` 实时渲染每流 DSN 进度 + 每子流 SSN/接收/在途/cwnd/credit + 接收端重组 | 浏览器实时刷新,cwnd/inflight 随 RL 决策变化可见 |

## 三域拥塞控制(真实闭环)

```
拥塞域① 交换机(共享):DCQCN — 主进程周期读真实 ecn_marks → 写入 ECN 全局视图
                       → 发送器内 RlScheduler 对高 ECN 交换机子流 cwnd 减半
拥塞域② 节点汇聚:     Credit — 每子流在途受 credit_limit(接收方授信上限)约束
拥塞域③ 直连(点对点):  Credit — 直连子流同样受窗口约束
全局:                 RL(RlScheduler)— 看真实 ECN + 各子流 in_flight/credit,
                     下发改动的 cwnd + 路径权重,真实控制每条子流发送速率
```

## 待办

- 重传优化:发送器优雅结束(消除尾部截断);SACK 位图精确报缺(dup → ~0)
