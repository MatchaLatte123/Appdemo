"""Bounded CIC input diagnostics from existing decoded TCP packet metadata."""
from collections import Counter, OrderedDict
from copy import deepcopy
from dataclasses import dataclass, field
from threading import Lock

from security_ai.web_ui.contract import (
    PARITY_BLOCK, PRESETS, SCHEMA, SHADOW_SIZES, TSHARK_FIELDS, from_tshark,
    has_cic21_metadata, packet_ns, summarize_group, vector,
)


MAX_PEERS_PER_LANE = 32


def describe_peers(packets, host):
    """Count actual TCP peers in this window, oriented relative to the capture host."""
    peers = {}
    unmapped = 0
    for packet in packets:
        inbound = packet.dst == host
        ip, remote_port, local_port = ((packet.src, packet.sport, packet.dport) if inbound
                                       else (packet.dst, packet.dport, packet.sport))
        if ip == host:
            unmapped += 1
            continue
        peer = peers.setdefault(ip, {})
        key = (remote_port, local_port)
        if key not in peer:
            peer[key] = {"remote_port": remote_port, "local_port": local_port,
                         "start": packet.stamp, "end": packet.stamp,
                         "packets_to_host": 0, "packets_from_host": 0,
                         "bytes_to_host": 0, "bytes_from_host": 0, "syn_to_host": 0}
        connection = peer[key]
        side = "to_host" if inbound else "from_host"
        connection[f"packets_{side}"] += 1
        connection[f"bytes_{side}"] += packet.length
        connection["syn_to_host"] += int(inbound and packet.flags & 0x12 == 0x02)
        connection["end"] = packet.stamp
    rows = []
    fields = ("packets_to_host", "packets_from_host", "bytes_to_host", "bytes_from_host", "syn_to_host")
    for ip, data in sorted(peers.items()):
        connections = [connection for _, connection in sorted(data.items())]
        rows.append({"ip": ip, "connections": connections,
                     **{field: sum(connection[field] for connection in connections) for field in fields}})
    inbound_count = sum(row["packets_to_host"] > 0 for row in rows)
    return {"local_ip": host, "peer_count": len(rows), "inbound_peer_count": inbound_count,
            "unmapped_packets": unmapped, "peers": rows, "attack_source_attribution": "UNKNOWN"}


@dataclass
class WindowState:
    pending: list = field(default_factory=list)
    latest: dict | None = None
    completed: int = 0
    discarded: int = 0

    def clear(self, clear_latest):
        self.discarded += len(self.pending)
        self.pending.clear()
        if clear_latest:
            self.latest = None


