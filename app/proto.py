"""
Wire format shared by the UDP sender, receiver and control client.

Data packet (UDP), network byte order:
    magic    uint32   0x42574D31 ("BWM1")
    test_id  uint32   identifies one measurement run
    seq      uint32   0, 1, 2, ... per run
    send_ts  float64  sender wall-clock time (seconds)
    payload  padding up to the requested packet size

Control messages (TCP) are newline-delimited JSON objects.
"""

import json
import struct

MAGIC = 0x42574D31
HEADER = struct.Struct("!IIId")
HEADER_SIZE = HEADER.size  # 20 bytes

DEFAULT_DATA_PORT = 5001
DEFAULT_CTRL_PORT = 5000


def pack_data(test_id, seq, send_ts, pkt_size):
    """Build one UDP datagram of exactly pkt_size bytes."""
    if pkt_size < HEADER_SIZE:
        raise ValueError(f"packet size must be >= {HEADER_SIZE} bytes")
    return HEADER.pack(MAGIC, test_id, seq, send_ts) + b"\x00" * (pkt_size - HEADER_SIZE)


def unpack_data(buf):
    """Return (test_id, seq, send_ts) or None if the datagram is not ours."""
    if len(buf) < HEADER_SIZE:
        return None
    magic, test_id, seq, send_ts = HEADER.unpack_from(buf)
    if magic != MAGIC:
        return None
    return test_id, seq, send_ts


def send_msg(sock, obj):
    sock.sendall((json.dumps(obj) + "\n").encode())


def recv_msg(fileobj):
    """Read one JSON line from a socket's makefile('r'). Returns None on EOF."""
    line = fileobj.readline()
    if not line:
        return None
    return json.loads(line)
