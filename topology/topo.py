#!/usr/bin/env python3
"""
Dumbbell topology with a single bottleneck link.

    h1 ─┐                        ┌─ h3
        s1 ═══ bottleneck ═══ s2
    h2 ─┘   (default 10 Mbps)    └─ h4

  host links : 100 Mbps, 1 ms
  s1 <-> s2  : --bw Mbps, --delay, --queue packets  (the link we measure)

Switches are Open vSwitch speaking OpenFlow 1.3 to a remote Ryu controller.

Usage (Ryu must already be running, see README):
    sudo python3 topology/topo.py                  # opens the Mininet CLI
    sudo python3 topology/topo.py --bw 20 --delay 10ms
"""

import argparse
from functools import partial

from mininet.cli import CLI
from mininet.link import TCLink
from mininet.log import setLogLevel, info
from mininet.net import Mininet
from mininet.node import OVSSwitch, RemoteController
from mininet.topo import Topo


class DumbbellTopo(Topo):
    def build(self, bw=10, delay="5ms", queue=100, host_bw=100):
        # delay needs the netem qdisc; "0ms"/"none" skips it (bandwidth-only shaping)
        no_delay = str(delay).lower() in ("0", "0ms", "none", "")
        host_opts = dict(bw=host_bw) if no_delay else dict(bw=host_bw, delay="1ms")
        neck_opts = dict(bw=bw)
        if queue:  # also implemented with netem by Mininet; 0 = kernel default queue
            neck_opts["max_queue_size"] = queue
        if not no_delay:
            neck_opts["delay"] = delay

        s1 = self.addSwitch("s1", protocols="OpenFlow13")
        s2 = self.addSwitch("s2", protocols="OpenFlow13")
        h1 = self.addHost("h1", ip="10.0.0.1/24")
        h2 = self.addHost("h2", ip="10.0.0.2/24")
        h3 = self.addHost("h3", ip="10.0.0.3/24")
        h4 = self.addHost("h4", ip="10.0.0.4/24")

        for h, s in ((h1, s1), (h2, s1), (h3, s2), (h4, s2)):
            self.addLink(h, s, **host_opts)
        self.addLink(s1, s2, **neck_opts)


def build_net(bw=10, delay="5ms", queue=100, ctrl_ip="127.0.0.1", ctrl_port=6633,
              userspace=False):
    """userspace=True runs OVS in userspace (datapath=netdev): for VMs/containers/WSL
    where the openvswitch kernel module is not available."""
    topo = DumbbellTopo(bw=bw, delay=delay, queue=queue)
    switch = partial(OVSSwitch, datapath="user" if userspace else "kernel")
    net = Mininet(topo=topo, switch=switch, link=TCLink,
                  controller=None, autoSetMacs=True)
    net.addController("c0", controller=RemoteController, ip=ctrl_ip, port=ctrl_port)
    return net


def disable_tx_offload(net):
    """With the userspace datapath, veth TX checksum offload leaves TCP checksums
    unfilled and the receiver drops every segment (ping/UDP still work, TCP hangs).
    Turning offload off on the hosts fixes it. Not needed with the kernel datapath."""
    for h in net.hosts:
        h.cmd(f"ethtool -K {h.defaultIntf()} tx off >/dev/null 2>&1")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bw", type=float, default=10, help="bottleneck bandwidth (Mbps)")
    ap.add_argument("--delay", default="5ms", help="bottleneck one-way delay")
    ap.add_argument("--queue", type=int, default=100, help="bottleneck queue (packets)")
    ap.add_argument("--controller", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=6633)
    ap.add_argument("--userspace", action="store_true",
                    help="userspace OVS datapath (no kernel module needed)")
    a = ap.parse_args()

    setLogLevel("info")
    net = build_net(a.bw, a.delay, a.queue, a.controller, a.port, a.userspace)
    net.start()
    if a.userspace:
        disable_tx_offload(net)
    info(f"*** Bottleneck s1<->s2: {a.bw} Mbps, {a.delay}, queue {a.queue} pkts\n")
    info("*** Try: h3 python3 app/udp_receiver.py &\n")
    info("***      h1 python3 app/ctrl_client.py --server 10.0.0.3 --rate 5\n")
    CLI(net)
    net.stop()


if __name__ == "__main__":
    main()
