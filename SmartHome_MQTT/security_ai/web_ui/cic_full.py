"""Export the 39-column CIC CSV schema from PCAP or passive Ethernet capture."""
import argparse
from collections import Counter
import csv
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
from math import sqrt
import os
from pathlib import Path
from statistics import fmean, variance
import subprocess
from tempfile import NamedTemporaryFile

from .contract import COUNT_FLAGS, EXTRA_FLAGS, FLAGS
from ..live.capture import DUMPCAP, TSHARK, discover, select, stop

CSV_FLAGS = {name: (FLAGS | EXTRA_FLAGS)[name] for name in (
    "fin_flag_number", "syn_flag_number", "rst_flag_number", "psh_flag_number",
    "ack_flag_number", "ece_flag_number", "cwr_flag_number")}
PROTOCOLS = ("HTTP", "HTTPS", "DNS", "Telnet", "SMTP", "SSH", "IRC", "TCP", "UDP",
             "DHCP", "ARP", "ICMP", "IGMP", "IPv", "LLC")
TCP_APPLICATION_PORTS = {"HTTPS": 443, "Telnet": 23, "SMTP": 25, "SSH": 22, "IRC": 21}
CIC_39 = ("Header_Length", "Protocol Type", "Time_To_Live", "Rate", *CSV_FLAGS, *COUNT_FLAGS,
          *PROTOCOLS, "Tot sum", "Min", "Max", "AVG", "Std", "Tot size", "IAT", "Number", "Variance")
CIC_38 = tuple(name for name in CIC_39 if name != "Number")
FIELDS = ("frame.time_epoch", "frame.cap_len", "frame.len", "eth.type", "ip.proto", "ip.ttl",
          "ip.frag_offset", "tcp.hdr_len", "tcp.flags", "tcp.srcport", "tcp.dstport", "udp.srcport",
          "udp.dstport")
SCHEMA = "cic39-author-aligned-v2"
NS = 10**9


@dataclass(frozen=True)
class Packet:
    stamp_ns: int
    size: int
    header: int
    protocol: int
    ttl: int
    flags: int
    indicators: frozenset[str]


def author_seconds(stamp_ns):
    seconds, nanos = divmod(stamp_ns, NS)
    return seconds + nanos / NS


def packet_gap(start, end):
    # dpkt computes PCAP timestamps as floats before the author's subtraction.
    if start % 1000 or end % 1000:
        return (end - start) / NS
    return author_seconds(end) - author_seconds(start)


def decode_packet(fields, http_ports=(80,)):
    # The author uses the outer Ethernet type and excludes IPv6 and other EtherTypes.
    outer = fields["eth.type"].split(";", 1)[0]
    if outer not in ("0x0800", "0x0806"):
        return None
    cells = {name: fields[name].split(";", 1)[0] for name in FIELDS}
    stamp = Decimal(cells["frame.time_epoch"])
    if not stamp.is_finite() or not 0 <= stamp <= Decimal(2**63 - 1) / NS:
        raise ValueError("Invalid timestamp")
    size = int(cells["frame.cap_len"])
    if size <= 0 or int(cells["frame.len"]) != size:
        raise ValueError("Truncated or invalid frame")
    if outer == "0x0806":
        return Packet(int(stamp * NS), size, 0, 0, 0, 0, frozenset(("ARP",)))

    protocol = int(cells["ip.proto"])
    ttl = int(cells["ip.ttl"])
    header = flags = 0
    # L1.LLC in the supplied source evaluates type(boolean), which is truthy on every IPv4 frame.
    indicators = {"IPv", "LLC"}
    if protocol in (1, 2):
        indicators.add({1: "ICMP", 2: "IGMP"}[protocol])
    if int(cells["ip.frag_offset"] or 0) == 0:
        if protocol == 6 and cells["tcp.hdr_len"] and cells["tcp.srcport"] and cells["tcp.dstport"]:
            header = int(cells["tcp.hdr_len"])
            if not 20 <= header <= 60 or header % 4:
                raise ValueError("Invalid TCP header length")
            flags = int(cells["tcp.flags"], 16)
            ports = {int(cells["tcp.srcport"]), int(cells["tcp.dstport"])}
            indicators.add("TCP")
            indicators.update(name for name, port in TCP_APPLICATION_PORTS.items() if port in ports)
            if ports.intersection(http_ports):
                indicators.add("HTTP")
        elif protocol == 17 and cells["udp.srcport"] and cells["udp.dstport"]:
            header = 8
            ports = {int(cells["udp.srcport"]), int(cells["udp.dstport"])}
            indicators.add("UDP")
            if 53 in ports:
                indicators.add("DNS")
            if ports == {67, 68}:
                indicators.add("DHCP")
    return Packet(int(stamp * NS), size, header, protocol, ttl, flags, frozenset(indicators))


