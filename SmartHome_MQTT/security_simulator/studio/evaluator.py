"""Evaluate each ground-truth category using compatible metrics."""
from statistics import median


def binary(y, predictions):
    tn = fp = fn = tp = 0
    for expected, predicted in zip(y, predictions, strict=True):
        if expected not in (0, 1) or predicted not in (0, 1):
            raise ValueError("Binary predictions and labels required")
        if expected == 0:
            tn += predicted == 0
            fp += predicted == 1
        else:
            tp += predicted == 1
            fn += predicted == 0
    return {"confusion_matrix": [[tn, fp], [fn, tp]], "normal_false_positive_rate": fp / (tn + fp) if tn + fp else None,
            "attack_recall": tp / (tp + fn) if tp + fn else None, "precision": tp / (tp + fp) if tp + fp else None,
            "f1": 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else None,
            "false_negatives": fn, "rows": tn + fp + fn + tp}


def physical(session, rows):
    windows = [row for row in rows if row["status"] == "PREDICTED"]
    normal = session["ground_truth"] == "VERIFIED_NORMAL"
    scores = [row["score"] for row in windows]
    flagged = sum(row["prediction"] == "ATTACK" for row in windows)
    delays = [row["window_age_at_score_seconds"] for row in windows if row["window_age_at_score_seconds"] is not None]
    duration = max(0.0, session["ended"] - session["started"])
    aligned_features = [row["features"] for row in windows if row.get("features")]
    reference = session.get("reference_features") or {}
    feature_ranges = {}
    if aligned_features:
        for name in aligned_features[0]:
            values = [feature[name] for feature in aligned_features]
            center = median(values)
            feature_ranges[name] = {"min": min(values), "max": max(values), "median": center,
                                    "delta_from_start": center - reference[name] if name in reference else None}
    return {
        "verification": "OBSERVED_WINDOW" if windows else "WAITING_FOR_PACKETS",
        "eligible_windows_observed": len(windows), "predicted_attack": flagged,
        "lane_alert_rate_during_normal_session": flagged / len(windows) if windows and normal else None,
        "lane_alerts_per_hour_during_normal_session": 3600 * flagged / duration if windows and normal and duration else None,
        "session_seconds": duration, "first_scored_window_after_start_seconds": (
            max(0.0, windows[0]["updated_at"] - session["started"]) if windows else None),
        "median_last_packet_to_worker_tick_seconds": median(delays) if delays else None,
        "true_attack_detection_latency_seconds": None, "attack_recall": None,
        "score_min": min(scores) if scores else None, "score_median": median(scores) if scores else None,
        "score_max": max(scores) if scores else None,
        "capture_quality_counts": {label: sum((r["quality"] or "UNKNOWN") == label for r in windows)
                                   for label in sorted({r["quality"] or "UNKNOWN" for r in windows})},
        "source_attribution": "UNKNOWN", "feature_ranges": feature_ranges,
        "limitations": ["The API exposes the latest service-wide window and may skip windows between polls.",
                        "Window labels are not verified per peer. Idle and controlled sessions do not measure attack recall."],
    }
