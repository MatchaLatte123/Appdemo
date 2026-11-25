"""V3 features from the existing capture and independent service telemetry."""
from collections import Counter, defaultdict, deque
from http.client import HTTPConnection, HTTPException
from ipaddress import IPv4Address
import json
from math import isfinite
from statistics import median
from threading import Lock
from time import time
from urllib.parse import urlsplit

SCHEMA = "behavior-v3.1"
SPAN = 5
BASE = ("packet_rate", "byte_rate", "connection_rate", "rst_rate")
NETWORK = (*BASE, "packet_rate_change")
MQTT = (*NETWORK, "publish_rate", "subscribe_rate")
CAMERA = (*NETWORK, "media_age", "camera_fps", "ffmpeg_health", "recorder_error")
FEATURES = {
    "MQTT_DEVICE": MQTT,
    "MQTT_BROKER": (*MQTT, "clients", "broker_health", "message_rate"),
    "CAMERA_DEVICE": CAMERA,
    "VIDEO_PEER": (*NETWORK, "http_rate"),
    "VIDEO_SERVER": (*NETWORK, "http_rate", "media_age", "camera_fps", "ffmpeg_health", "recorder_error"),
}
COUNT_FIELDS = {"packet_rate": "frames", "byte_rate": "bytes", "connection_rate": "syn",
                "rst_rate": "rst", "publish_rate": "publish", "subscribe_rate": "subscribe",
                "http_rate": "http"}


def first(packet, key):
    return packet.get(key, "").split(";", 1)[0]


def endpoint(url):
    try:
        parsed = urlsplit(url)
        return str(IPv4Address(parsed.hostname)), parsed.port or (443 if parsed.scheme == "https" else 80)
    except (ValueError, TypeError, AttributeError):
        return None


class Identity:
    """Bindings must come from a configured, operator-trusted external export."""
    def __init__(self, registry, cameras, bindings=()):
        self.registry = registry
        self.cameras = defaultdict(set)
        self.bindings = defaultdict(list)
        for camera in cameras:
            if camera.get("enabled", True) and (address := endpoint(camera.get("stream_url"))):
                if camera_id := camera.get("id") or camera.get("camera_id"):
                    self.cameras[address].add(camera_id)
        for b in bindings:
            if (b.get("source") not in ("BROKER_AUTH_SESSION", "VERIFIED_RECORDER_MAPPING")
                    or not isinstance(b.get("device_id"), str)
                    or type(b.get("port")) is not int or not 1 <= b["port"] <= 65535
                    or not all(type(b.get(k)) in (int, float) and isfinite(b[k]) for k in ("start", "end"))
                    or b["end"] <= b["start"]):
                raise ValueError("Invalid identity binding")
            self.bindings[b["source"], str(IPv4Address(b["ip"])), b["port"]].append(b)

    def resolve(self, role, ip, port, start, end, cameras):
        candidates = self.cameras.get((ip, port), set()) if role == "CAMERA_DEVICE" else set()
        source = {"CAMERA_DEVICE": "VERIFIED_RECORDER_MAPPING", "MQTT_DEVICE": "BROKER_AUTH_SESSION"}.get(role)
        matches = []
        partial = False
        for b in self.bindings.get((source, ip, port), ()):
            device = b["device_id"]
            if device not in self.registry or not (b["start"] < end and b["end"] > start):
                continue
            if role == "CAMERA_DEVICE" and (device not in candidates or device not in cameras):
                continue
            if role == "MQTT_DEVICE" and not all(b.get(k) for k in ("client_id", "username", "session_id")):
                continue
            if b["start"] > start or b["end"] < end:
                partial = True
            else:
                matches.append(b)
        result = {"status": "UNKNOWN", "ip": ip, "port": port}
        if partial or len(matches) > 1 or len(candidates) > 1:
            return {**result, "status": "AMBIGUOUS"}
        if matches:
            b = matches[0]
            return {**result, "status": "VERIFIED", "device_id": b["device_id"],
                    "device_type": self.registry[b["device_id"]].get("type"), "binding_source": source,
                    **{k: b[k] for k in ("username", "client_id", "session_id") if k in b}}
        if candidates:
            return {**result, "status": "PROBABLE", "candidate_device_id": next(iter(candidates))}
        return result


