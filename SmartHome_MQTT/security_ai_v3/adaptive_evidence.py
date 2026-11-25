"""Versioned, explainable evidence policy. Recommendations are descriptive only."""
from .adaptive_profiles import POLICY

TRAFFIC = {"packet_rate", "byte_rate", "connection_rate", "publish_rate", "http_rate", "rst_rate"}


def correlate(row, result, service_row):
    feature_evidence = result["abnormal"] if result else []
    values = row["values"]
    affected = service_row["service_state"]
    impact = affected == "DEGRADED" or service_row["values"].get("recorder_error") == 1
    camera = row["role"] == "CAMERA_DEVICE"
    if camera and values.get("recorder_error") == 1:
        impact = True
    service_down = service_row["service_state"] == "UNAVAILABLE"
    if not result or not result["anomaly"]:
        if not impact or row["role"] in ("MQTT_DEVICE", "VIDEO_PEER"):
            return None
        behavior = "SERVICE_DEGRADATION"
    else:
        rising = {f["feature"].removeprefix("60s:") for f in feature_evidence if f["deviation"] > 0}
        sources = row["contributors"]
        significant = [s for s in sources if s["activity"] >= max(
            2, (sources[0]["activity"] / 10 if sources else 0))]
        source_count = len(significant)
        if impact and row["quality"] in ("OBSERVED", "UNVERIFIED") and rising & TRAFFIC:
            if not camera and source_count >= 2 and rising & {"connection_rate", "publish_rate", "http_rate"}:
                behavior = "MULTI_SOURCE_OVERLOAD"
            elif row["service"] == "MQTT" and rising & {"connection_rate", "publish_rate"}:
                behavior = "SUSPECTED_MQTT_FLOOD"
            elif rising & {"http_rate"} and not camera:
                behavior = "SUSPECTED_VIDEO_HTTP_OVERLOAD"
            elif rising & {"connection_rate", "rst_rate"} and not camera:
                behavior = "SUSPECTED_NETWORK_FLOOD"
            else:
                behavior = "BEHAVIOR_ANOMALY"
        else:
            behavior = "BEHAVIOR_ANOMALY"
    attribution = row["identity"]
    suspected = []
    if row["quality"] in ("OBSERVED", "UNVERIFIED") and behavior != "SERVICE_DEGRADATION" and not camera:
        if attribution:
            suspected.append({"role": "SUSPECTED_SOURCE", **attribution})
        elif behavior.startswith("SUSPECTED") or behavior == "MULTI_SOURCE_OVERLOAD":
            suspected.extend({"role": "SUSPECTED_SOURCE", **s["identity"]} for s in significant)
    affected_devices = ([{"role": "AFFECTED_DEVICE", **attribution}]
                        if camera and impact and attribution.get("status") == "VERIFIED" else [])
    severity = ("HIGH" if behavior == "MULTI_SOURCE_OVERLOAD" and row["quality"] == "OBSERVED" else
                "MEDIUM" if impact else "LOW")
    limitation = ["Behavioral evidence is not confirmation of a network attack."]
    if row["quality"] != "OBSERVED":
        limitation.append("Zero capture drops were not independently verified for this window.")
    if service_down:
        limitation.append("Service telemetry unavailable.")
    if not any(s["status"] == "VERIFIED" for s in suspected):
        limitation.append("No verified source binding.")
    evidence = {"features": feature_evidence, "capture_quality": row["quality"], "capture": row["capture"],
                "service_state": affected, "impact": impact, "contributors": row["contributors"],
                "isolation_score": result["isolation_score"] if result else None,
                "statistical_score": result.get("statistical_score") if result else None,
                "statistical_threshold": result.get("statistical_threshold") if result else None,
                "isolation_threshold": result.get("threshold") if result else None,
                "current": values, "expected": result.get("expected", {}) if result else {}}
    return {"behavior": behavior, "severity": severity, "policy": POLICY, "evidence": evidence,
             "suspected_sources": suspected, "affected_devices": affected_devices,
            "attribution_confidence": [s["status"] for s in suspected],
            "affected_service": {"role": "AFFECTED_SERVICE", "service": row["service"]},
            "limitations": limitation, "recommendations": recommend(behavior, row["service"], suspected, feature_evidence)}


def recommend(behavior, service, sources, features):
    names = {f["feature"].removeprefix("60s:") for f in features}
    if behavior == "SERVICE_DEGRADATION":
        action = ("Kiểm tra Broker và kết nối thiết bị" if service == "MQTT"
                  else "Kiểm tra Recording Service, FFmpeg, lưu trữ và trạng thái luồng")
    elif service == "MQTT":
        action = ("Kiểm tra vòng lặp publish và firmware" if "publish_rate" in names
                  else "Kiểm tra vòng lặp kết nối lại, Wi-Fi và nhật ký xác thực")
    else:
        action = "Đối chiếu kết nối HTTP, bitrate camera và trạng thái FFmpeg"
    base = {"id": f"{behavior.lower()}-review", "action": action,
            "explanation": "Đối chiếu chỉ số lệch, chất lượng capture và tình trạng dịch vụ.",
            "evidence_refs": ["evidence.features", "evidence.service_state", "evidence.capture_quality"],
            "target": {"service": service}, "preconditions": ["Có quyền xem telemetry/nhật ký dịch vụ"],
            "risk": "READ_ONLY", "rollback": "Không thay đổi cấu hình", "approval_required": False}
    out = [base]
    verified = [s for s in sources if s["status"] == "VERIFIED"]
    if len(verified) == 1 and behavior == "SUSPECTED_MQTT_FLOOD":
        out.append({**base, "id": "review-client-rate-limit", "action": "Đánh giá giới hạn tốc độ theo MQTT client",
                    "target": {"service": service, "device_id": verified[0]["device_id"]},
                    "preconditions": ["Xác minh lại binding và vòng lặp", "Có bản sao cấu hình và operator duyệt"],
                    "risk": "MAY_INTERRUPT_DEVICE",
                    "rollback": "Phục hồi cấu hình giới hạn trước thay đổi và xác nhận thiết bị kết nối lại.",
                    "approval_required": True})
    return out
