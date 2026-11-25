"""Independently supervised MQTT and Video capture branches for staged deployment."""
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from io import TextIOWrapper
import re
import subprocess
from threading import Event, Thread, Timer
from time import monotonic
from uuid import uuid4

import psutil

from .capture import DUMPCAP, TSHARK, counters, discover, select, stop
from .live_monitor import ENDPOINT_FIELDS, FIELDS, LiveMonitor, utc_now

BRANCH_FIELDS = {
    "MQTT": tuple(field for field in FIELDS if not field.startswith("http.")),
    "Video": tuple(field for field in FIELDS if field != "mqtt.msgtype"),
}


@dataclass(frozen=True)
class CaptureBudget:
    mqtt_rss_mib: int = 256
    video_rss_mib: int = 256
    max_mqtt_streams: int = 16384
    pipe_buffer_mib: int = 4

    def __post_init__(self):
        for name, low, high in (("mqtt_rss_mib", 32, 2048), ("video_rss_mib", 32, 2048),
                                ("max_mqtt_streams", 1, 100000), ("pipe_buffer_mib", 1, 64)):
            if type(value := getattr(self, name)) is not int or not low <= value <= high:
                raise ValueError(f"Invalid capture resource budget {name}")


def decoder_command(service, executable=TSHARK, source="-"):
    argv = [executable, "-n", "-r", str(source), "-l", "-T", "fields",
            "-E", "occurrence=a", "-E", "aggregator=;"]
    if service == "MQTT":
        argv += ["-o", "tcp.desegment_tcp_streams:TRUE"]
    else:
        argv += ["-M", "10000", "-o", "tcp.desegment_tcp_streams:FALSE",
                 "-o", "http.desegment_body:FALSE"]
    return argv + [arg for field in BRANCH_FIELDS[service] for arg in ("-e", field)]


def drop_status(reported, *, complete=False):
    if reported is not None and reported > 0:
        return "DROPS_OBSERVED"
    if not complete:
        return "DROP_COUNTER_UNAVAILABLE"
    return "VERIFIED_ZERO_DROPS" if reported == 0 else "DROP_COUNTER_UNAVAILABLE"


