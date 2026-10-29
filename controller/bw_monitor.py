"""
Ryu application: L2 learning switch + bandwidth monitor + traffic analyzer.

Forwarding
    Standard MAC learning (OpenFlow 1.3). For IPv4 traffic the installed flow
    matches the full 5-tuple (src/dst IP, protocol, L4 ports) instead of just
    MAC addresses, so every conversation gets its own flow entry and its own
    byte counters. That is what makes per-flow analysis possible.

Monitoring
    Every --interval seconds (default 2) the controller polls each switch for
    port stats and flow stats and computes rates as
        rate = (bytes_now - bytes_prev) * 8 / (t_now - t_prev)
    using the switch-reported durations, not controller wall time, so polling
    jitter does not distort the result.

Output
    Console: per-port Mbps table and the top flows by rate with protocol mix
    logs/port_stats.csv, logs/flow_stats.csv for offline analysis

Run:
    ryu-manager controller/bw_monitor.py --ofp-tcp-listen-port 6633
    (env BWM_INTERVAL=1 to poll every second)
"""

import csv
import os
import time

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import (CONFIG_DISPATCHER, DEAD_DISPATCHER,
                                    MAIN_DISPATCHER, set_ev_cls)
from ryu.lib import hub
from ryu.lib.packet import ether_types, ethernet, in_proto, ipv4, packet, tcp, udp
from ryu.ofproto import ofproto_v1_3

PROTO_NAMES = {in_proto.IPPROTO_TCP: "TCP", in_proto.IPPROTO_UDP: "UDP",
               in_proto.IPPROTO_ICMP: "ICMP"}
LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")


