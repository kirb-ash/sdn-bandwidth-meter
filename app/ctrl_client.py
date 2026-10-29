#!/usr/bin/env python3
"""
Control client: orchestrates a measurement run end to end.

    1. TCP connect to the receiver's control port, send START(test_id)
    2. Wait for READY, then generate paced UDP traffic (udp_sender.run_sender)
    3. Send STOP with the number of packets actually sent
    4. Receive the REPORT, merge it with the sender-side summary, save JSON

A rate sweep (--rates 2 5 8 10 12 15) runs one test per rate, which is how
the bottleneck capacity is found: goodput tracks the offered load until it
flattens at the link capacity, and loss rises from that point on.

Usage:
    python3 ctrl_client.py --server 10.0.0.3 --rate 5 --duration 10
    python3 ctrl_client.py --server 10.0.0.3 --rates 2 5 8 10 12 15 --out ../results/sweep.json
"""

import argparse
import json
import os
import random
import socket
import time

from proto import DEFAULT_CTRL_PORT, recv_msg, send_msg
from udp_sender import run_sender


def run_test(server, rate_mbps, duration, pkt_size, ctrl_port=DEFAULT_CTRL_PORT,
             test_id=None, label=None, verbose=True):
    test_id = test_id if test_id is not None else random.randint(1, 2**31)
    with socket.create_connection((server, ctrl_port), timeout=10) as conn:
        conn.settimeout(duration + 30)
        f = conn.makefile("r")

        send_msg(conn, {"cmd": "START", "test_id": test_id})
        reply = recv_msg(f)
        if not reply or reply.get("status") != "READY":
            raise RuntimeError(f"receiver did not accept START: {reply}")
        data_port = reply["data_port"]

        sent = run_sender(server, data_port, rate_mbps, duration, pkt_size, test_id, verbose)

        send_msg(conn, {"cmd": "STOP", "packets_sent": sent["packets_sent"], "drain_s": 0.5})
        reply = recv_msg(f)
        if not reply or reply.get("status") != "REPORT":
            raise RuntimeError(f"no report from receiver: {reply}")

    result = {
        "label": label or f"{rate_mbps} Mbps",
        "server": server,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "sender": sent,
        "receiver": reply["report"],
    }
    r = result["receiver"]
    print(f"[client] {result['label']}: offered {sent['offered_mbps']:.2f} Mbps -> "
          f"goodput {r['goodput_mbps']:.2f} Mbps | loss {r['loss_pct']:.2f}% | "
          f"jitter {r['jitter_ms']:.3f} ms | reordered {r['reordered']}")
    return result


def main():
    ap = argparse.ArgumentParser(description="Bandwidth meter control client")
    ap.add_argument("--server", required=True, help="receiver IP")
    ap.add_argument("--ctrl-port", type=int, default=DEFAULT_CTRL_PORT)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--rate", type=float, default=5.0, help="single test rate (Mbps)")
    g.add_argument("--rates", type=float, nargs="+", help="sweep these rates (Mbps)")
    ap.add_argument("--duration", type=float, default=10.0)
    ap.add_argument("--size", type=int, default=1200)
    ap.add_argument("--pause", type=float, default=2.0, help="gap between sweep runs (s)")
    ap.add_argument("--out", help="write results JSON here")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    rates = a.rates if a.rates else [a.rate]
    results = []
    for i, rate in enumerate(rates):
        results.append(run_test(a.server, rate, a.duration, a.size, a.ctrl_port,
                                verbose=not a.quiet))
        if i < len(rates) - 1:
            time.sleep(a.pause)  # let queues drain between runs

    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, "w") as fh:
            json.dump(results, fh, indent=2)
        print(f"[client] saved {len(results)} result(s) to {a.out}")


if __name__ == "__main__":
    main()
