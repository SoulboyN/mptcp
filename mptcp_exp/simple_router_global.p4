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

header tcp_t {
    bit<16> srcPort;
    bit<16> dstPort;
    bit<32> seqNo;
    bit<32> ackNo;
    bit<4>  dataOffset;
    bit<4>  res;
    bit<8>  flags;
    bit<16> window;
    bit<16> checksum;
    bit<16> urgentPtr;
}

struct headers {
    ethernet_t eth;
    ipv4_t     ipv4;
    udp_t      udp;
    tcp_t      tcp;
}

struct meta {
    bit<32> portIdx;
    // pre-computed pseudo-header pieces for TCP checksum (no casts allowed
    // inside update_checksum): zero-padded protocol, tcp length
    bit<16> tcp_len16;
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
            6:  parse_tcp;
            default: accept;
        }
    }
    state parse_udp {
        pkt.extract(h.udp);
        transition accept;
    }
    state parse_tcp {
        pkt.extract(h.tcp);
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
        // pre-compute TCP length = ip totalLen - ip header (20 bytes),
        // for the TCP pseudo-header checksum (no casts inside the call).
        if (h.tcp.isValid()) {
            m.tcp_len16 = h.ipv4.totalLen - 20;
        }
        // 0-based egress port index so it matches register indices (0..15).
        m.portIdx = (bit<32>)(port - 1);
    }

    table ipv4_lpm {
        key = {
            h.ipv4.dstAddr: lpm;
        }
        actions = { forward; drop; }
        size = 1024;
        default_action = drop();
    }

    // Per-egress-port token bucket (bandwidth shaping). Index = 0..15.
    // Each switch instance is given a DIFFERENT rate via meter_set_rates,
    // so switch paths have genuinely different bandwidth (heterogeneous
    // subflows). GREEN -> forward, non-GREEN -> drop (rate limited).
    meter(16, MeterType.bytes) m_port;

    apply {
        if (h.ipv4.isValid()) {
            ipv4_lpm.apply();
            // shape by egress port AFTER the route is decided
            // (511 = the drop/cpu port in BMv2; skip those)
            if (sm.egress_spec != 511) {
                bit<32> color = 0;
                m_port.execute_meter<bit<32>>(m.portIdx, color);
                if (color != 0) {
                    drop();
                }
            }
        }
    }
}

control egress(inout headers h, inout meta m, inout standard_metadata_t sm) {

    // Tuning knobs (control plane can write these via CLI).
    // ECN threshold on enqueue queue depth (packets).
    register<bit<32>>(1) ecn_thresh;
    // Per-egress-port counter of packets we marked CE.
    register<bit<32>>(16) ecn_marks;
    // Debug: per-egress-port counter of every packet that reaches egress.
    register<bit<32>>(16) egress_total;

    action mark_ce() {
        h.ipv4.diffserv = h.ipv4.diffserv | 3;   // set ECN field to CE (11)
        bit<32> v = 0;
        ecn_marks.read(v, m.portIdx);
        ecn_marks.write(m.portIdx, v + 1);
    }

    apply {
        if (h.ipv4.isValid() && sm.egress_port != 511) {
            bit<32> thresh = 0;
            // BMv2's enq_qdepth is always 0 in the single-threaded switch,
            // so use deq_timedelta (how long the packet waited in the
            // queue) instead: if it exceeds the threshold (in clock ticks),
            // the packet experienced queuing -> mark CE.
            bit<32> wait = (bit<32>)sm.deq_timedelta;
            // debug: count every packet reaching egress
            bit<32> tv = 0;
            egress_total.read(tv, m.portIdx);
            egress_total.write(m.portIdx, tv + 1);
            ecn_thresh.read(thresh, 0);
            if (wait > thresh) {
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
        // BMv2 forwarding changes the IPv4 header (TTL), which invalidates
        // the TCP checksum (it covers the pseudo-header + TCP header +
        // payload). Recompute it so the receiving kernel accepts the
        // segment. TCP checksum CANNOT be zeroed (unlike UDP), so we must
        // recompute with payload.
        // pseudo-header: src(4) dst(4) [8w0+proto](2) tcp_len(2)
        update_checksum_with_payload(
            h.tcp.isValid(),
            { h.ipv4.srcAddr, h.ipv4.dstAddr, 8w0, h.ipv4.protocol,
              m.tcp_len16, h.tcp.srcPort, h.tcp.dstPort,
              h.tcp.seqNo, h.tcp.ackNo, h.tcp.dataOffset, h.tcp.res,
              h.tcp.flags, h.tcp.window, h.tcp.urgentPtr },
            h.tcp.checksum,
            HashAlgorithm.csum16);
    }
}

control dep(packet_out pkt, in headers h) {
    apply {
        pkt.emit(h.eth);
        pkt.emit(h.ipv4);
        pkt.emit(h.udp);
        pkt.emit(h.tcp);
    }
}

V1Switch(p(), verifyChecksum(), ingress(), egress(), compute(), dep()) main;
