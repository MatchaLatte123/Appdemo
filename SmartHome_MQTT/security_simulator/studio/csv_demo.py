"""Run local CIC_38 inference on prepared feature CSVs."""
import csv

import numpy as np

from security_ai.live.cic38 import CIC_38
from security_ai.live.cic38_shadow import LABELS
from security_ai.live.xgboost_shadow import load_model, probabilities

from .ai_client import ROOT


def score_csv(model, path):
    counts = np.zeros(len(LABELS), dtype=np.int64)
    attack_sum = 0.0
    batch = []

    def score_batch():
        nonlocal attack_sum
        scores = np.asarray(probabilities(model, batch))
        predicted = scores.argmax(axis=1)
        counts[:] += np.bincount(predicted, minlength=len(LABELS))
        attack_sum += float(scores[:, 1:].sum())
        batch.clear()

    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.reader(stream, strict=True)
        try:
            header = [name.strip() for name in next(reader, [])]
            if len(header) != len(set(header)):
                raise ValueError("Tên cột bị trùng.")
            missing = [name for name in CIC_38 if name not in header]
            if missing:
                raise ValueError("Thiếu đặc trưng CIC_38: " + ", ".join(missing))
            columns = [header.index(name) for name in CIC_38]
            for row in reader:
                if not row:
                    continue
                if len(row) != len(header):
                    raise ValueError("Số trường khác với tiêu đề CSV.")
                batch.append([float(row[index]) for index in columns])
                if len(batch) == 128:
                    score_batch()
            if batch:
                score_batch()
        except (csv.Error, UnicodeError, ValueError) as error:
            raise ValueError(f"{path.name}, dòng {max(reader.line_num, 1)}: {error}") from error

    rows = int(counts.sum())
    if not rows:
        raise ValueError(f"{path.name}: CSV không có dòng dữ liệu.")
    return {"path": str(path), "rows": rows, "counts": counts.tolist(), "attack_sum": attack_sum}


def evaluate_files(deployment, paths):
    if not paths:
        raise ValueError("Chọn ít nhất một file CSV.")
    settings = deployment.ai_config.get("xgboost_shadow", {})
    if tuple(settings.get("feature_order", ())) != CIC_38 or settings.get("label_mapping") != LABELS:
        raise ValueError("Tab Demo cần model CIC_38 ba nhãn được cấu hình trong Simulation Studio.")
    model = load_model({**settings, "artifact": ROOT / settings["artifact"]})
    files = [score_csv(model, path) for path in paths]
    rows = sum(item["rows"] for item in files)
    counts = np.sum([item["counts"] for item in files], axis=0).tolist()
    return {"run_id": settings["run_id"], "files": files, "rows": rows, "counts": counts,
            "mean_attack": sum(item["attack_sum"] for item in files) / rows}
