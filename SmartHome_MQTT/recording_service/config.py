from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SERVICE_ROOT = Path(__file__).resolve().parent


def _load_env_file(path: Path) -> None:
    """Load simple KEY=VALUE settings without overriding the process environment."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _as_bool(value: str, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class RecordingConfig:
    enabled: bool
    device_id: str
    segment_seconds: int
    max_storage_bytes: int
    recording_root: Path
    database_path: Path
    ffmpeg_binary: str
    video_fps: int
    encoder: str
    retry_initial_seconds: float
    retry_max_seconds: float
    process_stop_timeout_seconds: float
    mqtt_broker: str
    mqtt_port: int
    mqtt_status_topic: str


def load_config(env_file: Path | None = None) -> RecordingConfig:
    _load_env_file(env_file or SERVICE_ROOT / ".env")

    segment_seconds = int(os.getenv("RECORDING_SEGMENT_SECONDS", "300"))
    max_storage_gb = float(os.getenv("RECORDING_MAX_STORAGE_GB", "5"))
    if segment_seconds <= 0:
        raise ValueError("RECORDING_SEGMENT_SECONDS must be greater than zero")
    if max_storage_gb <= 0:
        raise ValueError("RECORDING_MAX_STORAGE_GB must be greater than zero")

    root_value = Path(os.getenv("RECORDING_ROOT", "./recordings"))
    recording_root = root_value if root_value.is_absolute() else PROJECT_ROOT / root_value
    db_value = Path(os.getenv("RECORDING_DATABASE", "./recording_service/data/recordings.sqlite3"))
    database_path = db_value if db_value.is_absolute() else PROJECT_ROOT / db_value

    return RecordingConfig(
        enabled=_as_bool(os.getenv("RECORDING_ENABLED", "true"), True),
        device_id=os.getenv("RECORDING_DEVICE_ID", "ESP32-CAM-01").strip(),
        segment_seconds=segment_seconds,
        max_storage_bytes=int(max_storage_gb * 1024**3),
        recording_root=recording_root.resolve(),
        database_path=database_path.resolve(),
        ffmpeg_binary=os.getenv("FFMPEG_BINARY", "ffmpeg").strip() or "ffmpeg",
        video_fps=max(1, int(os.getenv("RECORDING_VIDEO_FPS", "30"))),
        encoder=os.getenv("RECORDING_VIDEO_ENCODER", "auto").strip().lower(),
        retry_initial_seconds=max(1.0, float(os.getenv("RECORDING_RETRY_SECONDS", "5"))),
        retry_max_seconds=max(1.0, float(os.getenv("RECORDING_RETRY_MAX_SECONDS", "30"))),
        process_stop_timeout_seconds=max(1.0, float(os.getenv("RECORDING_STOP_TIMEOUT_SECONDS", "12"))),
        mqtt_broker=os.getenv("MQTT_BROKER", "192.168.1.130").strip(),
        mqtt_port=int(os.getenv("MQTT_PORT", "1883")),
        mqtt_status_topic=os.getenv(
            "CAMERA_STATUS_TOPIC", "smarthome/device/cam01/status"
        ).strip(),
    )

