#!/usr/bin/env python3
"""
Automated experiment suite on the dumbbell topology.

  E1  Rate sweep h1 -> h3 across the bottleneck: find the capacity knee.
  E2  Two competing flows h1 -> h3 and h2 -> h4 at the same time:
      how the bottleneck is shared.
  E3  Same-switch flow h1 -> h2 (does not cross the bottleneck):
      control case, should see the 100 Mbps host link instead.

Results go to results/*.json; analysis/analyze.py turns them into plots.
Ryu must already be running (see README).

Usage:
    sudo python3 topology/run_experiments.py --bw 10 --duration 10
"""

import argparse
import json
import os
import time

from mininet.log import setLogLevel, info

from topo import build_net, disable_tx_offload

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app")
RESULTS = os.path.join(ROOT, "results")


def start_receiver(host):
    proc = host.popen(["python3", os.path.join(APP, "udp_receiver.py")])
    time.sleep(1.0)
    return proc


def client_cmd(server_ip, out, duration, rates):
    return (f"python3 {os.path.join(APP, 'ctrl_client.py')} --server {server_ip} "
            f"--duration {duration} --rates {' '.join(str(r) for r in rates)} "
            f"--quiet --out {out}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bw", type=float, default=10)
    ap.add_argument("--delay", default="5ms")
    ap.add_argument("--queue", type=int, default=100)
    ap.add_argument("--duration", type=float, default=10)
    ap.add_argument("--userspace", action="store_true",
                    help="userspace OVS datapath (no kernel module needed)")
    ap.add_argument("--rates", type=float, nargs="+",
                    default=[2, 4, 6, 8, 9, 10, 11, 12, 15, 20])
    a = ap.parse_args()

    setLogLevel("info")
    os.makedirs(RESULTS, exist_ok=True)
    net = build_net(a.bw, a.delay, a.queue, userspace=a.userspace)
    net.start()
    if a.userspace:
        disable_tx_offload(net)
    h1, h2, h3, h4 = net.get("h1", "h2", "h3", "h4")

    info("*** Waiting for controller and warming up MAC tables\n")
    time.sleep(3)
    net.pingAll()

    recv = [start_receiver(h) for h in (h2, h3, h4)]
    meta = {"bottleneck_mbps": a.bw, "delay": a.delay, "queue_pkts": a.queue,
            "duration_s": a.duration}
    with open(os.path.join(RESULTS, "meta.json"), "w") as fh:
        json.dump(meta, fh, indent=2)

    try:
        info("*** E1: rate sweep h1 -> h3\n")
        info(h1.cmd(client_cmd("10.0.0.3", os.path.join(RESULTS, "e1_sweep.json"),
                               a.duration, a.rates)))

        info("*** E2: competing flows h1 -> h3 and h2 -> h4\n")
        share = a.bw * 0.8  # each flow alone fits, together they don't
        p1 = h1.popen(client_cmd("10.0.0.3", os.path.join(RESULTS, "e2_flow_a.json"),
                                 a.duration, [share]), shell=True)
        p2 = h2.popen(client_cmd("10.0.0.4", os.path.join(RESULTS, "e2_flow_b.json"),
                                 a.duration, [share]), shell=True)
        p1.wait()
        p2.wait()

        info("*** E3: same-switch control flow h1 -> h2\n")
        info(h1.cmd(client_cmd("10.0.0.2", os.path.join(RESULTS, "e3_local.json"),
                               a.duration, [20, 50])))
    finally:
        for p in recv:
            p.terminate()
        net.stop()
    info(f"*** Done. Results in {RESULTS}. Next: python3 analysis/analyze.py\n")


if __name__ == "__main__":
    main()
