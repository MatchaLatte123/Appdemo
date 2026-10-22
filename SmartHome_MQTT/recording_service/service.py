"""HTTP MJPEG continuous recorder for ESP32-CAM.

Each camera has one worker and at most one FFmpeg child. A recording is first
written as ``.partial.mp4`` and is published as footage only after FFmpeg exits
cleanly and ffprobe verifies a complete segment.
"""

from __future__ import annotations

import argparse
import csv
from collections import deque
import io
import json
import logging
import math
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .footage_manager import (
    ExportManager,
    FootageStore,
    StorageManager,
    localize_record,
    parse_local_range,
    parse_timestamp,
    timeline_gaps,
    utc_timestamp,
)


BASE_DIR = Path(__file__).resolve().parent
CAMERA_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")
STORAGE_MANAGEMENT_ENABLED = False


def load_config(path: Path) -> dict[str, Any]:
    config = json.loads(path.read_text(encoding="utf-8"))
    cameras = config.get("cameras")
    if not isinstance(cameras, list) or not cameras:
        raise ValueError("Configuration must contain at least one camera")
    ids: set[str] = set()
    for camera in cameras:
        camera_id = str(camera.get("camera_id", "")).strip()
        url = str(camera.get("stream_url", "")).strip()
        if not camera_id or not url.startswith(("http://", "https://")):
            raise ValueError("Each camera needs camera_id and an HTTP stream_url")
        if camera_id in ids:
            raise ValueError(f"Duplicate camera_id in configuration: {camera_id}")
        ids.add(camera_id)
    return config


def safe_id(camera_id: str) -> str:
    safe = CAMERA_ID_RE.sub("_", camera_id).strip("._")
    if not safe:
        raise ValueError("Camera ID contains no usable filename characters")
    return safe


def configure_logging(log_dir: Path) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("recording_service")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = RotatingFileHandler(
            log_dir / "recording_service.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(message)s"))
        logger.addHandler(handler)
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(console)
    return logger


def ffprobe_duration(ffprobe: str, file_path: Path, timeout: float = 20.0) -> float:
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(file_path)],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return float(result.stdout.strip())


def publish_complete_segment(
    partial_path: Path,
    final_path: Path,
    *,
    return_code: int,
    duration: float | None,
    expected_seconds: float,
    min_complete_ratio: float,
    shutting_down: bool,
) -> bool:
    """Atomically publish verified MP4 footage; leave all other output partial."""
    complete = (
        not shutting_down
        and return_code == 0
        and duration is not None
        and duration >= expected_seconds * min_complete_ratio
        and partial_path.is_file()
        and partial_path.stat().st_size > 0
    )
    if not complete:
        return False
    final_path.parent.mkdir(parents=True, exist_ok=True)
    os.replace(partial_path, final_path)
    return True


class LatestJpegFrame:
    """Bounded fan-out: each live viewer reads the newest source JPEG frame."""

    def __init__(self):
        self.condition = threading.Condition()
        self.sequence = 0
        self.jpeg: bytes | None = None
        self.frame_times: deque[float] = deque()

    def publish(self, jpeg: bytes) -> None:
        with self.condition:
            self.sequence += 1
            self.jpeg = jpeg
            now = time.monotonic()
            self.frame_times.append(now)
            while self.frame_times and now - self.frame_times[0] > 3.0:
                self.frame_times.popleft()
            self.condition.notify_all()

    def fps(self) -> float:
        with self.condition:
            if len(self.frame_times) < 2:
                return 0.0
            elapsed = self.frame_times[-1] - self.frame_times[0]
            return (len(self.frame_times) - 1) / elapsed if elapsed > 0 else 0.0

    def after(self, sequence: int, timeout: float = 0.5) -> tuple[int, bytes | None]:
        with self.condition:
            if self.sequence <= sequence:
                self.condition.wait(timeout)
            return self.sequence, self.jpeg if self.sequence > sequence else None


