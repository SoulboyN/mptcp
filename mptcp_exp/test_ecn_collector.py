#!/usr/bin/env python2

import unittest

import run_mptcp


class EcnCollectorTest(unittest.TestCase):

    def test_parse_all_indexed_registers(self):
        lines = []
        for name in ('ecn_marks', 'egress_total'):
            for idx in range(run_mptcp.NODES):
                lines.append('RuntimeCmd: %s[%d] = %d' %
                             (name, idx, idx + (100 if name == 'egress_total' else 0)))
        parsed = run_mptcp._parse_indexed_registers(
            '\n'.join(lines), ('ecn_marks', 'egress_total'))
        self.assertEqual(len(parsed['ecn_marks']), run_mptcp.NODES)
        self.assertEqual(parsed['ecn_marks'][15], 15.0)
        self.assertEqual(parsed['egress_total'][0], 100.0)

    def test_weighted_delta_uses_every_port(self):
        previous = {
            'ecn_marks': [0.0] * run_mptcp.NODES,
            'egress_total': [0.0] * run_mptcp.NODES,
        }
        current = {
            'ecn_marks': [0.0] * run_mptcp.NODES,
            'egress_total': [0.0] * run_mptcp.NODES,
        }
        # Congestion exists only on port 7. A collector restricted to port 0
        # would incorrectly report zero; the all-port result must be 40%.
        current['ecn_marks'][7] = 4.0
        current['egress_total'][7] = 10.0
        ratio, ports = run_mptcp._ecn_delta_snapshot(current, previous)
        self.assertAlmostEqual(ratio, 0.4)
        self.assertAlmostEqual(ports[7]['ratio'], 0.4)
        self.assertEqual(ports[0]['ratio'], 0.0)

    def test_weighting_uses_packet_counts_not_port_average(self):
        previous = {
            'ecn_marks': [0.0] * run_mptcp.NODES,
            'egress_total': [0.0] * run_mptcp.NODES,
        }
        current = {
            'ecn_marks': [0.0] * run_mptcp.NODES,
            'egress_total': [0.0] * run_mptcp.NODES,
        }
        current['ecn_marks'][1] = 10.0
        current['egress_total'][1] = 10.0
        current['egress_total'][2] = 90.0
        ratio, _ = run_mptcp._ecn_delta_snapshot(current, previous)
        self.assertAlmostEqual(ratio, 0.1)

    def test_missing_port_is_rejected(self):
        with self.assertRaises(ValueError):
            run_mptcp._parse_indexed_registers(
                'ecn_marks[0] = 1\negress_total[0] = 2',
                ('ecn_marks', 'egress_total'))


if __name__ == '__main__':
    unittest.main()
