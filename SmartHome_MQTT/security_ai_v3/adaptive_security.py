"""Adaptive security runtime on the existing AI Server."""
from collections import Counter, deque
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import secrets
import sqlite3

import psutil
from threading import Event, Lock, RLock, Thread
from time import time
from uuid import uuid4

from .adaptive_evidence import correlate
from .adaptive_profiles import POLICY, detect, eligible, exclusion, fit, grouped, load_artifact, partition, profile_key, type_key
from .adaptive_telemetry import FEATURES, COUNT_FIELDS, Identity, SCHEMA, SPAN, Telemetry, recorder_snapshot

ROOT = Path(__file__).resolve().parents[1]
STATE_NAME = "AdaptiveSecurity"
RETENTION_DAYS = 30
MAX_SESSION_SECONDS = 4 * 3600
MAX_REVIEW_SESSIONS = 64
MAX_SESSION_SAMPLES = 48_000
SERVICE_ROLES = ("MQTT_BROKER", "VIDEO_SERVER")
DRIFT_WINDOWS = 24
OPEN_WINDOWS = 3
RECOVERY_WINDOWS = 4
COOLDOWN_SECONDS = 60
LOG = logging.getLogger(__name__)


class Conflict(ValueError):
    pass


def state_dir():
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "state")
    return base / "SmartHomeAI" / STATE_NAME


def directions():
    units = {"packet_rate": "frames/s", "byte_rate": "bytes/s", "connection_rate": "initial SYN/s",
             "rst_rate": "frames/s", "publish_rate": "messages/s", "subscribe_rate": "messages/s",
             "http_rate": "requests/s", "clients": "clients", "message_rate": "messages/s",
             "media_age": "seconds", "camera_fps": "frames/s", "packet_rate_change": "frames/s"}
    return {"schema": SCHEMA, "features": {
        name: {"type": "signed number or null" if name == "packet_rate_change" else "nonnegative number or null", "unit": units.get(name, "boolean 0/1"),
               "profiles": [role for role, names in FEATURES.items() if name in names],
               "source": "TShark metadata" if name in COUNT_FIELDS or name == "packet_rate_change" else
               "Broker $SYS" if name in ("clients", "message_rate", "broker_health") else "Recording Service health",
               "formula": f"{COUNT_FIELDS[name]} observed / 5" if name in COUNT_FIELDS else
               {"packet_rate_change": "current 5s packet rate minus preceding contiguous 5s rate",
                "clients": "latest fresh client gauge", "broker_health": "telemetry connected flag",
                "message_rate": "delta of received-message counter / elapsed seconds",
                "media_age": "seconds since media, max across observed cameras for service",
                "camera_fps": "recorder FPS gauge per camera; median across currently recording cameras per service", "ffmpeg_health": "available/running flag",
                "recorder_error": "recent camera error or current storage error"}[name],
               "window": "UTC-aligned 5 seconds; 60s trailing mean of 12 consecutive non-null windows",
               "missing": "null when unobserved; fit-time median imputation of optional inputs only",
               "quality": "Capture reports OBSERVED/UNVERIFIED/LOSS/UNAVAILABLE; null fields are MISSING"}
        for name in sorted(set().union(*FEATURES.values()))}}