def summarize(entries):
    packets, gaps = zip(*entries)
    n = len(packets)
    sizes = [packet.size for packet in packets]
    span = packet_gap(packets[0].stamp_ns, packets[-1].stamp_ns)
    average = fmean(sizes)
    spread = variance(sizes)
    protocol_counts = Counter(p.protocol for p in packets)
    most_common = min(protocol_counts, key=lambda value: (-protocol_counts[value], value))
    flag_counts = {mask: sum(bool(packet.flags & mask) for packet in packets) for mask in CSV_FLAGS.values()}
    return {
        "Header_Length": fmean(p.header for p in packets),
        "Protocol Type": most_common,
        "Time_To_Live": fmean(p.ttl for p in packets),
        "Rate": n / span if span else 0.0,
        **{name: flag_counts[mask] / n for name, mask in CSV_FLAGS.items()},
        **{name: flag_counts[mask] for name, mask in COUNT_FLAGS.items()},
        **{name: sum(name in p.indicators for p in packets) / n for name in PROTOCOLS},
        "Tot sum": sum(sizes), "Min": min(sizes), "Max": max(sizes), "AVG": average,
        "Std": sqrt(spread), "Tot size": average, "IAT": fmean(gaps), "Number": n, "Variance": spread,
    }


class FullWindows:
    """Bounded author-aligned feature windows with one preceding timestamp per packet stream."""

    def __init__(self, sizes, http_ports=()):
        self.pending = {size: [] for size in sizes}
        self.completed = Counter(dict.fromkeys(sizes, 0))
        self.latest = dict.fromkeys(sizes)
        self.http_ports = frozenset((80, *http_ports))
        self.last_ns = None
        self.counts = Counter(frames=0, skipped_frames=0, invalid_frames=0, timestamp_regressions=0)

    def reset(self):
        for pending in self.pending.values():
            pending.clear()
        self.last_ns = None
        for size in self.latest:
            self.latest[size] = None

    def push(self, fields):
        self.counts["frames"] += 1
        try:
            packet = decode_packet(fields, self.http_ports)
        except (ValueError, InvalidOperation):
            self.counts["invalid_frames"] += 1
            self.reset()
            return ()
        if packet is None:
            self.counts["skipped_frames"] += 1
            return ()
        if self.last_ns is not None and packet.stamp_ns < self.last_ns:
            self.counts["timestamp_regressions"] += 1
            self.reset()
        gap = 0.0 if self.last_ns is None else packet_gap(self.last_ns, packet.stamp_ns)
        self.last_ns = packet.stamp_ns
        results = []
        for size, pending in self.pending.items():
            pending.append((packet, gap))
            if len(pending) == size:
                values = summarize(pending)
                self.completed[size] += 1
                self.latest[size] = {"start": pending[0][0].stamp_ns / NS, "end": packet.stamp_ns / NS,
                                     "features": values}
                results.append(values)
                pending.clear()
        return results

    def snapshot(self):
        return {"schema": SCHEMA, "feature_order": CIC_39, "scope": "ETHERNET_IPV4_ARP",
                "counters": dict(self.counts),
                "windows": {size: {"pending": len(pending), "completed": self.completed[size],
                                   "latest": self.latest[size]} for size, pending in self.pending.items()}}


