#!/usr/bin/env python3
"""
Paced UDP traffic generator.

Sends fixed-size datagrams at a target bit rate for a fixed duration.
Each datagram carries a sequence number and a send timestamp so the
receiver can compute throughput, loss, reordering and jitter.

Pacing: packet i is scheduled at t0 + i * gap, where
gap = pkt_size * 8 / rate_bps. The sender sleeps while it is ahead of
schedule and sends back-to-back when it falls behind, so the long-run
average rate stays on target even with coarse OS sleep resolution.

Usage:
    python3 udp_sender.py --dst 10.0.0.3 --rate 5 --duration 10
"""

import argparse
import socket
import time

from proto import DEFAULT_DATA_PORT, pack_data


def run_sender(dst, port=DEFAULT_DATA_PORT, rate_mbps=1.0, duration=10.0,
               pkt_size=1200, test_id=1, verbose=True):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)

    unlimited = rate_mbps <= 0
    gap = 0.0 if unlimited else (pkt_size * 8) / (rate_mbps * 1e6)

    seq = 0
    sent_bytes = 0
    send_errors = 0
    t0 = time.perf_counter()
    t_end = t0 + duration
    next_report = t0 + 1.0
    last_report_bytes = 0

    while True:
        now = time.perf_counter()
        if now >= t_end:
            break
        if not unlimited:
            due = t0 + seq * gap
            ahead = due - now
            if ahead > 0.0005:
                time.sleep(ahead)
                continue
            if ahead > 0:
                continue  # spin for the last half millisecond
        try:
            sock.sendto(pack_data(test_id, seq, time.time(), pkt_size), (dst, port))
            sent_bytes += pkt_size
        except OSError:
            send_errors += 1  # e.g. ENOBUFS; count it, keep the sequence moving
        seq += 1

        if verbose and now >= next_report:
            mbps = (sent_bytes - last_report_bytes) * 8 / 1e6
            print(f"[sender] t={now - t0:5.1f}s  offered={mbps:7.2f} Mbps  pkts={seq}")
            last_report_bytes = sent_bytes
            next_report += 1.0

    elapsed = time.perf_counter() - t0
    sock.close()
    summary = {
        "test_id": test_id,
        "packets_sent": seq,
        "bytes_sent": sent_bytes,
        "send_errors": send_errors,
        "duration_s": elapsed,
        "offered_mbps": sent_bytes * 8 / elapsed / 1e6 if elapsed > 0 else 0.0,
        "target_mbps": rate_mbps,
        "pkt_size": pkt_size,
    }
    if verbose:
        print(f"[sender] done: {seq} packets, {summary['offered_mbps']:.2f} Mbps offered "
              f"(target {rate_mbps} Mbps)")
    return summary


def main():
    ap = argparse.ArgumentParser(description="Paced UDP traffic generator")
    ap.add_argument("--dst", required=True, help="receiver IP")
    ap.add_argument("--port", type=int, default=DEFAULT_DATA_PORT)
    ap.add_argument("--rate", type=float, default=1.0, help="target rate in Mbps (<=0: unlimited)")
    ap.add_argument("--duration", type=float, default=10.0, help="seconds")
    ap.add_argument("--size", type=int, default=1200, help="datagram size in bytes")
    ap.add_argument("--test-id", type=int, default=1)
    a = ap.parse_args()
    run_sender(a.dst, a.port, a.rate, a.duration, a.size, a.test_id)


if __name__ == "__main__":
    main()