class BrokerTelemetry:
    """Optional read-only MQTT $SYS observer, disabled by default."""
    TOPICS = {"$SYS/broker/clients/connected": "clients", "$SYS/broker/messages/received": "raw_messages"}

    def __init__(self, config):
        self.config = config
        self.client = None
        self.lock = Lock()
        self.status = "UNCONFIGURED"
        self.values = {}
        self.raw = None

    def connected(self, client, _userdata, _flags, reason, _properties):
        with self.lock:
            self.status = "DEGRADED" if reason.is_failure else "HEALTHY"
            self.values.clear()
            self.raw = None
        if not reason.is_failure:
            client.subscribe([(topic, 0) for topic in self.TOPICS])

    def disconnected(self, _client, _userdata, _flags, _reason, _properties):
        with self.lock:
            self.status = "DEGRADED"
            self.values.clear()
            self.raw = None

    def message(self, _client, _userdata, msg):
        key = self.TOPICS.get(msg.topic)
        if not key or len(msg.payload) > 20 or not msg.payload.isdigit():
            return
        value, stamp = int(msg.payload), time()
        with self.lock:
            if key == "raw_messages":
                if self.raw and stamp > self.raw[1] and value >= self.raw[0]:
                    self.values["message_rate"] = ((value - self.raw[0]) / (stamp - self.raw[1]), stamp)
                else:
                    self.values.pop("message_rate", None)
                self.raw = (value, stamp)
            else:
                self.values[key] = (value, stamp)

    @contextmanager
    def start(self):
        if self.config:
            try:
                self.start_client()
            except (ImportError, OSError, TypeError, ValueError, KeyError):
                self.status = "UNAVAILABLE"
                LOG.warning("Optional MQTT service telemetry unavailable")
        try:
            yield
        finally:
            if self.client:
                self.client.disconnect()
                self.client.loop_stop()

    def start_client(self):
        from paho.mqtt.client import Client, CallbackAPIVersion
        self.status = "UNAVAILABLE"
        client = Client(CallbackAPIVersion.VERSION2)
        username_env = self.config.get("username_env")
        password_env = self.config.get("password_env")
        if username_env:
            client.username_pw_set(os.environ[username_env], os.environ[password_env] if password_env else None)
        if self.config.get("tls"):
            client.tls_set()
        client.on_connect, client.on_disconnect, client.on_message = self.connected, self.disconnected, self.message
        client.connect_async(self.config["host"], int(self.config.get("port", 1883)), 30)
        client.loop_start()
        self.client = client

    def snapshot(self, now):
        with self.lock:
            data = {key: value for key, (value, stamp) in self.values.items() if now - stamp <= 30}
            mode = f"clients:{min(int(data['clients']) // 4, 8)}" if "clients" in data else "unknown"
            return {**data, "status": self.status, "mode": mode, "broker_health":
                    {"HEALTHY": 1, "DEGRADED": 0}.get(self.status)}