def write_rows(stream, output, window_size, http_ports=()):
    windows = FullWindows((window_size,), http_ports)
    writer = csv.DictWriter(output, fieldnames=CIC_39)
    writer.writeheader()
    for line in stream:
        try:
            fields = dict(zip(FIELDS, line.rstrip("\r\n").split("\t"), strict=True))
        except ValueError:
            windows.counts["frames"] += 1
            windows.counts["invalid_frames"] += 1
            windows.reset()
            continue
        writer.writerows(windows.push(fields))
    done = windows.completed[window_size]
    counts = windows.counts
    return {**counts, "windows": done, "discarded_frames": counts["frames"] - counts["skipped_frames"] - done * window_size}


def extract(output, *, window_size=10, pcap=None, interface=None, seconds=30, display_filter=None, http_ports=()):
    if (pcap is None) == (interface is None):
        raise ValueError("Choose one input: PCAP or interface")
    if not 2 <= window_size <= 10000:
        raise ValueError("Window size must be between 2 and 10000")
    if interface is not None and not 1 <= seconds <= 120:
        raise ValueError("Capture duration must be between 1 and 120 seconds")
    if any(not 1 <= port <= 65535 or port == 443 for port in http_ports):
        raise ValueError("HTTP decode ports must be between 1 and 65535 and cannot include TLS port 443")
    output = Path(output)
    if output.resolve().exists():
        raise FileExistsError(f"CSV output already exists: {output}")
    if pcap is not None:
        pcap = Path(pcap).resolve(strict=True)
        if not pcap.is_file() or pcap.suffix.lower() not in {".pcap", ".pcapng", ".pcp", ".cap"}:
            raise ValueError("Expected a PCAP/PCAPNG capture file")
    command = [TSHARK, "-n", "-l", "-r", str(pcap) if pcap else "-", "-T", "fields", "-E", "occurrence=f"]
    command.extend(("-o", "ip.defragment:FALSE"))
    if display_filter:
        command.extend(("-Y", display_filter))
    for field in FIELDS:
        command.extend(("-e", field))
    capture = decoder = None
    temporary = None
    try:
        if interface is not None:
            adapter = select(discover(DUMPCAP), interface)
            capture = subprocess.Popen([DUMPCAP, "-i", adapter, "-p", "-s", "0", "-a",
                                        f"duration:{seconds}", "-Q", "-w", "-"], stdout=subprocess.PIPE)
        try:
            decoder = subprocess.Popen(command, stdin=capture.stdout if capture else subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, encoding="ascii", errors="replace")
        finally:
            if capture:
                capture.stdout.close()
        with NamedTemporaryFile(dir=output.parent, prefix=f".{output.name}.", suffix=".tmp",
                                mode="w", encoding="utf-8", newline="", delete=False) as stream:
            temporary = Path(stream.name)
            with decoder.stdout:
                counts = write_rows(decoder.stdout, stream, window_size, http_ports)
        if decoder.wait() or (capture and capture.wait(timeout=5)):
            raise RuntimeError("Capture or TShark decoding failed")
        # Publish only after a successful decode and refuse to replace any existing result.
        if os.name == "nt":
            temporary.rename(output)
        else:
            os.link(temporary, output)
        return {"schema": SCHEMA, "columns": len(CIC_39), "window_packets": window_size,
                "http_ports": sorted(set(http_ports) | {80}), **counts}
    finally:
        for proc in (decoder, capture):
            if proc is not None:
                stop(proc)
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--pcap", type=Path)
    source.add_argument("--interface")
    parser.add_argument("--seconds", type=int, default=30)
    parser.add_argument("--window-packets", type=int, default=10)
    parser.add_argument("--display-filter", help="Optional Wireshark display filter, also applicable to offline PCAP")
    parser.add_argument("--http-port", action="append", type=int, default=[], help="Add a clear-text TCP port to the HTTP indicator (default 80)")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        summary = extract(args.output, pcap=args.pcap, interface=args.interface, seconds=args.seconds,
                          window_size=args.window_packets, display_filter=args.display_filter, http_ports=args.http_port)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        parser.exit(1, f"{error}\n")
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
