"""Feature schemas for a deliberately non-deployable CICIoT2023 benchmark."""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from ipaddress import ip_address
from math import isfinite
from statistics import fmean, stdev

SCHEMA = "cic-network-reduced-1"
BENCHMARK = "BENCHMARK_ONLY"
PAPER = "https://doi.org/10.3390/s23135941"
FLAGS = {"syn_flag_number": 2, "rst_flag_number": 4, "ack_flag_number": 16,
         "fin_flag_number": 1, "psh_flag_number": 8}
BASIC_10 = (*FLAGS, "Min", "Max", "AVG", "Std", "IAT")
BASIC_8 = (*FLAGS, "Min", "Max", "Std")
EXTRA_FLAGS = {"ece_flag_number": 64, "cwr_flag_number": 128}
COUNT_FLAGS = {"ack_count": 16, "syn_count": 2, "fin_count": 1, "rst_count": 4}
CIC_21 = (*BASIC_10, "Rate", "Tot size", *EXTRA_FLAGS, *COUNT_FLAGS,
          "Header_Length", "Time_To_Live", "HTTP")
PRESETS = {"BASIC_8": BASIC_8, "BASIC_10": BASIC_10, "CIC_21": CIC_21}
SERVICES = ("MQTT", "Video")
SHADOW_SIZES = (10, 100)
PARITY_BLOCK = "BLOCKED_CLASS_CONDITIONED_GROUPING_AND_UNVERIFIED_EXTRACTOR"
CIC_21_FIELDS = ("tcp.hdr_len", "ip.ttl", "frame.protocols")
TSHARK_FIELDS = ("frame.time_epoch", "frame.len", "frame.cap_len", "tcp.flags", "tcp.srcport",
                 "tcp.dstport", "ip.src", "ip.dst", "ipv6.src", "ipv6.dst",
                 "tcp.analysis.lost_segment", "_ws.malformed.reassembly", *CIC_21_FIELDS)
FORMULAS = {**{name: ("mean of TCP flag indicators", "fraction") for name in FLAGS},
            "Min": ("minimum wire frame length", "bytes"), "Max": ("maximum wire frame length", "bytes"),
            "AVG": ("arithmetic mean of wire frame lengths", "bytes"),
            "Std": ("sample standard deviation of wire lengths (N-1)", "bytes"),
            "IAT": ("mean of consecutive packet timestamp differences", "seconds"),
            **{name: ("mean of TCP flag indicators", "fraction") for name in EXTRA_FLAGS},
            **{name: ("count of packets with the corresponding TCP flag", "packets") for name in COUNT_FLAGS},
            "Rate": ("packet count divided by elapsed time; zero for equal timestamps", "packets/second"),
            "Tot size": ("mean wire frame length", "bytes"),
            "Header_Length": ("mean TCP header length, inferred from the CIC CSV", "bytes"),
            "Time_To_Live": ("mean IPv4 TTL", "hops"),
            "HTTP": ("fraction of packets decoded as HTTP", "fraction")}


def contract(preset):
    return {"schema": SCHEMA, "status": BENCHMARK, "order": PRESETS[preset],
            "candidate_formulas": {name: dict(zip(("formula", "unit"), FORMULAS[name])) for name in PRESETS[preset]},
            "csv_semantics": "CIC paper describes flow values averaged over groups of 10 Benign and 100 DoS/DDoS packets",
            "candidate_semantics": "separate MQTT, Video API and configured camera TCP endpoint lanes, fixed packet counts",
            "diagnostic_window_sizes": SHADOW_SIZES,
            "parity": {service: PARITY_BLOCK for service in SERVICES},
            "exclusions": ["IP", "port", "payload", "filename", "device_id", "absolute_timestamp", "label", "scenario"],
            "reduction": {"IAT": "time-unit and temporal aggregation parity unverified",
                          "AVG": "interior length statistic overlaps Min/Max/Std; compare train-only correlations"},
            "citation": PAPER}


@dataclass(frozen=True)
class Packet:
    service: str
    stamp: float
    length: int
    flags: int
    src: str
    dst: str
    sport: int
    dport: int
    quality: str = "OBSERVED"
    scope: str = ""
    stamp_ns: int | None = None
    header_length: int | None = None
    ttl: int | None = None
    http: bool | None = None


