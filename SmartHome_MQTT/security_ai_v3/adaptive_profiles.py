"""Versioned statistical and Isolation Forest profiles, fitted on approved sessions."""
from collections import Counter
from hashlib import sha256
import json
from math import isfinite
from statistics import median
from time import perf_counter, process_time

import psutil

import joblib
import numpy as np
import sklearn
from sklearn.ensemble import IsolationForest

from .adaptive_telemetry import BASE, FEATURES, SCHEMA

POLICY = "adaptive-policy-1"
MIN_TRAIN = 60
MIN_VALIDATION = 36
MIN_TRAIN_SESSIONS = 2
MIN_SCORE_COVERAGE = .75
MAX_VALIDATION_ALERT_FRACTION = .05
MAX_TRAIN_ROWS = 12_000
FLOORS = {"packet_rate": .2, "byte_rate": 128, "connection_rate": .2,
          "rst_rate": .2, "message_rate": .2, "media_age": 2, "clients": 1}


def profile_key(row):
    identity = row["identity"]
    if row["role"] in ("MQTT_BROKER", "VIDEO_SERVER") or identity.get("status") == "VERIFIED":
        return json.dumps([row["key"], row["role"], row["mode"]], separators=(",", ":"))
    return None


def type_key(row):
    device_type = row["identity"].get("device_type")
    if row["identity"].get("status") == "VERIFIED" and device_type:
        return json.dumps([f"type:{device_type}", row["role"], row["mode"]], separators=(",", ":"))
    return None


def vector(row, columns):
    values = [row["rolling" if name.startswith("60s:") else "values"].get(name.removeprefix("60s:"))
              for name in columns]
    return [value if type(value) in (int, float) and isfinite(value) else None for value in values]


def exclusion(row, *, normal=True):
    if normal and row.get("learning_exclusion"):
        return row["learning_exclusion"]
    if row["schema"] != SCHEMA or row["end"] - row["start"] != 5 or row["start"] % 5:
        return "INVALID_WINDOW_OR_SCHEMA"
    if row["quality"] not in ("OBSERVED", "UNVERIFIED"):
        return row["quality"]
    capture = row.get("capture", {})
    if any(capture.get(key) for key in ("truncated", "decoder_gaps", "decoder_errors",
                                       "overflow_or_late", "kernel_drops")):
        return "CAPTURE_EVIDENCE"
    if normal and row["service_state"] == "DEGRADED":
        return "SERVICE_DEGRADED"
    if normal and row["role"] == "VIDEO_SERVER" and row["service_state"] == "UNAVAILABLE":
        return "SERVICE_HEALTH_UNAVAILABLE"
    if not profile_key(row):
        return "IDENTITY_NOT_VERIFIED"
    if any(type(row["values"].get(n)) not in (int, float) or not isfinite(row["values"][n])
           or row["values"][n] < 0 for n in BASE):
        return "MISSING_CORE_FEATURES"
    return None


def eligible(row):
    return exclusion(row) is None


def grouped(records):
    groups = {}
    for session, row in records:
        for key in filter(None, (profile_key(row), type_key(row))):
            groups.setdefault(key, {}).setdefault(session, []).append(row)
    return groups


def isolated_samples(rows):
    """Build training history within one approved session and one subject only."""
    from collections import deque
    history = {}
    result = []
    for row in rows:
        if not eligible(row):
            continue
        prior = history.setdefault(row["key"], deque(maxlen=12))
        if prior and prior[-1]["end"] != row["start"]:
            prior.clear()
        if "packet_rate_change" in row["values"]:
            change = row["values"]["packet_rate"] - prior[-1]["values"]["packet_rate"] if prior else None
            row = {**row, "values": {**row["values"], "packet_rate_change": change}}
        prior.append(row)
        rolling = {n: sum(r["values"][n] for r in prior) / 12
                   if len(prior) == 12 and all(r["values"].get(n) is not None for r in prior) else None
                   for n in row["values"]}
        result.append({**row, "rolling": rolling})
    return result