class CicInputProbe:
    """100- and 10-packet diagnostic lanes; neither consumes a model score or a label."""

    def __init__(self, host, video_port, cameras=(), *, preset="BASIC_10", sizes=SHADOW_SIZES):
        if not sizes or any(type(size) is not int or size not in SHADOW_SIZES for size in sizes):
            raise ValueError("INVALID_WINDOW_SIZES")
        self.sizes = tuple(sizes)
        self.preset = preset
        self.feature_order = PRESETS[preset]
        self.host = host
        self.video_port = video_port
        self.cameras = frozenset(cameras)
        self.lock = Lock()
        self.counters = {"MQTT": Counter(), "Video": Counter()}
        self.lanes = {"MQTT_BROKER": "MQTT", "VIDEO_API": "Video",
                      **{f"CAMERA[{ip}]:{port}": "Video" for ip, port in sorted(self.cameras)}}
        self.state = {scope: {size: WindowState() for size in self.sizes} for scope in self.lanes}
        self.last_ns = {}
        self.blocked = set()
        self.peer_size = None
        self.peer_span_ns = 0
        self.peer_state = {scope: OrderedDict() for scope in self.lanes}

    def enable_peer_windows(self, size, max_span_seconds):
        if size not in self.sizes or not 0 < max_span_seconds <= 120:
            raise ValueError("INVALID_PEER_WINDOW_CONFIG")
        self.peer_size = size
        self.peer_span_ns = int(max_span_seconds * 1e9)

    def _append(self, state, packet, size):
        state.pending.append(packet)
        if len(state.pending) != size:
            return None
        item = summarize_group(state.pending, self.preset)
        state.latest = {
            "features": item["features"], "vector": vector(item["features"], self.preset),
            "window": {key: item["metadata"][key] for key in ("start", "end", "span_seconds", "packet_count")},
            "source_evidence": describe_peers(state.pending, self.host),
            "capture_quality": "LOCAL_WINDOW_NO_LOSS_FLAG", "decision_eligible": False,
        }
        state.completed += 1
        state.pending.clear()
        return state.latest

    def _append_peer(self, scope, packet, stamp):
        ip = packet.src if packet.dst == self.host else packet.dst
        if ip == self.host:
            return
        states = self.peer_state[scope]
        if ip not in states:
            if len(states) >= MAX_PEERS_PER_LANE:
                states.popitem(last=False)
            states[ip] = WindowState()
        states.move_to_end(ip)
        state = states[ip]
        if state.pending and stamp - packet_ns(state.pending[0]) > self.peer_span_ns:
            state.clear(clear_latest=True)
        self._append(state, packet, self.peer_size)

    @staticmethod
    def _window_snapshot(state):
        return {"completed": state.completed, "pending": len(state.pending),
                "discarded": state.discarded, "latest": deepcopy(state.latest)}

    def _clear(self, service, *, clear_latest):
        for scope, lane_service in self.lanes.items():
            if service is None or service == lane_service:
                self.last_ns.pop(scope, None)
                self.peer_state[scope].clear()
                for state in self.state[scope].values():
                    state.clear(clear_latest)

    def start_capture(self, service=None):
        with self.lock:
            self._clear(service, clear_latest=True)
            self.blocked.difference_update((service,) if service else self.counters)

    def end_capture(self, service=None):
        with self.lock:
            self._clear(service, clear_latest=False)

    def invalidate(self, service=None, *, reason, hold=False):
        with self.lock:
            self._clear(service, clear_latest=True)
            for name in (service,) if service else self.counters:
                self.counters[name][reason] += 1
                if hold:
                    self.blocked.add(name)

    def observe(self, fields, service):
        try:
            # Multiple addresses or ports in one row indicate ambiguous encapsulation.
            packet = from_tshark(fields, self.host, video_port=self.video_port, cameras=self.cameras)
        except (ValueError, KeyError, TypeError, OverflowError):
            self.invalidate(service, reason="INVALID_METADATA")
            return ()
        if packet is None or packet.service != service:
            self.invalidate(service, reason="SCOPE_MISMATCH")
            return ()
        scope = packet.scope
        with self.lock:
            self.counters[service]["OBSERVED_PACKETS"] += 1
            if service in self.blocked:
                return ()
            stamp = packet_ns(packet)
            previous = self.last_ns.get(scope)
            if packet.quality != "OBSERVED" or packet.length <= 0 or stamp is None:
                reason = "FRAME_LOSS_OR_INVALID"
            elif self.preset == "CIC_21" and not has_cic21_metadata(packet):
                reason = "MISSING_CIC21_METADATA"
            elif previous is not None and stamp < previous:
                reason = "TIMESTAMP_REGRESSION"
            else:
                reason = None
            if reason:
                self.counters[service][reason] += 1
                self.last_ns.pop(scope, None)
                self.peer_state[scope].clear()
                for state in self.state[scope].values():
                    state.clear(clear_latest=True)
                return ()
            self.last_ns[scope] = stamp
            if self.peer_size is not None:
                self._append_peer(scope, packet, stamp)
            completed = []
            for size, state in self.state[scope].items():
                if latest := self._append(state, packet, size):
                    completed.append((scope, size, deepcopy(latest)))
            return tuple(completed)

    def snapshot(self):
        with self.lock:
            return {"status": "DIAGNOSTIC_ONLY", "parity": PARITY_BLOCK, "decision_eligible": False,
                    "input_schema": SCHEMA, "feature_order": self.feature_order,
                    "feature_orders": PRESETS, "packet_fields": TSHARK_FIELDS, "window_sizes": self.sizes,
                    "counters": deepcopy(self.counters), "blocked_services": sorted(self.blocked),
                    "lanes": {scope: {"service": service, "windows": {
                        size: self._window_snapshot(state) for size, state in self.state[scope].items()}}
                        for scope, service in self.lanes.items()},
                    "peer_windows": {scope: {ip: self._window_snapshot(state) for ip, state in states.items()}
                                     for scope, states in self.peer_state.items()}}



def attach_model_probe(monitor, *, preset="BASIC_10", sizes=SHADOW_SIZES):
    branches = tuple(monitor.branches.values()) if hasattr(monitor, "branches") else (monitor,)
    reference = branches[0]
    video_port = next(port for port, service in reference.ports.items() if service == "Video")
    probe = CicInputProbe(reference.host, video_port, reference.cameras, preset=preset, sizes=sizes)
    for branch in branches:
        branch.model_inputs = probe
    monitor.model_inputs = probe
    return probe
