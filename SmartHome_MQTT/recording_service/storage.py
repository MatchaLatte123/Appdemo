from __future__ import annotations

import logging
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


class StorageManager:
    """Tracks finalized segments and enforces a byte quota without touching active files."""

    def __init__(self, database_path: Path, recording_root: Path, max_storage_bytes: int):
        self.database_path = Path(database_path)
        self.recording_root = Path(recording_root)
        self.max_storage_bytes = int(max_storage_bytes)
        self.log = logging.getLogger("RECORDER")
        self._lock = threading.RLock()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.recording_root.mkdir(parents=True, exist_ok=True)
        self._initialize_database()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.database_path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _initialize_database(self) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS recordings (
                    id TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL,
                    start_time TEXT NOT NULL,
                    end_time TEXT NOT NULL,
                    duration REAL NOT NULL,
                    file_path TEXT NOT NULL UNIQUE,
                    file_size INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'ready',
                    created_at TEXT NOT NULL
                )"""
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_recordings_device_start "
                "ON recordings(device_id, start_time DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_recordings_status_start "
                "ON recordings(status, start_time)"
            )

    def record_segment(
        self,
        device_id: str,
        file_path: Path,
        start_time: datetime,
        duration: float,
    ) -> bool:
        path = Path(file_path).resolve()
        if not path.is_file():
            return False
        size = path.stat().st_size
        if size <= 0:
            return False
        start = start_time.astimezone().replace(microsecond=0)
        end = datetime.fromtimestamp(start.timestamp() + max(0.0, duration)).astimezone()
        with self._lock, closing(self._connect()) as conn, conn:
            cur = conn.execute(
                """INSERT OR IGNORE INTO recordings
                   (id, device_id, start_time, end_time, duration, file_path,
                    file_size, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'ready', ?)""",
                (
                    str(uuid.uuid4()),
                    device_id,
                    start.isoformat(),
                    end.isoformat(),
                    float(duration),
                    str(path),
                    size,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            return cur.rowcount > 0

    def remove_missing_rows(self) -> None:
        with self._lock, closing(self._connect()) as conn, conn:
            rows = conn.execute("SELECT id, file_path FROM recordings").fetchall()
            missing = [row["id"] for row in rows if not Path(row["file_path"]).is_file()]
            if missing:
                conn.executemany("DELETE FROM recordings WHERE id = ?", [(item,) for item in missing])

    def enforce_quota(self, protected_paths: set[Path] | None = None) -> int:
        protected = {Path(p).resolve() for p in (protected_paths or set())}
        files = [p for p in self.recording_root.rglob("*.mp4") if p.is_file()]
        total = sum(p.stat().st_size for p in files)
        if total <= self.max_storage_bytes:
            return 0

        removed = 0
        # Tracked finalized segments are evicted by recording start time.
        with self._lock, closing(self._connect()) as conn, conn:
            rows = conn.execute(
                "SELECT id, file_path, file_size FROM recordings "
                "WHERE status = 'ready' ORDER BY start_time ASC"
            ).fetchall()
            known = {Path(row["file_path"]).resolve() for row in rows}
            for row in rows:
                path = Path(row["file_path"]).resolve()
                if total <= self.max_storage_bytes:
                    break
                if path in protected or not path.exists():
                    continue
                size = path.stat().st_size
                path.unlink(missing_ok=True)
                conn.execute("DELETE FROM recordings WHERE id = ?", (row["id"],))
                total -= size
                removed += 1
                self.log.info("Recording evicted: %s", path)

            # Include completed MP4 files left by a crash before SQLite commit.
            # An actively written file is protected by its caller.
            if total > self.max_storage_bytes:
                orphans = sorted(
                    (p for p in files if p.resolve() not in known and p.resolve() not in protected),
                    key=lambda p: p.stat().st_mtime,
                )
                for path in orphans:
                    if total <= self.max_storage_bytes:
                        break
                    if path.exists():
                        size = path.stat().st_size
                        path.unlink(missing_ok=True)
                        total -= size
                        removed += 1
                        self.log.info("Unindexed recording evicted: %s", path)
        return removed

    def usage_bytes(self) -> int:
        return sum(p.stat().st_size for p in self.recording_root.rglob("*.mp4") if p.is_file())


def recently_written_paths(root: Path, within_seconds: float = 3.0) -> set[Path]:
    """Protect files FFmpeg is actively appending to (segment list reports closed files)."""
    cutoff = time.time() - within_seconds
    return {
        path.resolve()
        for path in Path(root).rglob("*.mp4")
        if path.is_file() and path.stat().st_mtime >= cutoff
    }
