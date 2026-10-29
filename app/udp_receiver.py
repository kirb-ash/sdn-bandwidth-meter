#!/usr/bin/env python3
"""
UDP measurement receiver with a TCP control channel.

Data plane (UDP):    collects datagrams from udp_sender.py
Control plane (TCP): ctrl_client.py starts/stops a run and pulls the report

Per run it computes:
  * goodput            bytes received / receive window, plus a 1 s time series
  * loss               packets_sent (reported by the client) - unique received
  * duplicates, reordering (seq lower than the highest already seen)
  * jitter             RFC 3550 interarrival jitter estimator, J += (|D| - J) / 16
  * one-way delay      arrival - send_ts. Valid inside Mininet because every
                       host shares the same kernel clock; meaningless across
                       real machines without clock sync.

Usage:
    python3 udp_receiver.py                 # serve control requests forever
    python3 udp_receiver.py --standalone    # just print live stats, Ctrl+C to stop
"""

import argparse
import json
import socket
import threading
import time

from proto import (DEFAULT_CTRL_PORT, DEFAULT_DATA_PORT, recv_msg, send_msg,
                   unpack_data)


class RunStats:
    """Statistics for one measurement run, keyed by test_id."""

    def __init__(self, test_id, interval=1.0):
        self.test_id = test_id
        self.interval = interval
        self.received = 0
        self.bytes = 0
        self.duplicates = 0
        self.reordered = 0
        self.seen = set()
        self.max_seq = -1
        self.first_arrival = None
        self.last_arrival = None
        self.jitter = 0.0
        self._prev_transit = None
        self.delay_sum = 0.0
        self.delay_min = float("inf")
        self.delay_max = 0.0
        self.bins = {}  # bin index -> bytes

    def add(self, seq, send_ts, arrival, nbytes):
        if seq in self.seen:
            self.duplicates += 1
            return
        self.seen.add(seq)
        self.received += 1
        self.bytes += nbytes

        if seq < self.max_seq:
            self.reordered += 1
        else:
            self.max_seq = seq

        if self.first_arrival is None:
            self.first_arrival = arrival
        self.last_arrival = arrival

        transit = arrival - send_ts
        if self._prev_transit is not None:
            d = abs(transit - self._prev_transit)
            self.jitter += (d - self.jitter) / 16.0
        self._prev_transit = transit

        self.delay_sum += transit
        self.delay_min = min(self.delay_min, transit)
        self.delay_max = max(self.delay_max, transit)

        b = int((arrival - self.first_arrival) / self.interval)
        self.bins[b] = self.bins.get(b, 0) + nbytes

    def report(self, packets_sent=None):
        window = 0.0
        if self.first_arrival is not None:
            window = self.last_arrival - self.first_arrival
        expected = packets_sent if packets_sent is not None else self.max_seq + 1
        lost = max(0, expected - self.received)
        n_bins = (max(self.bins) + 1) if self.bins else 0
        series = [self.bins.get(i, 0) * 8 / self.interval / 1e6 for i in range(n_bins)]
        # The last bin is usually partial; drop it from the series so plots don't dip.
        if len(series) > 1:
            series = series[:-1]
        return {
            "test_id": self.test_id,
            "packets_received": self.received,
            "packets_expected": expected,
            "packets_lost": lost,
            "loss_pct": 100.0 * lost / expected if expected else 0.0,
            "duplicates": self.duplicates,
            "reordered": self.reordered,
            "bytes_received": self.bytes,
            "receive_window_s": window,
            "goodput_mbps": (self.bytes * 8 / window / 1e6) if window > 0 else 0.0,
            "jitter_ms": self.jitter * 1e3,
            "delay_avg_ms": (self.delay_sum / self.received * 1e3) if self.received else None,
            "delay_min_ms": self.delay_min * 1e3 if self.received else None,
            "delay_max_ms": self.delay_max * 1e3 if self.received else None,
            "interval_s": self.interval,
            "throughput_series_mbps": series,
        }


