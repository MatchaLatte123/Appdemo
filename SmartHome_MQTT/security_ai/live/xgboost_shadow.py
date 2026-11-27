"""Read-only XGBoost scoring over the CIC probe, isolated from live decisions."""
from contextlib import contextmanager
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
from queue import Empty, Full, Queue
import subprocess
import sys
from threading import Event, Lock, Thread
from time import time

from security_ai.web_ui.cic_full import CIC_38
from security_ai.web_ui.contract import BASIC_10, PARITY_BLOCK, PRESETS, SCHEMA, SHADOW_SIZES

ROOT = Path(__file__).resolve().parents[2]
LIMIT = 4096
MODEL_LIMIT = 64 * 1024**2


def within(value, start, end):
    return type(value) in (int, float) and start <= value <= end


def preset_for_order(order):
    if tuple(order) == CIC_38:
        return "CIC_38"
    for name in ("BASIC_10", "CIC_21"):
        if tuple(order) == PRESETS[name]:
            return name
    raise ValueError("MODEL_SCHEMA_MISMATCH")


def load_model(settings):
    # The artifact path and expected digest are local startup configuration, never API input.
    import joblib

    with Path(settings["artifact"]).open("rb") as file:
        payload = file.read(MODEL_LIMIT + 1)
    if not payload or len(payload) > MODEL_LIMIT or sha256(payload).hexdigest() != settings["sha256"]:
        raise ValueError("MODEL_SHA256_MISMATCH")
    model = joblib.load(BytesIO(payload))
    order = tuple(settings.get("feature_order", BASIC_10))
    preset_for_order(order)
    classifier = model.named_steps["classifier"]
    classes = [0, 1, 2] if order == CIC_38 else [0, 1]
    if (type(classifier).__name__ != "XGBClassifier" or not type(classifier).__module__.startswith("xgboost.")
            or list(model.classes_) != classes or model.n_features_in_ != len(order)):
        raise ValueError("MODEL_SCHEMA_MISMATCH")
    classifier.set_params(n_jobs=1, device="cpu")
    return model


def probabilities(model, vectors):
    import numpy as np
    from threadpoolctl import threadpool_limits

    if not isinstance(vectors, list) or not 1 <= len(vectors) <= 128:
        raise ValueError("INVALID_INPUT_BATCH")
    for row in vectors:
        if not isinstance(row, list) or len(row) != model.n_features_in_ or not all(within(item, -1e30, 1e30) for item in row):
            raise ValueError("INVALID_INPUT_VECTOR")
    with threadpool_limits(limits=1):
        values = model.predict_proba(np.asarray(vectors, dtype=np.float32)).tolist()
    count = len(model.classes_)
    if (len(values) != len(vectors) or any(len(row) != count for row in values)
            or not all(within(score, 0, 1) for row in values for score in row)):
        raise ValueError("INVALID_MODEL_SCORE")
    return values if count == 3 else [row[1] for row in values]


def worker():
    def send(payload):
        print(json.dumps(payload, allow_nan=False), flush=True)

    try:
        request = json.loads(sys.stdin.readline(LIMIT))
        model = load_model(request)
    except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError) as exc:
        error = str(exc) if str(exc) in ("MODEL_SHA256_MISMATCH", "MODEL_SCHEMA_MISMATCH") else "MODEL_LOAD_FAILED"
        send({"error": error})
        return
    send({"ready": True})
    for line in sys.stdin:
        try:
            request = json.loads(line)
            send({"scores": probabilities(model, request["vectors"])})
        except (ValueError, TypeError, KeyError, RuntimeError):
            send({"error": "SCORING_FAILED"})


class Worker:
    def __init__(self, python):
        self.python = python
        self.process = None
        self.messages = Queue(maxsize=1)
        self.reader = None
        self.finished = Event()

    def read(self):
        try:
            for line in self.process.stdout:
                if len(line) > LIMIT:
                    break
                data = json.loads(line)
                if not isinstance(data, dict):
                    break
                self.messages.put_nowait(data)
        except (OSError, ValueError, RuntimeError, Full):
            pass
        finally:
            self.finished.set()

    def receive(self, timeout):
        try:
            response = self.messages.get(timeout=timeout)
        except Empty:
            raise RuntimeError("MODEL_WORKER_EXITED" if self.finished.is_set() else "MODEL_WORKER_TIMEOUT") from None
        if "error" in response:
            raise RuntimeError(response["error"])
        return response

    def send(self, message):
        self.process.stdin.write(json.dumps(message, allow_nan=False) + "\n")
        self.process.stdin.flush()

    def start(self, artifact, digest, feature_order=BASIC_10):
        self.process = subprocess.Popen(
            [str(self.python), "-u", "-B", "-m", "security_ai.live.xgboost_shadow", "--worker"],
            cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            encoding="utf-8", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.reader = Thread(target=self.read, daemon=True)
        self.reader.start()
        self.send({"artifact": str(artifact), "sha256": digest, "feature_order": feature_order})
        if self.receive(12).get("ready") is not True:
            raise RuntimeError("MODEL_HANDSHAKE_FAILED")

    def predict(self, vectors):
        self.send({"vectors": vectors})
        scores = self.receive(3).get("scores")
        if (not isinstance(scores, list) or len(scores) != len(vectors)
                or not all(within(score, 0, 1) for score in scores)):
            raise RuntimeError("INVALID_WORKER_SCORES")
        return scores

    def predict_cic38(self, vector):
        self.send({"vectors": [vector]})
        rows = self.receive(3).get("scores")
        if (not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], list)
                or len(rows[0]) != 3 or not all(within(value, 0, 1) for value in rows[0])
                or abs(sum(rows[0]) - 1.0) > 1e-5):
            raise RuntimeError("INVALID_WORKER_SCORES")
        return rows[0]

    def close(self):
        if not self.process:
            return
        try:
            self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=2)
        if self.reader:
            self.reader.join(timeout=1)
        self.process.stdout.close()


