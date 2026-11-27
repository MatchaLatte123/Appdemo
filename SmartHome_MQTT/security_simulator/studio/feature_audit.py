"""Pair raw diagnostics with the scored window from the existing collector."""
import numpy as np

from security_ai.web_ui.contract import FORMULAS, vector
from security_ai.live.xgboost_shadow import preset_for_order


def window_identity(window):
    return tuple(window.get(key) for key in ("start", "end", "packet_count")) if window else None


def inspect(scope, predictions, features, preprocessor=None, *, enforcement=False):
    model = predictions["model"]
    order = tuple(model["feature_order"])
    preset = preset_for_order(order)
    if tuple(features["feature_order"]) != order:
        raise ValueError("Collector and model feature order mismatch")
    size = model["window_packets"]
    captured = features.get("lanes", {}).get(scope, {})
    lane = predictions["lanes"].get(scope, {"status": predictions.get("worker", "STARTING")})
    windows = captured.get("windows", {})
    quality = windows.get(str(size), windows.get(size, {}))
    raw = quality.get("latest")
    window = lane.get("window")
    key = window_identity(window)
    if lane.get("status") != "PREDICTED" or not key or not raw or window_identity(raw["window"]) != key:
        raw = None
    transformed = None
    if raw:
        values = vector(raw["features"], preset)
        if tuple(raw["vector"]) != values:
            raise ValueError("Raw vector disagrees with API payload")
        if preprocessor is not None:
            transformed = preprocessor.transform(np.asarray([values], dtype=np.float32))[0].tolist()
    stamp = predictions.get("updated_at")
    delay = max(0.0, stamp - window["end"]) if stamp is not None and window and lane.get("status") == "PREDICTED" else None
    return {
        "scope": scope, "model_id": model["run_id"], "sha256": model["sha256"],
        "status": lane["status"], "window": window, "updated_at": stamp,
        "window_age_at_score_seconds": delay, "score": lane.get("score"), "threshold": lane.get("threshold"),
        "prediction": lane.get("prediction"), "quality": lane.get("capture_quality"),
        "completed": quality.get("completed"), "discarded_windows": quality.get("discarded"),
        "pending_packets": quality.get("pending", lane.get("pending_packets")),
        "features": raw["features"] if raw else None, "transformed": transformed,
        "feature_order": list(order), "formulas": {name: FORMULAS[name] for name in order},
        "capture_counters": features.get("counters", {}).get(lane.get("service") or captured.get("service"), {}),
        "flow_id": None, "direction": "BIDIRECTIONAL_PER_LANE", "source_attribution": "UNKNOWN",
        "source_evidence": lane.get("source_evidence") if lane.get("status") == "PREDICTED" else None,
        "parity": predictions.get("parity"),
        "decision_eligible": predictions["decision_eligible"], "enforcement": enforcement,
        "isolation_assessment": predictions.get("isolation_assessment"),
    }
