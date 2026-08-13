#include <core.p4>
#include <v1model.p4>

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

struct headers {
    ethernet_t eth;
    ipv4_t     ipv4;
}

struct meta {}

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
    }
}

V1Switch(p(), verifyChecksum(), ingress(), egress(), compute(), dep()) main;
