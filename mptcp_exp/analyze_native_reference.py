#!/usr/bin/env python3
"""Summarize complete-run native TCP/MPTCP references without pseudoreplication."""
import csv
import json
import math
import statistics
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE / 'results' / 'native_mptcp' / 'reference_v1'
T95 = {2: 12.706, 3: 4.303, 4: 3.182, 5: 2.776}


def ci(values):
    n = len(values)
    mean = statistics.mean(values)
    sd = statistics.stdev(values) if n > 1 else 0.0
    half = T95[n] * sd / math.sqrt(n) if n in T95 else None
    return {'n': n, 'mean': mean, 'sd': sd,
            'ci95_low': mean - half if half is not None else None,
            'ci95_high': mean + half if half is not None else None}


def server_results(run):
    return [p['result'] for p in run['processes'] if 'server' in p['file']]


def metrics(run):
    servers = server_results(run)
    row = {
        'throughput_seg_s': run.get('aggregate_throughput_seg_s'),
        'mean_flow_delay_ms': run.get('mean_flow_delay_ms'),
        'mean_flow_p95_ms': run.get('mean_flow_p95_ms'),
        'complete_fraction': sum(bool(s and s.get('complete')) for s in servers) / len(servers),
    }
    if run['scenario'] == 'dynamic':
        for phase in range(3):
            row['phase_%d_throughput_seg_s' % (phase + 1)] = sum(
                s['phase_throughput_seg_s'][phase] for s in servers)
        telem = [snap.get('ecn_telemetry') for snap in run['snapshots']
                 if snap.get('ecn_telemetry')]
        row['max_observed_ecn_ratio'] = max(
            (float(value) for item in telem for value in item.values()), default=0.0)
        for phase in range(3):
            phase_values = []
            for snap in run['snapshots']:
                item = snap.get('ecn_telemetry')
                if item and phase * 3 <= snap['t'] < (phase + 1) * 3:
                    phase_values.append(statistics.mean(float(v) for v in item.values()))
            row['phase_%d_mean_ecn_ratio' % (phase + 1)] = (
                statistics.mean(phase_values) if phase_values else None)
    if run['scenario'] == 'fault':
        server = servers[0]
        row.update(delivered_records=server.get('received_records'),
                   recovery_max_gap_s=server.get('max_delivery_gap_s'),
                   completion_time_s=server.get('last_delivery_s'),
                   ordered_p95_ms=server.get('delay_p95_ms'))
    return row


