from __future__ import annotations

import tempfile
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from recording_service.config import load_config
from recording_service.recorder import (
    RecorderManager,
    build_ffmpeg_command,
    cleanup_process,
    safe_device_id,
    validate_stream_url,
)
from recording_service.storage import StorageManager


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        with patch.dict("os.environ", {}, clear=True):
            config = load_config(env_file=Path("missing-recording-env"))
        self.assertEqual(config.device_id, "ESP32-CAM-01")
        self.assertEqual(config.segment_seconds, 300)
        self.assertEqual(config.max_storage_bytes, 5 * 1024**3)


class CommandTests(unittest.TestCase):
    def test_command_uses_mjpeg_h264_and_clock_segments(self):
        command = build_ffmpeg_command(
            "http://192.168.1.252:81/stream",
            Path("recordings/ESP32-CAM-01/%Y-%m-%d/%H-%M-%S.mp4"),
            segment_list=Path("segments.csv"),
        )
        for expected in (
            "mpjpeg", "-use_wallclock_as_timestamps", "-copyts", "libx264", "-f", "segment",
            "-segment_time", "300", "-segment_atclocktime", "1", "-strftime", "1",
        ):
            self.assertIn(expected, command)
        self.assertIn("-g", command)
        self.assertNotIn("-force_key_frames", command)
        self.assertNotIn("-c:v mjpeg", command)

    def test_hardware_encoder_options(self):
        command = build_ffmpeg_command(
            "http://camera.local:81/stream", "out.mp4", encoder="h264_nvenc"
        )
        self.assertIn("h264_nvenc", command)
        self.assertIn("-cq", command)

    def test_rejects_invalid_or_credentialed_stream_url(self):
        with self.assertRaises(ValueError):
            validate_stream_url("file:///tmp/camera")
        with self.assertRaises(ValueError):
            validate_stream_url("http://user:password@camera.local/stream")

    def test_device_id_is_safe_for_a_directory(self):
        self.assertEqual(safe_device_id("../CAM 01"), "CAM_01")


class StorageTests(unittest.TestCase):
    def test_quota_deletes_oldest_completed_but_not_protected_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "recordings"
            db = Path(temp) / "recordings.sqlite3"
            manager = StorageManager(db, root, max_storage_bytes=8)
            day = root / "ESP32-CAM-01" / "2026-10-06"
            day.mkdir(parents=True)
            old = day / "23-00-00.mp4"
            new = day / "23-05-00.mp4"
            old.write_bytes(b"123456")
            new.write_bytes(b"abcdef")
            from datetime import datetime
            manager.record_segment("ESP32-CAM-01", old, datetime.fromisoformat("2026-10-06T23:00:00"), 300)
            manager.record_segment("ESP32-CAM-01", new, datetime.fromisoformat("2026-10-06T23:05:00"), 300)
            manager.enforce_quota(protected_paths={new})
            self.assertFalse(old.exists())
            self.assertTrue(new.exists())


class DuplicateRecorderTests(unittest.TestCase):
    def test_start_is_idempotent_for_same_device_and_url(self):
        started = threading.Event()
        stopped = threading.Event()

        def fake_run(_manager, handle):
            started.set()
            handle.state = "recording"
            handle.stop_event.wait(2)
            stopped.set()

        with tempfile.TemporaryDirectory() as temp, patch(
            "recording_service.recorder.RecorderManager._run_recorder", fake_run
        ):
            manager = RecorderManager(database_path=Path(temp) / "db.sqlite3")
            args = ("ESP32-CAM-01", "http://127.0.0.1:81/stream", Path(temp) / "recordings", 100_000)
            self.assertTrue(manager.start_recorder(*args))
            self.assertTrue(started.wait(1))
            self.assertFalse(manager.start_recorder(*args))
            self.assertEqual(len(manager.get_recorder_status()), 1)
            manager.stop_recorder("ESP32-CAM-01")
            self.assertTrue(stopped.is_set())

    def test_cleanup_process_reaps_child_process(self):
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        cleanup_process(process, timeout_seconds=0.1)
        self.assertIsNotNone(process.poll())


if __name__ == "__main__":
    unittest.main()