def partition(sessions, *, hashes=True):
    ordered = sorted(sessions.items(), key=lambda item: min(r["start"] for r in item[1]))
    ordered = [(sid, sorted(rows, key=lambda r: (r["start"], r["key"]))) for sid, rows in ordered]
    fit_groups, cal_groups, val_groups = ordered[:-2], ordered[-2:-1], ordered[-1:]
    partitioned = [fit_groups, cal_groups, val_groups]
    sets = [[row for _, rows in group for row in isolated_samples(rows)] for group in partitioned]
    descriptions = {}
    for label, group, usable in zip(("fit", "calibration", "validation"), partitioned, sets):
        all_rows = [row for _, rows in group for row in rows]
        descriptions[label] = {
            "sessions": len(group), "eligible": len(usable), "recorded": len(all_rows),
            "excluded": len(all_rows) - len(usable), "audit_complete": bool(all_rows) and all(
                r.get("audit_version") == 1 for r in all_rows),
            "session_sha256": {sid: sha256(json.dumps(rows, sort_keys=True, separators=(",", ":"),
                                                    allow_nan=False).encode()).hexdigest() for sid, rows in group}
            if hashes else {},
            "start": min((row["start"] for row in all_rows), default=None),
            "end": max((row["end"] for row in all_rows), default=None)}
    train, calibration, validation = sets
    reasons = Counter(exclusion(row) for _, rows in ordered for row in rows)
    reasons.pop(None, None)
    stats = {"status": "NEEDS_MORE_NORMAL_DATA", "accepted_sessions": len(ordered),
             "train_sessions": sum(bool(isolated_samples(rows)) for _, rows in fit_groups),
             "train_samples": len(train), "calibration_samples": len(calibration),
             "validation_samples": len(validation), "partitions": descriptions, "excluded_reasons": reasons}
    if hashes:
        provenance = {name: info["session_sha256"] for name, info in descriptions.items()}
        stats["dataset_sha256"] = sha256(json.dumps(provenance, sort_keys=True).encode()).hexdigest()
    if (stats["train_sessions"] < MIN_TRAIN_SESSIONS or len(train) < MIN_TRAIN
            or min(len(calibration), len(validation)) < MIN_VALIDATION):
        return stats, sets
    if any(max(r["end"] for r in left) > min(r["start"] for r in right)
           for (_, left), (_, right) in zip(ordered, ordered[1:])):
        stats["status"] = "OVERLAPPING_SESSIONS"
    elif not all(descriptions[n]["audit_complete"] for n in ("calibration", "validation")):
        stats["status"] = "INCOMPLETE_QUALITY_AUDIT"
    elif any(descriptions[n]["eligible"] < .9 * descriptions[n]["recorded"]
             for n in ("calibration", "validation")):
        stats["status"] = "VALIDATION_INPUT_ELIGIBILITY"
    else:
        stats["status"] = "READY_TO_BUILD"
    return stats, sets


def false_incidents(observations):
    opened, streak, clean, last = 0, 0, 0, None
    active, recovered_at = False, None
    for (row, _), anomaly in observations:
        stamp = row["end"]
        consecutive = last is not None and stamp == last + 5
        last = stamp
        if anomaly:
            clean = 0
            streak = streak + 1 if consecutive else 1
            if streak >= 3 and not active:
                if recovered_at is None or stamp - recovered_at > 60:
                    opened += 1
                active = True
        else:
            streak = 0
            clean = clean + 1 if consecutive else 1
            if active and clean >= 4:
                active, recovered_at = False, stamp
    return opened