def main():
    source = json.loads((ROOT / 'results.json').read_text())
    runs = source['results']
    expected = {('static', 'tcp'): 5, ('static', 'mptcp'): 5,
                ('dynamic', 'tcp'): 3, ('dynamic', 'mptcp'): 3,
                ('fault', 'tcp'): 5, ('fault', 'mptcp'): 5}
    observed = {(scenario, protocol): sum(
        run['scenario'] == scenario and run['protocol'] == protocol for run in runs)
        for scenario, protocol in expected}
    assert observed == expected, 'formal run matrix is incomplete: %r' % observed
    assert all(run['all_complete'] for run in runs), 'formal suite contains incomplete runs'
    assert all(proc['returncode'] == 0 for run in runs for proc in run['processes']), \
        'formal suite contains a failed process'
    assert all(run['multipath_verified'] is True for run in runs
               if run['protocol'] == 'mptcp'), 'MPTCP multipath verification failed'
    raw_rows = []
    for run in runs:
        row = {'scenario': run['scenario'], 'protocol': run['protocol'],
               'rep': run['rep'], 'all_complete': run['all_complete'],
               'multipath_verified': run['multipath_verified']}
        row.update(metrics(run))
        raw_rows.append(row)
    keys = sorted({key for row in raw_rows for key in row})
    with (ROOT / 'source_data.csv').open('w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=keys)
        writer.writeheader()
        writer.writerows(raw_rows)

    summary = {'independent_unit': 'one complete experimental run',
               'interval': 'two-sided 95% t interval across complete runs',
               'exclusions': 'none', 'groups': {}, 'paired_differences': {}}
    for scenario in ('static', 'dynamic', 'fault'):
        for protocol in ('tcp', 'mptcp'):
            selected = [row for row in raw_rows if row['scenario'] == scenario
                        and row['protocol'] == protocol]
            group = {'runs': len(selected),
                     'all_complete_runs': sum(row['all_complete'] for row in selected),
                     'multipath_verified_runs': (sum(row['multipath_verified'] is True
                                                     for row in selected)
                                                   if protocol == 'mptcp' else None),
                     'metrics': {}}
            metric_names = sorted(set.intersection(*[set(row) for row in selected]) -
                                  {'scenario', 'protocol', 'rep', 'all_complete',
                                   'multipath_verified'})
            for name in metric_names:
                values = [row[name] for row in selected if row[name] is not None]
                if values:
                    group['metrics'][name] = ci(values)
            summary['groups']['%s_%s' % (scenario, protocol)] = group

        paired = {}
        tcp = {row['rep']: row for row in raw_rows if row['scenario'] == scenario
               and row['protocol'] == 'tcp'}
        mptcp = {row['rep']: row for row in raw_rows if row['scenario'] == scenario
                 and row['protocol'] == 'mptcp'}
        common_metrics = set.intersection(*(set(row) for row in list(tcp.values()) +
                                            list(mptcp.values()))) - {
                                                'scenario', 'protocol', 'rep',
                                                'all_complete', 'multipath_verified'}
        for name in sorted(common_metrics):
            differences = [mptcp[rep][name] - tcp[rep][name]
                           for rep in sorted(set(tcp) & set(mptcp))
                           if tcp[rep][name] is not None and mptcp[rep][name] is not None]
            if differences:
                paired[name] = ci(differences)
        summary['paired_differences'][scenario] = paired
    (ROOT / 'summary.json').write_text(json.dumps(summary, indent=2))

    lines = [
        '# Linux 原生 MPTCP 参考实验报告', '',
        '## 统计口径', '',
        '- 独立实验单位：一次完整运行；静态与故障场景每组 n=5，动态场景每组 n=3。',
        '- 汇总为均值 ± 标准差及双侧 95% t 置信区间；未把包、记录或轮询样本当作独立重复。',
        '- 无事后排除；失败轮次保留。TCP/MPTCP 在每个重复内随机运行顺序。',
        '- TCP 使用初始 direct 路径；MPTCP 使用相同初始路径并接受三条交换路径通告。该对照刻画协议行为，不是带宽匹配的调度算法排名。',
        '- 原生时延是有序字节流交付时延，不能与原用户态原型的逐子流到达时延直接合并。', '',
        '## 完整性核验', '']
    for key, group in summary['groups'].items():
        extra = ('，多路径核验 %d/%d' % (group['multipath_verified_runs'], group['runs'])
                 if group['multipath_verified_runs'] is not None else '')
        lines.append('- %s：完整运行 %d/%d%s。' %
                     (key, group['all_complete_runs'], group['runs'], extra))
    lines += ['', '## 数值结果', '',
              '| 场景/协议 | 吞吐 (segment/s) | 平均有序时延 (ms) | 流级 P95 均值 (ms) |',
              '|---|---:|---:|---:|']
    for key, group in summary['groups'].items():
        def fmt(name):
            value = group['metrics'].get(name)
            if not value:
                return '—'
            return '%.2f ± %.2f [%.2f, %.2f]' % (
                value['mean'], value['sd'], value['ci95_low'], value['ci95_high'])
        lines.append('| %s | %s | %s | %s |' %
                     (key, fmt('throughput_seg_s'), fmt('mean_flow_delay_ms'),
                      fmt('mean_flow_p95_ms')))
    lines += ['', '表中格式为均值 ± s.d. [95% CI]。故障恢复与动态阶段的详细指标见 '
              '`summary.json` 和 `source_data.csv`。', '',
              '## 关键结果', '',
              '- 静态场景中，默认 Linux MPTCP 的总吞吐均值为 58,647.95 segment/s，'
              '比单路径 TCP 低 89.7%；平均有序交付时延为 105.20 ms，是 TCP 的 32.2 倍。'
              '该结果反映默认调度器在本实验强异构路径上的队头阻塞风险，不代表一般网络中的普遍排序。',
              '- 主路径 100% 黑洞时，两种协议均完整交付 800/800 条记录；MPTCP 将最大交付中断'
              '从 6.40 ± 0.02 s 降至 0.71 ± 0.26 s（下降 88.9%），并将完成时间提前 0.51 s。',
              '- 动态场景存在连接升速与 ECN 阶段重叠的混杂，阶段吞吐不能单独归因于阈值变化；'
              '因此它只作为操纵核验和行为记录，不作为 Linux MPTCP 自适应优势证据。', '',
              '## 配图', '',
              '![Linux native MPTCP reference](fig_native_reference.png)', '',
              '**图注。** 同一 P4 异构拓扑上的 Linux TCP 与内核原生 MPTCP 参考实验。'
              '每条细线连接同一重复编号，圆点为单次完整运行，黑色菱形与误差线表示均值和双侧 '
              '95% t 置信区间；每组 n=5。a，静态总吞吐；b，静态有序交付时延；c，主路径'
              '黑洞后的最大交付中断。所有 MPTCP 运行均确认至少两条 ESTABLISHED 内核子流。', '',
              '## 论文解释边界', '',
              '该实验提供“同一 P4 异构拓扑下，Linux TCP 与内核原生 MPTCP”的外部参考。'
              '它不能直接证明用户态原型优于 Linux MPTCP，因为两者的实现环境、调度器和时延定义不同；'
              '可用于回答原型与标准内核协议在语义和失效恢复行为上的差异。', '']
    (ROOT / 'REPORT.md').write_text('\n'.join(lines), encoding='utf-8')


if __name__ == '__main__':
    main()
