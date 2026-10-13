from __future__ import annotations

import logging
import queue
import signal
import threading
import time
from urllib.parse import urlparse

from .config import load_config
from .mqtt_camera import CameraStatusListener
from .recorder import RecorderManager
from .storage import StorageManager


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(name)s] %(message)s",
        handlers=[logging.StreamHandler()],
    )


def _valid_status_event(event: dict, expected_device_id: str) -> bool:
    if event.get("device_id") != expected_device_id or not event.get("online"):
        return False
    parsed = urlparse(event.get("stream_url", ""))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    reported_ip = event.get("ip")
    return not reported_ip or parsed.hostname == reported_ip


def run() -> None:
    configure_logging()
    log = logging.getLogger("RECORDER")
    config = load_config()
    if not config.enabled:
        log.info("Recording disabled by RECORDING_ENABLED")
        return

    # Ensure storage/schema exist before MQTT delivers its retained status.
    config.recording_root.mkdir(parents=True, exist_ok=True)
    storage = StorageManager(config.database_path, config.recording_root, config.max_storage_bytes)
    storage.remove_missing_rows()
    storage.enforce_quota()
    manager = RecorderManager(database_path=config.database_path)
    events: queue.Queue = queue.Queue()
    listener = CameraStatusListener(
        config.mqtt_broker,
        config.mqtt_port,
        config.mqtt_status_topic,
        events,
    )
    stopping = threading.Event()

    def request_shutdown(_signum=None, _frame=None):
        stopping.set()

    signal.signal(signal.SIGINT, request_shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_shutdown)

    listener.start()
    log.info(
        "Service ready for %s (segment=%ss, quota=%.2f GiB, root=%s)",
        config.device_id,
        config.segment_seconds,
        config.max_storage_bytes / 1024**3,
        config.recording_root,
    )
    last_status = time.monotonic()
    offline_reported = False
    try:
        while not stopping.is_set():
            try:
                event = events.get(timeout=1.0)
            except queue.Empty:
                event = None
            if event is not None and _valid_status_event(event, config.device_id):
                last_status = time.monotonic()
                offline_reported = False
                try:
                    manager.start_recorder(
                        device_id=config.device_id,
                        stream_url=event["stream_url"],
                        storage_directory=config.recording_root,
                        max_storage_size=config.max_storage_bytes,
                        segment_duration=config.segment_seconds,
                        video_fps=config.video_fps,
                        ffmpeg_binary=config.ffmpeg_binary,
                        encoder=config.encoder,
                        retry_initial_seconds=config.retry_initial_seconds,
                        retry_max_seconds=config.retry_max_seconds,
                        stop_timeout_seconds=config.process_stop_timeout_seconds,
                    )
                except Exception:
                    log.exception("Could not start recorder for %s", config.device_id)

            # Camera status is sent every 30s by the current firmware. A missed
            # heartbeat marks it offline but does not terminate the service.
            if time.monotonic() - last_status > 75 and not offline_reported:
                log.warning("Camera offline: %s (MQTT heartbeat timeout)", config.device_id)
                if manager.get_recorder_status(config.device_id):
                    try:
                        manager.stop_recorder(config.device_id)
                    except Exception:
                        log.exception("Could not stop recorder after camera timeout")
                offline_reported = True
    finally:
        log.info("Shutting down recording service")
        try:
            listener.stop()
        finally:
            manager.shutdown()


if __name__ == "__main__":
    run()
