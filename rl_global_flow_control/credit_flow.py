#!/usr/bin/env python2
"""
credit_flow.py -- credit-based (lossless-style) flow control for the
receiver-sender pairs in the experiment.

The receiver grants the sender a credit allowance (in packets). The sender
only transmits while it has credit; every packet consumes one credit. The
receiver returns credit when its socket buffer drains below a low-water
mark. This prevents the *receiver's socket buffer* from being a drop point
(a constraint we observed in the earlier token-bucket experiments: high
rate + slow application reads -> RcvbufErrors / lost packets).

Run inside each namespace pair:
  receiver: python2 credit_flow.py recv <port> <grant>
  sender:   python2 credit_flow.py send <dst_ip> <port> <n> <base_sleep>
            (base_sleep in seconds between packets; scheduler scales it)
"""

import socket
import sys
import time
import struct


class CreditReceiver(object):
    def __init__(self, port, grant, water_low=8):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(('0.0.0.0', port))
        self.sock.setblocking(False)
        self.grant = grant          # initial credit handed to sender
        self.water_low = water_low  # drain threshold to return credit
        self.granted = grant
        self.received = 0

    def run(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            # drain whatever is in the socket buffer
            n = 0
            while True:
                try:
                    self.sock.recvfrom(1500)
                    n += 1
                except socket.error:
                    break
            self.received += n
            # return credit: if we drained >= grant, top the sender back up
            if n >= self.water_low and self.granted < self.grant:
                self.granted = self.grant
                self.sock.sendto(struct.pack('>I', self.grant), ('127.0.0.1', 0))
                # note: in the real topology credit travels on a control
                # channel; here we just log the intent.
            time.sleep(0.01)
        print 'receiver: received', self.received


class CreditSender(object):
    def __init__(self, dst_ip, port, base_sleep, scheduler=None):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.dst = (dst_ip, port)
        self.base_sleep = base_sleep
        self.credit = 0             # granted by receiver (starts 0 -> wait)
        self.scheduler = scheduler  # optional GlobalScheduler for pacing
        self.sock.settimeout(0.05)

    def run(self, n, max_credit=40):
        sent = 0
        while sent < n:
            if self.credit > 0:
                self.sock.sendto(b'P' * 1400, self.dst)
                self.credit -= 1
                sent += 1
            else:
                # no credit: ask for / wait for a grant
                try:
                    data, _ = self.sock.recvfrom(4)
                    if len(data) == 4:
                        self.credit = struct.unpack('>I', data)[0]
                except socket.timeout:
                    pass
            sleep = self.base_sleep
            if self.scheduler is not None:
                sleep = self.scheduler.pacing_sleep(self.base_sleep, 0)
            time.sleep(sleep)
        print 'sender: sent', sent


def main():
    role = sys.argv[1]
    if role == 'recv':
        port = int(sys.argv[2])
        grant = int(sys.argv[3])
        seconds = int(sys.argv[4]) if len(sys.argv) > 4 else 20
        CreditReceiver(port, grant).run(seconds)
    elif role == 'send':
        dst_ip = sys.argv[2]
        port = int(sys.argv[3])
        n = int(sys.argv[4])
        base_sleep = float(sys.argv[5])
        CreditSender(dst_ip, port, base_sleep).run(n)
    else:
        print 'usage: recv <port> <grant> [seconds] | send <ip> <port> <n> <sleep>'


if __name__ == '__main__':
    main()