def recorder_snapshot(host, port, now):
    from datetime import datetime
    conn = HTTPConnection(host, port, timeout=1.5)
    try:
        conn.request("GET", "/health")
        response = conn.getresponse()
        if response.status != 200:
            return {"status": "UNAVAILABLE"}
        raw = response.read(128 * 1024 + 1)
        if len(raw) > 128 * 1024:
            return {"status": "UNAVAILABLE"}
        data = json.loads(raw)
        cameras = {}
        for camera in data["cameras"][:64]:
            age = None
            if camera.get("last_media_at"):
                age = max(0, now - datetime.fromisoformat(camera["last_media_at"]).timestamp())
            last_error = camera.get("last_error_at")
            recent_error = bool(last_error and 0 <= now - datetime.fromisoformat(last_error).timestamp() <= 60)
            measured_fps = camera.get("live_source_fps")
            if type(measured_fps) not in (int, float) or not isfinite(measured_fps) or measured_fps <= 0:
                measured_fps = None
            cameras[camera["camera_id"]] = {"mode": camera.get("recorder_state") or "unknown",
                                           "media_age": age,  "camera_fps": measured_fps,
                                           "ffmpeg_health": int(bool(camera.get("ffmpeg_pid"))),
                                           "recorder_error": int(recent_error)}
        storage = data.get("storage", {})
        storage_error = bool(storage.get("last_error") or storage.get("last_cleanup_error"))
        ages = [c["media_age"] for c in cameras.values() if c["media_age"] is not None]
        observed_fps = [c["camera_fps"] for c in cameras.values()
                        if c["mode"] == "recording" and type(c["camera_fps"]) in (int, float)
                        and isfinite(c["camera_fps"]) and c["camera_fps"] >= 0]
        return {"status": "HEALTHY" if data["service"] == "running" and data.get("ffmpeg_available")
                and not storage_error else "DEGRADED", "cameras": cameras,
                "media_age": max(ages, default=None),
                "camera_fps": median(observed_fps) if observed_fps else None,
                "ffmpeg_health": int(data["ffmpeg_available"]) if type(data.get("ffmpeg_available")) is bool else None,
                "recorder_error": int(storage_error or any(c["recorder_error"] for c in cameras.values()))}
    except (HTTPException, OSError, TypeError, ValueError, KeyError, AttributeError):
        return {"status": "UNAVAILABLE"}
    finally:
        conn.close()


