"""Read-only three-class CIC_38 scoring by protected TCP endpoint."""
from hashlib import sha256
import json
from threading import Lock
from time import time

from .cic38 import CIC_38, SCHEMA, WINDOW_PACKETS
from .xgboost_shadow import ROOT, ScoringRuntime, within

SCOPE = "PROTECTED_TCP_ENDPOINTS"
LABELS = {"Benign": 0, "DoS": 1, "DDoS": 2}
PARITY = "CIC38_LIVE_10_PACKET_VALIDATION_PENDING"


class Cic38Shadow(ScoringRuntime):
    def __init__(self, monitor, settings):
        self.monitor = monitor
        self.lock = Lock()
        self.state = {"worker": "STARTING", "lanes": {}}
        self.scores = {}
        self.feature_order = tuple(settings["feature_order"])
        self.group_size = settings["window_packets"]
        self.max_age = settings.get("max_age_seconds", 30)
        self.artifact = (ROOT / settings["artifact"]).resolve()
        self.python = ROOT / settings["python"]
        self.digest = settings["sha256"]
        collector = monitor.cic38
        if (collector is None or self.feature_order != CIC_38 or self.group_size != WINDOW_PACKETS
                or settings["feature_schema"] != SCHEMA or not within(self.max_age, 1, 120)
                or settings["label_mapping"] != LABELS or settings.get("benchmark_threshold") is not None):
            raise ValueError("INVALID_CIC38_MODEL_CONFIG")
        report = json.loads(self.artifact.with_name("report.json").read_bytes())
        manifest_sha = sha256(self.artifact.with_name("dataset_manifest.json").read_bytes()).hexdigest()
        scaler_sha = sha256(self.artifact.with_name("preprocessing.json").read_bytes()).hexdigest()
        run_id = settings["run_id"]
        if (self.artifact.name != "model.joblib" or self.artifact.parent.name != run_id
                or report["run_id"] != run_id or report["model_sha256"] != self.digest
                or report["algorithm"] != "XGBOOST" or tuple(report["schema"]["order"]) != CIC_38
                or report["label_mapping"] != LABELS or report["dataset_version"] != settings["dataset_version"]
                or report["dataset_manifest_sha256"] != collector.dataset_sha
                or manifest_sha != collector.dataset_sha or scaler_sha != collector.scaler_sha):
            raise ValueError("CIC38_MODEL_DATASET_MISMATCH")
        self.model = {"algorithm": "XGBOOST", "run_id": run_id, "sha256": self.digest,
                      "dataset_version": report["dataset_version"], "feature_order": self.feature_order,
                      "benchmark_threshold": None, "window_packets": WINDOW_PACKETS, "scope": SCOPE,
                      "label_mapping": LABELS, "preprocessing_sha256": scaler_sha,
                      "decision_rule": "max_class_probability"}

    def snapshot(self):
        with self.lock:
            return {"status": "LIVE_PREDICTION", "decision_eligible": False, "parity": PARITY,
                    "isolation_assessment": {"status": "UNVALIDATED", "blockers": ["NO_TARGET_ATTRIBUTION", PARITY]},
                    "model": self.model, "peer_prediction_status": "DISABLED", **self.state}

    def tick(self, worker, *, now=None):
        capture = self.monitor.cic38_snapshot()
        now = time() if now is None else now
        lanes, scores = {}, {}
        for scope, source in capture["lanes"].items():
            state = source["windows"][WINDOW_PACKETS]
            lanes[scope] = self.score_lane(worker, now, scope, source["service"], state, scores)
        self.scores = scores
        with self.lock:
            self.state = {"worker": "READY", "updated_at": now, "lanes": lanes, "peers": {}}

    def score_lane(self, worker, now, scope, service, state, scores):
        latest = state["latest"]
        lane = {"service": service, "status": "WAITING_FOR_WINDOW", "score": None,
                "prediction": None, "window_packets": WINDOW_PACKETS,
                "pending_packets": state["pending"], "completed_windows": state["completed"]}
        if latest:
            bounds = {"start": latest["start"], "end": latest["end"], "packet_count": WINDOW_PACKETS}
            lane["window"] = bounds
            if within(now - latest["end"], -2, self.max_age):
                key = (scope, state["completed"], latest["start"], latest["end"])
                values = self.scores.get(key)
                if values is None:
                    values = worker.predict_cic38(latest["normalized_vector"])
                scores[key] = values
                probabilities = dict(zip(LABELS, values, strict=True))
                lane.update(status="PREDICTED", prediction=max(probabilities, key=probabilities.get),
                            score=min(1.0, probabilities["DoS"] + probabilities["DDoS"]),
                            probabilities=probabilities)
            else:
                lane["status"] = "STALE_WINDOW"
        return lane
