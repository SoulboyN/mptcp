#include <core.p4>
#include <v1model.p4>

// 16-node IPv4 LPM forwarding with ECN congestion marking (DCQCN-style
// congestion point). NO token-bucket meter here — rate control is done on
// the software side (RL scheduler + DCQCN quantized reduction + credit).
//
// Egress marks the CE bit when the per-port enqueue queue depth exceeds a
// threshold (stored in a register so the control plane can tune it), and
// counts marked packets per port (readable via simple_switch_CLI).

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
    bit<32> portIdx;
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
        // BMv2 does not recompute the UDP checksum (it covers the payload);
        // zero it so the receiving kernel accepts the forwarded datagram.
        if (h.udp.isValid()) {
            h.udp.checksum = 0;
        }
        m.portIdx = (bit<32>)port;
    }

    table ipv4_lpm {
        key = {
            h.ipv4.dstAddr: lpm;
        }
        actions = { forward; drop; }
        size = 1024;
        default_action = drop();
    }

    apply {
        if (h.ipv4.isValid()) {
            ipv4_lpm.apply();
        }
    }
}

control egress(inout headers h, inout meta m, inout standard_metadata_t sm) {

    // Tuning knobs (control plane can write these via CLI).
    // ECN threshold on enqueue queue depth (packets).
    register<bit<32>>(1) ecn_thresh;
    // Per-egress-port counter of packets we marked CE.
    register<bit<32>>(16) ecn_marks;

    action mark_ce() {
        h.ipv4.diffserv = h.ipv4.diffserv | 3;   // set ECN field to CE (11)
        bit<32> v = 0;
        ecn_marks.read(v, m.portIdx);
        ecn_marks.write(m.portIdx, v + 1);
    }

    apply {
        if (h.ipv4.isValid() && sm.egress_port != 511) {
            bit<32> thresh = 0;
            bit<32> qdepth = (bit<32>)sm.enq_qdepth;
            ecn_thresh.read(thresh, 0);
            // enq_qdepth is the queue depth (in packets) seen at enqueue.
            if (qdepth > thresh) {
                mark_ce();
            }
        }
    }
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