class CameraRecorder:
    def __init__(self, camera: dict[str, Any], config: dict[str, Any], storage_dir: Path,
                 logger: logging.Logger, store: FootageStore, storage_manager: StorageManager):
        self.camera_id = str(camera["camera_id"])
        self.file_id = safe_id(self.camera_id)
        self.stream_url = str(camera["stream_url"])
        self.live_frames = LatestJpegFrame()
        self.ffmpeg = str(config.get("ffmpeg_path", "ffmpeg"))
        self.ffprobe = str(config.get("ffprobe_path", "ffprobe"))
        self.segment_seconds = max(30, int(config.get("segment_seconds", 300)))
        self.min_complete_ratio = min(1.0, max(0.5, float(config.get("min_complete_ratio", 0.95))))
        self.storage_dir = storage_dir / self.file_id
        self.retention_dir = storage_dir
        self.logger = logger
        self.store = store
        self.storage_manager = storage_manager
        self.retry_initial = max(1.0, float(config.get("retry_initial_seconds", 2)))
        self.retry_max = max(self.retry_initial, float(config.get("retry_max_seconds", 60)))
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.process: subprocess.Popen[bytes] | None = None
        self.thread = threading.Thread(target=self._run, name=f"recorder-{self.file_id}", daemon=True)
        self.state = "stopped"
        self.started_at: str | None = None
        self.last_error: str | None = None
        self.last_error_at: str | None = None
        self.last_segment: str | None = None
        self.last_media_at: str | None = None
        self.retry_in_seconds = 0.0

    def start(self) -> None:
        with self.lock:
            if self.thread.is_alive():
                return
            self.stop_event.clear()
            self.state = "connecting"
            self.thread = threading.Thread(target=self._run, name=f"recorder-{self.file_id}", daemon=True)
            self.thread.start()

    def stop(self, timeout: float = 15.0) -> None:
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout)
        with self.lock:
            if not self.thread.is_alive():
                self.state = "stopped"

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            pid = self.process.pid if self.process and self.process.poll() is None else None
            return {
                "camera_id": self.camera_id,
                "camera_status": "recording" if self.state == "recording" else self.state,
                "recorder_state": self.state,
                "ffmpeg_pid": pid,
                "started_at": self.started_at,
                "last_error": self.last_error,
                "last_error_at": self.last_error_at,
                "last_segment": self.last_segment,
                "last_media_at": self.last_media_at,
                "retry_in_seconds": self.retry_in_seconds,
                "live_source_fps": round(self.live_frames.fps(), 1),
            }

    def _new_paths(self) -> tuple[Path, Path, str, datetime]:
        started_at = datetime.now(timezone.utc)
        recording_id = str(uuid.uuid4())
        stamp = started_at.strftime("%Y-%m-%dT%H-%M-%S_%fZ")
        stem = f"{self.file_id}_{stamp}_{recording_id[:8]}"
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        return (self.storage_dir / f"{stem}_%06d.partial.mp4",
                self.storage_dir / f"{stem}.csv", stem, started_at)

    def _ffmpeg_command(self, segment_pattern: Path, segment_list: Path) -> list[str]:
        return [
            self.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "warning",
            "-rw_timeout", "15000000", "-reconnect", "1", "-reconnect_streamed", "1",
            "-reconnect_at_eof", "1", "-reconnect_on_network_error", "1",
            "-reconnect_max_retries", "5", "-reconnect_delay_max", "5",
            "-use_wallclock_as_timestamps", "1", "-f", "mpjpeg", "-i", self.stream_url,
            "-map", "0:v:0", "-an", "-c:v", "libx264", "-preset", "veryfast",
            "-tune", "zerolatency", "-pix_fmt", "yuv420p", "-r", "20",
            "-g", "40", "-sc_threshold", "0",
            "-force_key_frames", f"expr:gte(t,n_forced*{self.segment_seconds})",
            "-f", "segment", "-segment_time", str(self.segment_seconds),
            "-segment_time_delta", "0.025", "-reset_timestamps", "1",
            "-segment_format", "mp4", "-segment_format_options", "movflags=+faststart",
            "-segment_list", str(segment_list), "-segment_list_type", "csv",
            "-segment_list_flags", "+live", str(segment_pattern),
            # Copy the already-compressed camera JPEGs to stdout for live
            # live clients. The server shares the source without live transcoding.
            "-map", "0:v:0", "-c:v", "copy", "-f", "image2pipe", "pipe:1",
        ]

    def _publish_closed_segments(self, segment_list: Path, stem: str,
                                 processed: set[str], previous_end: datetime | None
                                 ) -> datetime | None:
        try:
            listing = segment_list.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return previous_end
        for row in csv.reader(io.StringIO(listing)):
            if len(row) < 3:
                continue
            listed_name = row[0]
            if listed_name in processed:
                continue
            candidate = Path(listed_name)
            if not candidate.is_absolute():
                candidate = self.storage_dir / candidate
            try:
                candidate = candidate.resolve()
                candidate.relative_to(self.storage_dir.resolve())
            except (OSError, ValueError):
                self.logger.error("Ignoring unsafe segment path from FFmpeg list: %s", listed_name)
                processed.add(listed_name)
                continue
            if not candidate.name.startswith(stem + "_") or not candidate.name.endswith(".partial.mp4"):
                self.logger.error("Ignoring unexpected segment path from FFmpeg list: %s", listed_name)
                processed.add(listed_name)
                continue
            if not candidate.is_file():
                continue
            try:
                duration = ffprobe_duration(self.ffprobe, candidate)
                if duration < self.segment_seconds * self.min_complete_ratio:
                    raise ValueError(f"segment too short ({duration:.2f}s)")
                # Use the previous segment's end as this segment's start. The
                # same FFmpeg input stays open across cuts, so adjacent files
                # form one continuous timeline despite muxer close latency.
                end_time = datetime.now(timezone.utc)
                start_time = previous_end if previous_end is not None else end_time - timedelta(seconds=duration)
                end_time = start_time + timedelta(seconds=duration)
                final_path = candidate.with_name(candidate.name.removesuffix(".partial.mp4") + ".mp4")
                os.replace(candidate, final_path)
                recording_id = str(uuid.uuid4())
                self.store.add_segment(
                    recording_id=recording_id, device_id=self.camera_id,
                    start_time=start_time, end_time=end_time, duration=duration,
                    file_path=final_path, file_size=final_path.stat().st_size,
                )
                processed.add(listed_name)
                previous_end = end_time
                with self.lock:
                    self.last_segment = str(final_path)
                    self.last_error = None
                    self.last_error_at = None
                self.logger.info("Published continuous segment camera=%s duration=%.2fs path=%s",
                                 self.camera_id, duration, final_path)
                self.storage_manager.cleanup()
            except Exception as exc:
                self._record_error(f"Could not publish segment {candidate.name}: {exc}")
                self.logger.exception("Could not publish closed segment camera=%s path=%s",
                                      self.camera_id, candidate)
                processed.add(listed_name)
        return previous_end

    def _read_live_frames(self, process: subprocess.Popen[bytes]) -> None:
        if process.stdout is None:
            return
        buffer = bytearray()
        while process.poll() is None or process.stdout.peek(1):
            chunk = process.stdout.read1(65536)
            if not chunk:
                break
            buffer.extend(chunk)
            if len(buffer) > 8 * 1024 * 1024:
                # A malformed/truncated JPEG must not grow memory indefinitely.
                start = buffer.rfind(b"\xff\xd8")
                buffer = bytearray(buffer[start:] if start >= 0 else b"")
            while True:
                start = buffer.find(b"\xff\xd8")
                if start < 0:
                    if len(buffer) > 1:
                        del buffer[:-1]
                    break
                end = buffer.find(b"\xff\xd9", start + 2)
                if end < 0:
                    if start:
                        del buffer[:start]
                    break
                self.live_frames.publish(bytes(buffer[start:end + 2]))
                del buffer[:end + 2]

    def _run(self) -> None:
        delay = self.retry_initial
        while not self.stop_event.is_set():
            segment_pattern: Path | None = None
            segment_list: Path | None = None
            stem: str | None = None
            process: subprocess.Popen[bytes] | None = None
            stderr_thread: threading.Thread | None = None
            live_thread: threading.Thread | None = None
            processed_segments: set[str] = set()
            previous_end: datetime | None = None
            try:
                self.storage_manager.cleanup()
                segment_pattern, segment_list, stem, started_at = self._new_paths()
                self.logger.info("Starting FFmpeg camera=%s url=%s output=%s", self.camera_id,
                                 self.stream_url, segment_pattern.name)
                kwargs: dict[str, Any] = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE}
                if os.name == "nt":
                    kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
                else:
                    kwargs["start_new_session"] = True
                process = subprocess.Popen(self._ffmpeg_command(segment_pattern, segment_list), **kwargs)
                with self.lock:
                    self.process = process
                    self.state = "connecting"
                    self.started_at = started_at.isoformat()
                    self.retry_in_seconds = 0
                stderr_thread = threading.Thread(target=self._log_stderr, args=(process,), daemon=True)
                stderr_thread.start()
                live_thread = threading.Thread(target=self._read_live_frames, args=(process,), daemon=True)
                live_thread.start()
                last_size = 0
                last_growth = time.monotonic()
                last_active_path: Path | None = None
                while process.poll() is None and not self.stop_event.wait(0.5):
                    previous_end = self._publish_closed_segments(
                        segment_list, stem, processed_segments, previous_end
                    )
                    active_paths = sorted(self.storage_dir.glob(f"{stem}_*.partial.mp4"))
                    active_path = active_paths[-1] if active_paths else None
                    if active_path != last_active_path:
                        last_active_path = active_path
                        last_size = 0
                        last_growth = time.monotonic()
                    try:
                        current_size = active_path.stat().st_size if active_path else 0
                    except OSError:
                        current_size = 0
                    now = time.monotonic()
                    if current_size > last_size:
                        with self.lock:
                            self.state = "recording"
                            self.last_media_at = datetime.now(timezone.utc).isoformat()
                        last_size = current_size
                        last_growth = now
                    elif now - last_growth > 15:
                        with self.lock:
                            self.state = "connecting"
                return_code = process.poll()
                if return_code is None:
                    self._stop_child(process)
                    return_code = process.poll()
                previous_end = self._publish_closed_segments(
                    segment_list, stem, processed_segments, previous_end
                )
                stderr_thread.join(timeout=1)
                live_thread.join(timeout=1)
                if not self.stop_event.is_set():
                    self._record_error(f"Continuous FFmpeg recorder exited (exit={return_code})")
            except FileNotFoundError as exc:
                self._record_error(f"FFmpeg executable not found: {self.ffmpeg}")
                self.logger.exception("Recorder executable missing camera=%s", self.camera_id)
            except Exception as exc:
                self._record_error(str(exc))
                self.logger.exception("Recorder error camera=%s", self.camera_id)
            finally:
                if process is not None and process.poll() is None:
                    try:
                        self._stop_child(process)
                    except Exception:
                        self.logger.exception("Could not stop FFmpeg after recorder error camera=%s", self.camera_id)
                if process is not None:
                    if stderr_thread and stderr_thread.is_alive():
                        stderr_thread.join(timeout=1)
                    if live_thread and live_thread.is_alive():
                        live_thread.join(timeout=1)
                with self.lock:
                    self.process = None

            if self.stop_event.is_set():
                break
            with self.lock:
                self.state = "retrying"
                self.retry_in_seconds = delay
            self.logger.info("Retry camera=%s in %.1fs", self.camera_id, delay)
            if self.stop_event.wait(delay):
                break
            delay = min(self.retry_max, delay * 2)

        with self.lock:
            self.process = None
            self.retry_in_seconds = 0
            self.state = "stopped"
        self.logger.info("Recorder stopped camera=%s", self.camera_id)

    def _log_stderr(self, process: subprocess.Popen[bytes]) -> None:
        if process.stderr is None:
            return
        try:
            for raw_line in iter(process.stderr.readline, b""):
                line = raw_line.decode(errors="replace").strip()
                if line:
                    self.logger.warning("FFmpeg camera=%s: %s", self.camera_id, line)
                    if "error" in line.lower() or "failed" in line.lower():
                        self._record_error(line[-1000:])
        except Exception:
            self.logger.exception("Could not read FFmpeg stderr camera=%s", self.camera_id)

    def _record_error(self, message: str) -> None:
        with self.lock:
            self.last_error = message[-1000:]
            self.last_error_at = datetime.now(timezone.utc).isoformat()

    def _stop_child(self, process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        self.logger.info("Stopping FFmpeg camera=%s pid=%s", self.camera_id, process.pid)
        try:
            process.terminate()
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.logger.error("Killing hung FFmpeg camera=%s pid=%s", self.camera_id, process.pid)
            process.kill()
            process.wait(timeout=5)

class RecordingService:
    def __init__(self, config: dict[str, Any], service_dir: Path = BASE_DIR,
                 config_path: Path | None = None):
        self.config = config
        self.config_path = (config_path or service_dir / "config.json").resolve()
        self.storage_settings_lock = threading.Lock()
        configured_storage = Path(config.get("storage_dir", "footage"))
        self.storage_dir = configured_storage if configured_storage.is_absolute() else service_dir / configured_storage
        self.logger = configure_logging(service_dir / "logs")
        database_setting = Path(config.get("metadata_db", "footage.sqlite3"))
        database_path = database_setting if database_setting.is_absolute() else service_dir / database_setting
        self.store = FootageStore(database_path, self.storage_dir, self.logger)
        gib = 1024 ** 3
        self.storage_manager = StorageManager(
            self.store,
            max_bytes=int(float(config.get("max_storage_gb", 5.0)) * gib),
            cleanup_target_bytes=int(float(config.get("cleanup_target_gb", 4.5)) * gib),
            logger=self.logger,
            enabled=STORAGE_MANAGEMENT_ENABLED,
        )
        self.export_manager = ExportManager(
            self.store, self.storage_dir,
            ffmpeg=str(config.get("ffmpeg_path", "ffmpeg")),
            ffprobe=str(config.get("ffprobe_path", "ffprobe")),
            logger=self.logger,
        )
        self.store.reconcile()
        self.storage_manager.cleanup()
        self.recorders: dict[str, CameraRecorder] = {}
        for camera in config["cameras"]:
            if not camera.get("enabled", True):
                continue
            recorder = CameraRecorder(camera, config, self.storage_dir, self.logger,
                                      self.store, self.storage_manager)
            if recorder.camera_id in self.recorders:
                raise ValueError(f"Duplicate recorder for {recorder.camera_id}")
            self.recorders[recorder.camera_id] = recorder
        self.server: ThreadingHTTPServer | None = None
        self.stopping = threading.Event()
        self.stop_lock = threading.Lock()
        self.stop_complete = threading.Event()
        self.live_sessions: dict[str, dict[str, Any]] = {}
        self.live_lock = threading.Lock()

    def update_storage_limit(self, max_storage_gb: float) -> dict[str, Any]:
        if not self.storage_manager.enabled:
            raise RuntimeError("Storage management is temporarily disabled")
        if not math.isfinite(max_storage_gb) or not 0.1 <= max_storage_gb <= 1024:
            raise ValueError("max_storage_gb must be between 0.1 and 1024 GiB")
        gib = 1024 ** 3
        max_bytes = int(max_storage_gb * gib)
        old_max = self.storage_manager.max_bytes
        old_target = self.storage_manager.cleanup_target_bytes
        target_ratio = min(0.99, max(0.0, old_target / old_max))
        cleanup_target_bytes = int(max_bytes * target_ratio)
        with self.storage_settings_lock:
            updated = dict(self.config)
            updated["max_storage_gb"] = float(max_storage_gb)
            updated["cleanup_target_gb"] = cleanup_target_bytes / gib
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path = self.config_path.with_name(self.config_path.name + ".tmp")
            try:
                temporary_path.write_text(json.dumps(updated, indent=2) + "\n", encoding="utf-8")
                os.replace(temporary_path, self.config_path)
            finally:
                temporary_path.unlink(missing_ok=True)
            self.storage_manager.configure_limit(max_bytes, cleanup_target_bytes)
            self.config.clear()
            self.config.update(updated)
            result = self.storage_manager.cleanup()
        status = self.storage_manager.status()
        status["max_storage_gb"] = float(max_storage_gb)
        status["cleanup_target_gb"] = cleanup_target_bytes / gib
        status["deleted_count"] = len(result.get("deleted_recording_ids", []))
        return status

    def start(self) -> None:
        self.server = ThreadingHTTPServer(
            (str(self.config.get("http_host", "127.0.0.1")), int(self.config.get("http_port", 8765))),
            self._handler_type(),
        )
        self.server.daemon_threads = True
        for recorder in self.recorders.values():
            recorder.start()
        self.logger.info("Recording service listening at http://%s:%s",
                         *self.server.server_address[:2])
        try:
            self.server.serve_forever(poll_interval=0.5)
        finally:
            self.stop()

    def stop(self) -> None:
        with self.stop_lock:
            is_owner = not self.stopping.is_set()
            if is_owner:
                self.stopping.set()
        if not is_owner:
            self.stop_complete.wait(timeout=30)
            return
        try:
            self.logger.info("Stopping recording service")
            if self.server:
                self.server.shutdown()
                self.server.server_close()
            for recorder in self.recorders.values():
                recorder.stop()
        finally:
            self.stop_complete.set()

    def status(self) -> dict[str, Any]:
        return {
            "service": "running" if not self.stopping.is_set() else "stopping",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "ffmpeg_available": shutil.which(str(self.config.get("ffmpeg_path", "ffmpeg"))) is not None,
            "cameras": [r.snapshot() for r in self.recorders.values()],
            "storage": self.storage_manager.status(),
        }

    def footage(self, camera_id: str, start: datetime, end: datetime) -> dict[str, Any]:
        recorder = self.recorders.get(camera_id)
        if not recorder:
            raise KeyError(camera_id)
        records = self.store.query(camera_id, start, end)
        for record in records:
            record["file_exists"] = self.store.resolve_file(record["file_path"]).is_file()
        return {
            "camera_id": camera_id,
            "device_id": camera_id,
            "interval": {"start_time_utc": utc_timestamp(start), "end_time_utc": utc_timestamp(end),
                         "start_time_local": start.astimezone().isoformat(),
                         "end_time_local": end.astimezone().isoformat(),
                         "boundary_rule": "half-open [start, end)"},
            "segments": [self._public_record(record) for record in records],
            "gaps": timeline_gaps(records, start, end),
        }

    def _recording_file(self, recording_id: str) -> tuple[dict[str, Any], Path]:
        record = self.store.get(recording_id)
        if not record or record.get("status") != "complete":
            raise KeyError(recording_id)
        path = self.store.resolve_file(record["file_path"])
        if not path.is_file() or path.suffix.lower() != ".mp4":
            raise KeyError(recording_id)
        return self._public_record(record), path

    @staticmethod
    def _public_record(record: dict[str, Any]) -> dict[str, Any]:
        public = localize_record(record)
        public.pop("file_path", None)
        return public

    def create_live_session(self, camera_id: str) -> dict[str, Any]:
        if camera_id not in self.recorders:
            raise KeyError(camera_id)
        session_id = str(uuid.uuid4())
        expires_at = datetime.now(timezone.utc) + timedelta(hours=8)
        with self.live_lock:
            self.live_sessions[session_id] = {"camera_id": camera_id, "expires_at": expires_at}
        return {"session_id": session_id, "camera_id": camera_id,
                "stream_url": f"/api/live-sessions/{session_id}/stream.mjpg",
                "expires_at": expires_at.isoformat(), "transport": "mjpeg",
                "note": "Original JPEG frames are relayed from the recorder's shared camera input."}

    def live_file(self, session_id: str, filename: str) -> Path | None:
        session = self.live_session(session_id)
        if not session or not re.fullmatch(r"index\.m3u8|[A-Za-z0-9_.-]+_live_\d{9,}\.ts", filename):
            return None
        recorder = self.recorders.get(session["camera_id"])
        if not recorder:
            return None
        root = (self.storage_dir.parent / "_live" / recorder.file_id).resolve()
        target = (root / filename).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            return None
        return target

    def live_session(self, session_id: str) -> dict[str, Any] | None:
        with self.live_lock:
            session = self.live_sessions.get(session_id)
            if session and session["expires_at"] <= datetime.now(timezone.utc):
                self.live_sessions.pop(session_id, None)
                return None
            return session

    def _handler_type(self) -> type[BaseHTTPRequestHandler]:
        service = self

        class Handler(BaseHTTPRequestHandler):
            def _send_json(self, body: dict[str, Any], status: int = 200) -> None:
                data = json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def _send_file(self, path: Path, content_type: str, *, download_name: str | None = None) -> None:
                size = path.stat().st_size
                range_header = self.headers.get("Range")
                start, end, status = 0, size - 1, 200
                if range_header:
                    match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
                    if not match or (not match.group(1) and not match.group(2)):
                        self.send_error(416, "Invalid byte range")
                        return
                    if match.group(1):
                        start = int(match.group(1))
                        end = int(match.group(2)) if match.group(2) else size - 1
                    else:
                        suffix_length = int(match.group(2))
                        start = max(0, size - suffix_length)
                    if size == 0 or start >= size or end < start:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{size}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    end = min(end, size - 1)
                    status = 206
                length = end - start + 1 if size else 0
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(length))
                self.send_header("Cache-Control", "no-store")
                if status == 206:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                if download_name:
                    self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
                self.end_headers()
                with path.open("rb") as file_obj:
                    file_obj.seek(start)
                    remaining = length
                    while remaining:
                        chunk = file_obj.read(min(64 * 1024, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)

            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                try:
                    if parsed.path in ("/", "/health", "/api/status"):
                        body = service.status()
                    elif parsed.path == "/api/footage":
                        query = parse_qs(parsed.query)
                        camera_id = query.get("device_id", query.get("camera_id", [""]))[0]
                        start_value = query.get("start_time", [None])[0]
                        end_value = query.get("end_time", [None])[0]
                        if start_value and end_value and "T" in start_value and "T" in end_value:
                            start = parse_timestamp(start_value)
                            end = parse_timestamp(end_value)
                            if end <= start:
                                raise ValueError("The query interval must satisfy start < end")
                            body = service.footage(camera_id, start, end)
                            self._send_json(body)
                            return
                        day = query.get("date", [None])[0]
                        if not day:
                            raise ValueError("date=YYYY-MM-DD is required")
                        offset = query.get("utc_offset_minutes", [None])[0]
                        start, end = parse_local_range(
                            day,
                            query.get("start_time", [None])[0],
                            query.get("end_time", [None])[0],
                            int(offset) if offset is not None else None,
                        )
                        body = service.footage(camera_id, start, end)
                    elif parsed.path == "/api/playback/manifest":
                        query = parse_qs(parsed.query)
                        device_id = query.get("device_id", [""])[0]
                        start = parse_timestamp(query.get("start_time", [""])[0])
                        end = parse_timestamp(query.get("end_time", [""])[0])
                        body = service.footage(device_id, start, end)
                        body["segments"] = [
                            {**segment, "playback_url": f"/api/recordings/{segment['recording_id']}/file"}
                            for segment in body["segments"]
                        ]
                    elif parsed.path.startswith("/api/recordings/"):
                        match = re.fullmatch(r"/api/recordings/([0-9a-fA-F-]{36})(?:/(file))?", parsed.path)
                        if not match:
                            self.send_error(404, "Recording not found")
                            return
                        recording_id = str(uuid.UUID(match.group(1)))
                        record, file_path = service._recording_file(recording_id)
                        if match.group(2):
                            self._send_file(file_path, "video/mp4", download_name=f"{recording_id}.mp4")
                            return
                        body = record
                    elif parsed.path.startswith("/api/live-sessions/"):
                        stream_match = re.fullmatch(
                            r"/api/live-sessions/([0-9a-fA-F-]{36})/stream\.mjpg", parsed.path
                        )
                        if stream_match:
                            session_id = str(uuid.UUID(stream_match.group(1)))
                            session = service.live_session(session_id)
                            if not session:
                                self.send_error(404, "Live session not found")
                                return
                            recorder = service.recorders.get(session["camera_id"])
                            if not recorder:
                                self.send_error(404, "Camera not found")
                                return
                            boundary = "camera-frame"
                            self.send_response(200)
                            self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={boundary}")
                            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
                            self.send_header("Connection", "close")
                            self.send_header("Access-Control-Allow-Origin", "*")
                            self.end_headers()
                            sequence = 0
                            try:
                                while service.live_session(session_id) and not service.stopping.is_set():
                                    sequence, jpeg = recorder.live_frames.after(sequence)
                                    if jpeg is None:
                                        continue
                                    self.wfile.write(
                                        f"--{boundary}\r\nContent-Type: image/jpeg\r\n"
                                        f"Content-Length: {len(jpeg)}\r\n\r\n".encode("ascii")
                                    )
                                    self.wfile.write(jpeg)
                                    self.wfile.write(b"\r\n")
                                    self.wfile.flush()
                            except (BrokenPipeError, ConnectionResetError, OSError):
                                pass
                            return
                        match = re.fullmatch(r"/api/live-sessions/([0-9a-fA-F-]{36})/(index\.m3u8|[A-Za-z0-9_.-]+_live_\d{9,}\.ts)", parsed.path)
                        if not match:
                            self.send_error(404, "Live session not found")
                            return
                        session_id = str(uuid.UUID(match.group(1)))
                        live_path = service.live_file(session_id, match.group(2))
                        if not live_path:
                            self.send_error(404, "Live session or segment not found")
                            return
                        content_type = "application/vnd.apple.mpegurl" if live_path.suffix == ".m3u8" else "video/mp2t"
                        self._send_file(live_path, content_type)
                        return
                    elif parsed.path.startswith("/api/exports/"):
                        export_id = unquote(parsed.path.rsplit("/", 1)[-1])
                        export_id = str(uuid.UUID(export_id))
                        export_root = (service.storage_dir / "exports").resolve()
                        export_path = (export_root / f"{export_id}.mp4").resolve()
                        if not export_path.is_relative_to(export_root) or not export_path.is_file():
                            self.send_error(404, "Export not found")
                            return
                        self._send_file(export_path, "video/mp4", download_name=f"{export_id}.mp4")
                        return
                    else:
                        self.send_error(404, "Not found")
                        return
                    self._send_json(body)
                except KeyError:
                    self.send_error(404, "Unknown camera")
                except ValueError as exc:
                    self.send_error(400, str(exc))
                except Exception:
                    service.logger.exception("HTTP API request failed path=%s", self.path)
                    self.send_error(500, "Internal server error")

            def do_POST(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > 1024 * 1024:
                        raise ValueError("Request body must be between 1 byte and 1 MiB")
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    if not isinstance(payload, dict):
                        raise ValueError("Request JSON must be an object")
                    if parsed.path == "/api/storage/settings":
                        if not service.storage_manager.enabled:
                            self.send_error(503, "Storage management is temporarily disabled")
                            return
                        value = payload.get("max_storage_gb")
                        if isinstance(value, bool) or not isinstance(value, (int, float)):
                            raise ValueError("max_storage_gb must be a number")
                        self._send_json(service.update_storage_limit(float(value)))
                        return
                    if parsed.path == "/api/export":
                        device_id = str(payload.get("device_id", ""))
                        start = parse_timestamp(str(payload.get("start_time", "")))
                        end = parse_timestamp(str(payload.get("end_time", "")))
                        if end <= start:
                            raise ValueError("The export interval must satisfy start < end")
                        result = service.export_manager.export(device_id, start, end)
                        self._send_json(result, 201)
                        return
                    if parsed.path == "/api/live-sessions":
                        camera_id = str(payload.get("device_id", payload.get("camera_id", ""))).strip()
                        self._send_json(service.create_live_session(camera_id), 201)
                        return
                    match = re.fullmatch(r"/api/footage/([0-9a-fA-F-]{36})/protection", parsed.path)
                    if match:
                        recording_id = str(uuid.UUID(match.group(1)))
                        protected = payload.get("is_protected")
                        if not isinstance(protected, bool):
                            raise ValueError("is_protected must be a JSON boolean")
                        if not service.store.set_protected(recording_id, protected):
                            self.send_error(404, "Complete footage segment not found")
                            return
                        self._send_json({"recording_id": recording_id, "is_protected": protected})
                        return
                    self.send_error(404, "Not found")
                except KeyError:
                    self.send_error(404, "Camera or footage not found")
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    self.send_error(400, str(exc))
                except Exception:
                    service.logger.exception("HTTP API request failed path=%s", self.path)
                    self.send_error(500, "Internal server error")

            def do_DELETE(self) -> None:  # noqa: N802
                path = urlparse(self.path).path
                match = re.fullmatch(r"/api/live-sessions/([0-9a-fA-F-]{36})", path)
                if match:
                    session_id = str(uuid.UUID(match.group(1)))
                    with service.live_lock:
                        removed = service.live_sessions.pop(session_id, None)
                    if not removed:
                        self.send_error(404, "Live session not found")
                        return
                    self._send_json({"session_id": session_id, "closed": True,
                                     "recording_running": True})
                    return
                match = re.fullmatch(r"/api/exports/([0-9a-fA-F-]{36})", path)
                if match:
                    export_id = str(uuid.UUID(match.group(1)))
                    export_root = (service.storage_dir / "exports").resolve()
                    export_path = (export_root / f"{export_id}.mp4").resolve()
                    if not export_path.is_relative_to(export_root) or not export_path.is_file():
                        self.send_error(404, "Export not found")
                        return
                    export_path.unlink()
                    self._send_json({"export_id": export_id, "deleted": True})
                    return
                self.send_error(404, "Not found")

            def log_message(self, fmt: str, *args: Any) -> None:
                service.logger.info("HTTP %s - %s", self.address_string(), fmt % args)

        return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="SmartHome_MQTT camera recording service")
    parser.add_argument("--config", type=Path, default=BASE_DIR / "config.json")
    args = parser.parse_args()
    config = load_config(args.config)
    service = RecordingService(config, args.config.resolve().parent, args.config.resolve())

    def request_stop(_signum: int, _frame: Any) -> None:
        # shutdown() must run off the serve_forever thread.
        threading.Thread(target=service.stop, name="service-shutdown", daemon=True).start()

    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_stop)
    try:
        service.start()
    except KeyboardInterrupt:
        service.stop()
    except OSError as exc:
        service.logger.exception("Could not start HTTP status API: %s", exc)
        service.stop()
        raise


if __name__ == "__main__":
    main()
