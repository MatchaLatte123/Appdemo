"""Endpoint-scoped live counters with supervision of the capture process."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from ipaddress import IPv4Address
import subprocess
from threading import Event, Lock, Thread, Timer
from time import monotonic
from urllib.parse import urlsplit

from .capture import DUMPCAP, TSHARK, discover, select, stop

ENDPOINT_FIELDS = ("ip.src", "ip.dst", "tcp.srcport", "tcp.dstport", "frame.len", "tcp.flags")
FIELDS = (*ENDPOINT_FIELDS, "frame.time_epoch", "frame.cap_len", "mqtt.msgtype",
          "tcp.analysis.lost_segment", "_ws.malformed.reassembly", "tls.record.content_type",
          "http.request.method", "http.response.code", "tcp.analysis.retransmission")


def utc_now():
    return datetime.now(timezone.utc)


def camera_endpoints(config):
    endpoints = set()
    for camera in config["cameras"]:
        if not camera.get("enabled", True):
            continue
        url = urlsplit(camera["stream_url"])
        if url.scheme not in ("http", "https"):
            raise ValueError("Camera stream must use HTTP or HTTPS")
        try:
            address = str(IPv4Address(url.hostname))
        except ValueError:
            continue  # The monitor's capture scope is IPv4 only.
        endpoints.add((address, url.port or (443 if url.scheme == "https" else 80)))
    return tuple(sorted(endpoints))


def packet_counters():
    return {"frames": 0, "bytes": 0, "syn": 0, "rst": 0, "last_seen": None}


class LiveMonitor:
    def __init__(self, *, host, interface, seconds, video_port, cameras=()):
        if (seconds is not None and not 1 <= seconds <= 120) or not 1 <= video_port <= 65535 or video_port == 1883:
            raise ValueError("Invalid live duration or video port")
        self.host = str(IPv4Address(host))
        self.interface = interface
        self.seconds = seconds
        self.ports = {1883: "MQTT", video_port: "Video"}
        self.cameras = frozenset((str(IPv4Address(ip)), int(port)) for ip, port in cameras)
        self.lock = Lock()
        self.shutdown = Event()
        self.processes = ()
        self.adaptive = None
        self.model_inputs = None
        self.state = {"mode": "MONITOR_ONLY", "status": "STARTING", "interface": interface,
                      "host": self.host, "continuous": seconds is None, "expires_at": None,
                      "packet_drops": None, "restarts": 0, "last_interruption": None,
                      "camera_endpoints": sorted(self.cameras),
                      "services": {name: {"port": port, **packet_counters()} for port, name in self.ports.items()},
                      "video_paths": {name: packet_counters() for name in ("camera_to_server", "server_to_camera", "api")}}

    def snapshot(self):
        with self.lock:
            return {**self.state,
                    **{key: {name: row.copy() for name, row in self.state[key].items()}
                                    for key in ("services", "video_paths")}}

    def update(self, line):
        src, dst, sport, dport, length, flags = line.rstrip("\r\n").split("\t")
        if src == self.host:
            local, remote, camera_path = sport, (dst, dport), "server_to_camera"
        elif dst == self.host:
            local, remote, camera_path = dport, (src, sport), "camera_to_server"
        else:
            return
        if not local.isdecimal() or not remote[1].isdecimal():
            return
        service = self.ports.get(int(local))
        video_path = None
        if service != "MQTT" and (remote[0], int(remote[1])) in self.cameras:
            service, video_path = "Video", camera_path
        elif service == "Video":
            video_path = "api"
        if not service:
            return
        size = int(length)
        bits = int(flags, 16) if flags else 0
        if size < 0:
            raise ValueError("Incorrect wire size")
        with self.lock:
            self.state["status"] = "ACTIVE"
            counters = [self.state["services"][service]]
            if video_path:
                counters.append(self.state["video_paths"][video_path])
            seen = utc_now().isoformat()
            for row in counters:
                row["frames"] += 1
                row["bytes"] += size
                row["syn"] += bool(bits & 2)
                row["rst"] += bool(bits & 4)
                row["last_seen"] = seen
        return service

    def consume(self, line):
        packet = dict(zip(FIELDS, line.rstrip("\r\n").split("\t"), strict=True))
        service = self.update("\t".join(packet[key].split(";", 1)[0] for key in ENDPOINT_FIELDS))
        if service:
            self.observe_packet(packet, service)

    def observe_packet(self, packet, service):
        if self.model_inputs:
            self.model_inputs.observe(packet, service)
        if self.adaptive:
            self.adaptive.observe(packet, service, self.host, self.cameras)

    def capture_filter(self):
        services = [f"(src host {self.host} and src port {port}) or (dst host {self.host} and dst port {port})"
                    for port in self.ports]
        cameras = [f"(src host {ip} and src port {port}) or (dst host {ip} and dst port {port})"
                   for ip, port in sorted(self.cameras)]
        scope = " or ".join(services + cameras)
        return f"ip and host {self.host} and tcp and ({scope})"

    def capture_once(self):
        interface = select(discover(DUMPCAP), self.interface)
        capture_args = [DUMPCAP, "-i", interface, "-p", "-s", "0", "-B", "4", "-Q",
                        "-f", self.capture_filter(), "-w", "-"]
        if self.seconds is not None:
            capture_args += ["-a", f"duration:{self.seconds}", "-c", "100000"]
        decode_args = [TSHARK, "-r", "-", "-n", "-l", "-M", "10000",
                       "-o", "tcp.desegment_tcp_streams:TRUE",
                       "-T", "fields", "-E", "occurrence=a", "-E", "aggregator=;"]
        for field in FIELDS:
            decode_args += ["-e", field]
        with self.lock:
            if self.shutdown.is_set():
                return 0
            capture = subprocess.Popen(capture_args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            try:
                decoder = subprocess.Popen(decode_args, stdin=capture.stdout, stdout=subprocess.PIPE,
                                           stderr=subprocess.DEVNULL, encoding="ascii", errors="replace")
            except OSError:
                stop(capture)
                raise
            finally:
                capture.stdout.close()
            self.processes = (capture, decoder)
            self.state["status"] = "WAITING_FOR_TRAFFIC"
            duration = self.seconds
            self.state["expires_at"] = (utc_now() + timedelta(seconds=duration)).isoformat() if duration else None
        if self.model_inputs:
            self.model_inputs.start_capture()
        deadline = Timer(duration + 5, self.stop_capture, args=(self.processes,)) if duration else None
        if deadline:
            deadline.daemon = True
            deadline.start()
        try:
            for line in decoder.stdout:
                try:
                    self.consume(line)
                except ValueError:
                    if self.model_inputs:
                        self.model_inputs.invalidate(reason="DECODER_ROW_ERROR")
                    with self.lock:
                        self.state["invalid_packets"] = self.state.get("invalid_packets", 0) + 1
            return decoder.wait(timeout=3) or capture.wait(timeout=3)
        finally:
            if self.model_inputs:
                self.model_inputs.end_capture()
            if deadline:
                deadline.cancel()
            try:
                self.stop_capture((capture, decoder))
            finally:
                decoder.stdout.close()
                with self.lock:
                    self.processes = ()

    def stop_capture(self, processes):
        with self.lock:
            for process in processes:
                stop(process)

    def run(self):
        delay = 1
        while not self.shutdown.is_set():
            started = monotonic()
            try:
                code = self.capture_once()
            except (OSError, ValueError, subprocess.SubprocessError):
                code = -1
            if self.shutdown.is_set():
                return
            with self.lock:
                if self.seconds is not None:
                    self.state["status"] = "ENDED" if code == 0 else "CAPTURE_ERROR"
                    return
                self.state["status"] = "RETRYING"
                self.state["last_interruption"] = utc_now().isoformat()
                self.state["restarts"] += 1
            if monotonic() - started >= 30:
                delay = 1
            self.shutdown.wait(delay)
            delay = min(30, delay * 2)

    @contextmanager
    def start(self):
        worker = Thread(target=self.run, daemon=True)
        worker.start()
        try:
            yield self
        finally:
            self.shutdown.set()
            with self.lock:
                processes = self.processes
            self.stop_capture(processes)
            worker.join(timeout=20)