class BandwidthMonitor(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.interval = float(os.environ.get("BWM_INTERVAL", "2"))
        self.mac_to_port = {}      # dpid -> {mac: port}
        self.datapaths = {}        # dpid -> datapath
        self.port_prev = {}        # (dpid, port) -> (rx_bytes, tx_bytes, t)
        self.flow_prev = {}        # (dpid, key) -> (bytes, t)

        os.makedirs(LOG_DIR, exist_ok=True)
        self.port_csv = self._open_csv("port_stats.csv",
            ["time", "dpid", "port", "rx_mbps", "tx_mbps", "rx_pkts", "tx_pkts",
             "rx_dropped", "tx_dropped"])
        self.flow_csv = self._open_csv("flow_stats.csv",
            ["time", "dpid", "proto", "src", "dst", "sport", "dport", "bytes",
             "packets", "mbps"])
        self.monitor_thread = hub.spawn(self._monitor)

    def _open_csv(self, name, header):
        fh = open(os.path.join(LOG_DIR, name), "w", newline="")
        w = csv.writer(fh)
        w.writerow(header)
        return fh, w

    # ------------------------------------------------------------------
    # Switch lifecycle
    # ------------------------------------------------------------------
    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def _state_change(self, ev):
        dp = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            self.datapaths[dp.id] = dp
            self.logger.info("switch %016x connected", dp.id)
        elif ev.state == DEAD_DISPATCHER and dp.id in self.datapaths:
            del self.datapaths[dp.id]
            self.logger.info("switch %016x disconnected", dp.id)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def _features(self, ev):
        dp = ev.msg.datapath
        ofp, parser = dp.ofproto, dp.ofproto_parser
        # table-miss: send to controller
        actions = [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)]
        self._add_flow(dp, 0, parser.OFPMatch(), actions)

    def _add_flow(self, dp, priority, match, actions, buffer_id=None, idle=0):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        kwargs = dict(datapath=dp, priority=priority, match=match,
                      instructions=inst, idle_timeout=idle)
        if buffer_id is not None and buffer_id != ofp.OFP_NO_BUFFER:
            kwargs["buffer_id"] = buffer_id
        dp.send_msg(parser.OFPFlowMod(**kwargs))

    # ------------------------------------------------------------------
    # Learning switch with 5-tuple flow granularity
    # ------------------------------------------------------------------
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in(self, ev):
        msg = ev.msg
        dp = msg.datapath
        ofp, parser = dp.ofproto, dp.ofproto_parser
        in_port = msg.match["in_port"]

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None or eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        table = self.mac_to_port.setdefault(dp.id, {})
        table[eth.src] = in_port
        out_port = table.get(eth.dst, ofp.OFPP_FLOOD)
        actions = [parser.OFPActionOutput(out_port)]

        # Only IPv4 gets a flow entry. ARP and other non-IP frames are rare and are
        # just packet-out'd: a MAC-only entry would otherwise swallow the reverse
        # IP traffic and hide it from per-flow accounting.
        is_ip = pkt.get_protocol(ipv4.ipv4) is not None
        if out_port != ofp.OFPP_FLOOD and is_ip:
            match = self._build_match(parser, in_port, eth, pkt)
            self._add_flow(dp, 10, match, actions, msg.buffer_id, idle=30)
            if msg.buffer_id != ofp.OFP_NO_BUFFER:
                return  # switch forwards the buffered packet itself

        data = msg.data if msg.buffer_id == ofp.OFP_NO_BUFFER else None
        dp.send_msg(parser.OFPPacketOut(datapath=dp, buffer_id=msg.buffer_id,
                                        in_port=in_port, actions=actions, data=data))

    @staticmethod
    def _build_match(parser, in_port, eth, pkt):
        ip = pkt.get_protocol(ipv4.ipv4)
        if ip is None:
            return parser.OFPMatch(in_port=in_port, eth_src=eth.src, eth_dst=eth.dst)
        fields = dict(in_port=in_port, eth_type=ether_types.ETH_TYPE_IP,
                      ipv4_src=ip.src, ipv4_dst=ip.dst, ip_proto=ip.proto)
        if ip.proto == in_proto.IPPROTO_UDP:
            u = pkt.get_protocol(udp.udp)
            fields.update(udp_src=u.src_port, udp_dst=u.dst_port)
        elif ip.proto == in_proto.IPPROTO_TCP:
            t = pkt.get_protocol(tcp.tcp)
            fields.update(tcp_src=t.src_port, tcp_dst=t.dst_port)
        return parser.OFPMatch(**fields)

    # ------------------------------------------------------------------
    # Monitoring
    # ------------------------------------------------------------------
    def _monitor(self):
        while True:
            for dp in list(self.datapaths.values()):
                parser, ofp = dp.ofproto_parser, dp.ofproto
                dp.send_msg(parser.OFPPortStatsRequest(dp, 0, ofp.OFPP_ANY))
                dp.send_msg(parser.OFPFlowStatsRequest(dp))
            hub.sleep(self.interval)

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def _port_stats(self, ev):
        dpid = ev.msg.datapath.id
        now = time.time()
        _, w = self.port_csv
        lines = []
        for s in sorted(ev.msg.body, key=lambda s: s.port_no):
            if s.port_no > 0xffffff00:  # skip LOCAL / reserved ports
                continue
            t = s.duration_sec + s.duration_nsec / 1e9
            key = (dpid, s.port_no)
            prev = self.port_prev.get(key)
            self.port_prev[key] = (s.rx_bytes, s.tx_bytes, t)
            if prev is None or t <= prev[2]:
                continue
            dt = t - prev[2]
            rx = max(0, s.rx_bytes - prev[0]) * 8 / dt / 1e6
            tx = max(0, s.tx_bytes - prev[1]) * 8 / dt / 1e6
            w.writerow([f"{now:.3f}", dpid, s.port_no, f"{rx:.4f}", f"{tx:.4f}",
                        s.rx_packets, s.tx_packets, s.rx_dropped, s.tx_dropped])
            if rx > 0.01 or tx > 0.01:
                lines.append(f"  s{dpid} port {s.port_no}: rx {rx:8.3f} Mbps  "
                             f"tx {tx:8.3f} Mbps  drops {s.tx_dropped}")
        self.port_csv[0].flush()
        if lines:
            self.logger.info("[ports] switch s%d\n%s", dpid, "\n".join(lines))

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def _flow_stats(self, ev):
        dpid = ev.msg.datapath.id
        now = time.time()
        _, w = self.flow_csv
        rows = []
        proto_bytes = {}
        for s in ev.msg.body:
            if s.priority == 0:
                continue  # table-miss entry
            m = s.match
            if "ipv4_src" not in m:
                continue
            proto = PROTO_NAMES.get(m.get("ip_proto"), str(m.get("ip_proto")))
            sport = m.get("udp_src", m.get("tcp_src", "-"))
            dport = m.get("udp_dst", m.get("tcp_dst", "-"))
            key = (dpid, m["in_port"], m["ipv4_src"], m["ipv4_dst"], proto, sport, dport)
            t = s.duration_sec + s.duration_nsec / 1e9
            prev = self.flow_prev.get(key)
            self.flow_prev[key] = (s.byte_count, t)
            if prev is None or t <= prev[1] or s.byte_count < prev[0]:
                continue  # first sample, or entry was re-installed
            mbps = (s.byte_count - prev[0]) * 8 / (t - prev[1]) / 1e6
            proto_bytes[proto] = proto_bytes.get(proto, 0) + s.byte_count
            w.writerow([f"{now:.3f}", dpid, proto, m["ipv4_src"], m["ipv4_dst"],
                        sport, dport, s.byte_count, s.packet_count, f"{mbps:.4f}"])
            rows.append((mbps, proto, m["ipv4_src"], sport, m["ipv4_dst"], dport,
                         s.packet_count))
        self.flow_csv[0].flush()

        active = [r for r in rows if r[0] > 0.01]
        if active:
            active.sort(reverse=True)
            total = sum(proto_bytes.values()) or 1
            mix = ", ".join(f"{p} {100 * b / total:.0f}%" for p, b in
                            sorted(proto_bytes.items(), key=lambda kv: -kv[1]))
            body = "\n".join(f"  {p:4s} {src}:{sp} -> {dst}:{dp_}  {mb:8.3f} Mbps  "
                             f"({n} pkts)" for mb, p, src, sp, dst, dp_, n in active[:5])
            self.logger.info("[flows] switch s%d  mix: %s\n%s", dpid, mix, body)
