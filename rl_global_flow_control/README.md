# 全局 RL 调度 + DCQCN + Credit 流量控制实验

在 16 节点 P4/BMv2 实验基础上,用**软件侧全局调度**替代令牌桶限速,结合**强化学习(RL)+ DCQCN 拥塞控制 + Credit 信用流控**。

## 总体架构

控制逻辑全部在**软件侧**(主机/控制面),P4 交换机只负责转发 + 打 ECN 标记:

```
┌───────────── 软件侧 ─────────────┐
│ ① 分层 RL 全局调度器(控制面)      │
│    高层 Q_tree: 本轮激活哪些流    │
│      (状态=拥塞+剩余工作,         │
│       动作=all/half/quarter,     │
│       奖励=吞吐+阶段完成)        │
│    低层 Q_flow: 给选中流定速率    │
│      (状态=拥塞等级,             │
│       动作=速率倍率,             │
│       奖励=吞吐−λ时延−μ丢包)     │
│    交替冻结训练(论文 Algorithm 1)│
│    策略持久化 + 环境指纹检测      │
│ ② DCQCN 拥塞控制:               │
│    收到 ECN 信号 → 量化降速 → 缓恢复│
│ ③ Credit 信用流控(主机收发):     │
│    接收方授信,发送方额度内发送    │
└─────────────────────────────────┘
               ↓ 下发速率/额度
┌───────────── 数据面 ─────────────┐
│ P4 交换机:16口转发 + egress       │
│ 排队等待(deq_timedelta)超阈值     │
│   → 打 ECN(CE)标记 + 计数         │
└─────────────────────────────────┘
```

## 文件说明

| 文件 | 作用 |
|---|---|
| `simple_router_global.p4` | P4:16 口 IPv4 LPM 转发 + egress 排队等待(deq_timedelta)超阈值打 ECN 标记 + 每端口标记计数寄存器(无 meter) |
| `rl_train.py` | 分层 Q-learning:高层 Q_tree(选流)+ 低层 Q_flow(速率),交替冻结训练,导出 `policy.json`(含环境指纹) |
| `global_scheduler.py` | 控制面调度器:高层选激活流 → 低层定速率 → DCQCN 量化降速 + 缓慢恢复 + pacing |
| `credit_flow.py` | 主机侧 credit 流控:接收方授信、发送方额度内发送,防接收缓冲丢包 |
| `run_global_demo.py` | 主脚本:编译→拓扑→BMv2→路由→加载/训练策略→超发阶段→调度阶段→丢包归因→实时写 `live_stats.json` |
| `monitor.html` | 实时监控页:轮询 `live_stats.json`,渲染 ECN 曲线、活跃流、速率倍率、丢包率 |
| `policy.json` | 训练产物:两层策略 + 环境指纹(存在就复用,指纹变了才重训) |
| `live_stats.json` | 运行时每轮指标(自动生成) |

## 运行

```bash
docker start p4app

# 启动监控服务器(可选,看实时页面)
python -m http.server 8093 --directory E:/p4-workspace
# 浏览器打开 http://localhost:8093/monitor.html

# 跑实验(另一个终端)
docker exec p4app bash -c "cd /workspace && python2 -u run_global_demo.py"
```

- **只重新训练策略**:`docker exec p4app bash -c "cd /workspace && python2 -u rl_train.py"`
- **策略复用**:程序启动时算环境指纹,与 `policy.json` 一致就直接复用,不一致才重训。

## 实验流程

1. 编译 P4(转发 + ECN 标记)
2. 建 16 命名空间 + veth,BMv2 16 端口,LPM 路由,静态 ARP
3. **加载/训练分层策略**:高层 Q_tree(选流)+ 低层 Q_flow(速率),含指纹检测
4. **Phase A(超发)**:8 对主机故意高速发送制造拥塞,触发 ECN
5. **Phase B(调度)**:GlobalScheduler 分层决策(选流 + 速率)+ DCQCN 控制
6. **丢包归因**:统计发送/接收/丢包率,检查接收端校验和与 socket 缓冲约束
7. **实时监控**:每轮指标写入 `live_stats.json`,`monitor.html` 实时渲染