def from_tshark(fields, host, *, mqtt_port=1883, video_port=8765, cameras=()):
    """Map one offline TShark fields row to scoped TCP metadata."""
    host = ip_address(host)
    src = ip_address(fields.get("ip.src") or fields.get("ipv6.src"))
    dst = ip_address(fields.get("ip.dst") or fields.get("ipv6.dst"))
    sport, dport = (int(fields[name]) for name in ("tcp.srcport", "tcp.dstport"))
    if not all(0 < value <= 65535 for value in (sport, dport)):
        raise ValueError("INVALID_TCP_PORT")
    if src == host:
        local_port, remote = sport, (str(dst), dport)
    elif dst == host:
        local_port, remote = dport, (str(src), sport)
    else:
        return None
    if local_port == mqtt_port:
        service, scope = "MQTT", "MQTT_BROKER"
    elif remote in cameras:
        service, scope = "Video", f"CAMERA[{remote[0]}]:{remote[1]}"
    elif local_port == video_port:
        service, scope = "Video", "VIDEO_API"
    else:
        return None
    length = int(fields["frame.len"])
    quality = "OBSERVED"
    if int(fields["frame.cap_len"]) != length or any(fields.get(key) for key in (
            "tcp.analysis.lost_segment", "_ws.malformed.reassembly")):
        quality = "LOSS"
    try:
        timestamp = Decimal(fields["frame.time_epoch"])
        if not timestamp.is_finite() or not Decimal(0) <= timestamp <= Decimal(2**63-1) / 10**9:
            raise ValueError("INVALID_TIMESTAMP")
        ns = int(timestamp * 10**9)
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("INVALID_TIMESTAMP") from exc
    header_length = int(fields["tcp.hdr_len"]) if fields.get("tcp.hdr_len") else None
    ttl = int(fields["ip.ttl"]) if fields.get("ip.ttl") else None
    protocols = fields.get("frame.protocols")
    http = "http" in protocols.split(":") if protocols else None
    return Packet(service, float(timestamp), length, int(fields["tcp.flags"], 16),
                  str(src), str(dst), sport, dport, quality, scope, ns, header_length, ttl, http)


def packet_ns(packet):
    if packet.stamp_ns is not None:
        return packet.stamp_ns
    if not isfinite(packet.stamp) or not 0 <= packet.stamp <= (2**63-1) / 10**9:
        return None
    return round(packet.stamp * 10**9)


def vector(features, preset="BASIC_10"):
    values = tuple(float(features[key]) for key in PRESETS[preset])
    if not all(isfinite(value) for value in values):
        raise ValueError("NONFINITE_MODEL_INPUT")
    return values


def has_cic21_metadata(packet):
    return (packet.header_length is not None and 20 <= packet.header_length <= 60 and packet.header_length % 4 == 0
            and packet.ttl is not None and 0 <= packet.ttl <= 255 and packet.http is not None)


def http_decode_args(ports):
    return [arg for port in sorted(set(ports) - {443}) for arg in ("-d", f"tcp.port=={port},http")]


def summarize_group(group, preset="BASIC_10"):
    lengths = [p.length for p in group]
    elapsed = (packet_ns(group[-1]) - packet_ns(group[0])) / 10**9
    features = {name: fmean(bool(p.flags & mask) for p in group) for name, mask in FLAGS.items()}
    features.update(Min=min(lengths), Max=max(lengths), AVG=fmean(lengths), Std=stdev(lengths),
                    IAT=elapsed / (len(group) - 1))
    if preset == "CIC_21":
        if not all(map(has_cic21_metadata, group)):
            raise ValueError("MISSING_CIC21_METADATA")
        features.update({name: fmean(bool(p.flags & mask) for p in group)
                         for name, mask in EXTRA_FLAGS.items()})
        features.update({name: sum(bool(p.flags & mask) for p in group)
                         for name, mask in COUNT_FLAGS.items()})
        features.update({"Rate": len(group) / elapsed if elapsed else 0.0, "Tot size": features["AVG"],
                         "Header_Length": fmean(p.header_length for p in group),
                         "Time_To_Live": fmean(p.ttl for p in group), "HTTP": fmean(p.http for p in group)})
    initial = group[0]
    return {"features": features, "metadata": {
        "service": initial.service, "scope": initial.scope or initial.service, "start": initial.stamp,
        "end": group[-1].stamp, "span_seconds": elapsed, "packet_count": len(group),
        "src": initial.src, "dst": initial.dst, "sport": initial.sport, "dport": initial.dport,
        "attribution": "UNKNOWN", "quality": "OBSERVED",
    }}


def windows(packets, size=10, preset="BASIC_10"):
    """Offline packet features using the same lane and formulas as the capture probe."""
    if type(size) is not int or size < 2:
        raise ValueError("GROUP_SIZE_INVALID")
    buckets = {}
    for packet in packets:
        group = buckets.setdefault(packet.scope or packet.service, [])
        stamp = packet_ns(packet)
        if (packet.quality != "OBSERVED" or stamp is None or packet.length <= 0
                or (preset == "CIC_21" and not has_cic21_metadata(packet))
                or (group and stamp < packet_ns(group[-1]))):
            group.clear()
            continue
        group.append(packet)
        if len(group) == size:
            yield summarize_group(group, preset)
            group.clear()