class Telemetry:
    def __init__(self, identity):
        self.identity = identity
        self.lock = Lock()
        self.pending = {}
        self.bad = set()
        self.history = {}
        self.next_start = (int(time()) // SPAN + 1) * SPAN

    def observe(self, packet, service, host, cameras):
        stamp = float(first(packet, "frame.time_epoch"))
        if not isfinite(stamp) or stamp < 0 or stamp > time() + SPAN:
            raise ValueError("Invalid capture timestamp")
        start = int(stamp // SPAN) * SPAN
        incoming = first(packet, "ip.dst") == host
        side = "src" if incoming else "dst"
        ip, port = first(packet, f"ip.{side}"), int(first(packet, f"tcp.{side}port"))
        role = "MQTT_DEVICE" if service == "MQTT" else "CAMERA_DEVICE" if (ip, port) in cameras else "VIDEO_PEER"
        flags = int(first(packet, "tcp.flags") or "0", 16)
        size = int(first(packet, "frame.len"))
        types = packet.get("mqtt.msgtype", "").split(";")
        methods = packet.get("http.request.method", "").split(";")
        with self.lock:
            if start < self.next_start:
                if start >= self.next_start - SPAN:
                    self.bad.add((self.next_start, service))
                return
            peers = self.pending.setdefault((start, service), {})
            key = (role, ip, port)
            if key not in peers and len(peers) >= 256:
                self.bad.add((start, service))
                key = (role, None, 0)
            counts = peers.setdefault(key, Counter())
            counts.update(frames=1, bytes=size, syn=int(incoming and flags & 18 == 2),
                          rst=int(bool(flags & 4)),
                          truncated=int(int(first(packet, "frame.cap_len") or size) < size),
                          gap=int(bool(packet.get("tcp.analysis.lost_segment") or packet.get("_ws.malformed.reassembly"))),
                          tls=int(bool(packet.get("tls.record.content_type"))),
                          decoded=int(any(types)), publish=types.count("3") if incoming else 0,
                          subscribe=types.count("8") if incoming else 0,
                          http=sum(bool(m) for m in methods) if incoming and role == "VIDEO_PEER" else 0)

    def drain(self, now, monitor, video, broker):
        with self.lock:
            if now - self.next_start > 60:
                self.pending.clear()
                self.bad.clear()
                self.history.clear()
                self.next_start = (int(now) // SPAN + 1) * SPAN
            batches = []
            while self.next_start + SPAN + 2 <= now:
                start = self.next_start
                self.next_start += SPAN
                windows = {s: (self.pending.pop((start, s), {}), (start, s) in self.bad) for s in ("MQTT", "Video")}
                self.bad.difference_update((start, s) for s in windows)
                batches.append((start, windows))
        for start, windows in batches:
            rows = []
            for service, (peers, bad) in windows.items():
                rows.extend(self.rows(start, service, peers, bad, monitor, video, broker))
            active = {r["key"] for r in rows}
            for key in self.history.keys() - active:
                del self.history[key]
            for row in rows:
                history = self.history.setdefault(row["key"], deque(maxlen=12))
                if history and (history[-1]["end"] != start or history[-1]["mode"] != row["mode"]):
                    history.clear()
                previous = history[-1]["values"]["packet_rate"] if history else None
                current = row["values"]["packet_rate"]
                if previous is not None and current is not None:
                    row["values"]["packet_rate_change"] = current - previous
                    row["feature_quality"]["packet_rate_change"] = (
                        "OBSERVED" if all(h["quality"] == "OBSERVED" for h in (history[-1], row)) else "UNVERIFIED")
                history.append(row)
                row["rolling"] = {
                    name: sum(h["values"][name] for h in history) / 12
                    if len(history) == 12 and all(h["values"].get(name) is not None for h in history) else None
                    for name in row["values"]}
            yield rows

    def rows(self, start, service, peers, bad, monitor, video, broker):
        branch = service
        decoder = monitor.get("capture_branches", {}).get(branch, {})
        status = monitor.get("branch_status", {}).get(branch, monitor.get("status"))
        total = sum(peers.values(), Counter())
        if bad or total["truncated"] or decoder.get("packet_drops") or decoder.get("decode_errors") or (
                service == "MQTT" and monitor.get("packet_drops")):
            quality = "LOSS"
        elif status not in ("ACTIVE", "WAITING_FOR_TRAFFIC"):
            quality = "UNAVAILABLE"
        elif decoder.get("drop_status") == "VERIFIED_ZERO_DROPS":
            quality = "OBSERVED"
        else:
            quality = "UNVERIFIED" if total["frames"] else "UNAVAILABLE"
        cameras = video.get("cameras", {})
        groups = {}
        contributors = {}
        for (role, ip, port), counts in peers.items():
            if ip is None:
                continue
            identity = self.identity.resolve(role, ip, port, start, start + SPAN, cameras)
            device = identity.get("device_id")
            subject = device if device else f"{ip}/{port}" if role == "CAMERA_DEVICE" else ip
            key = f"{role}:{'device' if device else 'peer'}:{subject}"
            if key not in groups:
                groups[key] = (role, identity, Counter(), set())
            _, kept, combined, ports = groups[key]
            if not device and identity["status"] == "AMBIGUOUS":
                kept["status"] = "AMBIGUOUS"
            combined.update(counts)
            ports.add(port)
            amount = counts["syn"] + counts["publish"] + counts["http"]
            if amount and role != "CAMERA_DEVICE":
                entry = contributors.setdefault(ip, {"activity": 0, "sources": {}})
                entry["activity"] += amount
                entry["sources"][key] = identity

        sources = []
        for ip, entry in sorted(contributors.items(), key=lambda item: -item[1]["activity"])[:8]:
            candidates = list(entry["sources"].values())
            identity = candidates[0] if len(candidates) == 1 else {"status": "AMBIGUOUS", "ip": ip}
            sources.append({"identity": identity, "activity": entry["activity"]})

        def make(key, role, identity, counts, state, mode, active_sources=()):
            values = {name: None for name in FEATURES[role]}
            if quality in ("OBSERVED", "UNVERIFIED"):
                for name, counter in COUNT_FIELDS.items():
                    if name not in values:
                        continue
                    if name in ("publish_rate", "subscribe_rate") and (
                            not counts["decoded"] or counts["tls"] or counts["gap"]):
                        continue
                    if name == "http_rate" and not counts["http"]:
                        continue
                    values[name] = counts[counter] / SPAN
            for name in values.keys() - COUNT_FIELDS.keys():
                value = state.get(name)
                if type(value) in (int, float) and isfinite(value) and value >= 0:
                    values[name] = value
            return {"schema": SCHEMA, "key": key, "service": service, "role": role, "start": start,
                    "end": start + SPAN, "identity": identity, "values": values, "quality": quality,
                    "mode": mode, "service_state": state.get("status", "UNAVAILABLE"),
                    "capture": {"packets_observed": counts["frames"], "truncated": counts["truncated"],
                                "decoder_gaps": counts["gap"], "decoder_errors": decoder.get("decode_errors"),
                                "overflow_or_late": bad, "kernel_drops": decoder.get("packet_drops"),
                                "drop_status": decoder.get("drop_status", "DROP_COUNTER_UNAVAILABLE")},
                    "contributors": list(active_sources), "feature_quality": {
                        name: ("MISSING" if value is None else quality if name in COUNT_FIELDS else "OBSERVED")
                        for name, value in values.items()}}

        for key, (role, identity, counts, ports) in groups.items():
            camera = cameras.get(identity.get("device_id") or identity.get("candidate_device_id"), {})
            state = ({**camera, "status": "DEGRADED" if camera.get("recorder_error") else
                      "HEALTHY" if camera.get("mode") == "recording" else "UNAVAILABLE"}
                     if role == "CAMERA_DEVICE" else {})
            mode = camera.get("mode", "unknown") if role == "CAMERA_DEVICE" else "online" if role == "MQTT_DEVICE" else "unknown"
            yield make(key, role, {**identity, "ports": sorted(ports)[:8]}, counts, state, mode)
        if service == "Video":
            present = {r[1].get("device_id") or r[1].get("candidate_device_id") for r in groups.values()}
            for (ip, port), identities in self.identity.cameras.items():
                if len(identities) != 1:
                    continue
                camera_id = next(iter(identities))
                if camera_id in present or not (camera := cameras.get(camera_id)):
                    continue
                identity = self.identity.resolve("CAMERA_DEVICE", ip, port, start, start + SPAN, cameras)
                verified = identity.get("device_id")
                key = f"CAMERA_DEVICE:device:{verified}" if verified else f"CAMERA_DEVICE:peer:{ip}/{port}"
                state = {**camera, "status": "DEGRADED" if camera.get("recorder_error") else
                         "HEALTHY" if camera.get("mode") == "recording" else "UNAVAILABLE"}
                item = make(key, "CAMERA_DEVICE", identity, Counter(), state, camera["mode"])
                item["quality"] = "UNAVAILABLE"
                item["values"].update(dict.fromkeys(BASE))
                item["feature_quality"].update(dict.fromkeys(BASE, "MISSING"))
                yield item
        role = "MQTT_BROKER" if service == "MQTT" else "VIDEO_SERVER"
        state = broker if service == "MQTT" else video
        if service == "MQTT":
            mode = state.get("mode", "unknown")
        else:
            recording = sum(c["mode"] == "recording" for c in cameras.values())
            mode = f"recording:{recording}" if "cameras" in state else "unknown"
        yield make(f"service:{service}", role, {}, total, state, mode, sources)
