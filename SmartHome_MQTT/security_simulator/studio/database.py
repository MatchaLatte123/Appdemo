"""Separate SQLite storage for validation sessions and observations."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3


class Database:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.open() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, started REAL NOT NULL, body TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS observations (
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    stamp REAL NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS observations_session ON observations(session_id, stamp);
            """)

    @contextmanager
    def open(self):
        db = sqlite3.connect(self.path, timeout=4)
        try:
            db.execute("PRAGMA foreign_keys=ON")
            with db:
                yield db
        finally:
            db.close()

    def save(self, session, observations=()):
        with self.open() as db:
            db.execute("""INSERT INTO sessions VALUES (?, ?, ?) ON CONFLICT(id)
                          DO UPDATE SET body=excluded.body""",
                       (session["id"], session["started"], json.dumps(session, allow_nan=False)))

            db.executemany("INSERT INTO observations VALUES (?, ?, ?, ?)",
                           ((session["id"], stamp, kind, json.dumps(sample, allow_nan=False))
                            for stamp, kind, sample in observations))

    def append(self, session_id, stamp, kind, sample):
        with self.open() as db:
            db.execute("INSERT INTO observations VALUES (?, ?, ?, ?)",
                       (session_id, stamp, kind, json.dumps(sample, allow_nan=False)))

    def history(self):
        with self.open() as db:
            rows = db.execute("SELECT body FROM sessions ORDER BY started DESC LIMIT 200").fetchall()
        return [json.loads(row[0]) for row in rows]

    def report(self, session_id):
        with self.open() as db:
            row = db.execute("SELECT body FROM sessions WHERE id=?", (session_id,)).fetchone()
            entries = db.execute("SELECT stamp,kind,body FROM observations WHERE session_id=? ORDER BY stamp,rowid",
                                 (session_id,)).fetchall()
        if row is None:
            raise KeyError("Unknown session")
        return {"session": json.loads(row[0]), "observations": [
            {"stamp": stamp, "kind": kind, "data": json.loads(body)} for stamp, kind, body in entries]}