class Store:
    def __init__(self, directory):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.token = self.read_token(directory / "operator.token")
        self.db = sqlite3.connect(directory / "telemetry.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA busy_timeout=5000;
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, start INTEGER NOT NULL, stop INTEGER, status TEXT NOT NULL,
                note TEXT NOT NULL, review TEXT, schema TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_learning_session ON sessions(status) WHERE status = 'LEARNING_NORMAL';
            CREATE TABLE IF NOT EXISTS samples (
                session TEXT NOT NULL, stamp INTEGER NOT NULL, subject TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY (session, stamp, subject)
            );
            CREATE TABLE IF NOT EXISTS learning_exclusions (
                session TEXT NOT NULL, stamp INTEGER NOT NULL, subject TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY (session, stamp, subject)
            );
            CREATE TABLE IF NOT EXISTS versions (
                id TEXT PRIMARY KEY, created INTEGER NOT NULL, status TEXT NOT NULL, digest TEXT,
                report TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS incidents (
                id TEXT PRIMARY KEY, stamp INTEGER NOT NULL, subject TEXT NOT NULL, status TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS incidents_recent ON incidents(stamp);
            CREATE INDEX IF NOT EXISTS incidents_subject ON incidents(subject, stamp DESC);
        """)

    def close_interrupted_sessions(self):
        with self.db:
            self.db.execute("UPDATE sessions SET stop=?, status='PENDING_REVIEW' WHERE status='LEARNING_NORMAL'", (int(time()),))

    @staticmethod
    def read_token(path):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w", encoding="ascii") as file:
                file.write(secrets.token_urlsafe(32))
        except FileExistsError:
            pass
        value = path.read_text(encoding="ascii").strip()
        if len(value) < 32 or not value.replace("-", "").replace("_", "").isalnum():
            raise ValueError("Invalid local operator token")
        return value

    def purge(self, now):
        cutoff = now - 86400 * RETENTION_DAYS
        with self.db:
            self.db.execute("DELETE FROM samples WHERE session IN (SELECT id FROM sessions WHERE stop < ?)", (cutoff,))
            self.db.execute("DELETE FROM learning_exclusions WHERE session IN (SELECT id FROM sessions WHERE stop < ?)", (cutoff,))
            self.db.execute("DELETE FROM sessions WHERE stop < ?", (cutoff,))
            self.db.execute("DELETE FROM incidents WHERE stamp < ? AND status='RECOVERED'", (cutoff,))

    def sessions(self):
        return [dict(row) for row in self.db.execute("""
            SELECT s.*, (SELECT count(*) FROM samples WHERE session=s.id) AS samples,
                     (SELECT count(*) FROM learning_exclusions WHERE session=s.id) AS exclusions,
                     NOT EXISTS (SELECT 1 FROM samples WHERE session=s.id
                                 AND json_extract(payload, '$.audit_version') IS NULL) AS audit_complete
            FROM sessions s ORDER BY start DESC LIMIT ?
        """, (MAX_REVIEW_SESSIONS,))]

    def review(self, session):
        row = self.db.execute("SELECT * FROM sessions WHERE id=?", (session,)).fetchone()
        if row is None:
            raise Conflict("SESSION_NOT_FOUND")
        buckets = {}
        quality, coverage, missing = Counter(), Counter(), Counter()
        audit_complete = True
        window_count = 0
        for _, sample in self.learning_records(session):
            window_count += 1
            audit_complete &= sample.get("audit_version") == 1
            role, mode = sample["role"], sample["mode"]
            role_data = buckets.setdefault((sample["key"], role, mode), {
                "subject": sample["key"], "profile_key": profile_key(sample),
                "service": sample["service"], "role": role, "mode": mode,
                "eligible": 0, "excluded": 0, "quality": Counter(), "missing_features": Counter(),
                "feature_observations": Counter(), "exclusion_reasons": Counter(), "service_health": Counter(),
                "capture_evidence": Counter(), "drop_status": Counter(), "packets_observed": 0,
                "packet_count_missing_windows": 0, "start": sample["start"], "end": sample["end"]})
            reason = exclusion(sample)
            role_data["excluded" if reason else "eligible"] += 1
            role_data["quality"][sample["quality"]] += 1
            role_data["service_health"][sample["service_state"]] += 1
            role_data["start"] = min(role_data["start"], sample["start"])
            role_data["end"] = max(role_data["end"], sample["end"])
            if reason:
                role_data["exclusion_reasons"][reason] += 1
            absent = sample.get("missing_features", [k for k, v in sample["values"].items() if v is None])
            role_data["feature_observations"].update(sample["values"].keys())
            role_data["missing_features"].update(absent)
            capture = sample["capture"]
            role_data["capture_evidence"].update(k for k in ("truncated", "decoder_gaps", "decoder_errors",
                                                             "overflow_or_late", "kernel_drops") if capture.get(k))
            role_data["drop_status"][capture.get("drop_status", "UNAVAILABLE")] += 1
            packets = capture.get("packets_observed")
            if packets is None:
                role_data["packet_count_missing_windows"] += 1
            else:
                role_data["packets_observed"] += packets
            quality[sample["quality"]] += 1
            if not reason:
                coverage[role] += 1
            missing.update(absent)
        groups = list(buckets.values())
        for group in groups:
            count = group["eligible"] + group["excluded"]
            group["feature_completeness"] = {
                name: 1 - group["missing_features"][name] / count
                for name in group.pop("feature_observations")}
        return {**dict(row), "coverage": coverage, "quality": quality, "missing_features": missing,
                "audit_complete": bool(window_count) and audit_complete,
                "eligible_windows": sum(g["eligible"] for g in groups),
                "excluded_windows": sum(g["excluded"] for g in groups),
                "start_utc": datetime.fromtimestamp(row["start"], timezone.utc).isoformat(),
                "end_utc": datetime.fromtimestamp(row["stop"], timezone.utc).isoformat() if row["stop"] else None,
                "duration_seconds": (row["stop"] or int(time())) - row["start"],
                "normal_label_provenance": "OPERATOR_ACCEPTED" if row["status"] == "ACCEPTED" else row["status"],
                "groups": groups}

    def learning_records(self, session=None):
        if session is None:
            condition = "s.status='ACCEPTED' AND s.schema=? AND x.stamp >= ?"
            args = (SCHEMA, int(time()) - RETENTION_DAYS * 86400)
        else:
            condition = "s.id=?"
            args = (session,)
        rows = self.db.execute(f"""
            SELECT x.session, x.payload FROM samples x JOIN sessions s ON s.id=x.session WHERE {condition}
            UNION ALL
            SELECT x.session, x.payload FROM learning_exclusions x JOIN sessions s ON s.id=x.session WHERE {condition}
        """, (*args, *args))
        return [(row[0], json.loads(row[1])) for row in rows]

    def artifact(self, version):
        return self.directory / f"{version}.joblib"

    def save_incident(self, incident):
        self.db.execute("INSERT OR REPLACE INTO incidents VALUES (?, ?, ?, ?, ?)",
                        (incident["id"], incident["updated_at"], incident["subject"],
                         incident["status"], json.dumps(incident, allow_nan=False)))

    def incidents(self, limit=30):
        return [json.loads(row[0]) for row in self.db.execute(
            "SELECT payload FROM incidents ORDER BY (status='ACTIVE') DESC, stamp DESC LIMIT ?", (limit,))]

    def close(self):
        self.db.close()


class Adaptive:
    def __init__(self, settings, monitor, video_config, video_endpoint, directory=None):
        self.lock = RLock()
        self.fitting = Lock()
        self.monitor = monitor
        self.xgboost_shadow = None
        self.video_endpoint = video_endpoint
        self.settings = settings
        self.directory = directory or state_dir()
        self.store = Store(self.directory)
        self.registry_path = ROOT / settings.get("registry", "appdemo/device_profiles.json")
        self.binding_path = ROOT / settings["identity_bindings"] if settings.get("identity_bindings") else None
        self.cameras = video_config.get("cameras", ())
        self.identity_signature = None
        self.identity_status = "UNCONFIGURED"
        self.telemetry = Telemetry(Identity({}, self.cameras))
        self.refresh_identity()
        self.broker = BrokerTelemetry(settings.get("broker_sys"))
        self.latest = {}
        self.anomaly_history = {}
        self.monitoring = {"status": "STARTING"}
        self.profiles = {}
        self.version = None
        self.profile_error = None
        self.previous_capture = None
        current = self.store.db.execute("SELECT id, digest FROM versions WHERE status='ACTIVE'").fetchone()
        if current:
            try:
                self.profiles = load_artifact(self.store.artifact(current["id"]), current["digest"])
                self.version = current["id"]
            except (ValueError, KeyError, OSError, ImportError, EOFError):
                self.profile_error = "ARTIFACT_INCOMPATIBLE"
        self.active = {row["subject"]: json.loads(row["payload"]) for row in self.store.db.execute(
            "SELECT subject, payload FROM incidents WHERE status='ACTIVE'")}
        self.counters = {}
        self.day = None
        self.coverage = Counter()
        self.readiness_cache = None
        self.drift = {}
        self.process_metrics = psutil.Process()

    @staticmethod
    def read(path, default):
        path = ROOT / path
        return json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else default

    def refresh_identity(self):
        paths = (self.registry_path, self.binding_path)
        signature = tuple(p.stat().st_mtime_ns if p and p.exists() else None for p in paths)
        if signature == self.identity_signature:
            return
        registry = self.read(self.registry_path, {})
        bindings = self.read(self.binding_path, []) if self.binding_path else []
        self.telemetry.identity = Identity(registry, self.cameras, bindings)
        self.identity_signature = signature
        self.identity_status = "BINDINGS_CONFIGURED" if self.binding_path and signature[1] else "UNCONFIGURED"

    @property
    def token(self):
        return self.store.token

    def run(self, stop):
        while not stop.is_set():
            started = time()
            try:
                try:
                    self.refresh_identity()
                except (OSError, TypeError, ValueError, KeyError, AttributeError):
                    self.telemetry.identity = Identity({}, self.cameras)
                    self.identity_status = "BINDINGS_INVALID"
                    self.identity_signature = None
                capture = self.monitor.snapshot()
                signature = (capture.get("restarts"), capture.get("capture_session_ids"))
                if self.previous_capture is not None and self.previous_capture != signature:
                    capture = {**capture, "status": "RETRYING",
                               "branch_status": {"MQTT": "RETRYING", "Video": "RETRYING"}}
                self.previous_capture = signature
                video = recorder_snapshot(*self.video_endpoint, started)
                broker = self.broker.snapshot(started)
                now = time()
                with self.lock:
                    if self.day != int(now // 86400):
                        self.store.purge(int(now))
                        self.readiness_cache = None
                        self.day = int(now // 86400)
                    for batch in self.telemetry.drain(now, capture, video, broker):
                        self.process(batch)
                    self.monitoring = {"status": "MONITORING", "capture_status": capture["status"],
                                       "capture_profile": capture.get("capture_profile", "SHARED"),
                                       "capture_branches": capture.get("branch_status"),
                                       "identity_source": self.identity_status,
                                       "broker": broker["status"], "video": video["status"],
                                       "updated_at": now, "tick_ms": round((time() - started) * 1000, 1),
                                       "ai_process_rss_bytes": self.process_metrics.memory_info().rss,
                                       "coverage": dict(self.coverage)}
            except (KeyError, ValueError, TypeError, OSError, sqlite3.Error, psutil.Error):
                LOG.exception("Adaptive telemetry tick failed")
                with self.lock:
                    self.monitoring = {"status": "ERROR"}
            stop.wait(max(.1, 2 - (time() - started)))

    @contextmanager
    def start(self):
        stop = Event()
        self.store.close_interrupted_sessions()
        branches = getattr(self.monitor, "branches", {"shared": self.monitor})
        for branch in branches.values():
            branch.adaptive = self.telemetry
        try:
            with self.broker.start():
                thread = Thread(target=self.run, args=(stop,), daemon=True)
                thread.start()
                try:
                    yield self
                finally:
                    stop.set()
                    thread.join()
        finally:
            self.store.close()

    def transition_incident(self, row, evidence, *, tested):
        subject, stamp = row["key"], row["end"]
        last, streak, clean = self.counters.get(subject, (0, 0, 0))
        consecutive = last == stamp - SPAN
        incident = self.active.get(subject)
        if not evidence:
            clean_observed = row["quality"] in ("OBSERVED", "UNVERIFIED") and (
                tested or row["service_state"] == "HEALTHY" and incident
                and incident["behavior"] == "SERVICE_DEGRADATION")
            clean = (clean + 1 if consecutive else 1) if clean_observed else 0
            self.counters[subject] = stamp, 0, clean
            if incident and clean >= RECOVERY_WINDOWS:
                incident.update(status="RECOVERED", updated_at=stamp, recovered_at=stamp)
                self.store.save_incident(incident)
                del self.active[subject]
            return
        streak = streak + 1 if consecutive else 1
        self.counters[subject] = stamp, streak, 0
        if streak < OPEN_WINDOWS and not incident:
            return
        if not incident:
            previous = self.store.db.execute(
                "SELECT status, payload FROM incidents WHERE subject=? ORDER BY stamp DESC LIMIT 1", (subject,)).fetchone()
            if previous and previous["status"] == "RECOVERED":
                saved = json.loads(previous["payload"])
                if stamp - saved["recovered_at"] <= COOLDOWN_SECONDS:
                    incident = saved
                    incident.pop("recovered_at", None)
        if not incident:
            incident = {"id": uuid4().hex, "subject": subject,
                        "opened_at": stamp - (OPEN_WINDOWS - 1) * SPAN, "windows": 0}
        incident.update(evidence, status="ACTIVE", updated_at=stamp, profile_version=self.version,
                        duration_seconds=stamp - incident["opened_at"], windows=incident["windows"] + 1,
                        consecutive_anomalous_windows=streak)
        self.active[subject] = incident
        self.store.save_incident(incident)

    def process(self, batch):
        if not batch:
            return
        with self.lock, self.store.db:
            session = self.store.db.execute("SELECT * FROM sessions WHERE status='LEARNING_NORMAL'").fetchone()
            window_end = batch[0]["end"]
            if session and (window_end - session["start"] > MAX_SESSION_SECONDS or
                            self.store.db.execute("SELECT count(*) FROM samples WHERE session=?", (session["id"],)).fetchone()[0]
                            + self.store.db.execute("SELECT count(*) FROM learning_exclusions WHERE session=?", (session["id"],)).fetchone()[0]
                            >= MAX_SESSION_SAMPLES):
                self.store.db.execute("UPDATE sessions SET status='PENDING_REVIEW', stop=? WHERE id=?",
                                      (window_end, session["id"]))
                session = None
            services = {r["service"]: r for r in batch if r["role"] in ("MQTT_BROKER", "VIDEO_SERVER")}
            current, evidence_by_key = {}, {}
            for row in batch:
                key = profile_key(row)
                resolved_profile = key if key in self.profiles else type_key(row)
                model = self.profiles.get(resolved_profile)
                result = detect(model, row) if model else None
                service = services[row["service"]]
                incident = correlate(row, result, service)
                evidence_by_key[row["key"]] = incident
                state = ("ANOMALY" if result["anomaly"] else "NORMAL") if result else (
                    "BASELINE_NOT_READY" if not model else "INSUFFICIENT_INPUT")
                compact = {**row, "detector_state": state, "result": result, "profile_version": self.version if model else None,
                           "profile_key": resolved_profile if model else None}
                current[row["key"]] = compact
                if result and row["role"] in SERVICE_ROLES:
                    history = self.drift.setdefault(resolved_profile, deque(maxlen=DRIFT_WINDOWS))
                    if history and history[-1][0] != row["start"]:
                        history.clear()
                    history.append((row["end"], result["anomaly"], result["statistical_score"],
                                    result["isolation_score"]))
                if not result and key in self.drift:
                    self.drift[key].clear()
                if incident:
                    incident["profile_key"] = compact["profile_key"]
                self.transition_incident(row, incident, tested=result is not None)
                if incident:
                    history = self.anomaly_history.setdefault(row["key"], deque(maxlen=12))
                    history.append({"stamp": row["end"], "state": state,
                                    "severity": incident["severity"], "behavior": incident["behavior"],
                                    "features": incident["evidence"]["features"]})
                self.coverage[f"{row['role']}:{row['quality']}"] += 1

            blocked = {r["service"] for r in batch if evidence_by_key[r["key"]]
                       or r["key"] in self.active}
            if session:
                for row in batch:
                    if row["start"] < session["start"]:
                        continue
                    reason = exclusion(row)
                    if not reason and row["service"] in blocked:
                        reason = "ACTIVE_INCIDENT_OR_ANOMALY"
                    sample = {**row, "audit_version": 1}
                    if reason:
                        table = "learning_exclusions"
                        sample["learning_exclusion"] = reason
                        sample["missing_features"] = [k for k, v in row["values"].items() if v is None]
                        sample["values"] = dict.fromkeys(row["values"])
                        sample["rolling"] = {}
                        sample["identity"] = {k: v for k, v in row["identity"].items()
                                              if k in ("status", "device_id", "device_type")}
                        sample["contributors"] = []
                    else:
                        table = "samples"
                    self.store.db.execute(f"INSERT OR IGNORE INTO {table} VALUES (?, ?, ?, ?)",
                                          (session["id"], row["start"], row["key"], json.dumps(sample, allow_nan=False)))
            self.latest = current
            self.counters = {k: v for k, v in self.counters.items() if v[0] >= window_end - COOLDOWN_SECONDS}
            self.anomaly_history = {k: v for k, v in self.anomaly_history.items()
                                    if v[-1]["stamp"] >= window_end - 3600}

    def build(self):
        if not self.fitting.acquire(blocking=False):
            raise Conflict("BASELINE_BUILD_IN_PROGRESS")
        try:
            with self.lock:
                records = self.store.learning_records()
            version = uuid4().hex
            output = self.store.artifact(version)
            report, digest = fit(records, output)
            status = "CANDIDATE" if digest else "NEEDS_MORE_NORMAL_DATA"
            with self.lock, self.store.db:
                self.store.db.execute("INSERT INTO versions VALUES (?, ?, ?, ?, ?)",
                                      (version, int(time()), status, digest, json.dumps(report, allow_nan=False)))
            return {"id": version, "status": status, "model_hash": digest, "profiles": report}
        finally:
            self.fitting.release()

    def activate(self, version, rollback):
        row = self.store.db.execute("SELECT * FROM versions WHERE id=?", (version,)).fetchone()
        allowed = "RETIRED" if rollback else "CANDIDATE"
        if row is None or row["status"] != allowed:
            raise Conflict("BASELINE_VERSION_NOT_ELIGIBLE")
        models = load_artifact(self.store.artifact(version), row["digest"])
        self.store.db.execute("UPDATE versions SET status='RETIRED' WHERE status='ACTIVE'")
        self.store.db.execute("UPDATE versions SET status='ACTIVE' WHERE id=?", (version,))
        self.store.db.commit()
        self.profiles, self.version = models, version
        self.profile_error = None
        self.counters.clear()
        self.latest.clear()
        self.drift.clear()
        return {"status": "ACTIVE", "id": version}

    def learning_summary(self):
        db = self.store.db
        counts = dict(db.execute("SELECT status, count(*) FROM sessions GROUP BY status"))
        seconds = db.execute("SELECT COALESCE(sum(COALESCE(stop, ?) - start), 0) FROM sessions",
                             (int(time()),)).fetchone()[0]
        return {"sessions": counts, "duration_seconds": seconds,
                "eligible_windows": db.execute("SELECT count(*) FROM samples WHERE json_extract(payload,'$.audit_version')=1").fetchone()[0],
                "excluded_windows": db.execute("SELECT count(*) FROM learning_exclusions").fetchone()[0],
                "legacy_unaudited_rows": db.execute("SELECT count(*) FROM samples WHERE json_extract(payload,'$.audit_version') IS NULL").fetchone()[0]}

    def readiness(self):
        if self.readiness_cache is None:
            modes = {role: [] for role in SERVICE_ROLES}
            for key, sessions in grouped(self.store.learning_records()).items():
                _, role, mode = json.loads(key)
                if role not in modes:
                    continue
                stats, sets = partition(sessions, hashes=False)
                stats.update(profile_key=key, mode=mode, eligible_windows=sum(map(len, sets)))
                modes[role].append(stats)
            self.readiness_cache = modes
        return {role: {"status": ("ACTIVE" if any(m["profile_key"] in self.profiles for m in modes)
                                  else "READY_TO_BUILD" if any(m["status"] == "READY_TO_BUILD" for m in modes)
                                  else "NEEDS_MORE_NORMAL_DATA"),
                       "modes": modes}
                for role, modes in self.readiness_cache.items()}

    def drift_status(self):
        result = {}
        for key, history in self.drift.items():
            if not history or history[-1][0] != max((r["end"] for r in self.latest.values()), default=0):
                continue
            unusual = sum(r[1] for r in history)
            status = len(history) == DRIFT_WINDOWS and unusual >= DRIFT_WINDOWS // 4
            result[key] = {"status": "POSSIBLE_DRIFT" if status else "MONITORING", "windows": len(history),
                           "anomalies": unusual, "start": history[0][0] - SPAN, "end": history[-1][0],
                           "median_statistical_score": sorted(r[2] for r in history)[len(history) // 2],
                           "median_isolation_score": sorted(r[3] for r in history)[len(history) // 2],
                           "recommendation": "Thu và duyệt phiên bình thường mới nếu thay đổi là hợp lệ."
                           if status else None}
        return result

    def get(self, resource, item=None):
        if resource == "xgboost-shadow" and item is None:
            return (self.xgboost_shadow.snapshot() if self.xgboost_shadow else
                    {"status": "DISABLED", "decision_eligible": False, "lanes": {}})
        if resource == "collector-features" and item is None:
            probe = getattr(self.monitor, "model_inputs", None)
            return probe.snapshot() if probe else {"status": "DISABLED", "decision_eligible": False}
        with self.lock:
            sessions = self.store.sessions() if resource in ("overview", "learning") else []
            versions = [dict(r) for r in self.store.db.execute(
                "SELECT id, created, status, digest AS model_hash FROM versions ORDER BY created DESC LIMIT 20"
            )] if resource in ("overview", "profiles") else []
            if resource == "overview":
                devices = [r for r in self.latest.values() if r["identity"].get("status") == "VERIFIED"]
                return {"schema": SCHEMA, "policy": POLICY, "monitoring": self.monitoring, "enforcement": False,
                        "baseline": "READY" if self.profiles else "NOT_TRAINED", "profile_error": self.profile_error,
                        "active_version": self.version, "profiles": len(self.profiles),
                        "learning": "LEARNING_NORMAL" if any(s["status"] == "LEARNING_NORMAL" for s in sessions) else "IDLE",
                        "normal_devices": sum(r["detector_state"] == "NORMAL" for r in devices),
                        "suspicious_devices": sum(r["detector_state"] == "ANOMALY" for r in devices),
                        "active_incidents": len(self.active), "active_profile": self.version,
                        "versions": versions, "sessions": sessions[:12],
                        "service_readiness": self.readiness(), "drift": self.drift_status(),
                        "learning_summary": self.learning_summary(),
                        "services": [r for r in self.latest.values() if r["role"] in ("MQTT_BROKER", "VIDEO_SERVER")]}
            if resource == "learning":
                return self.store.review(item) if item else {"sessions": sessions}
            if resource == "health":
                return self.monitoring
            if resource == "schema":
                return directions()
            if resource == "profiles":
                if item:
                    row = self.store.db.execute("SELECT * FROM versions WHERE id=?", (item,)).fetchone()
                    if not row:
                        raise Conflict("PROFILE_VERSION_NOT_FOUND")
                    return {**dict(row), "report": json.loads(row["report"])}
                return {"versions": versions, "active_version": self.version, "service_readiness": self.readiness(),
                        "profiles": [{"profile_key": k, "role": v["role"], "features": v["columns"],
                                      "ranges": v["ranges"]} for k, v in self.profiles.items()]}
            if resource == "devices":
                if item:
                    row = self.latest.get(item)
                    return {"current": row, "history": list(self.anomaly_history.get(item, ()))}
                return {"devices": sorted(self.latest.values(), key=lambda r: (
                    r["identity"].get("status") != "VERIFIED", r["key"]))[:64]}
            if resource in ("incidents", "recommendations"):
                incidents = self.store.incidents()
                if item:
                    row = self.store.db.execute("SELECT payload FROM incidents WHERE id=?", (item,)).fetchone()
                    incidents = [json.loads(row[0])] if row else []
                if resource == "recommendations":
                    return {"recommendations": [a for row in incidents for a in row["recommendations"]]}
                return {"incidents": incidents}
            raise KeyError(resource)

    def command(self, action, data):
        if action == "build":
            if data:
                raise ValueError("INVALID_OPERATOR_INPUT")
            return self.build()
        if action not in ("start", "stop", "accept", "reject", "activate", "rollback"):
            raise KeyError(action)
        note = data.get("note")
        allowed = {"start": {"note"}, "stop": {"id"}, "accept": {"id", "note"}, "reject": {"id", "note"},
                   "activate": {"id"}, "rollback": {"id"}}
        if set(data) != allowed[action] or ("note" in data and
                (not isinstance(note, str) or not 1 <= len(note.strip()) <= 240)):
            raise ValueError("INVALID_OPERATOR_INPUT")
        item = data.get("id")
        if "id" in data and (not isinstance(item, str) or len(item) != 32 or any(c not in "0123456789abcdef" for c in item)):
            raise ValueError("INVALID_ID")
        now = int(time())
        with self.lock, self.store.db:
            if action in ("activate", "rollback"):
                return self.activate(item, action == "rollback")
            if action == "start":
                if self.store.db.execute("SELECT 1 FROM sessions WHERE status='LEARNING_NORMAL'").fetchone():
                    raise Conflict("SESSION_ALREADY_RUNNING")
                item = uuid4().hex
                self.store.db.execute("INSERT INTO sessions VALUES (?, ?, NULL, 'LEARNING_NORMAL', ?, NULL, ?)",
                                      (item, now, note.strip(), SCHEMA))
                return {"id": item, "status": "LEARNING_NORMAL"}
            if action == "stop":
                status = "PENDING_REVIEW"
                query = self.store.db.execute(
                    "UPDATE sessions SET stop=?, status=? WHERE id=? AND status='LEARNING_NORMAL'", (now, status, item))
            else:
                status = "ACCEPTED" if action == "accept" else "REJECTED"
                if status == "ACCEPTED" and not self.store.db.execute(
                        "SELECT 1 FROM samples WHERE session=? LIMIT 1", (item,)).fetchone():
                    raise Conflict("EMPTY_SESSION")
                query = self.store.db.execute(
                    "UPDATE sessions SET status=?, review=? WHERE id=? AND status='PENDING_REVIEW'",
                    (status, note.strip(), item))
            if not query.rowcount:
                raise Conflict("SESSION_STATE_CONFLICT")
            if status == "ACCEPTED":
                self.readiness_cache = None
            return {"id": item, "status": status}