class ScoringRuntime:
    def run(self, stop):
        while not stop.is_set():
            self.scores.clear()
            missing = ("MODEL_ARTIFACT_NOT_FOUND" if not self.artifact.is_file() else
                       "MODEL_PYTHON_NOT_FOUND" if not self.python.is_file() else None)
            if missing:
                with self.lock:
                    self.state = {"worker": "ERROR", "reason": missing, "lanes": {}}
                stop.wait(5)
                continue
            worker = Worker(self.python)
            try:
                worker.start(self.artifact, self.digest, self.feature_order)
                while not stop.is_set():
                    self.tick(worker)
                    stop.wait(1)
            except (OSError, ValueError, RuntimeError) as exc:
                reason = str(exc) if str(exc) in (
                    "MODEL_SHA256_MISMATCH", "MODEL_SCHEMA_MISMATCH", "MODEL_LOAD_FAILED", "PROBE_SCHEMA_MISMATCH",
                    "MODEL_WORKER_TIMEOUT", "MODEL_WORKER_EXITED", "MODEL_HANDSHAKE_FAILED",
                    "INVALID_WORKER_SCORES", "SCORING_FAILED",
                ) else "MODEL_WORKER_UNAVAILABLE"
                with self.lock:
                    self.state = {"worker": "ERROR", "reason": reason, "lanes": {}}
            finally:
                worker.close()
            stop.wait(5)

    @contextmanager
    def start(self):
        stop = Event()
        thread = Thread(target=self.run, args=(stop,), daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=16)


