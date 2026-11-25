"""Pair raw diagnostics with the scored window from the existing collector."""
import numpy as np

from security_ai_v2.rebuild.contract import BASIC_10, FORMULAS, vector


def window_identity(window):
    return tuple(window.get(key) for key in ("start", "end", "packet_count")) if window else None


def inspect(scope, predictions, features, preprocessor=None, *, enforcement=False):
    model = predictions["model"]
    if tuple(model["feature_order"]) != BASIC_10 or tuple(features["feature_order"]) != BASIC_10:
        raise ValueError("BASIC_10 order mismatch")
    size = model["window_packets"]
    lane = predictions["lanes"][scope]
    windows = features.get("lanes", {}).get(scope, {}).get("windows", {})
    quality = windows.get(str(size), windows.get(size, {}))
    raw = quality.get("latest")
    window = lane.get("window")
    key = window_identity(window)
    if lane.get("status") != "PREDICTED" or not key or not raw or window_identity(raw["window"]) != key:
        raw = None
    transformed = None
    if raw:
        values = vector(raw["features"])
        if tuple(raw["vector"]) != values:
            raise ValueError("BASIC_10 raw vector disagrees with API payload")
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
        "feature_order": list(BASIC_10), "formulas": FORMULAS,
        "capture_counters": features.get("counters", {}).get(lane["service"], {}),
        "flow_id": None, "direction": "BIDIRECTIONAL_PER_LANE", "source_attribution": "UNKNOWN",
        "parity": predictions.get("parity"),
        "decision_eligible": predictions["decision_eligible"], "enforcement": enforcement,
        "isolation_assessment": predictions.get("isolation_assessment"),
    }


def source_evidence(scope, participants, devices, incidents=(), started=0):
    """Document observed peers without converting Serial IDs or IP matches into verified sources."""
    service = "MQTT" if scope == "MQTT_BROKER" else "Video"
    ips = {p["ip"] for p in participants if p.get("wifi") and p.get("ip") not in (None, "0.0.0.0")}
    observed = []
    for row in devices:
        if row.get("service") != service:
            continue
        identity = row.get("identity") or {}
        if identity.get("ip") in ips:
            observed.append({"identity": identity, "role": row.get("role"), "peer_key": row.get("key")})
    candidates = [{"id": item["id"], "suspected_sources": item.get("suspected_sources", []),
                   "affected_service": item["affected_service"]}
                  for item in incidents if item.get("updated_at", 0) >= started
                  and item.get("affected_service", {}).get("service") == service]
    return {"expected_participants": sorted(ips), "observed_service_peers": observed,
            "separate_v3_incident_evidence": candidates,
            "source_attribution": "UNKNOWN", "precision": None,
            "reason": "Device telemetry is separate from the scored service-wide window."}
