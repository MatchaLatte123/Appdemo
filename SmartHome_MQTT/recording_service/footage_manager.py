"""SQLite footage catalog, quota cleanup, and timestamp-aware MP4 export."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, time as daytime, timedelta, timezone
import logging
import os
from pathlib import Path
import queue
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from typing import Any, Iterator


UTC = timezone.utc


def utc_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("Timestamp must include a timezone")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timestamp must include a timezone")
    return parsed.astimezone(UTC)


def parse_local_range(day: str, start_time: str | None, end_time: str | None,
                      utc_offset_minutes: int | None = None) -> tuple[datetime, datetime]:
    selected_day = date.fromisoformat(day)
    start_clock = daytime.fromisoformat(start_time) if start_time else daytime.min
    if end_time:
        end_clock = daytime.fromisoformat(end_time)
        end_day = selected_day
    else:
        end_clock = daytime.min
        end_day = selected_day + timedelta(days=1)
    if start_clock.tzinfo or end_clock.tzinfo:
        raise ValueError("Use local wall-clock time without a UTC offset in start_time/end_time")
    if utc_offset_minutes is not None:
        from datetime import timezone as make_timezone
        local_tz = make_timezone(timedelta(minutes=int(utc_offset_minutes)))
        start = datetime.combine(selected_day, start_clock, tzinfo=local_tz)
        end = datetime.combine(end_day, end_clock, tzinfo=local_tz)
    else:
        start = datetime.combine(selected_day, start_clock).astimezone()
        end = datetime.combine(end_day, end_clock).astimezone()
    start_utc, end_utc = start.astimezone(UTC), end.astimezone(UTC)
    if end_utc <= start_utc:
        raise ValueError("The query interval must satisfy start < end")
    return start_utc, end_utc


class FootageStore:
    """Persistent segment metadata. Queries use the half-open interval [start, end)."""

    def __init__(self, database_path: Path, storage_root: Path, logger: logging.Logger,
                 *, create_storage_root: bool = True):
        self.database_path = database_path.resolve()
        self.storage_root = storage_root.resolve()
        self.logger = logger
        self.lock = threading.RLock()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        if create_storage_root:
            self.storage_root.mkdir(parents=True, exist_ok=True)
        bootstrap = sqlite3.connect(self.database_path, timeout=15)
        try:
            bootstrap.execute("PRAGMA journal_mode=WAL")
        finally:
            bootstrap.close()
        with self._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS recordings (
                    recording_id TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL,
                    start_time TEXT NOT NULL,
                    end_time TEXT NOT NULL,
                    duration REAL NOT NULL,
                    file_path TEXT NOT NULL,
                    file_size INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    is_protected INTEGER NOT NULL DEFAULT 0,
                    storage_root TEXT NOT NULL DEFAULT ''
                )
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(recordings)")}
            if "storage_root" not in columns:
                db.execute("ALTER TABLE recordings ADD COLUMN storage_root TEXT NOT NULL DEFAULT ''")
            db.execute("UPDATE recordings SET storage_root=? WHERE storage_root=''",
                       (str(self.storage_root),))
            db.execute("CREATE INDEX IF NOT EXISTS idx_recordings_time ON recordings(device_id, start_time, end_time)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_recordings_status ON recordings(status, is_protected, start_time)")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.database_path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=15000")
        db.execute("PRAGMA synchronous=FULL")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _relative_file(self, file_path: Path) -> str:
        resolved = file_path.resolve()
        try:
            return resolved.relative_to(self.storage_root).as_posix()
        except ValueError as exc:
            raise ValueError("Recording file must be inside the managed storage directory") from exc

    def resolve_file(self, relative_path: str) -> Path:
        return self.resolve_from_root(relative_path, self.storage_root)

    @staticmethod
    def resolve_from_root(relative_path: str, root: Path) -> Path:
        root = root.resolve()
        target = (root / relative_path).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise ValueError("Metadata path escapes the managed storage directory") from exc
        return target

    def resolve_record_file(self, record: dict[str, Any]) -> Path:
        root = Path(record.get("storage_root") or self.storage_root)
        return self.resolve_from_root(str(record["file_path"]), root)

    def add_segment(self, *, recording_id: str, device_id: str, start_time: datetime,
                    end_time: datetime, duration: float, file_path: Path, file_size: int) -> None:
        relative_path = self._relative_file(file_path)
        if duration <= 0 or end_time <= start_time:
            raise ValueError("Completed segment must have a positive duration")
        with self.lock, self._connect() as db:
            db.execute("""
                INSERT INTO recordings(recording_id, device_id, start_time, end_time, duration,
                                       file_path, file_size, status, is_protected, storage_root)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'complete', 0, ?)
            """, (recording_id, device_id, utc_timestamp(start_time), utc_timestamp(end_time),
                  float(duration), relative_path, int(file_size), str(self.storage_root)))

    def begin_segment(self, *, recording_id: str, device_id: str, start_time: datetime,
                      partial_path: Path) -> None:
        relative_path = self._relative_file(partial_path)
        stamp = utc_timestamp(start_time)
        with self.lock, self._connect() as db:
            db.execute("""
                INSERT INTO recordings(recording_id, device_id, start_time, end_time, duration,
                                       file_path, file_size, status, is_protected, storage_root)
                VALUES (?, ?, ?, ?, 0, ?, 0, 'recording', 0, ?)
            """, (recording_id, device_id, stamp, stamp, relative_path, str(self.storage_root)))

    def complete_segment(self, recording_id: str, *, end_time: datetime,
                         duration: float, file_path: Path, file_size: int) -> None:
        relative_path = self._relative_file(file_path)
        if duration <= 0:
            raise ValueError("Completed segment must have a positive duration")
        with self.lock, self._connect() as db:
            result = db.execute("""
                UPDATE recordings SET end_time=?, duration=?, file_path=?, file_size=?, status='complete', storage_root=?
                WHERE recording_id=? AND status='recording'
            """, (utc_timestamp(end_time), float(duration), relative_path, int(file_size),
                  str(self.storage_root), recording_id))
            if result.rowcount != 1:
                raise ValueError(f"Recording metadata is not in recording state: {recording_id}")

    def mark_incomplete(self, recording_id: str, partial_path: Path | None) -> None:
        size = 0
        relative_path: str | None = None
        if partial_path is not None:
            try:
                relative_path = self._relative_file(partial_path)
                if partial_path.is_file():
                    size = partial_path.stat().st_size
            except (OSError, ValueError):
                pass
        with self.lock, self._connect() as db:
            db.execute("""
                UPDATE recordings SET status='incomplete', file_size=?,
                    file_path=COALESCE(?, file_path)
                WHERE recording_id=? AND status='recording'
            """, (size, relative_path, recording_id))

    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["is_protected"] = bool(result["is_protected"])
        return result

    def _validate_complete_rows(self) -> None:
        with self.lock, self._connect() as db:
            rows = [dict(row) for row in db.execute(
                "SELECT * FROM recordings WHERE status='complete'").fetchall()]
        for row in rows:
            issue: str | None = None
            actual_size: int | None = None
            try:
                start, end = parse_timestamp(row["start_time"]), parse_timestamp(row["end_time"])
                if end <= start or float(row["duration"]) <= 0:
                    issue = "invalid time or duration metadata"
                elif abs((end - start).total_seconds() - float(row["duration"])) > 2.0:
                    issue = "duration metadata does not match timestamps"
                path = self.resolve_from_root(row["file_path"], Path(row["storage_root"] or self.storage_root))
            except (TypeError, ValueError, OSError) as exc:
                issue = f"invalid file path or metadata: {exc}"
                path = self.storage_root
            if not issue:
                try:
                    root = Path(row["storage_root"] or self.storage_root)
                    if not root.is_dir():
                        self.logger.error("Storage root unavailable during reconciliation root=%s", root)
                        continue
                    if not path.is_file():
                        issue = "file is missing"
                    else:
                        actual_size = path.stat().st_size
                except OSError as exc:
                    issue = f"file cannot be inspected: {exc}"
            with self.lock, self._connect() as db:
                if issue:
                    status = "missing" if issue == "file is missing" else "invalid"
                    db.execute("UPDATE recordings SET status=?, file_size=0 WHERE recording_id=? AND status='complete'",
                               (status, row["recording_id"]))
                    self.logger.warning("Footage validation failed recording_id=%s reason=%s",
                                        row["recording_id"], issue)
                elif actual_size is not None and actual_size != int(row["file_size"]):
                    db.execute("UPDATE recordings SET file_size=? WHERE recording_id=? AND status='complete'",
                               (actual_size, row["recording_id"]))

    def reconcile(self) -> None:
        self._validate_complete_rows()
        with self.lock, self._connect() as db:
            interrupted = [dict(row) for row in db.execute(
                "SELECT recording_id, file_path, storage_root FROM recordings WHERE status='recording'").fetchall()]
            known = {str(row[0]) for row in db.execute("SELECT file_path FROM recordings")}
        for row in interrupted:
            try:
                path = self.resolve_from_root(row["file_path"], Path(row["storage_root"] or self.storage_root))
                size = path.stat().st_size if path.is_file() else 0
            except (OSError, ValueError):
                size = 0
            with self.lock, self._connect() as db:
                db.execute("UPDATE recordings SET status='incomplete', file_size=? WHERE recording_id=? AND status='recording'",
                           (size, row["recording_id"]))
            self.logger.warning("Recovered interrupted recording metadata recording_id=%s",
                                row["recording_id"])
        for path in self.storage_root.rglob("*.mp4"):
            if path.name.endswith(".partial.mp4") or "exports" in path.relative_to(self.storage_root).parts:
                continue
            try:
                relative = path.resolve().relative_to(self.storage_root).as_posix()
            except ValueError:
                self.logger.error("Ignoring footage symlink outside storage root: %s", path)
                continue
            if relative not in known:
                self.logger.warning("Unindexed MP4 is not eligible for deletion: %s", path)

    def query(self, device_id: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        if start.tzinfo is None or end.tzinfo is None or end <= start:
            raise ValueError("Query requires timezone-aware timestamps with start < end")
        start_text, end_text = utc_timestamp(start), utc_timestamp(end)
        with self.lock, self._connect() as db:
            rows = db.execute("""
                SELECT * FROM recordings
                WHERE device_id=? AND status='complete' AND start_time < ? AND end_time > ?
                ORDER BY start_time ASC, recording_id ASC
            """, (device_id, end_text, start_text)).fetchall()
            records = [self._row_to_dict(row) for row in rows]
        available: list[dict[str, Any]] = []
        for record in records:
            try:
                root = Path(record.get("storage_root") or self.storage_root)
                path = self.resolve_record_file(record)
                if root.is_dir() and not path.is_file():
                    self.update_status(record["recording_id"], "missing")
                    self.logger.warning("Footage file disappeared recording_id=%s path=%s",
                                        record["recording_id"], path)
                    continue
            except OSError:
                # An unavailable volume is not evidence that its footage was deleted.
                pass
            except ValueError as exc:
                self.update_status(record["recording_id"], "invalid")
                self.logger.warning("Invalid footage path recording_id=%s reason=%s",
                                    record["recording_id"], exc)
                continue
            available.append(record)
        return available

    def complete_records_oldest_first(self) -> list[dict[str, Any]]:
        with self.lock, self._connect() as db:
            rows = db.execute("""
                SELECT * FROM recordings WHERE status='complete' AND is_protected=0
                ORDER BY start_time ASC, recording_id ASC
            """).fetchall()
            return [self._row_to_dict(row) for row in rows]

    def update_status(self, recording_id: str, status: str, file_size: int = 0) -> None:
        with self.lock, self._connect() as db:
            db.execute("UPDATE recordings SET status=?, file_size=? WHERE recording_id=?",
                       (status, int(file_size), recording_id))

    def set_protected(self, recording_id: str, is_protected: bool) -> bool:
        with self.lock, self._connect() as db:
            result = db.execute("""
                UPDATE recordings SET is_protected=?
                WHERE recording_id=? AND status='complete'
            """, (int(is_protected), recording_id))
            return result.rowcount > 0

    def get(self, recording_id: str) -> dict[str, Any] | None:
        with self.lock, self._connect() as db:
            row = db.execute("SELECT * FROM recordings WHERE recording_id=?", (recording_id,)).fetchone()
            return self._row_to_dict(row) if row else None


class StorageManager:
    def __init__(self, store: FootageStore, *, max_bytes: int, cleanup_target_bytes: int,
                 logger: logging.Logger, enabled: bool = True):
        if max_bytes <= 0 or cleanup_target_bytes < 0 or cleanup_target_bytes >= max_bytes:
            raise ValueError("Storage cleanup target must be nonnegative and below the maximum")
        self.store = store
        self.root = store.storage_root
        self.max_bytes = int(max_bytes)
        self.cleanup_target_bytes = int(cleanup_target_bytes)
        self.logger = logger
        self.enabled = bool(enabled)
        self.lock = threading.Lock()
        self.config_lock = threading.Lock()
        self.usage_lock = threading.Lock()
        self.active_paths: set[Path] = set()
        self.in_use_paths: dict[Path, int] = {}
        self.deleting_paths: set[Path] = set()
        self.warning: str | None = None
        self.last_error: str | None = None
        self.last_cleanup_seconds = 0.0
        self.last_deleted_count = 0
        self._cached_used_bytes = 0
        self._cleanup_queue: queue.Queue[bool] = queue.Queue(maxsize=1)
        self._worker_stop = threading.Event()
        self._worker: threading.Thread | None = None

    def configure_limit(self, max_bytes: int, cleanup_target_bytes: int) -> None:
        if max_bytes <= 0 or cleanup_target_bytes < 0 or cleanup_target_bytes >= max_bytes:
            raise ValueError("Storage cleanup target must be nonnegative and below the maximum")
        with self.config_lock:
            self.max_bytes = int(max_bytes)
            self.cleanup_target_bytes = int(cleanup_target_bytes)

    def configure_enabled(self, enabled: bool) -> None:
        with self.config_lock:
            self.enabled = bool(enabled)

    def mark_storage_ready(self) -> None:
        self.last_error = None

    def mark_active(self, path: Path) -> None:
        with self.usage_lock:
            self.active_paths.add(path.resolve())

    def unmark_active(self, path: Path) -> None:
        with self.usage_lock:
            self.active_paths.discard(path.resolve())

    def acquire_file(self, path: Path) -> bool:
        resolved = path.resolve()
        with self.usage_lock:
            if resolved in self.deleting_paths:
                return False
            self.in_use_paths[resolved] = self.in_use_paths.get(resolved, 0) + 1
            return True

    def release_file(self, path: Path) -> None:
        resolved = path.resolve()
        with self.usage_lock:
            count = self.in_use_paths.get(resolved, 0)
            if count <= 1:
                self.in_use_paths.pop(resolved, None)
            else:
                self.in_use_paths[resolved] = count - 1

    def start_worker(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._worker_stop.clear()
        self._worker = threading.Thread(target=self._worker_loop, name="storage-cleanup", daemon=True)
        self._worker.start()

    def stop_worker(self) -> None:
        self._worker_stop.set()
        try:
            self._cleanup_queue.put_nowait(False)
        except queue.Full:
            pass
        if self._worker:
            self._worker.join(timeout=5)

    def request_cleanup(self) -> bool:
        if not self.enabled:
            return False
        return self.request_inventory()

    def request_inventory(self) -> bool:
        try:
            self._cleanup_queue.put_nowait(True)
            return True
        except queue.Full:
            return False

    def _worker_loop(self) -> None:
        while not self._worker_stop.is_set():
            try:
                self._cleanup_queue.get(timeout=1)
            except queue.Empty:
                continue
            if self._worker_stop.is_set():
                break
            try:
                self.cleanup()
            except Exception as exc:
                self.last_error = str(exc)
                self.logger.exception("Storage cleanup worker failed")

    def set_root(self, root: Path) -> None:
        with self.lock:
            self.root = root.resolve()
            self.store.storage_root = self.root
            self._cached_used_bytes = 0

    def wait_for_idle(self) -> None:
        with self.lock:
            return

    def _physical_files(self) -> list[Path]:
        files = []
        for path in self.root.rglob("*"):
            try:
                if path.is_file() and not path.is_symlink():
                    files.append(path)
            except OSError:
                continue
        return files

    def used_bytes(self) -> int:
        total = 0
        for path in self._physical_files():
            try:
                total += path.stat().st_size
            except OSError:
                pass
        return total

    def cleanup(self) -> dict[str, Any]:
        with self.config_lock:
            enabled = self.enabled
            max_bytes = self.max_bytes
            cleanup_target_bytes = self.cleanup_target_bytes
        if not enabled:
            try:
                self._cached_used_bytes = self.used_bytes()
            except OSError as exc:
                self.last_error = str(exc)
                self.warning = f"Storage is unavailable: {exc}"
            return {"used_bytes": self._cached_used_bytes, "max_bytes": max_bytes,
                    "cleanup_target_bytes": cleanup_target_bytes,
                    "warning": self.warning, "deleted_recording_ids": [], "disabled": True}
        started = time.monotonic()
        with self.lock:
            try:
                used = self.used_bytes()
            except OSError as exc:
                self.last_error = str(exc)
                self.warning = f"Storage is unavailable: {exc}"
                self.logger.exception(self.warning)
                return {**self.status(), "warning": self.warning, "deleted_recording_ids": []}
            deleted: list[str] = []
            used_before = used
            if used > max_bytes:
                for record in self.store.complete_records_oldest_first():
                    if used <= cleanup_target_bytes:
                        break
                    try:
                        path = self.store.resolve_record_file(record)
                        if Path(record.get("storage_root") or self.root).resolve() != self.root.resolve():
                            continue
                        with self.usage_lock:
                            in_use = path in self.active_paths or self.in_use_paths.get(path, 0) > 0
                            if not in_use:
                                self.deleting_paths.add(path)
                        if in_use:
                            continue
                        try:
                            if not path.is_relative_to(self.root) or path.is_symlink():
                                self.store.update_status(record["recording_id"], "invalid")
                                self.logger.warning("Skipping unsafe footage path recording_id=%s path=%s",
                                                    record["recording_id"], path)
                                continue
                            if not path.is_file():
                                self.store.update_status(record["recording_id"], "missing")
                                continue
                            size = path.stat().st_size
                            path.unlink()
                            self.store.update_status(record["recording_id"], "deleted")
                            used -= size
                            deleted.append(record["recording_id"])
                            self.logger.info("Storage cleanup removed recording_id=%s path=%s",
                                             record["recording_id"], path)
                        finally:
                            with self.usage_lock:
                                self.deleting_paths.discard(path)
                    except (OSError, ValueError) as exc:
                        self.logger.exception("Could not clean footage recording_id=%s: %s",
                                              record["recording_id"], exc)
                used = self.used_bytes()
                if used > cleanup_target_bytes:
                    self.warning = (
                        f"Storage remains at {used} bytes after cleanup; target is "
                        f"{cleanup_target_bytes}. Protected, active, partial, or unindexed files may prevent cleanup."
                    )
                    self.logger.error(self.warning)
                else:
                    self.warning = None
            else:
                if used <= cleanup_target_bytes:
                    self.warning = None
            self._cached_used_bytes = used
            self.last_cleanup_seconds = time.monotonic() - started
            self.last_deleted_count = len(deleted)
            self.last_error = None
            self.logger.info("Storage cleanup finished duration=%.3fs deleted=%d used_before=%d used_after=%d",
                             self.last_cleanup_seconds, len(deleted), used_before, used)
            return {"used_bytes": used, "max_bytes": max_bytes,
                    "cleanup_target_bytes": cleanup_target_bytes,
                    "warning": self.warning, "deleted_recording_ids": deleted}

    def status(self) -> dict[str, Any]:
        with self.config_lock:
            max_bytes = self.max_bytes
            cleanup_target_bytes = self.cleanup_target_bytes
            enabled = self.enabled
        try:
            free = shutil.disk_usage(self.root).free
            available = True
        except OSError as exc:
            free = None
            available = False
            self.last_error = self.last_error or str(exc)
        disk_warning = None
        if available and free <= 0:
            disk_warning = "Storage volume is full"
        elif available and free < 1024 ** 3:
            disk_warning = "Storage volume has less than 1 GiB free"
        return {"used_bytes": self._cached_used_bytes, "max_bytes": max_bytes,
                "cleanup_target_bytes": cleanup_target_bytes, "warning": self.warning,
                "management_enabled": enabled, "cleanup_enabled": enabled,
                "disk_free_bytes": free, "storage_available": available,
                "storage_ready": available and self.last_error is None,
                "disk_warning": disk_warning,
                "last_error": self.last_error, "last_cleanup_seconds": self.last_cleanup_seconds,
                "last_deleted_count": self.last_deleted_count}


def build_export_command(ffmpeg: str, records: list[dict[str, Any]],
                         start: datetime, end: datetime, output_path: Path) -> list[str]:
    """Create a re-encoded MP4 preserving requested gaps as black video."""
    if not records:
        raise ValueError("No footage overlaps the requested interval")
    cursor = start.astimezone(UTC)
    parts: list[tuple[str, Any]] = []
    inputs: list[dict[str, Any]] = []
    for record in records:
        clip_start = max(cursor, parse_timestamp(record["start_time"]), start.astimezone(UTC))
        clip_end = min(parse_timestamp(record["end_time"]), end.astimezone(UTC))
        if clip_end <= clip_start:
            continue
        if clip_start > cursor:
            parts.append(("gap", (clip_start - cursor).total_seconds()))
        segment_start = parse_timestamp(record["start_time"])
        input_index = len(inputs)
        inputs.append(record)
        parts.append(("clip", (input_index, (clip_start - segment_start).total_seconds(),
                                (clip_end - segment_start).total_seconds())))
        cursor = max(cursor, clip_end)
    if cursor < end.astimezone(UTC):
        parts.append(("gap", (end.astimezone(UTC) - cursor).total_seconds()))
    if not any(kind == "clip" for kind, _ in parts):
        raise ValueError("No playable footage overlaps the requested interval")

    command = [ffmpeg, "-y", "-hide_banner", "-nostdin", "-loglevel", "error"]
    for record in inputs:
        command.extend(["-i", record["_absolute_path"]])
    filters: list[str] = []
    labels: list[str] = []
    clip_index = gap_index = 0
    for kind, value in parts:
        if kind == "clip":
            input_index, clip_from, clip_to = value
            label = f"v{clip_index}"
            filters.append(
                f"[{input_index}:v:0]trim=start={clip_from:.6f}:end={clip_to:.6f},"
                f"setpts=PTS-STARTPTS,scale=640:480:force_original_aspect_ratio=decrease,"
                f"pad=640:480:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p[{label}]"
            )
            clip_index += 1
        else:
            label = f"gap{gap_index}"
            filters.append(f"color=c=black:s=640x480:r=20:d={value:.6f},format=yuv420p[{label}]")
            gap_index += 1
        labels.append(f"[{label}]")
    filters.append(f"{''.join(labels)}concat=n={len(labels)}:v=1:a=0[outv]")
    command.extend([
        "-filter_complex", ";".join(filters), "-map", "[outv]", "-an",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart", "-f", "mp4", str(output_path),
    ])
    return command


class ExportManager:
    def __init__(self, store: FootageStore, storage_root: Path, *, ffmpeg: str, ffprobe: str,
                 logger: logging.Logger, storage_manager: StorageManager | None = None):
        self.store = store
        self.storage_root = storage_root.resolve()
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.logger = logger
        self.storage_manager = storage_manager
        self.export_dir = self.storage_root / "exports"

    def export(self, device_id: str, start: datetime, end: datetime) -> dict[str, Any]:
        records = self.store.query(device_id, start, end)
        for record in records:
            path = self.store.resolve_record_file(record)
            record["_absolute_path"] = str(path)
        export_id = str(uuid.uuid4())
        self.export_dir.mkdir(parents=True, exist_ok=True)
        final_path = self.export_dir / f"{export_id}.mp4"
        partial_path = self.export_dir / f"{export_id}.partial.mp4"
        command = build_export_command(self.ffmpeg, records, start, end, partial_path)
        leased_paths = [Path(record["_absolute_path"]) for record in records]
        acquired_paths: list[Path] = []
        try:
            if self.storage_manager:
                for path in leased_paths:
                    if not self.storage_manager.acquire_file(path):
                        raise RuntimeError("A footage segment is being removed by storage cleanup")
                    acquired_paths.append(path)
                    if not path.is_file():
                        raise RuntimeError(f"Footage segment is no longer available: {path.name}")
            subprocess.run(command, capture_output=True, text=True, check=True,
                           timeout=max(180, (end - start).total_seconds() * 4),
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            probe = subprocess.run(
                [self.ffprobe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(partial_path)],
                capture_output=True, text=True, timeout=30, check=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            duration = float(probe.stdout.strip())
            expected_duration = (end - start).total_seconds()
            if (duration <= 0 or abs(duration - expected_duration) > 1.0
                    or not partial_path.is_file() or partial_path.stat().st_size == 0):
                raise RuntimeError("FFprobe did not confirm a playable export")
            os.replace(partial_path, final_path)
        except Exception:
            if partial_path.exists():
                self.logger.warning("Removing failed export temporary file: %s", partial_path)
                partial_path.unlink(missing_ok=True)
            self.logger.exception("Footage export failed device_id=%s", device_id)
            raise
        finally:
            if self.storage_manager:
                for path in acquired_paths:
                    self.storage_manager.release_file(path)
        return {"export_id": export_id, "file": str(final_path), "duration": duration,
                "size_bytes": final_path.stat().st_size,
                "download_url": f"/api/exports/{export_id}"}


def localize_record(record: dict[str, Any]) -> dict[str, Any]:
    result = dict(record)
    result.pop("_absolute_path", None)
    result["start_time_local"] = parse_timestamp(record["start_time"]).astimezone().isoformat()
    result["end_time_local"] = parse_timestamp(record["end_time"]).astimezone().isoformat()
    return result


def timeline_gaps(records: list[dict[str, Any]], start: datetime, end: datetime) -> list[dict[str, str]]:
    cursor = start.astimezone(UTC)
    gaps = []

    def gap_result(gap_start: datetime, gap_end: datetime) -> dict[str, str]:
        return {
            "start_time_utc": utc_timestamp(gap_start),
            "end_time_utc": utc_timestamp(gap_end),
            "start_time_local": gap_start.astimezone().isoformat(),
            "end_time_local": gap_end.astimezone().isoformat(),
        }

    for record in records:
        clip_start = max(cursor, parse_timestamp(record["start_time"]), start.astimezone(UTC))
        clip_end = min(parse_timestamp(record["end_time"]), end.astimezone(UTC))
        if clip_start > cursor:
            gaps.append(gap_result(cursor, clip_start))
        cursor = max(cursor, clip_end)
    if cursor < end.astimezone(UTC):
        gaps.append(gap_result(cursor, end.astimezone(UTC)))
    return gaps