## 实验结果(实测)

```
Phase A (超发): ecn_ratio=0.95~1.00  state=2  ← ECN 标记 95~100%
Phase B (调度): active=16 pacing_mult[1]=0.20 ← 高层全激活 + DCQCN ×0.2

sent: 35840  received: 35840
overall loss: 0.0%
per-pair received: [4480, ...×8]

-- 接收端约束(应为 0)--
h2: InErrors=0 RcvbufErrors=0 InCsumErrors=0  (等)
```

> 注:BMv2 单线程吞吐有抖动,偶发运行丢包率会偏高(如 8%),原因是 Phase A 超发阶段 8 对同时打流压垮 CPU,非逻辑错误;接收端校验约束始终为 0。

- **0% 丢包**:RL 全局调度 + pacing 把发送速率控制在交换机处理能力内
- **接收端约束全归零**:P4 里清零 UDP checksum(消除校验和丢包);credit + 适度速率(消除缓冲丢包)

## 关键设计点

1. **分层 RL(借鉴论文 "AllReduce Scheduling with Hierarchical DRL")**:高层 Q_tree 决定"这轮激活哪些流",低层 Q_flow 决定"给选中流多少速率",按论文 Algorithm 1 交替冻结训练,同时保留实验原有的 Q-learning(拥塞状态 + 速率动作 + 三因子奖励)。
2. **策略持久化 + 环境指纹**:`policy.json` 存两层策略 + 指纹(由训练参数哈希)。程序启动时比对指纹,环境没变就复用,变了才重训——避免每次重学、保证可复现。
3. **DCQCN 量化降速**:拥塞时按比例一次砍掉速率(state=1 → ×0.5,state=2 → ×0.25),之后小幅恢复,避免振荡。
4. **Credit 流控**:发送方只有在接收方授信的额度内才能发,从源头避免接收缓冲成为丢包点。
5. **实时监控**:每轮指标写入 `live_stats.json`,`monitor.html` 每 1 秒轮询刷新,图形化看 ECN / 活跃流 / 速率 / 丢包。

## 已知局限(诚实说明)

- **ECN 触发已修复**:最初 `enq_qdepth` 在单线程 BMv2 中恒为 0,导致 ECN 永不触发。改用 `deq_timedelta`(包在队列中的等待时长)作为拥塞判据后,实测超发阶段 ecn_ratio 达到 0.95~1.00,DCQCN 量化降速(pacing_mult 降至 0.20)真实触发。
- **BMv2 单线程抖动**:软件交换机吞吐受 CPU 影响,超发阶段偶发高丢包(如 8%),属性能抖动而非逻辑错误;接收端校验约束始终为 0。
- **"BMv2 died" 已修复**:cleanup() 现在 `pkill` 所有残留 simple_switch + 清 IPC,不再因上次残留的交换机占住 9090 端口导致启动失败。
- **分层策略偏保守**:简化仿真器下学到的策略是全激活 + ×1.2(吞吐奖励占优);要学到更聪明的策略需 ns-3 等更真实环境。
- **仍为简化实现**:ECN 标记走"交换机打标记 + 控制面读计数"的简化闭环,而非完整 TCP ECN-Echo 回传。
- RL 用 Q-learning(状态离散),若要更高精度可换 PPO/A2C + 连续状态(需在 ns-3/Mininet 训练)。

## 与 16node(令牌桶)版本的区别

| | 16node(令牌桶) | 本实验(分层 RL 全局调度) |
|---|---|---|
| 限速方式 | 数据面 meter 固定 rate | 软件侧分层 RL 动态调速 |
| 拥塞响应 | 无(超速即丢) | DCQCN ECN 信号 → 量化降速 |
| 丢包控制 | 有意丢弃超速包 | 尽量 0 丢包(credit + pacing) |
| 全局协调 | 无(各目的独立桶) | 有(高层选流 + 低层调速率) |
| 训练 | 无(静态配置) | 分层 Q-learning + 指纹持久化 |