class XGBoostShadow(ScoringRuntime):
    def __init__(self, probe, settings):
        self.probe = probe
        self.lock = Lock()
        self.state = {"worker": "STARTING", "lanes": {}}
        self.scores = {}
        self.run_id = settings["run_id"]
        self.digest = settings["sha256"]
        self.dataset = settings["dataset_version"]
        self.feature_order = tuple(settings["feature_order"])
        preset_for_order(self.feature_order)
        self.threshold = settings["benchmark_threshold"]
        self.live_predictions = settings.get("live_predictions", False)
        self.peer_predictions = settings.get("peer_predictions", False)
        idle_thresholds = settings.get("idle_thresholds", {})
        idle_validation = settings.get("idle_validation", {})
        self.group_size = settings.get("window_packets", 10)
        self.max_age = settings.get("max_age_seconds", 30)
        self.artifact = ROOT / settings["artifact"]
        self.python = ROOT / settings["python"]
        if (not isinstance(idle_thresholds, dict)
                or any(not isinstance(scope, str) or not scope or not within(value, 0, 1) or value in (0, 1)
                       for scope, value in idle_thresholds.items())):
            raise ValueError("INVALID_XGBOOST_IDLE_THRESHOLDS")
        self.idle_thresholds = {scope: threshold for scope, threshold in idle_thresholds.items()
                                if scope in probe.lanes} if idle_thresholds else {}
        if not isinstance(idle_validation, dict):
            raise ValueError("INVALID_XGBOOST_IDLE_VALIDATION")
        self.idle_validation = {}
        for scope, result in idle_validation.items():
            if (not isinstance(result, dict) or not within(result.get("threshold"), 0, 1)
                    or type(result.get("windows")) is not int or type(result.get("normal_windows")) is not int
                    or result["windows"] <= 0 or not 0 <= result["normal_windows"] <= result["windows"]):
                raise ValueError("INVALID_XGBOOST_IDLE_VALIDATION")
            if (result.get("run_id") == self.run_id and result.get("window_packets") == self.group_size
                    and result["threshold"] == self.idle_thresholds.get(scope)):
                self.idle_validation[scope] = {"windows": result["windows"], "normal_windows": result["normal_windows"]}
        if (not isinstance(self.run_id, str) or len(self.run_id) != 32
                or not isinstance(self.digest, str) or len(self.digest) != 64
                or any(ch not in "0123456789abcdef" for ch in self.run_id + self.digest)
                or settings["feature_schema"] != SCHEMA
                or not within(self.threshold, 0, 1)
                or type(self.live_predictions) is not bool
                or type(self.peer_predictions) is not bool
                or (self.peer_predictions and not self.live_predictions)
                or (self.live_predictions and not 0 < self.threshold < 1)
                or type(self.group_size) is not int or self.group_size not in SHADOW_SIZES
                or not within(self.max_age, 1, 120)):
            raise ValueError("INVALID_XGBOOST_SHADOW_CONFIG")

    def snapshot(self):
        with self.lock:
            return {"status": "LIVE_PREDICTION" if self.live_predictions else "BENCHMARK_ONLY",
                    "decision_eligible": False, "parity": PARITY_BLOCK,
                    "isolation_assessment": {
                        "status": "UNVALIDATED",
                        # Current local labels cover idle windows only. Lane predictions do not identify a source.

                        "blockers": ["NO_REVIEWED_ISOLATION_CANDIDATES", "NO_TARGET_ATTRIBUTION", PARITY_BLOCK],
                    },
                    "model": {"algorithm": "XGBOOST", "run_id": self.run_id, "sha256": self.digest,
                              "dataset_version": self.dataset, "feature_order": self.feature_order,
                              "benchmark_threshold": self.threshold, "idle_thresholds": self.idle_thresholds.copy(),
                              "window_packets": self.group_size},
                    "peer_prediction_status": "UNVALIDATED" if self.peer_predictions else "DISABLED",
                    **self.state}

    def scored(self, info, score):
        info.update(status="SHADOW_SCORED", score=score)
        if self.live_predictions:
            threshold = info["threshold"]
            attack = score >= threshold
            # Normalize distance to the threshold within the chosen side. This is an
            # uncalibrated confidence score, with zero at the decision boundary.
            confidence = ((score - threshold) / (1 - threshold) if attack else (threshold - score) / threshold)
            info.update(status="PREDICTED", prediction="ATTACK" if attack else "NORMAL", confidence=confidence)
            if "peer_ip" in info:
                info["source_status"] = "SUSPECTED_PEER" if attack else "NO_ALERT"

    def tick(self, worker, *, now=None):
        data = self.probe.snapshot()
        now = time() if now is None else now
        rows, peers, changes, vectors = {}, {}, [], []
        if tuple(data["feature_order"]) != self.feature_order:
            raise RuntimeError("PROBE_SCHEMA_MISMATCH")
        peer_windows = data.get("peer_windows", {}) if self.peer_predictions else {}
        work = []
        for scope, lane in data["lanes"].items():
            work.append((scope, lane["service"], None, lane["windows"][self.group_size]))
            work.extend((scope, lane["service"], ip, window) for ip, window in peer_windows.get(scope, {}).items())
        valid_scores = {}
        for scope, service, ip, size in work:
            latest = size["latest"]
            info = {"service": service, "status": "WAITING_FOR_WINDOW", "score": None,
                    "window_packets": self.group_size, "pending_packets": size["pending"],
                    "completed_windows": size["completed"]}
            if self.live_predictions:
                info.update(prediction=None, confidence=None, threshold=self.idle_thresholds.get(scope, self.threshold),
                            threshold_source="LOCAL_IDLE" if scope in self.idle_thresholds else "BENCHMARK")
            if ip is None:
                if self.live_predictions and scope in self.idle_validation:
                    info["idle_validation"] = self.idle_validation[scope].copy()
                rows[scope] = info
            else:
                info.update(peer_ip=ip, source_status="UNKNOWN", threshold_source="LANE_THRESHOLD_UNVALIDATED_PER_PEER")
                peers.setdefault(scope, {})[ip] = info
            if service in data["blocked_services"]:
                info["status"] = "CAPTURE_QUALITY_BLOCK"
                continue
            if latest is None:
                continue
            window = latest["window"]
            info.update(window=window, capture_quality=latest["capture_quality"])
            if not within(now - window["end"], -2, self.max_age):
                info["status"] = "STALE_WINDOW"
                continue
            evidence = latest.get("source_evidence")
            if evidence is not None:
                info["source_evidence"] = evidence
            if ip is not None and not (evidence and evidence["inbound_peer_count"]):
                info["status"] = "NO_INBOUND_EVIDENCE"
                continue
            cache_key = (scope, ip)
            window_key = (size["completed"], window["start"], window["end"])
            prior = self.scores.get(cache_key)
            if prior is not None and prior[0] == window_key:
                self.scored(info, prior[1])
                valid_scores[cache_key] = prior
                continue
            changes.append((info, cache_key, window_key))
            vectors.append(latest["vector"])
        for start in range(0, len(vectors), 128):
            batch = slice(start, start + 128)
            for (info, cache_key, window_key), score in zip(changes[batch], worker.predict(vectors[batch]), strict=True):
                self.scored(info, score)
                valid_scores[cache_key] = (window_key, score)
        self.scores = valid_scores
        with self.lock:
            self.state = {"worker": "READY", "updated_at": now, "lanes": rows, "peers": peers}



if __name__ == "__main__" and sys.argv[1:] == ["--worker"]:
    worker()
