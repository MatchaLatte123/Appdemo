from __future__ import annotations

import csv
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .storage import StorageManager, recently_written_paths


def safe_device_id(device_id: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", str(device_id)).strip("._-")
    if not value:
        raise ValueError("device_id must contain at least one safe filename character")
    return value


def validate_stream_url(stream_url: str) -> str:
    value = str(stream_url or "").strip()
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("stream_url must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password:
        raise ValueError("stream_url must not contain embedded credentials")
    return value


def _encoder_options(encoder: str) -> list[str]:
    if encoder == "h264_nvenc":
        return ["-preset", "p4", "-tune", "ll", "-rc", "vbr", "-cq", "25"]
    if encoder == "h264_qsv":
        return ["-preset", "veryfast", "-global_quality", "25"]
    if encoder == "h264_amf":
        return ["-quality", "speed", "-qp_i", "25", "-qp_p", "27"]
    return ["-preset", "veryfast", "-tune", "zerolatency", "-crf", "25", "-threads", "2"]


def build_ffmpeg_command(
    stream_url: str,
    output_pattern: str | Path,
    segment_seconds: int = 300,
    video_fps: int = 30,
    encoder: str = "libx264",
    ffmpeg_binary: str = "ffmpeg",
    segment_list: str | Path | None = None,
) -> list[str]:
    """Build an argv list; output pattern uses FFmpeg strftime syntax."""
    stream_url = validate_stream_url(stream_url)
    if segment_seconds <= 0 or video_fps <= 0:
        raise ValueError("segment_seconds and video_fps must be greater than zero")
    if encoder not in {"libx264", "h264_nvenc", "h264_qsv", "h264_amf"}:
        raise ValueError(f"Unsupported H.264 encoder: {encoder}")

    args = [
        ffmpeg_binary,
        "-hide_banner",
        "-loglevel", "warning",
        "-f", "mpjpeg",
        "-use_wallclock_as_timestamps", "1",
        "-copyts",
        "-rw_timeout", "10000000",
        "-i", stream_url,
        "-an",
        "-c:v", encoder,
        *_encoder_options(encoder),
        "-pix_fmt", "yuv420p",
        "-r", str(video_fps),
        "-g", str(video_fps),
        "-keyint_min", str(video_fps),
        "-sc_threshold", "0",
        "-f", "segment",
        "-segment_time", str(segment_seconds),
        "-segment_atclocktime", "1",
        "-segment_time_delta", "0.05",
        "-segment_format", "mp4",
        "-reset_timestamps", "1",
        "-strftime", "1",
    ]
    if segment_list is not None:
        args.extend(["-segment_list", str(segment_list), "-segment_list_type", "csv"])
    args.append(str(output_pattern))
    return args


def list_ffmpeg_encoders(ffmpeg_binary: str) -> set[str]:
    result = subprocess.run(
        [ffmpeg_binary, "-hide_banner", "-encoders"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "FFmpeg encoder query failed")
    return {
        name
        for name in ("h264_nvenc", "h264_qsv", "h264_amf", "libx264")
        if re.search(rf"\b{re.escape(name)}\b", result.stdout)
    }


def _probe_encoder(ffmpeg_binary: str, encoder: str, timeout: float = 10.0) -> bool:
    command = [
        ffmpeg_binary,
        "-hide_banner",
        "-loglevel", "error",
        "-f", "lavfi",
        "-i", "color=c=black:s=640x480:r=30",
        "-frames:v", "1",
        "-an",
        "-c:v", encoder,
        *_encoder_options(encoder),
        "-f", "null",
        "-",
    ]
    try:
        result = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def select_video_encoder(ffmpeg_binary: str, requested: str = "auto") -> str:
    available = list_ffmpeg_encoders(ffmpeg_binary)
    if requested != "auto":
        if requested not in available:
            raise RuntimeError(f"Requested FFmpeg encoder is unavailable: {requested}")
        if requested != "libx264" and not _probe_encoder(ffmpeg_binary, requested):
            logging.getLogger("RECORDER").warning(
                "Hardware encoder %s is listed but failed its startup probe; falling back to libx264",
                requested,
            )
            if "libx264" not in available:
                raise RuntimeError("No usable requested encoder and libx264 fallback is unavailable")
            return "libx264"
        return requested

    for encoder in ("h264_nvenc", "h264_qsv", "h264_amf"):
        if encoder in available and _probe_encoder(ffmpeg_binary, encoder):
            return encoder
    if "libx264" in available:
        return "libx264"
    raise RuntimeError("FFmpeg has no usable H.264 hardware encoder or libx264")


def cleanup_process(process: subprocess.Popen[bytes], timeout_seconds: float = 12.0) -> None:
    """Ask FFmpeg to finalize its active MP4, then force-stop and reap if needed."""
    try:
        if process.poll() is not None:
            process.wait(timeout=0)
        else:
            try:
                if process.stdin:
                    process.stdin.write(b"q\n")
                    process.stdin.flush()
                process.wait(timeout=timeout_seconds)
                return
            except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
                pass

        try:
            process.terminate()
            process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
    finally:
        if process.stdin and not process.stdin.closed:
            process.stdin.close()


def _segment_start_from_path(path: Path) -> datetime | None:
    try:
        return datetime.strptime(
            f"{path.parent.name} {path.stem}", "%Y-%m-%d %H-%M-%S"
        ).astimezone()
    except ValueError:
        return None


@dataclass
class RecorderSettings:
    device_id: str
    stream_url: str
    storage_directory: Path
    max_storage_size: int
    segment_duration: int = 300
    video_fps: int = 30
    ffmpeg_binary: str = "ffmpeg"
    encoder: str = "auto"
    retry_initial_seconds: float = 5.0
    retry_max_seconds: float = 30.0
    stop_timeout_seconds: float = 12.0


@dataclass
class _RecorderHandle:
    settings: RecorderSettings
    stop_event: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    state: str = "starting"
    pid: int | None = None
    active_encoder: str | None = None
    last_error: str | None = None
    started_at: float | None = None


class RecorderManager:
    def __init__(self, database_path: Path | None = None):
        self.log = logging.getLogger("RECORDER")
        self._lock = threading.RLock()
        self._recorders: dict[str, _RecorderHandle] = {}
        self._database_path = database_path
        self._storage: dict[Path, StorageManager] = {}

    def start_recorder(
        self,
        device_id: str,
        stream_url: str,
        storage_directory: str | Path,
        max_storage_size: int,
        segment_duration: int = 300,
        **options: Any,
    ) -> bool:
        if not device_id or max_storage_size <= 0 or segment_duration <= 0:
            raise ValueError("device_id, max_storage_size, and segment_duration must be valid")
        stream_url = validate_stream_url(stream_url)
        root = Path(storage_directory).resolve()
        root.mkdir(parents=True, exist_ok=True)
        settings = RecorderSettings(
            device_id=str(device_id),
            stream_url=stream_url,
            storage_directory=root,
            max_storage_size=int(max_storage_size),
            segment_duration=int(segment_duration),
            video_fps=int(options.get("video_fps", 30)),
            ffmpeg_binary=str(options.get("ffmpeg_binary", "ffmpeg")),
            encoder=str(options.get("encoder", "auto")).lower(),
            retry_initial_seconds=float(options.get("retry_initial_seconds", 5.0)),
            retry_max_seconds=float(options.get("retry_max_seconds", 30.0)),
            stop_timeout_seconds=float(options.get("stop_timeout_seconds", 12.0)),
        )
        with self._lock:
            current = self._recorders.get(settings.device_id)
            if current and current.thread and current.thread.is_alive():
                same_settings = (
                    current.settings.stream_url == settings.stream_url
                    and current.settings.storage_directory == settings.storage_directory
                    and current.settings.segment_duration == settings.segment_duration
                    and current.settings.max_storage_size == settings.max_storage_size
                    and current.settings.video_fps == settings.video_fps
                    and current.settings.encoder == settings.encoder
                )
                if same_settings and not current.stop_event.is_set():
                    return False
                self._stop_handle(current)
            self._get_storage(settings)
            handle = _RecorderHandle(settings=settings)
            handle.thread = threading.Thread(
                target=self._run_recorder,
                args=(handle,),
                name=f"recorder-{safe_device_id(settings.device_id)}",
                daemon=False,
            )
            self._recorders[settings.device_id] = handle
            self.log.info("Starting %s", settings.device_id)
            handle.thread.start()
            return True

    def stop_recorder(self, device_id: str) -> bool:
        with self._lock:
            handle = self._recorders.get(device_id)
            if not handle:
                return False
            self._stop_handle(handle)
            self._recorders.pop(device_id, None)
            return True

    def _stop_handle(self, handle: _RecorderHandle) -> None:
        handle.stop_event.set()
        if handle.thread and handle.thread.is_alive():
            handle.thread.join(timeout=handle.settings.stop_timeout_seconds + 15)
            if handle.thread.is_alive():
                raise RuntimeError(
                    f"Recorder thread did not stop; refusing duplicate process for {handle.settings.device_id}"
                )

    def restart_recorder(self, device_id: str, **overrides: Any) -> bool:
        with self._lock:
            handle = self._recorders.get(device_id)
            if not handle:
                return False
            old = handle.settings
            options = {
                "video_fps": old.video_fps,
                "ffmpeg_binary": old.ffmpeg_binary,
                "encoder": old.encoder,
                "retry_initial_seconds": old.retry_initial_seconds,
                "retry_max_seconds": old.retry_max_seconds,
                "stop_timeout_seconds": old.stop_timeout_seconds,
            }
            options.update(overrides)
            args = (old.device_id, overrides.get("stream_url", old.stream_url),
                    overrides.get("storage_directory", old.storage_directory),
                    overrides.get("max_storage_size", old.max_storage_size),
                    overrides.get("segment_duration", old.segment_duration))
            self._stop_handle(handle)
            self._recorders.pop(device_id, None)
        return self.start_recorder(*args, **options)

    def get_recorder_status(self, device_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            handles = (
                {device_id: self._recorders[device_id]}
                if device_id in self._recorders
                else {} if device_id else dict(self._recorders)
            )
            return {
                did: {
                    "device_id": did,
                    "state": handle.state,
                    "stream_url": handle.settings.stream_url,
                    "pid": handle.pid,
                    "encoder": handle.active_encoder,
                    "last_error": handle.last_error,
                }
                for did, handle in handles.items()
            }

    def shutdown(self) -> None:
        with self._lock:
            handles = list(self._recorders.values())
            for handle in handles:
                try:
                    self._stop_handle(handle)
                except Exception:
                    self.log.exception("Failed to stop recorder thread: %s", handle.settings.device_id)
            self._recorders.clear()

    def _get_storage(self, settings: RecorderSettings) -> StorageManager:
        db_path = self._database_path or settings.storage_directory.parent / "data" / "recordings.sqlite3"
        key = Path(db_path).resolve()
        storage = self._storage.get(key)
        if storage is None:
            storage = StorageManager(key, settings.storage_directory, settings.max_storage_size)
            self._storage[key] = storage
        else:
            storage.max_storage_bytes = settings.max_storage_size
        return storage

    def _run_recorder(self, handle: _RecorderHandle) -> None:
        settings = handle.settings
        storage = self._get_storage(settings)
        delay = settings.retry_initial_seconds
        device_dir = settings.storage_directory / safe_device_id(settings.device_id)
        device_dir.mkdir(parents=True, exist_ok=True)
        session_id = f"{int(time.time())}-{threading.get_ident()}"
        segment_list = device_dir / f".segments-{session_id}.csv"
        output_pattern = device_dir / "%Y-%m-%d" / "%H-%M-%S.mp4"
        ffmpeg_binary = shutil.which(settings.ffmpeg_binary) or settings.ffmpeg_binary
        encoder: str | None = None

        while not handle.stop_event.is_set():
            try:
                self._ensure_output_days(device_dir)
                if encoder is None:
                    encoder = select_video_encoder(ffmpeg_binary, settings.encoder)
                    handle.active_encoder = encoder
                command = build_ffmpeg_command(
                    settings.stream_url,
                    output_pattern,
                    settings.segment_duration,
                    settings.video_fps,
                    encoder,
                    ffmpeg_binary,
                    segment_list,
                )
                self.log.info("Stream URL: %s", settings.stream_url)
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                   | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)),
                )
                handle.pid = process.pid
                handle.state = "recording"
                handle.started_at = time.time()
                handle.last_error = None
                self.log.info("FFmpeg started (pid=%s, encoder=%s)", process.pid, encoder)

                stderr_tail: deque[str] = deque(maxlen=30)
                reader = threading.Thread(
                    target=self._drain_stderr,
                    args=(process, stderr_tail),
                    name=f"ffmpeg-log-{safe_device_id(settings.device_id)}",
                    daemon=True,
                )
                reader.start()
                last_import = 0.0
                while process.poll() is None and not handle.stop_event.wait(1.0):
                    self._ensure_output_days(device_dir)
                    now = time.monotonic()
                    if now - last_import >= 2.0:
                        self._import_closed_segments(settings, segment_list, storage)
                        storage.enforce_quota(recently_written_paths(settings.storage_directory))
                        last_import = now

                stopped_by_request = handle.stop_event.is_set()
                cleanup_process(process, settings.stop_timeout_seconds)
                reader.join(timeout=2)
                self._import_closed_segments(settings, segment_list, storage)
                storage.enforce_quota(recently_written_paths(settings.storage_directory, within_seconds=1.0))
                handle.pid = None
                self.log.info("FFmpeg stopped (exit=%s)", process.returncode)
                if stopped_by_request:
                    break

                detail = " | ".join(stderr_tail)
                handle.last_error = detail or f"FFmpeg exited with code {process.returncode}"
                hardware_markers = ("nvenc", "qsv", "amf", "no capable devices", "hardware device")
                if (
                    process.returncode != 0
                    and encoder != "libx264"
                    and any(marker in detail.lower() for marker in hardware_markers)
                ):
                    encoder = "libx264"
                    handle.active_encoder = encoder
                    delay = settings.retry_initial_seconds
                    self.log.warning("Hardware encoder failed; falling back to software libx264")
                    continue
                handle.state = "offline"
                self.log.warning("Camera offline: %s", settings.device_id)
                self.log.info("Reconnecting... retry in %.1fs", delay)
            except Exception as exc:
                handle.pid = None
                handle.state = "offline"
                handle.last_error = str(exc)
                self.log.exception("Recorder error for %s", settings.device_id)
                if handle.stop_event.is_set():
                    break
                # An unavailable/incompatible hardware encoder falls back once to software.
                if encoder and encoder != "libx264" and isinstance(exc, OSError):
                    encoder = "libx264"
                    handle.active_encoder = encoder
                    self.log.warning("Falling back to software encoder libx264")

            if handle.stop_event.wait(delay):
                break
            delay = min(max(delay * 2, settings.retry_initial_seconds), settings.retry_max_seconds)

        handle.pid = None
        handle.state = "stopped"
        try:
            self._import_closed_segments(settings, segment_list, storage)
            storage.enforce_quota(recently_written_paths(settings.storage_directory, within_seconds=1.0))
        except Exception:
            self.log.exception("Final storage reconciliation failed for %s", settings.device_id)
        segment_list.unlink(missing_ok=True)

    @staticmethod
    def _ensure_output_days(device_dir: Path) -> None:
        today = datetime.now().astimezone().date()
        for offset in (0, 1, 2):
            (device_dir / (today + timedelta(days=offset)).isoformat()).mkdir(
                parents=True, exist_ok=True
            )

    def _drain_stderr(self, process: subprocess.Popen[bytes], tail: deque[str]) -> None:
        if not process.stderr:
            return
        for raw in iter(process.stderr.readline, b""):
            line = raw.decode("utf-8", errors="replace").strip()
            if line:
                tail.append(line)
                self.log.debug("FFmpeg: %s", line)
        process.stderr.close()

    def _import_closed_segments(
        self,
        settings: RecorderSettings,
        segment_list: Path,
        storage: StorageManager,
    ) -> None:
        if not segment_list.is_file():
            return
        try:
            with segment_list.open("r", encoding="utf-8", newline="") as source:
                rows = list(csv.reader(source))
        except (OSError, csv.Error):
            return
        for row in rows:
            if len(row) < 3:
                continue
            output_path = Path(row[0])
            if not output_path.is_absolute():
                output_path = (Path.cwd() / output_path).resolve()
            try:
                duration = float(row[2]) - float(row[1])
            except ValueError:
                continue
            start_time = _segment_start_from_path(output_path)
            if start_time is None or duration <= 0:
                continue
            if storage.record_segment(settings.device_id, output_path, start_time, duration):
                self.log.info("Segment created: %s", output_path)
                storage.enforce_quota(recently_written_paths(settings.storage_directory))


_default_manager: RecorderManager | None = None


def configure_recorder_manager(manager: RecorderManager | None) -> None:
    global _default_manager
    _default_manager = manager


def _manager() -> RecorderManager:
    global _default_manager
    if _default_manager is None:
        _default_manager = RecorderManager()
    return _default_manager


def start_recorder(
    device_id: str,
    stream_url: str,
    storage_directory: str | Path,
    max_storage_size: int,
    segment_duration: int = 300,
    **options: Any,
) -> bool:
    return _manager().start_recorder(
        device_id, stream_url, storage_directory, max_storage_size, segment_duration, **options
    )


def stop_recorder(device_id: str) -> bool:
    return _manager().stop_recorder(device_id)


def restart_recorder(device_id: str, **overrides: Any) -> bool:
    return _manager().restart_recorder(device_id, **overrides)


def get_recorder_status(device_id: str | None = None) -> dict[str, Any]:
    return _manager().get_recorder_status(device_id)
