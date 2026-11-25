"""Bounded CIC input diagnostics from existing decoded TCP packet metadata."""
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, field
from threading import Lock

from security_ai_v2.rebuild.contract import (
    PARITY_BLOCK, PRESETS, SCHEMA, SHADOW_SIZES, TSHARK_FIELDS, from_tshark,
    packet_ns, summarize_group, vector,
)


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

    def __init__(self, host, video_port, cameras=()):
        self.host = host
        self.video_port = video_port
        self.cameras = frozenset(cameras)
        self.lock = Lock()
        self.counters = {"MQTT": Counter(), "Video": Counter()}
        self.lanes = {"MQTT_BROKER": "MQTT", "VIDEO_API": "Video",
                      **{f"CAMERA[{ip}]:{port}": "Video" for ip, port in sorted(self.cameras)}}
        self.state = {scope: {size: WindowState() for size in SHADOW_SIZES} for scope in self.lanes}
        self.last_ns = {}
        self.blocked = set()

    def _clear(self, service, *, clear_latest):
        for scope, lane_service in self.lanes.items():
            if service is None or service == lane_service:
                self.last_ns.pop(scope, None)
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
            elif previous is not None and stamp < previous:
                reason = "TIMESTAMP_REGRESSION"
            else:
                reason = None
            if reason:
                self.counters[service][reason] += 1
                self.last_ns.pop(scope, None)
                for state in self.state[scope].values():
                    state.clear(clear_latest=True)
                return ()
            self.last_ns[scope] = stamp
            completed = []
            for size, state in self.state[scope].items():
                state.pending.append(packet)
                if len(state.pending) != size:
                    continue
                item = summarize_group(state.pending)
                state.latest = {
                    "features": item["features"], "vector": vector(item["features"]),
                    "window": {key: item["metadata"][key] for key in ("start", "end", "span_seconds", "packet_count")},
                    "capture_quality": "LOCAL_WINDOW_NO_LOSS_FLAG",
                    "decision_eligible": False,
                }
                state.completed += 1
                state.pending.clear()
                completed.append((scope, size, deepcopy(state.latest)))
            return tuple(completed)

    def snapshot(self):
        with self.lock:
            return {"status": "DIAGNOSTIC_ONLY", "parity": PARITY_BLOCK, "decision_eligible": False,
                    "input_schema": SCHEMA, "feature_order": PRESETS["BASIC_10"],
                    "feature_orders": PRESETS, "packet_fields": TSHARK_FIELDS, "window_sizes": SHADOW_SIZES,
                    "counters": deepcopy(self.counters), "blocked_services": sorted(self.blocked),
                    "lanes": {scope: {"service": service, "windows": {
                        size: {"completed": state.completed, "pending": len(state.pending),
                               "discarded": state.discarded, "latest": deepcopy(state.latest)}
                        for size, state in self.state[scope].items()}}
                        for scope, service in self.lanes.items()}}


def attach_model_probe(monitor):
    branches = tuple(monitor.branches.values()) if hasattr(monitor, "branches") else (monitor,)
    reference = branches[0]
    video_port = next(port for port, service in reference.ports.items() if service == "Video")
    probe = CicInputProbe(reference.host, video_port, reference.cameras)
    for branch in branches:
        branch.model_inputs = probe
    monitor.model_inputs = probe
    return probe
