#include <core.p4>
#include <v1model.p4>

// 16-node IPv4 LPM forwarding switch with per-destination token-bucket
// rate limiting (v1model meter).
//
// Topology:
//   ns-h1..ns-h16  (10.0.0.1..16/24) -- veth -- [BMv2 ports 1..16]

header ethernet_t {
    bit<48> dstAddr;
    bit<48> srcAddr;
    bit<16> etherType;
}

header ipv4_t {
    bit<4>  version;
    bit<4>  ihl;
    bit<8>  diffserv;
    bit<16> totalLen;
    bit<16> identification;
    bit<3>  flags;
    bit<13> fragOffset;
    bit<8>  ttl;
    bit<8>  protocol;
    bit<16> hdrChecksum;
    bit<32> srcAddr;
    bit<32> dstAddr;
}

header udp_t {
    bit<16> srcPort;
    bit<16> dstPort;
    bit<16> length;
    bit<16> checksum;
}

struct headers {
    ethernet_t eth;
    ipv4_t     ipv4;
    udp_t      udp;
}

struct meta {
    // 1..16 for hosts h1..h16 (derived from destination IP last octet).
    // 0 = not for a known host (e.g. broadcast) -> not meter-limited.
    bit<32> destIdx;
}

parser p(packet_in pkt, out headers h, inout meta m, inout standard_metadata_t sm) {
    state start {
        pkt.extract(h.eth);
        transition select(h.eth.etherType) {
            0x0800: parse_ipv4;
            default: accept;
        }
    }
    state parse_ipv4 {
        pkt.extract(h.ipv4);
        transition select(h.ipv4.protocol) {
            17: parse_udp;
            default: accept;
        }
    }
    state parse_udp {
        pkt.extract(h.udp);
        transition accept;
    }
}

control verifyChecksum(inout headers h, inout meta m) {
    apply { }
}

control ingress(inout headers h, inout meta m, inout standard_metadata_t sm) {

    action drop() {
        mark_to_drop(sm);
    }

    action forward(bit<9> port, bit<48> dstMac) {
        sm.egress_spec = port;
        h.eth.dstAddr  = dstMac;
        h.eth.srcAddr  = 0x0a0a0a0a0a0a;
        h.ipv4.ttl     = h.ipv4.ttl - 1;
        // BMv2 does not recompute the UDP checksum (covers payload) when
        // the IPv4 header changes, so zero it (valid for IPv4, RFC 768).
        if (h.udp.isValid()) {
            h.udp.checksum = 0;
        }
    }

    table ipv4_lpm {
        key = {
            h.ipv4.dstAddr: lpm;
        }
        actions = { forward; drop; }
        size = 1024;
        default_action = drop();
    }

    // Per-destination token bucket. Index = dest host index (1..16).
    // GREEN (0) -> forward, YELLOW/RED -> drop (rate limited).
    meter(16, MeterType.bytes) m_dst;

    // Convert the destination IP to a per-host meter index (0..15).
    // 10.0.0.x -> x-1. Index 0..15 (16 hosts).
    action set_dest_index() {
        m.destIdx = (bit<32>)((h.ipv4.dstAddr & 0xff) - 1);
    }

    apply {
        if (h.ipv4.isValid()) {
            set_dest_index();
            // Only rate-limit unicast to a known host (destIdx 0..15);
            // everything else (e.g. broadcast) is not limited.
            if (m.destIdx >= 0 && m.destIdx <= 15) {
                bit<32> color = 0;
                m_dst.execute_meter<bit<32>>(m.destIdx, color);
                if (color == 0) {           // GREEN
                    ipv4_lpm.apply();
                } else {                    // YELLOW or RED
                    drop();
                }
            } else {
                ipv4_lpm.apply();
            }
        }
    }
}

control egress(inout headers h, inout meta m, inout standard_metadata_t sm) {
    apply { }
}

control compute(inout headers h, inout meta m) {
    apply {
        update_checksum(
            h.ipv4.isValid(),
            { h.ipv4.version, h.ipv4.ihl, h.ipv4.diffserv, h.ipv4.totalLen,
              h.ipv4.identification, h.ipv4.flags, h.ipv4.fragOffset,
              h.ipv4.ttl, h.ipv4.protocol,
              h.ipv4.srcAddr, h.ipv4.dstAddr },
            h.ipv4.hdrChecksum,
            HashAlgorithm.csum16);
    }
}

control dep(packet_out pkt, in headers h) {
    apply {
        pkt.emit(h.eth);
        pkt.emit(h.ipv4);
        pkt.emit(h.udp);
    }
}

V1Switch(p(), verifyChecksum(), ingress(), egress(), compute(), dep()) main;