class Receiver:
    def __init__(self, bind="0.0.0.0", data_port=DEFAULT_DATA_PORT,
                 ctrl_port=DEFAULT_CTRL_PORT, interval=1.0):
        self.bind = bind
        self.data_port = data_port
        self.ctrl_port = ctrl_port
        self.interval = interval
        self.lock = threading.Lock()
        self.current = None  # RunStats or None
        self.running = True
        self.standalone = False

        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
        self.udp.bind((bind, data_port))
        self.udp.settimeout(0.5)

    # ---------------- data plane ----------------
    def udp_loop(self):
        while self.running:
            try:
                buf, _ = self.udp.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            arrival = time.time()
            hdr = unpack_data(buf)
            if hdr is None:
                continue
            test_id, seq, send_ts = hdr
            with self.lock:
                if self.current is None or self.current.test_id != test_id:
                    if self.current is None and self.standalone:
                        self.current = RunStats(test_id, self.interval)
                    else:
                        continue
                self.current.add(seq, send_ts, arrival, len(buf))

    # ---------------- control plane ----------------
    def handle_client(self, conn, addr):
        f = conn.makefile("r")
        try:
            while True:
                msg = recv_msg(f)
                if msg is None:
                    break
                cmd = msg.get("cmd")
                if cmd == "START":
                    with self.lock:
                        self.current = RunStats(int(msg["test_id"]), self.interval)
                    print(f"[receiver] run {msg['test_id']} started by {addr[0]}")
                    send_msg(conn, {"status": "READY", "data_port": self.data_port})
                elif cmd == "STOP":
                    time.sleep(float(msg.get("drain_s", 0.5)))  # let in-flight packets land
                    with self.lock:
                        run, self.current = self.current, None
                    if run is None:
                        send_msg(conn, {"status": "ERROR", "error": "no active run"})
                        continue
                    rep = run.report(msg.get("packets_sent"))
                    print(f"[receiver] run {rep['test_id']}: {rep['goodput_mbps']:.2f} Mbps, "
                          f"loss {rep['loss_pct']:.2f}%, jitter {rep['jitter_ms']:.3f} ms")
                    send_msg(conn, {"status": "REPORT", "report": rep})
                elif cmd == "PING":
                    send_msg(conn, {"status": "PONG"})
                else:
                    send_msg(conn, {"status": "ERROR", "error": f"unknown cmd {cmd!r}"})
        finally:
            f.close()
            conn.close()

    def serve(self, standalone=False):
        self.standalone = standalone
        threading.Thread(target=self.udp_loop, daemon=True).start()
        print(f"[receiver] UDP data on {self.bind}:{self.data_port}")

        if standalone:
            try:
                while True:
                    time.sleep(self.interval)
                    with self.lock:
                        run = self.current
                        if run is not None:
                            r = run.report()
                            print(f"[receiver] rx={r['packets_received']} pkts  "
                                  f"goodput={r['goodput_mbps']:.2f} Mbps  "
                                  f"loss~{r['loss_pct']:.2f}%  jitter={r['jitter_ms']:.3f} ms")
            except KeyboardInterrupt:
                with self.lock:
                    if self.current is not None:
                        print(json.dumps(self.current.report(), indent=2))
            return

        tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        tcp.bind((self.bind, self.ctrl_port))
        tcp.listen(4)
        print(f"[receiver] TCP control on {self.bind}:{self.ctrl_port}")
        try:
            while True:
                conn, addr = tcp.accept()
                threading.Thread(target=self.handle_client, args=(conn, addr),
                                 daemon=True).start()
        except KeyboardInterrupt:
            pass
        finally:
            self.running = False
            tcp.close()
            self.udp.close()


def main():
    ap = argparse.ArgumentParser(description="UDP measurement receiver")
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--data-port", type=int, default=DEFAULT_DATA_PORT)
    ap.add_argument("--ctrl-port", type=int, default=DEFAULT_CTRL_PORT)
    ap.add_argument("--interval", type=float, default=1.0, help="time-series bin width (s)")
    ap.add_argument("--standalone", action="store_true",
                    help="no control channel; print live stats until Ctrl+C")
    a = ap.parse_args()
    Receiver(a.bind, a.data_port, a.ctrl_port, a.interval).serve(a.standalone)


if __name__ == "__main__":
    main()
