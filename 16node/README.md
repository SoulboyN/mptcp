# 16 节点 + 流量控制实验(16node)

用一台 BMv2 可编程交换机连接 16 台虚拟主机,并演示 P4 meter(令牌桶)的逐目的流量控制。

## 文件说明

| 文件 | 说明 |
|---|---|
| `simple_router16.p4` | P4 交换程序:16 端口 IPv4 LPM 转发 + meter 逐目的限速 |
| `run_16node.py` | 一键脚本:编译 → 16 命名空间+veth → BMv2 → 流表 → 连通性 → 限速演示 |
| `traffic_control_demo.html` | 结果可视化(拓扑 + 令牌桶动画 + 数据对比) |
| `traffic_demo_beginners.html` | 小白版可视化(快递比喻 + 动画) |

## 运行环境

- Docker 容器 `p4app`(镜像 `p4lang/p4app`,需 `--privileged`,挂载本目录到 `/workspace`)
- 依赖:p4c、BMv2 `simple_switch`、`simple_switch_CLI`、Python 2.7

## 运行

```bash
docker start p4app
docker exec p4app bash -c "cd /workspace && python2 -u run_16node.py"
```

脚本自动完成全部流程,最后打印限速对比:

```
[*] h1 -> h2   (RATE LIMITED to 8 Mbps): sent 300, received 150 (50% loss)
[*] h1 -> h16  (NOT LIMITED):            sent 300, received 300 (0% loss)
```

## 流量控制原理

P4 用 v1model `meter`(2-rate 3-color 令牌桶,单位 bytes/µs)做**逐目的地限速**:

- 目的 IP 末字节 − 1 作为 meter 索引(0..15),每台主机一个桶
- `rate` = 令牌补充速度(允许的持续速率);`burst` = 桶容量(突发容忍度)
- `execute_meter` 返回颜色:GREEN(0)→ LPM 转发;非 GREEN → 丢弃
- CLI 配置:`meter_set_rates m_dst <idx> <rate>:<burst> <rate>:<burst>`

实验给 h2 配 rate=1 byte/µs(=8 Mbps)、burst=1500B;h16 配 10⁹(不限速)。打 15.7 Mbps 的 UDP 流:限速的 h2 丢 ~50%,不限速的 h16 全通。

## 关键经验

1. **UDP checksum**:BMv2 转发改 TTL 后不重算 UDP checksum,接收端内核静默丢弃(表现为只有 ICMP 通)。须在 `forward` action 里清零(`h.udp.checksum = 0`)。
2. **勿加 `--log-console`**:逐包日志严重拖慢吞吐;switch 输出重定向到文件而非 PIPE(否则缓冲填满死锁)。
3. **burst 必须 ≥ 一个包大小(~1500B)**:小于包长时每个包都被判 RED 全丢。
4. **速率换算**:bmv2 meter rate 单位 bytes/µs,从 bps 换算需 `/8/1000000`。
5. **静态 ARP + 真实 MAC**:跨命名空间转发必须用 `ip neigh ... nud permanent` 绑定真实 MAC。
