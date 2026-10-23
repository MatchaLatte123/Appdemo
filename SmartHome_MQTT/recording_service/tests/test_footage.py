from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch
from urllib.request import Request, urlopen

from recording_service.footage_manager import (
    ExportManager,
    FootageStore,
    StorageManager,
    build_export_command,
    parse_local_range,
    timeline_gaps,
)
from recording_service.service import CameraRecorder, RecordingService


UTC = timezone.utc


class FootageManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "footage"
        self.db_path = Path(self.temp.name) / "footage.sqlite3"
        self.logger = logging.getLogger(f"test-footage-{id(self)}")
        self.store = FootageStore(self.db_path, self.root, self.logger)

    def tearDown(self):
        self.temp.cleanup()

    def add_segment(self, recording_id, start, end, *, protected=False, size=10, path=None):
        segment_path = path or self.root / "cam01" / f"{recording_id}.mp4"
        segment_path.parent.mkdir(parents=True, exist_ok=True)
        segment_path.write_bytes(b"v" * size)
        self.store.add_segment(
            recording_id=recording_id,
            device_id="cam01",
            start_time=start,
            end_time=end,
            duration=(end - start).total_seconds(),
            file_path=segment_path,
            file_size=size,
        )
        if protected:
            self.assertTrue(self.store.set_protected(recording_id, True))
        return segment_path

    def test_recorder_segments_one_camera_input_without_restarting_at_each_cut(self):
        manager = StorageManager(self.store, max_bytes=10_000_000,
                                  cleanup_target_bytes=9_000_000, logger=self.logger)
        recorder = CameraRecorder(
            {"camera_id": "cam01", "stream_url": "http://127.0.0.1:81/stream"},
            {"segment_seconds": 300}, self.root, self.logger, self.store, manager,
        )
        pattern = self.root / "cam01" / "session_%06d.partial.mp4"
        listing = self.root / "cam01" / "session.csv"
        command = recorder._ffmpeg_command(pattern, listing)
        self.assertEqual(command.count("-i"), 1)
        self.assertIn("segment", command)
        self.assertEqual(command.count("libx264"), 1)
        self.assertNotIn("hls", command)
        self.assertIn("image2pipe", command)
        self.assertNotIn("-t", command)

        stem = "cam01_session"
        (self.root / "cam01").mkdir(parents=True, exist_ok=True)
        first = self.root / "cam01" / f"{stem}_000000.partial.mp4"
        second = self.root / "cam01" / f"{stem}_000001.partial.mp4"
        first.write_bytes(b"mp4-one")
        second.write_bytes(b"mp4-two")
        listing.write_text(
            f'"{first}",0.000000,300.000000\n"{second}",300.000000,600.000000\n',
            encoding="utf-8",
        )
        processed: set[str] = set()
        with patch("recording_service.service.ffprobe_duration", return_value=300.0):
            previous_end = recorder._publish_closed_segments(listing, stem, processed, None)
        records = self.store.complete_records_oldest_first()
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["end_time"], records[1]["start_time"])
        self.assertIsNotNone(previous_end)

    def test_query_by_day_and_local_hour_converts_to_utc(self):
        start, end = parse_local_range("2026-10-17", "14:00", "15:00", 420)
        self.assertEqual(start, datetime(2026, 10, 17, 7, tzinfo=UTC))
        self.assertEqual(end, datetime(2026, 10, 17, 8, tzinfo=UTC))
        self.assertEqual(end - start, timedelta(hours=1))

    def test_segment_boundaries_are_half_open_and_gaps_are_explicit(self):
        base = datetime(2026, 10, 17, 7, tzinfo=UTC)
        first = {"recording_id": "first", "start_time": "2026-10-17T07:00:00.000000Z",
                 "end_time": "2026-10-17T07:05:00.000000Z"}
        second = {"recording_id": "second", "start_time": "2026-10-17T07:10:00.000000Z",
                  "end_time": "2026-10-17T07:15:00.000000Z"}
        self.add_segment("first", base, base + timedelta(minutes=5))
        self.add_segment("second", base + timedelta(minutes=10), base + timedelta(minutes=15))

        # Ending exactly at the next segment's start excludes that segment.
        exact = self.store.query("cam01", base + timedelta(minutes=5), base + timedelta(minutes=10))
        self.assertEqual(exact, [])
        overlapping = self.store.query("cam01", base + timedelta(minutes=4), base + timedelta(minutes=11))
        self.assertEqual([row["recording_id"] for row in overlapping], ["first", "second"])
        gaps = timeline_gaps(overlapping, base + timedelta(minutes=4), base + timedelta(minutes=11))
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["start_time_utc"], "2026-10-17T07:05:00.000000Z")
        self.assertEqual(gaps[0]["end_time_utc"], "2026-10-17T07:10:00.000000Z")
        self.assertEqual(first["recording_id"], "first")
        self.assertEqual(second["recording_id"], "second")

    def test_metadata_survives_store_restart(self):
        start = datetime(2026, 10, 17, 7, tzinfo=UTC)
        path = self.add_segment("persistent", start, start + timedelta(minutes=5), size=19)
        restarted = FootageStore(self.db_path, self.root, self.logger)
        rows = restarted.query("cam01", start, start + timedelta(minutes=5))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["recording_id"], "persistent")
        self.assertEqual(rows[0]["file_size"], path.stat().st_size)
        self.assertEqual(rows[0]["status"], "complete")

    def test_active_recording_is_marked_incomplete_after_restart(self):
        start = datetime(2026, 10, 17, 7, tzinfo=UTC)
        partial = self.root / "cam01" / "interrupted.partial.mp4"
        partial.parent.mkdir(parents=True, exist_ok=True)
        partial.write_bytes(b"partial-video")
        self.store.begin_segment(recording_id="interrupted", device_id="cam01",
                                 start_time=start, partial_path=partial)
        restarted = FootageStore(self.db_path, self.root, self.logger)
        restarted.reconcile()
        record = restarted.get("interrupted")
        self.assertEqual(record["status"], "incomplete")
        self.assertEqual(record["file_size"], len(b"partial-video"))
        self.assertEqual(restarted.query("cam01", start, start + timedelta(hours=1)), [])
        self.assertTrue(partial.exists())

    def test_missing_file_is_marked_and_not_returned_as_video(self):
        start = datetime(2026, 10, 17, 7, tzinfo=UTC)
        path = self.add_segment("missing", start, start + timedelta(minutes=5))
        path.unlink()
        self.store.reconcile()
        self.assertEqual(self.store.query("cam01", start, start + timedelta(minutes=5)), [])
        record = self.store.get("missing")
        self.assertEqual(record["status"], "missing")
        self.assertEqual(record["file_size"], 0)

    def test_invalid_metadata_is_marked_and_not_returned(self):
        with self.store._connect() as db:
            db.execute("""
                INSERT INTO recordings(recording_id, device_id, start_time, end_time, duration,
                                       file_path, file_size, status, is_protected)
                VALUES ('bad-row', 'cam01', 'not-a-timestamp', 'also-invalid', 60,
                        '../outside.mp4', 10, 'complete', 0)
            """)
        start = datetime(2026, 10, 17, 7, tzinfo=UTC)
        self.store.reconcile()
        self.assertEqual(self.store.query("cam01", start, start + timedelta(hours=1)), [])
        self.assertEqual(self.store.get("bad-row")["status"], "invalid")

    def test_storage_cleanup_removes_oldest_until_target_and_preserves_protected(self):
        start = datetime(2026, 10, 17, 7, tzinfo=UTC)
        old = self.add_segment("old", start, start + timedelta(minutes=1), size=5)
        protected = self.add_segment("protected", start + timedelta(minutes=1),
                                     start + timedelta(minutes=2), protected=True, size=5)
        newest = self.add_segment("newest", start + timedelta(minutes=2),
                                  start + timedelta(minutes=3), size=5)
        partial = self.root / "cam01" / "active.partial.mp4"
        partial.write_bytes(b"pp")
        manager = StorageManager(self.store, max_bytes=16, cleanup_target_bytes=8, logger=self.logger)

        result = manager.cleanup()
        self.assertEqual(result["deleted_recording_ids"], ["old", "newest"])
        self.assertFalse(old.exists())
        self.assertTrue(protected.exists())
        self.assertFalse(newest.exists())
        self.assertTrue(partial.exists())
        self.assertEqual(self.store.get("old")["status"], "deleted")
        self.assertEqual(self.store.get("newest")["status"], "deleted")
        self.assertEqual(self.store.get("protected")["status"], "complete")
        self.assertLessEqual(result["used_bytes"], 8)
        self.assertIsNone(result["warning"])

    def test_cleanup_warns_if_only_protected_files_prevent_target(self):
        start = datetime(2026, 10, 17, 7, tzinfo=UTC)
        protected = self.add_segment("protected-only", start, start + timedelta(minutes=1),
                                     protected=True, size=20)
        active = self.add_segment("active", start + timedelta(minutes=1),
                                  start + timedelta(minutes=2), size=5)
        partial = self.root / "cam01" / "active.partial.mp4"
        partial.write_bytes(b"pending")
        manager = StorageManager(self.store, max_bytes=16, cleanup_target_bytes=8, logger=self.logger)
        manager.mark_active(active)
        result = manager.cleanup()
        self.assertTrue(protected.exists())
        self.assertTrue(active.exists())
        self.assertTrue(partial.exists())
        self.assertIn("Protected, active, partial, or unindexed", result["warning"])
        self.assertEqual(self.store.get("protected-only")["status"], "complete")
        manager.unmark_active(active)

    def test_cleanup_warning_persists_until_target_is_reached(self):
        start = datetime(2026, 10, 17, 7, tzinfo=UTC)
        self.add_segment("protected", start, start + timedelta(minutes=1), protected=True, size=9)
        orphan = self.root / "cam01" / "unindexed.mp4"
        orphan.write_bytes(b"u" * 8)
        manager = StorageManager(self.store, max_bytes=16, cleanup_target_bytes=8, logger=self.logger)
        first = manager.cleanup()
        self.assertIsNotNone(first["warning"])
        orphan.unlink()
        second = manager.cleanup()
        self.assertIsNotNone(second["warning"])
        (self.root / "cam01" / "protected.mp4").unlink()
        self.store.update_status("protected", "missing")
        third = manager.cleanup()
        self.assertIsNone(third["warning"])

    def test_cleanup_never_deletes_files_outside_managed_root(self):
        start = datetime(2026, 10, 17, 7, tzinfo=UTC)
        external = Path(self.temp.name) / "outside.mp4"
        external.write_bytes(b"outside")
        with self.assertRaises(ValueError):
            self.store.add_segment(recording_id="escape", device_id="cam01", start_time=start,
                                   end_time=start + timedelta(minutes=1), duration=60,
                                   file_path=external, file_size=external.stat().st_size)
        self.assertTrue(external.exists())

    def test_cleanup_preserves_playback_lease_and_keeps_metadata(self):
        start = datetime(2026, 10, 17, 7, tzinfo=UTC)
        path = self.add_segment("leased", start, start + timedelta(minutes=1), size=20)
        manager = StorageManager(self.store, max_bytes=16, cleanup_target_bytes=8, logger=self.logger)
        self.assertTrue(manager.acquire_file(path))
        result = manager.cleanup()
        self.assertTrue(path.exists())
        self.assertEqual(self.store.get("leased")["status"], "complete")
        self.assertIsNotNone(result["warning"])
        manager.release_file(path)
        with manager.usage_lock:
            manager.deleting_paths.add(path.resolve())
        self.assertFalse(manager.acquire_file(path))
        with manager.usage_lock:
            manager.deleting_paths.discard(path.resolve())

    def test_failed_file_delete_does_not_mark_metadata_deleted(self):
        start = datetime(2026, 10, 17, 7, tzinfo=UTC)
        path = self.add_segment("locked", start, start + timedelta(minutes=1), size=20)
        manager = StorageManager(self.store, max_bytes=16, cleanup_target_bytes=8, logger=self.logger)
        with patch.object(Path, "unlink", side_effect=PermissionError("locked")):
            result = manager.cleanup()
        self.assertTrue(path.exists())
        self.assertEqual(self.store.get("locked")["status"], "complete")
        self.assertIsNotNone(result["warning"])

    def test_status_uses_cached_usage_without_recursive_inventory(self):
        manager = StorageManager(self.store, max_bytes=100, cleanup_target_bytes=80, logger=self.logger)
        manager._cached_used_bytes = 42
        with patch.object(manager, "_physical_files", side_effect=AssertionError("unexpected scan")):
            status = manager.status()
        self.assertEqual(status["used_bytes"], 42)

    def test_cleanup_requests_use_a_bounded_single_slot_queue(self):
        manager = StorageManager(self.store, max_bytes=100, cleanup_target_bytes=80, logger=self.logger)
        self.assertTrue(manager.request_cleanup())
        self.assertFalse(manager.request_cleanup())
        self.assertFalse(manager.request_inventory())

    def test_storage_root_change_keeps_old_footage_indexed_in_place(self):
        config = {
            "storage_dir": "footage", "metadata_db": "footage.sqlite3",
            "cameras": [{"camera_id": "cam01", "stream_url": "http://127.0.0.1:81/stream"}],
        }
        service_dir = Path(self.temp.name) / "root-change-service"
        service = RecordingService(config, service_dir)
        old_root = service.storage_dir.resolve()
        start = datetime(2026, 10, 17, 7, tzinfo=UTC)
        old_file = service.storage_dir / "cam01" / "old.mp4"
        old_file.parent.mkdir(parents=True, exist_ok=True)
        old_file.write_bytes(b"old-footage")
        service.store.add_segment(recording_id="old-root", device_id="cam01", start_time=start,
                                  end_time=start + timedelta(minutes=1), duration=60,
                                  file_path=old_file, file_size=old_file.stat().st_size)
        new_root = Path(self.temp.name) / "alternate-drive" / "footage"
        try:
            invalid_target = Path(self.temp.name) / "not-a-directory"
            invalid_target.write_text("keep", encoding="utf-8")
            with self.assertRaises(ValueError):
                service.update_storage_settings({
                    "storage_dir": str(invalid_target), "max_storage_gib": 5.0,
                    "cleanup_target_gib": 4.5, "cleanup_enabled": False,
                })
            self.assertEqual(service.storage_dir.resolve(), old_root)
            with patch.object(service.recorders["cam01"], "stop"), \
                 patch.object(service.recorders["cam01"], "start"):
                result = service.update_storage_settings({
                    "storage_dir": str(new_root), "max_storage_gib": 5.0,
                    "cleanup_target_gib": 4.5, "cleanup_enabled": False,
                })
            record = service.store.get("old-root")
            self.assertEqual(Path(record["storage_root"]), old_root)
            self.assertTrue(service.store.resolve_record_file(record).is_file())
            self.assertTrue(old_file.is_file())
            self.assertEqual(Path(result["storage_dir"]), new_root.resolve())
            self.assertTrue(new_root.is_dir())
            saved_config = json.loads((service_dir / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(Path(saved_config["storage_dir"]), new_root.resolve())
            restarted = RecordingService(saved_config, service_dir, service_dir / "config.json")
            try:
                self.assertEqual(restarted.storage_dir, new_root.resolve())
                self.assertEqual(restarted.store.get("old-root")["status"], "complete")
            finally:
                for handler in restarted.logger.handlers[:]:
                    handler.flush()
                    handler.close()
                    restarted.logger.removeHandler(handler)
        finally:
            service.storage_manager.stop_worker()
            for handler in service.logger.handlers[:]:
                handler.flush()
                handler.close()
                service.logger.removeHandler(handler)

    def test_missing_storage_volume_does_not_fall_back_or_create_service_directory(self):
        unavailable = Path(self.temp.name) / "unmounted-drive" / "footage"
        config = {
            "storage_dir": str(unavailable), "metadata_db": "footage.sqlite3",
            "cleanup_enabled": False,
            "cameras": [{"camera_id": "cam01", "stream_url": "http://127.0.0.1:81/stream"}],
        }
        service_dir = Path(self.temp.name) / "offline-service"
        with patch("recording_service.service.validate_storage_directory",
                   side_effect=ValueError("volume is unavailable")):
            service = RecordingService(config, service_dir)
        try:
            status = service.storage_manager.status()
            self.assertEqual(service.storage_dir, unavailable.resolve())
            self.assertFalse(unavailable.exists())
            self.assertFalse(status["storage_available"])
            self.assertIn("volume is unavailable", status["last_error"])
        finally:
            for handler in service.logger.handlers[:]:
                handler.flush()
                handler.close()
                service.logger.removeHandler(handler)

    def test_export_command_reencodes_clips_and_preserves_gaps(self):
        start = datetime(2026, 10, 17, 7, tzinfo=UTC)
        records = [
            {"recording_id": "a", "start_time": "2026-10-17T07:00:00.000000Z",
             "end_time": "2026-10-17T07:05:00.000000Z", "_absolute_path": "a.mp4"},
            {"recording_id": "b", "start_time": "2026-10-17T07:10:00.000000Z",
             "end_time": "2026-10-17T07:15:00.000000Z", "_absolute_path": "b.mp4"},
        ]
        command = build_export_command("ffmpeg", records, start,
                                       start + timedelta(minutes=15), Path("joined.partial.mp4"))
        filter_graph = command[command.index("-filter_complex") + 1]
        self.assertIn("trim=start=0.000000:end=300.000000", filter_graph)
        self.assertIn("color=c=black", filter_graph)
        self.assertIn("concat=n=3:v=1:a=0", filter_graph)
        self.assertIn("libx264", command)
        self.assertIn("-i", command)

    def test_export_manager_writes_and_verifies_separate_mp4(self):
        start = datetime(2026, 10, 17, 7, tzinfo=UTC)
        segment = self.add_segment("export-source", start, start + timedelta(minutes=20), size=20)
        manager = ExportManager(self.store, self.root, ffmpeg="ffmpeg", ffprobe="ffprobe",
                                logger=self.logger)

        def fake_run(command, **_kwargs):
            if command[0] == "ffmpeg":
                Path(command[-1]).write_bytes(b"exported-mp4")
                return SimpleNamespace(stdout="", stderr="")
            self.assertEqual(Path(command[-1]).parent, manager.export_dir)
            return SimpleNamespace(stdout="600.0", stderr="")

        with patch("recording_service.footage_manager.subprocess.run", side_effect=fake_run) as run:
            result = manager.export("cam01", start + timedelta(minutes=5),
                                    start + timedelta(minutes=15))
        self.assertTrue(Path(result["file"]).is_file())
        self.assertEqual(result["duration"], 600.0)
        self.assertIn("/api/exports/", result["download_url"])
        self.assertEqual(run.call_count, 2)
        self.assertTrue(segment.exists())

    def test_recording_service_config_defaults_to_five_and_four_point_five_gib(self):
        config = {
            "storage_dir": "footage",
            "cameras": [{"camera_id": "cam01", "stream_url": "http://127.0.0.1:81/stream"}],
        }
        service_dir = Path(self.temp.name) / "service"
        service = RecordingService(config, service_dir)
        gib = 1024 ** 3
        self.assertEqual(service.storage_manager.max_bytes, 5 * gib)
        self.assertEqual(service.storage_manager.cleanup_target_bytes, int(4.5 * gib))
        for handler in service.logger.handlers[:]:
            handler.flush()
            handler.close()
            service.logger.removeHandler(handler)

    def test_http_footage_query_and_protection_api(self):
        config = {
            "storage_dir": "footage",
            "metadata_db": "footage.sqlite3",
            "cameras": [{"camera_id": "cam01", "stream_url": "http://127.0.0.1:81/stream"}],
        }
        service_dir = Path(self.temp.name) / "http-service"
        service = RecordingService(config, service_dir)
        start = datetime(2026, 10, 17, 7, tzinfo=UTC)
        recording_id = "00000000-0000-4000-8000-000000000001"
        segment_path = service.storage_dir / "cam01" / f"{recording_id}.mp4"
        segment_path.parent.mkdir(parents=True, exist_ok=True)
        segment_path.write_bytes(b"video")
        service.store.add_segment(recording_id=recording_id, device_id="cam01", start_time=start,
                                  end_time=start + timedelta(minutes=20), duration=1200,
                                  file_path=segment_path, file_size=segment_path.stat().st_size)
        server = ThreadingHTTPServer(("127.0.0.1", 0), service._handler_type())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            query = "/api/footage?camera_id=cam01&date=2026-10-17&start_time=14:05&end_time=14:15&utc_offset_minutes=420"
            with urlopen(base + query, timeout=2) as response:
                payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual(len(payload["segments"]), 1)
            self.assertEqual(payload["segments"][0]["recording_id"], recording_id)
            self.assertEqual(payload["segments"][0]["file_size"], segment_path.stat().st_size)
            self.assertEqual(payload["gaps"], [])

            # Device ID + explicit ISO timestamps, individual metadata, a
            # multi-segment manifest and byte-range playback share this API.
            start_text = "2026-10-17T07:05:00Z"
            end_text = "2026-10-17T07:15:00Z"
            with urlopen(base + f"/api/footage?device_id=cam01&start_time={start_text}&end_time={end_text}", timeout=2) as response:
                listed = json.loads(response.read().decode("utf-8"))
            self.assertEqual(len(listed["segments"]), 1)
            with urlopen(base + f"/api/recordings/{recording_id}", timeout=2) as response:
                metadata = json.loads(response.read().decode("utf-8"))
            self.assertEqual(metadata["recording_id"], recording_id)
            with urlopen(base + f"/api/playback/manifest?device_id=cam01&start_time={start_text}&end_time={end_text}", timeout=2) as response:
                manifest = json.loads(response.read().decode("utf-8"))
            self.assertTrue(manifest["segments"][0]["playback_url"].endswith(f"/api/recordings/{recording_id}/file"))
            range_request = Request(base + f"/api/recordings/{recording_id}/file", headers={"Range": "bytes=1-3"})
            with urlopen(range_request, timeout=2) as response:
                self.assertEqual(response.status, 206)
                self.assertEqual(response.headers["Content-Range"], "bytes 1-3/5")
                self.assertEqual(response.read(), b"ide")
            try:
                urlopen(Request(base + f"/api/recordings/{recording_id}/file",
                                headers={"Range": "bytes=99-100"}), timeout=2)
                self.fail("unsatisfiable range should return HTTP 416")
            except Exception as exc:
                self.assertEqual(getattr(exc, "code", None), 416)

            try:
                urlopen(base + "/api/recordings/../../outside/file", timeout=2)
                self.fail("arbitrary file path should not be exposed")
            except Exception as exc:
                self.assertEqual(getattr(exc, "code", None), 404)

            with urlopen(base + "/api/status", timeout=2) as response:
                status = json.loads(response.read().decode("utf-8"))
            self.assertIn("used_bytes", status["storage"])
            self.assertIn("max_bytes", status["storage"])
            self.assertIn("last_media_at", status["cameras"][0])
            self.assertNotIn("stream_url", status["cameras"][0])
            with urlopen(base + "/api/storage/settings", timeout=2) as response:
                settings = json.loads(response.read().decode("utf-8"))
            self.assertEqual(settings["max_storage_gib"], 5.0)
            self.assertIn("storage_dir", settings)
            validated_dir = Path(self.temp.name) / "api-created-storage"
            validate_request = Request(
                base + "/api/storage/validate",
                data=json.dumps({"storage_dir": str(validated_dir)}).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
            with urlopen(validate_request, timeout=2) as response:
                validation = json.loads(response.read().decode("utf-8"))
            self.assertTrue(validation["valid"])
            self.assertTrue(validated_dir.is_dir())
            update_request = Request(
                base + "/api/storage/settings",
                data=json.dumps({"storage_dir": str(service.storage_dir),
                                 "max_storage_gib": 5.0, "cleanup_target_gib": 4.5,
                                 "cleanup_enabled": False}).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
            with urlopen(update_request, timeout=2) as response:
                updated = json.loads(response.read().decode("utf-8"))
            self.assertFalse(updated["cleanup_enabled"])
            saved = json.loads((service_dir / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["max_storage_gib"], 5.0)

            create_live = Request(base + "/api/live-sessions", data=b'{"device_id":"cam01"}',
                                  headers={"Content-Type": "application/json"}, method="POST")
            with urlopen(create_live, timeout=2) as response:
                live = json.loads(response.read().decode("utf-8"))
            self.assertEqual(live["transport"], "mjpeg")
            self.assertTrue(live["stream_url"].endswith("/stream.mjpg"))
            frame = b"\xff\xd8test-jpeg\xff\xd9"
            service.recorders["cam01"].live_frames.publish(frame)
            with urlopen(base + live["stream_url"], timeout=2) as response:
                self.assertEqual(response.headers["Content-Type"],
                                 "multipart/x-mixed-replace; boundary=camera-frame")
                self.assertEqual(response.readline(), b"--camera-frame\r\n")
                self.assertEqual(response.readline(), b"Content-Type: image/jpeg\r\n")
                content_length = int(response.readline().decode().split(":", 1)[1])
                self.assertEqual(response.readline(), b"\r\n")
                self.assertEqual(response.read(content_length), frame)
            close_live = Request(base + f"/api/live-sessions/{live['session_id']}", method="DELETE")
            with urlopen(close_live, timeout=2) as response:
                closed = json.loads(response.read().decode("utf-8"))
            self.assertTrue(closed["closed"])
            self.assertTrue(closed["recording_running"])
            for _ in range(4):
                create_live = Request(base + "/api/live-sessions", data=b'{"device_id":"cam01"}',
                                      headers={"Content-Type": "application/json"}, method="POST")
                with urlopen(create_live, timeout=2) as response:
                    repeated = json.loads(response.read().decode("utf-8"))
                close_live = Request(base + f"/api/live-sessions/{repeated['session_id']}", method="DELETE")
                with urlopen(close_live, timeout=2) as response:
                    self.assertTrue(json.loads(response.read().decode("utf-8"))["closed"])
            self.assertEqual(service.live_sessions, {})
            self.assertFalse(service.recorders["cam01"].thread.is_alive())

            export_id = "00000000-0000-4000-8000-000000000009"
            export_file = service.storage_dir / "exports" / f"{export_id}.mp4"
            export_file.parent.mkdir(parents=True, exist_ok=True)
            export_file.write_bytes(b"temporary-export")
            delete_export = Request(base + f"/api/exports/{export_id}", method="DELETE")
            with urlopen(delete_export, timeout=2) as response:
                self.assertTrue(json.loads(response.read().decode("utf-8"))["deleted"])
            self.assertFalse(export_file.exists())

            request = Request(base + f"/api/footage/{recording_id}/protection",
                              data=b'{"is_protected": true}',
                              headers={"Content-Type": "application/json"}, method="POST")
            with urlopen(request, timeout=2) as response:
                protected = json.loads(response.read().decode("utf-8"))
            self.assertTrue(protected["is_protected"])
            self.assertTrue(service.store.get(recording_id)["is_protected"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            for handler in service.logger.handlers[:]:
                handler.flush()
                handler.close()
                service.logger.removeHandler(handler)


if __name__ == "__main__":
    unittest.main()