def fit(records, output):
    """Fit only approved sessions, reserving the last two sessions before calibration."""
    process = psutil.Process()
    models, report = {}, {}
    for key, sessions in sorted(grouped(records).items()):
        stats, (train, calibration, validation) = partition(sessions)
        report[key] = stats
        if stats["status"] != "READY_TO_BUILD":
            continue
        cpu_start, rss_start = process_time(), process.memory_info().rss
        if json.loads(key)[0].startswith("type:"):
            training_devices = {r["key"] for r in train}
            validation_devices = {r["key"] for r in validation}
            if not validation_devices - training_devices:
                stats["status"] = "NO_UNSEEN_DEVICE_VALIDATION"
                continue
        stride = max(1, (len(train) + MAX_TRAIN_ROWS - 1) // MAX_TRAIN_ROWS)
        fitting = train[::stride]
        role = fitting[0]["role"]
        candidates = [*FEATURES[role], *(f"60s:{name}" for name in FEATURES[role])]
        columns, center, scale, ranges = [], [], [], {}
        for name in candidates:
            observed = [v for row in fitting if (v := vector(row, [name])[0]) is not None]
            if len(observed) < MIN_SCORE_COVERAGE * len(fitting):
                continue
            middle = median(observed)
            mad = median(abs(v - middle) for v in observed)
            lo, hi = np.quantile(observed, [.01, .99])
            columns.append(name)
            center.append(middle)
            scale.append(max(mad * 1.4826, float(hi - lo) / 4, FLOORS.get(name.removeprefix("60s:"), .2)))
            ranges[name] = {"median": middle, "mad": mad, "p01": float(lo), "p99": float(hi)}
        model = {"role": role, "columns": columns, "center": np.asarray(center), "scale": np.asarray(scale),
                 "ranges": ranges, "forest": IsolationForest(n_estimators=96, max_samples=min(256, len(fitting)),
                 random_state=7123, contamination="auto", n_jobs=1)}
        model["forest"].fit([scaled(model, row)[0] for row in fitting])
        def observed_vectors(rows):
            return [(row, x) for row in rows for x, present in [scaled(model, row)]
                    if len(present) >= MIN_SCORE_COVERAGE * len(columns)]

        calibrating = observed_vectors(calibration)
        usable = observed_vectors(validation)
        descriptions = stats["partitions"]
        if any(len(xs) < MIN_VALIDATION or len(xs) < .9 * descriptions[label]["recorded"]
               for xs, label in ((calibrating, "calibration"), (usable, "validation"))):
            stats["status"] = "VALIDATION_MISSINGNESS"
            continue
        scores = -model["forest"].score_samples([x for _, x in calibrating])
        calibration_robust = [max(abs(x)) for _, x in calibrating]
        model.update(forest_threshold=float(np.quantile(scores, .99)),
                     stat_threshold=max(4., float(np.quantile(calibration_robust, .99))))
        inference_start = perf_counter()
        forest = -model["forest"].score_samples([x for _, x in usable])
        inference_ms = (perf_counter() - inference_start) * 1000 / len(usable)
        robust = [max(abs(x)) for _, x in usable]
        anomalies = [abs(x) > model["stat_threshold"] or f > model["forest_threshold"] for x, f in zip(robust, forest)]
        false_rate = sum(anomalies) / len(anomalies)
        coverage_hours = len(anomalies) * 5 / 3600
        incidents = false_incidents(zip(usable, anomalies))
        validation_recorded = descriptions["validation"]["recorded"]
        feature_missing = {n: sum(vector(row, [n])[0] is None for row in validation) / len(validation)
                           for n in columns}
        stats.update(schema=SCHEMA, detector_config={"seed": 7123, "n_estimators": 96,
                     "max_samples": min(256, len(fitting)), "policy": POLICY},
                     feature_order=columns, scaler_center=center, scaler_scale=scale,
                     normal_stat_p50_p95_p99=[float(x) for x in np.quantile(robust, [.5, .95, .99])],
                     false_incidents=incidents, false_incidents_per_hour=incidents / coverage_hours,
                     validation_input_eligibility=len(usable) / validation_recorded,
                     validation_quality_rejection_rate=1 - len(validation) / validation_recorded,
                     validation_feature_missing_ratio=feature_missing,
                     validation_batch_inference_ms_per_window=inference_ms,
                     process_cpu_seconds=process_time() - cpu_start, process_rss_bytes=process.memory_info().rss,
                     rss_delta_bytes=process.memory_info().rss - rss_start,
                     usable_validation_samples=len(usable), fitted_samples=len(fitting),
                     false_anomaly_fraction=false_rate, false_anomaly_windows_per_hour=sum(anomalies) / coverage_hours,
                     evaluated_hours=coverage_hours, validation_missing_fraction=1 - len(usable) / len(validation),
                     normal_score_p50_p95_p99=[float(x) for x in np.quantile(forest, [.5, .95, .99])],
                     stat_threshold=model["stat_threshold"], forest_threshold=model["forest_threshold"],
                     feature_ranges=ranges, data_start=min(r["start"] for r in train),
                     data_end=max(r["end"] for r in validation))
        if false_rate <= MAX_VALIDATION_ALERT_FRACTION:
            stats["status"] = "READY"
            models[key] = model
        else:
            stats["status"] = "VALIDATION_FALSE_ALERT_RATE"
    if not models:
        return report, None
    joblib.dump({"schema": SCHEMA, "policy": POLICY, "sklearn": sklearn.__version__, "models": models}, output, compress=3)
    return report, sha256(output.read_bytes()).hexdigest()


def scaled(model, row):
    values = vector(row, model["columns"])
    observed = [name for name, value in zip(model["columns"], values) if value is not None]
    filled = [model["center"][i] if value is None else value for i, value in enumerate(values)]
    return np.clip((np.asarray(filled) - model["center"]) / model["scale"], -1000, 1000), observed


def detect(model, row):
    if row["schema"] != SCHEMA or row["role"] != model["role"] or exclusion(row, normal=False):
        return None
    values, observed = scaled(model, row)
    if len(observed) < MIN_SCORE_COVERAGE * len(model["columns"]):
        return None
    forest = float(-model["forest"].score_samples([values])[0])
    abnormal = []
    for name, value in zip(model["columns"], values):
        if name in observed and abs(value) > model["stat_threshold"]:
            abnormal.append({"feature": name, "deviation": float(value), "value": vector(row, [name])[0],
                             "typical": model["ranges"][name]})
    return {"anomaly": bool(abnormal or forest > model["forest_threshold"]), "isolation_score": forest,
            "statistical_score": float(max(abs(values))),
            "statistical_threshold": model["stat_threshold"],
            "expected": model["ranges"],
            "threshold": model["forest_threshold"], "abnormal": abnormal, "observed_features": len(observed)}


def load_artifact(path, expected_hash):
    if sha256(path.read_bytes()).hexdigest() != expected_hash:
        raise ValueError("Profile hash mismatch")
    artifact = joblib.load(path)
    if (artifact["schema"], artifact["policy"], artifact["sklearn"]) != (SCHEMA, POLICY, sklearn.__version__):
        raise ValueError("Profile compatibility mismatch")
    return artifact["models"]
