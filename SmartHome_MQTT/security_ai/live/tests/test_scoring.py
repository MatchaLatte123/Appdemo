from hashlib import sha256
import sys

import joblib
import numpy as np
import pytest
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline
from xgboost import XGBClassifier

from security_ai.web_ui.contract import BASIC_10, SCHEMA
from security_ai.live.xgboost_shadow import Worker, XGBoostShadow


def test_isolated_worker_scores_pinned_pipeline(tmp_path):
    values = np.array([[feature + row for feature in range(10)] for row in range(20)], dtype=np.float32)
    model = Pipeline([("imputer", SimpleImputer()), ("classifier", XGBClassifier(n_estimators=3, n_jobs=1))])
    model.fit(values, [0] * 10 + [1] * 10)
    artifact = tmp_path / "model.joblib"
    joblib.dump(model, artifact)

    worker = Worker(sys.executable)
    try:
        worker.start(artifact, sha256(artifact.read_bytes()).hexdigest())
        assert worker.predict([values[0].tolist(), values[-1].tolist()]) == pytest.approx(
            model.predict_proba(values[[0, -1]])[:, 1])
        with pytest.raises(RuntimeError, match="SCORING_FAILED"):
            worker.predict([[1, 2]])
    finally:
        worker.close()

    worker = Worker(sys.executable)
    try:
        with pytest.raises(RuntimeError, match="MODEL_SHA256_MISMATCH"):
            worker.start(artifact, "0" * 64)
    finally:
        worker.close()


@pytest.mark.parametrize(("live", "score", "label", "confidence", "idle_threshold"), [
    (False, .81, None, None, None),
    (True, .65, "ATTACK", .5, None),
    (True, .15, "NORMAL", .5, None),
    (True, .3, "ATTACK", 0, None),
    (True, .4, "NORMAL", .5, .8),
])
def test_shadow_scores_only_current_windows_and_reuses_result(live, score, label, confidence, idle_threshold):
    class Probe:
        lanes = {"MQTT_BROKER": "MQTT"}
        blocked = []
        pending = 3
        latest = {"window": {"start": 58, "end": 60, "packet_count": 10},
                  "capture_quality": "LOCAL_WINDOW_NO_LOSS_FLAG", "vector": (0,) * 10,
                  "source_evidence": {"peer_count": 2, "attack_source_attribution": "UNKNOWN"}}

        def snapshot(self):
            return {"feature_order": BASIC_10, "blocked_services": self.blocked,
                    "lanes": {"MQTT_BROKER": {"service": "MQTT", "windows": {
                        10: {"latest": self.latest, "completed": 1, "pending": self.pending}}}}}

    class Model:
        calls = 0

        def predict(self, vectors):
            self.calls += 1
            assert vectors == [(0,) * 10]
            return [score]

    probe, model = Probe(), Model()
    shadow = XGBoostShadow(probe, {
        "run_id": "1" * 32, "sha256": "2" * 64, "dataset_version": "fixture",
        "feature_order": BASIC_10, "feature_schema": SCHEMA, "benchmark_threshold": .3,
        "artifact": "test-model", "python": sys.executable, "live_predictions": live,
        "idle_thresholds": {"MQTT_BROKER": idle_threshold} if idle_threshold is not None else {},
        "idle_validation": {"MQTT_BROKER": {"run_id": "1" * 32, "window_packets": 10,
                                           "threshold": .8, "windows": 10, "normal_windows": 8}},
    })
    shadow.tick(model, now=61)
    shadow.tick(model, now=62)
    record = shadow.snapshot()
    lane = record["lanes"]["MQTT_BROKER"]
    assert lane["score"] == score
    assert lane["source_evidence"] == Probe.latest["source_evidence"]
    assert not record["decision_eligible"]
    assert record["isolation_assessment"]["status"] == "UNVALIDATED"
    if live:
        assert record["status"] == "LIVE_PREDICTION"
        assert lane["status"] == "PREDICTED"
        assert lane["prediction"] == label
        assert lane["confidence"] == pytest.approx(confidence)
        assert lane["threshold"] == (idle_threshold if idle_threshold is not None else .3)
        assert lane["threshold_source"] == ("LOCAL_IDLE" if idle_threshold is not None else "BENCHMARK")
        assert lane.get("idle_validation") == (
            {"windows": 10, "normal_windows": 8} if idle_threshold is not None else None)
    else:
        assert record["status"] == "BENCHMARK_ONLY"
        assert lane["status"] == "SHADOW_SCORED"
        assert "prediction" not in lane
    assert model.calls == 1
    shadow.tick(model, now=94)
    stale = shadow.snapshot()["lanes"]["MQTT_BROKER"]
    assert stale["status"] == "STALE_WINDOW"
    assert stale["score"] is None
    assert "source_evidence" not in stale
    assert stale.get("prediction") is None and stale.get("confidence") is None
    probe.blocked = ["MQTT"]
    shadow.tick(model, now=95)
    assert shadow.snapshot()["lanes"]["MQTT_BROKER"]["status"] == "CAPTURE_QUALITY_BLOCK"
    assert model.calls == 1

def test_peer_is_scored_separately_and_only_with_inbound_evidence():
    class Probe:
        lanes = {"MQTT_BROKER": "MQTT"}

        def snapshot(self):
            def window(inbound):
                return {"completed": 1, "pending": 0, "latest": {
                    "window": {"start": 58, "end": 60, "packet_count": 10},
                    "capture_quality": "LOCAL_WINDOW_NO_LOSS_FLAG",
                    "source_evidence": {"inbound_peer_count": inbound}, "vector": (0,) * 10,
                }}

            return {"feature_order": BASIC_10, "blocked_services": [],
                    "lanes": {"MQTT_BROKER": {"service": "MQTT", "windows": {10: window(2)}}},
                    "peer_windows": {"MQTT_BROKER": {"192.0.2.2": window(1), "192.0.2.3": window(0)}}}

    class Worker:
        def predict(self, vectors):
            assert len(vectors) == 2
            return [.1, .8]

    shadow = XGBoostShadow(Probe(), {
        "run_id": "1" * 32, "sha256": "2" * 64, "dataset_version": "fixture", "feature_order": BASIC_10,
        "feature_schema": SCHEMA, "benchmark_threshold": .3, "artifact": "test", "python": sys.executable,
        "live_predictions": True, "peer_predictions": True,
    })
    shadow.tick(Worker(), now=61)
    result = shadow.snapshot()
    assert result["lanes"]["MQTT_BROKER"]["prediction"] == "NORMAL"
    peers = result["peers"]["MQTT_BROKER"]
    assert peers["192.0.2.2"]["source_status"] == "SUSPECTED_PEER"
    assert peers["192.0.2.3"]["status"] == "NO_INBOUND_EVIDENCE"
    assert peers["192.0.2.3"]["prediction"] is None
    assert result["decision_eligible"] is False
    shadow.tick(Worker(), now=95)
    assert shadow.snapshot()["peers"]["MQTT_BROKER"]["192.0.2.2"]["source_status"] == "UNKNOWN"