class CaptureBranch(LiveMonitor):
    def __init__(self, service, budget, **options):
        super().__init__(**options)
        self.service = service
        self.budget = budget
        self.session = {"drop_status": "DROP_COUNTER_UNAVAILABLE"}
        self.previous_session = None
        self.started = None
        self.finished = None

    def capture_filter(self):
        mqtt = f"((src host {self.host} and src port 1883) or (dst host {self.host} and dst port 1883))"
        prefix = f"ip and tcp and host {self.host}"
        if self.service == "MQTT":
            return f"{prefix} and {mqtt}"
        return f"({super().capture_filter()}) and not {mqtt}"

    def consume(self, line):
        packet = dict(zip(BRANCH_FIELDS[self.service], line.rstrip("\r\n").split("\t"), strict=True))
        service = self.update("\t".join(packet[key].split(";", 1)[0] for key in ENDPOINT_FIELDS))
        if service != self.service:
            return
        self.observe_packet(packet, service)
        with self.lock:
            s = self.session
            s["decoder_frames"] += 1
            stamp = packet["frame.time_epoch"]
            s["first_packet_epoch"] = s["first_packet_epoch"] or stamp
            s["last_packet_epoch"] = stamp
            if service == "Video":
                s["http_requests"] += bool(packet["http.request.method"])
                s["http_errors"] += sum(x.isdecimal() and int(x) >= 400
                                        for x in packet["http.response.code"].split(";"))
                s["tcp_retransmissions"] += bool(packet["tcp.analysis.retransmission"])

    def snapshot(self):
        data = super().snapshot()
        with self.lock:
            diagnostics = self.session.copy()
            diagnostics.update(
                uptime_seconds=round((self.finished or monotonic()) - self.started, 2) if self.started else 0,
                python_queue_length=0, kernel_queue_length=None, queue_overflow=None,
                previous_session=self.previous_session,
            )
            data["diagnostics"] = diagnostics
        return data

    def observe(self, done, capture, decoder):
        budget = self.budget.mqtt_rss_mib if self.service == "MQTT" else self.budget.video_rss_mib
        try:
            decoder_proc, capture_proc, own = psutil.Process(decoder.pid), psutil.Process(capture.pid), psutil.Process()
        except psutil.Error:
            return
        previous_count, previous_at = 0, monotonic()
        while not done.wait(.5):
            try:
                rss = decoder_proc.memory_info().rss
                now = monotonic()
                with self.lock:
                    count = self.session["decoder_frames"]
                    self.session.update(
                        tshark_rss_bytes=rss, dumpcap_rss_bytes=capture_proc.memory_info().rss,
                        python_rss_bytes=own.memory_info().rss, tshark_cpu_pct=decoder_proc.cpu_percent(),
                        dumpcap_cpu_pct=capture_proc.cpu_percent(), python_cpu_pct=own.cpu_percent(),
                        decode_frames_per_second=(count - previous_count) / (now - previous_at),
                    )
                    if rss > budget * 1024**2:
                        self.session["health_event"] = "TSHARK_RSS_LIMIT"
                        self.session["drop_status"] = "CAPTURE_INCOMPLETE"
                        self.state["status"] = "CAPTURE_ERROR"
                previous_count, previous_at = count, now
                if rss > budget * 1024**2:
                    self.stop_capture((capture, decoder))
                    return
            except psutil.Error:
                with self.lock:
                    self.session["metrics_error"] = "PROCESS_METRICS_UNAVAILABLE"
                return

    def read_capture_status(self, stream):
        last_count, last_at = 0, monotonic()
        for line in stream:
            values = counters(line)
            progress = re.fullmatch(r"\s*Packets:\s*(\d+)\s*", line, re.I)
            n = int(progress[1]) if progress else values["packets_captured"]
            with self.lock:
                if n is not None:
                    now = monotonic()
                    self.session["captured_packets"] = n
                    if now > last_at:
                        self.session["capture_frames_per_second"] = max(0, n - last_count) / (now - last_at)
                    last_count, last_at = n, now
                dropped = values["packets_dropped"]
                if dropped is not None:
                    self.session["packet_drops"] = dropped
                    if dropped:
                        self.state["status"] = "CAPTURE_ERROR"
                        self.session["drop_status"] = "DROPS_OBSERVED"
            if dropped and self.model_inputs:
                self.model_inputs.invalidate(self.service, reason="KERNEL_DROPS", hold=True)

    def capture_once(self):
        iface = select(discover(DUMPCAP), self.interface)
        bpf = self.capture_filter()
        args = [DUMPCAP, "-i", iface, "-p", "-s", "0", "-B", str(self.budget.pipe_buffer_mib),
                "-f", bpf, "-w", "-"]
        if self.seconds is not None:
            args += ["-a", f"duration:{self.seconds}", "-c", "100000"]
        done = Event()
        with self.lock:
            if self.shutdown.is_set():
                return 0
            capture = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                decoder = subprocess.Popen(decoder_command(self.service), stdin=capture.stdout, stdout=subprocess.PIPE,
                                           stderr=subprocess.DEVNULL, encoding="ascii", errors="replace")
            except OSError:
                capture.stderr.close()
                self.processes = ()
                stop(capture)
                raise
            finally:
                capture.stdout.close()
            self.processes = (capture, decoder)
            self.started = monotonic()
            self.finished = None
            self.session = {"session_id": uuid4().hex, "interface_id": iface, "capture_filter": bpf,
                            "started_at": utc_now().isoformat(), "decoder_frames": 0,
                            "first_packet_epoch": None, "last_packet_epoch": None,
                            "captured_packets": None, "packet_drops": None, "clean_stop": None,
                            "drop_status": "DROP_COUNTER_UNAVAILABLE",
                            "segment_continuity": "SINGLE_PIPE_UNVERIFIED", "decode_errors": 0,
                            "http_requests": 0, "http_errors": 0, "tcp_retransmissions": 0}
            self.state["status"] = "WAITING_FOR_TRAFFIC"
        if self.model_inputs:
            self.model_inputs.start_capture(self.service)
        stderr = TextIOWrapper(capture.stderr, encoding="utf8", errors="replace")
        reporter = Thread(target=self.read_capture_status, args=(stderr,), daemon=True)
        watchdog = Thread(target=self.observe, args=(done, capture, decoder), daemon=True)
        reporter.start()
        watchdog.start()
        deadline = Timer(self.seconds + 5, self.stop_capture, args=((capture, decoder),)) if self.seconds else None
        if deadline:
            deadline.daemon = True
            deadline.start()
        code = -1
        try:
            for line in decoder.stdout:
                try:
                    self.consume(line)
                except ValueError:
                    if self.model_inputs:
                        self.model_inputs.invalidate(self.service, reason="DECODER_ROW_ERROR")
                    with self.lock:
                        self.session["decode_errors"] += 1
            code = decoder.wait(timeout=3) or capture.wait(timeout=3)
            return code
        finally:
            if deadline:
                deadline.cancel()
            done.set()
            self.stop_capture((capture, decoder))
            reporter.join(timeout=3)
            watchdog.join(timeout=3)
            if self.model_inputs:
                if code:
                    self.model_inputs.invalidate(self.service, reason="CAPTURE_INTERRUPTED")
                self.model_inputs.end_capture(self.service)
            decoder.stdout.close()
            stderr.close()
            with self.lock:
                self.processes = ()
                self.session["clean_stop"] = code == 0
                self.finished = monotonic()
                self.session["exit_code"] = code
                self.session["ended_at"] = utc_now().isoformat()
                if code:
                    self.session.setdefault("health_event", "CAPTURE_INTERRUPTED")
                self.session["drop_status"] = (drop_status(self.session["packet_drops"], complete=code == 0)
                    if code == 0 or self.session["packet_drops"] else "CAPTURE_INCOMPLETE")
                self.previous_session = self.session.copy()


class IsolatedMonitor:
    def __init__(self, budget=None, **options):
        budget = budget or CaptureBudget()
        self.branches = {name: CaptureBranch(name, budget, **options) for name in ("MQTT", "Video")}

    def snapshot(self):
        mqtt, video = (self.branches[name].snapshot() for name in ("MQTT", "Video"))
        branches = {"MQTT": mqtt, "Video": video}
        diagnostics = {name: row["diagnostics"] for name, row in branches.items()}
        statuses = {name: row["status"] for name, row in branches.items()}
        for name, diag in diagnostics.items():
            if diag["drop_status"] in ("DROPS_OBSERVED", "CAPTURE_INCOMPLETE") or diag.get("decode_errors"):
                statuses[name] = "CAPTURE_ERROR"
        return {**mqtt, "capture_profile": "ISOLATED", "capture_branches": diagnostics,
                "branch_status": statuses, "restarts": mqtt["restarts"] + video["restarts"],
                "packet_drops": mqtt["diagnostics"].get("packet_drops"),
                "services": {"MQTT": mqtt["services"]["MQTT"], "Video": video["services"]["Video"]},
                "video_paths": video["video_paths"],
                "capture_session_ids": {name: d.get("session_id") for name, d in diagnostics.items()}}

    @contextmanager
    def start(self):
        with ExitStack() as stack:
            for branch in self.branches.values():
                stack.enter_context(branch.start())
            yield self
