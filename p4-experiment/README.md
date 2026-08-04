# P4/BMv2 实验

基于 `p4lang/p4app` Docker 镜像的网络命名空间 + BMv2 可编程交换机实验。

## 环境

- Docker 容器:`p4app`(镜像 `p4lang/p4app`),需 `--privileged` 并挂载本目录到 `/workspace`
- 运行依赖:p4c(编译 P4)、BMv2 `simple_switch`、`simple_switch_CLI`、Python 2.7

## 目录内容

| 文件 | 说明 |
|---|---|
| `simple_router.p4` | 2 节点版本:P4 IPv4 LPM 转发程序 |
| `run_dual_node.py` | 2 节点一键脚本:编译→拓扑→BMv2→流表→ping→抓包 |
| `simple_router16.p4` | 16 节点版本:16 端口 LPM 转发 + v1model meter 逐目的限速 |
| `run_16node.py` | 16 节点一键脚本:拓扑→流表→连通性→流量控制演示 |
| `topo.py` | Mininet 版本拓扑(备用) |
| `run_demo.sh` | 演示辅助脚本 |
| `capture_demo.html` | ICMP 抓包可视化页面(自包含,双击打开) |

## 运行

### 2 节点

```bash
docker start p4app
docker exec p4app bash -c "cd /workspace && python2 run_dual_node.py"
```

运行后生成 `ping_capture.pcap`,用 Wireshark 打开即可看到 h1↔h2 的 ICMP 请求/应答。

### 16 节点 + 流量控制

```bash
docker start p4app
docker exec p4app bash -c "cd /workspace && python2 -u run_16node.py"
```

脚本自动完成:编译 → 16 个网络命名空间 + veth → BMv2 16 端口 → 全对全静态 ARP → 120 条 LPM 路由 → 连通性检查 → meter 限速演示。

## 流量控制原理

`simple_router16.p4` 用 v1model `meter`(令牌桶)做**逐目的地限速**:

- 目的 IP 末字节 - 1 作为 meter 索引(0..15)
- GREEN(未超速)→ 正常 LPM 转发;非 GREEN → 丢弃
- 用 `simple_switch_CLI` 的 `meter_set_rates` 按目的地配速(单位 bytes/µs)

演示:限速 h2 到 8 Mbps,15.7 Mbps 流量下 h2 丢 ~50%,不限速的 h16 全通。

## 关键调试经验

1. **UDP checksum**:BMv2 转发改 TTL 后不重算 UDP checksum,接收端内核会静默丢弃(只有 ICMP 通)。必须在 `forward` action 里把 UDP checksum 清零(`h.udp.checksum = 0`,IPv4 下合法)。
2. **不要加 `--log-console`**:逐包日志严重拖慢吞吐;输出应重定向到文件而非 PIPE(否则缓冲填满死锁)。
3. **meter burst 必须 ≥ 一个包大小**(~1500B),否则所有包都被判 RED 全丢。
4. **速率换算**:BMv2 meter 的 rate 单位是 bytes/µs,从 bps 换算需 `/8/1000000`。
5. **静态 ARP + 真实 MAC**:跨命名空间转发必须用 `ip neigh ... nud permanent` 静态绑定真实 MAC。
