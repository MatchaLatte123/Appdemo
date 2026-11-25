"""Operator-triggered comparison, validation selection and sealed test export."""
from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path
from time import perf_counter
from uuid import uuid4
import json
import shutil
import warnings

import joblib
import numpy as np
import pyarrow.parquet as pq
from sklearn.ensemble import ExtraTreesClassifier, HistGradientBoostingClassifier, RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (accuracy_score, average_precision_score, confusion_matrix, f1_score,
                             precision_recall_curve, precision_score, roc_auc_score, roc_curve)
from sklearn.model_selection import GroupShuffleSplit
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import RobustScaler, StandardScaler
from sklearn.utils.class_weight import compute_sample_weight
from threadpoolctl import threadpool_limits

from .contract import BENCHMARK, PRESETS, contract
from .data import SPLITS, connect, digest, read_json, sql_column, sql_literal, write_json

from .parameters import ALGORITHMS, LEVELS, validate_parameters
COMMON = {"seed": 42, "threads": 2, "memory_mib": 2048, "timeout_seconds": 1800, "train_cap_per_class": 60000,
          "validation_cap": 30000, "features": "BASIC_8", "preset": "QUICK_EXPERIMENT", "balanced": True,
          "normal_fpr_limit": .01, "scaling": "AUTO", "level": "quick", "auto_test": False, "reuse_test": False,
          "dataset_manifest_sha256": None}

def bounded(name, value, low, high, kind=int):
    allowed_types = (int,) if kind is int else (int, float)
    if type(value) not in allowed_types or not low <= value <= high:
        raise ValueError(f"INVALID_{name.upper()}")
    return value


def config(values):
    if not isinstance(values, dict) or set(values) - (COMMON.keys() | {"algorithms", "hyperparameters"}):
        raise ValueError("INVALID_TRAINING_CONFIG")
    out = {**COMMON, **values}
    algorithms = values.get("algorithms")
    if (not isinstance(algorithms, list) or not algorithms or len(algorithms) > len(ALGORITHMS)
            or any(not isinstance(a, str) or a not in ALGORITHMS for a in algorithms)
            or len(set(algorithms)) != len(algorithms)):
        raise ValueError("INVALID_ALGORITHMS")
    for k, lo, hi in (("seed", 0, 2**31-1), ("threads", 1, 8), ("memory_mib", 512, 8192),
                      ("timeout_seconds", 10, 28800), ("train_cap_per_class", 0, 5_000_000),
                      ("validation_cap", 100, 500_000)):
        bounded(k, out[k], lo, hi)
    bounded("normal_fpr_limit", out["normal_fpr_limit"], 0., .5, float)
    if (out["features"] not in PRESETS or out["preset"] not in ("QUICK_EXPERIMENT", "FULL_TRAINING")
            or out["scaling"] not in ("AUTO", "STANDARD", "ROBUST", "NONE") or out["level"] not in LEVELS
            or any(type(out[k]) is not bool for k in ("balanced", "auto_test", "reuse_test"))
            or out["preset"] == "QUICK_EXPERIMENT" and out["train_cap_per_class"] < 2):
        raise ValueError("INVALID_PRESET")
    manifest_sha = out["dataset_manifest_sha256"]
    if manifest_sha is not None and (not isinstance(manifest_sha, str) or len(manifest_sha) != 64
                                     or any(ch not in "0123456789abcdef" for ch in manifest_sha)):
        raise ValueError("INVALID_DATASET_MANIFEST_HASH")
    raw = values.get("hyperparameters", {})
    if not isinstance(raw, dict) or set(raw) - set(algorithms):
        raise ValueError("INVALID_HYPERPARAMETER_ALGORITHMS")
    n_features = len(PRESETS[out["features"]])
    out["hyperparameters"] = {
        algorithm: validate_parameters(algorithm, raw.get(algorithm, {}), out["level"], n_features, out["threads"])
        for algorithm in algorithms
    }
    return out


def estimator(name, cfg):
    args = cfg["hyperparameters"][name].copy()
    args["random_state"] = cfg["seed"] if args.get("random_state") is None else args["random_state"]
    threads = args.pop("n_jobs", None) or cfg["threads"]
    args.pop("early_stopping_validation_fraction", None)
    if isinstance(args.get("class_weight"), dict):
        args["class_weight"] = {int(key): value for key, value in args["class_weight"].items()}
    if name == "LOGISTIC":
        if "C" in args and args["C"] is None:
            args["C"] = np.inf
        classifier = LogisticRegression(**args)
    elif name == "HIST_GRADIENT":
        classifier = HistGradientBoostingClassifier(**args)
    else:
        args["n_jobs"] = threads
        if name == "RANDOM_FOREST":
            classifier = RandomForestClassifier(**args)
        elif name == "EXTRA_TREES":
            classifier = ExtraTreesClassifier(**args)
        elif name == "XGBOOST":
            from xgboost import XGBClassifier
            args.setdefault("tree_method", "hist")
            args["objective"] = "binary:logistic"
            args["device"] = "cpu"
            args["validate_parameters"] = True
            if args.get("monotone_constraints") is not None:
                args["monotone_constraints"] = tuple(args["monotone_constraints"])
            if args.get("interaction_constraints") is not None:
                args["interaction_constraints"] = json.dumps(args["interaction_constraints"])
            if args.get("feature_weights") is not None:
                args["feature_weights"] = np.asarray(args["feature_weights"], dtype=np.float32)
            classifier = XGBClassifier(**args)
        else:
            from lightgbm import LGBMClassifier
            args.pop("early_stopping_rounds", None)
            args.setdefault("verbosity", -1)
            if args.get("interaction_constraints") is not None:
                args["interaction_constraints"] = json.dumps(args["interaction_constraints"])
            classifier = LGBMClassifier(objective="binary", deterministic=True, force_col_wise=True, **args)
    mode = cfg["scaling"]
    mode = ("STANDARD" if name == "LOGISTIC" else "NONE") if mode == "AUTO" else mode
    scaler = {"STANDARD": StandardScaler, "ROBUST": RobustScaler}.get(mode)
    return Pipeline([("imputer", SimpleImputer(strategy="median")),
                     ("scaler", scaler() if scaler else "passthrough"), ("classifier", classifier)]), mode


def fit_estimator(model, name, cfg, x, y, groups):
    params = cfg["hyperparameters"][name]
    custom_weights = (params.get("class_weight") is not None or params.get("is_unbalance")
                      or params.get("scale_pos_weight") not in (None, 1))
    balance = cfg["balanced"] and not custom_weights
    rounds = params.get("early_stopping_rounds")
    if not rounds:
        weights = compute_sample_weight("balanced", y) if balance else None
        model.fit(x, y, classifier__sample_weight=weights)
        return len(y), 0

    # Calibration and test stay independent; the stopping split comes from the training groups.
    seed = cfg["seed"] if params.get("random_state") is None else params["random_state"]
    splitter = GroupShuffleSplit(n_splits=24, test_size=params.get("early_stopping_validation_fraction", .15),
                                 random_state=seed)
    try:
        fit_rows, stop_rows = next((fit, stop) for fit, stop in splitter.split(x, y, groups)
                                   if len(np.unique(y[fit])) == len(np.unique(y[stop])) == 2)
    except (ValueError, StopIteration):
        raise ValueError(f"{name}: không đủ nhóm train có cả Normal và Attack cho early stopping.") from None
    prep = model[:-1]
    fit_x = prep.fit_transform(x[fit_rows], y[fit_rows])
    stop_x = prep.transform(x[stop_rows])
    weight = compute_sample_weight("balanced", y[fit_rows]) if balance else None
    args = {"sample_weight": weight}
    if name == "XGBOOST":
        args.update(eval_set=[(stop_x, y[stop_rows])], verbose=False)
    else:
        from lightgbm import early_stopping
        args.update(eval_X=stop_x, eval_y=y[stop_rows],
                    callbacks=[early_stopping(rounds, first_metric_only=True, verbose=False)])
    model["classifier"].fit(fit_x, y[fit_rows], **args)
    return len(fit_rows), len(stop_rows)


def check_evaluation_memory(rows, cfg):
    if rows * 160 > cfg["memory_mib"] * 2**20 * .55:
        raise ValueError("DATA_EXCEEDS_MEMORY_BUDGET_SET_TRAIN_CAP_OR_INCREASE_MEMORY")


def sample_table(work, split, columns, cfg, cap, *, manifest=None):
    if manifest is None:
        manifest = read_json(work / "manifest.json")
    path = work / f"{split.lower()}.parquet"
    if digest(path) != manifest["splits"][split]["sha256"]:
        raise ValueError(f"{split}_HASH_MISMATCH")
    source = sql_literal(path.as_posix())
    columns_sql = ", ".join(map(sql_column, (*columns, "y", "scenario", "group_id")))
    n = manifest["splits"][split]["rows"]
    if cap == 0:
        check_evaluation_memory(n, cfg)
    # Hash seeded physical identities deterministically. Train caps apply per class, never per file.
    order = f"hash(source_sha256,row_id,{cfg['seed']}), source_name,row_id"
    restriction = (f"QUALIFY row_number() OVER (PARTITION BY y ORDER BY {order})<={cap}"
                   if cap and split == "TRAIN" else f"ORDER BY {order} LIMIT {cap}" if cap else "")
    with connect(work, max(128, cfg["memory_mib"] // 3), 12, cfg["threads"]) as db:
        return db.sql(f"SELECT {columns_sql} FROM read_parquet({source}) {restriction}").to_arrow_table()


def as_xy(table, columns):
    return (np.column_stack([table[n].to_numpy(zero_copy_only=False) for n in columns]).astype(np.float32),
            table["y"].to_numpy(zero_copy_only=False))


def decision_threshold(y, score, fpr_limit):
    fpr, recall, thresholds = roc_curve(y, score, drop_intermediate=False)
    valid = np.flatnonzero((fpr <= fpr_limit) & np.isfinite(thresholds))
    if not len(valid):
        return float(np.nextafter(np.max(score), np.inf))
    idx = max(valid, key=lambda k: (recall[k], -fpr[k], thresholds[k]))
    return float(thresholds[idx])


def metrics(y, scores, threshold):
    pred = scores >= threshold
    tn, fp, fn, tp = map(int, confusion_matrix(y, pred, labels=[0,1]).ravel())
    normal = tn + fp
    attack = tp + fn
    return {"rows": len(y), "accuracy": float(accuracy_score(y, pred)),
            "precision": float(precision_score(y, pred, zero_division=0)),
            "attack_recall": tp / attack if attack else None, "specificity": tn / normal if normal else None,
            "fpr": fp / normal if normal else None, "f1": float(f1_score(y, pred, zero_division=0)),
            "macro_f1": float(f1_score(y, pred, labels=[0,1], average="macro", zero_division=0)),
            "roc_auc": float(roc_auc_score(y, scores)) if normal and attack else None,
            "pr_auc": float(average_precision_score(y, scores)) if normal and attack else None,
            "tn": tn, "fp": fp, "fn": fn, "tp": tp, "confusion_matrix": [[tn,fp],[fn,tp]]}


def curve(a, b):
    return [[float(a[i]), float(b[i])] for i in np.linspace(0, len(a)-1, min(150,len(a)), dtype=int)]


def report_metrics(y, scores, threshold, table, *, compare_thresholds=True):
    summary = metrics(y, scores, threshold)
    pr, rec, _ = precision_recall_curve(y, scores)
    fpr, tpr, _ = roc_curve(y, scores)
    summary["curves"] = {"roc": curve(fpr, tpr), "pr": curve(rec[::-1], pr[::-1])}
    compared = sorted({.5, threshold}) if compare_thresholds else [threshold]
    summary["thresholds"] = [{"threshold": value, **metrics(y, scores, value)} for value in compared]
    # Dictionary encoding must be on one combined array for stable scenario codes across batches.
    encoded = table["scenario"].combine_chunks().dictionary_encode()
    names = encoded.dictionary.to_pylist()
    summary["scenarios"] = {name: metrics(y[mask], scores[mask], threshold)
                            for i, name in enumerate(names) if (mask := np.asarray(encoded.indices) == i).any()}
    groups = table["group_id"].combine_chunks().dictionary_encode()
    n_group = len(groups.dictionary)
    if n_group:
        code = np.asarray(groups.indices)
        predicted = scores >= threshold
        counts = np.bincount(code, minlength=n_group)
        errors = np.bincount(code, weights=(y != predicted), minlength=n_group)
        summary["groups"] = {"count": n_group, "macro_accuracy": float(np.mean(1-errors/counts)),
                             "per_group": {key: {"rows": int(count), "errors": int(error)}
                                           for key,count,error in zip(groups.dictionary.to_pylist(),counts,errors)}}
    return summary


def predict(model, values):
    start = perf_counter()
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="X does not have valid feature names")
        scores = np.concatenate([model.predict_proba(values[i:i+16384])[:,1]
                                 for i in range(0, len(values), 16384)])
    return scores, (perf_counter() - start) * 1000 / len(values)


def dataset_manifest(work):
    raw = (work / "manifest.json").read_bytes()
    return json.loads(raw), sha256(raw).hexdigest()


def dataset_details(manifest, manifest_sha):
    return {
        "version": manifest["version"],
        "id": manifest_sha[:16],
        "manifest_sha256": manifest_sha,
        "splits": {name: {key: info[key] for key in ("rows", "fraction", "sha256")}
                   for name, info in manifest["splits"].items()},
    }


def matches_dataset(report, manifest_sha):
    return (report.get("dataset_id") == manifest_sha[:16]
            and report.get("dataset_manifest_sha256") == manifest_sha)


def run_dataset_view(work, report):
    manifest, manifest_sha = dataset_manifest(work)
    if not matches_dataset(report, manifest_sha):
        raise ValueError("DATASET_MISMATCH")
    return {**report, "dataset_version": manifest["version"], "dataset": dataset_details(manifest, manifest_sha)}


def identifier(work):
    return digest(work / "manifest.json")[:16]


def verify_run(runs, run_id):
    if not isinstance(run_id, str) or len(run_id) != 32 or any(x not in "0123456789abcdef" for x in run_id):
        raise ValueError("INVALID_RUN_ID")
    folder = runs / run_id
    report = read_json(folder / "report.json")
    if report["run_id"] != run_id or digest(folder / "model.joblib") != report["model_sha256"]:
        raise ValueError("MODEL_ARTIFACT_HASH_MISMATCH")
    return folder, report


def train(work, values, progress=print):
    cfg = config(values)
    columns = PRESETS[cfg["features"]]
    selected = cfg["algorithms"]
    run_ids = []
    validation_rank = []
    manifest, manifest_sha = dataset_manifest(work)
    if cfg["dataset_manifest_sha256"] not in (None, manifest_sha):
        raise ValueError("DATASET_CHANGED_REFRESH_STUDIO")
    cfg["dataset_manifest_sha256"] = manifest_sha
    dataset = dataset_details(manifest, manifest_sha)
    sizes = ", ".join(f"{name}={info['rows']:,}" for name, info in dataset["splits"].items())
    progress(f"dataset {dataset['version']} [{dataset['id']}]: {sizes}")
    runs = work / "runs"
    runs.mkdir(exist_ok=True)
    train_table = sample_table(work, "TRAIN", columns, cfg, cfg["train_cap_per_class"], manifest=manifest)
    val_cap = cfg["validation_cap"] if cfg["preset"] == "QUICK_EXPERIMENT" else 0
    val_table = sample_table(work, "VALIDATION", columns, cfg, val_cap, manifest=manifest)
    xt, yt = as_xy(train_table, columns)
    xv, yv = as_xy(val_table, columns)
    if len(np.unique(yt)) < 2 or len(np.unique(yv)) < 2:
        raise ValueError("MISSING_BINARY_CLASS")
    for name in selected:
        progress(f"fit {name}: train {len(yt):,}/{dataset['splits']['TRAIN']['rows']:,}; "
                 f"validation {len(yv):,}/{dataset['splits']['VALIDATION']['rows']:,}")
        model, scaling = estimator(name, cfg)
        threads = cfg["hyperparameters"][name].get("n_jobs") or cfg["threads"]
        groups = (train_table["group_id"].to_numpy(zero_copy_only=False)
                  if cfg["hyperparameters"][name].get("early_stopping_rounds") else None)
        with threadpool_limits(limits=threads):
            start = perf_counter()
            fitted_rows, stopping_rows = fit_estimator(model, name, cfg, xt, yt, groups)
            elapsed = perf_counter() - start
            vs, latency = predict(model, xv)
            threshold = decision_threshold(yv, vs, cfg["normal_fpr_limit"])
            ts, _ = predict(model, xt)
        run_id = uuid4().hex
        folder = runs / run_id
        folder.mkdir()
        artifact = folder / "model.joblib"
        joblib.dump(model, artifact, compress=3)
        classifier = model["classifier"]
        importance = getattr(classifier, "feature_importances_", None)
        if importance is None and hasattr(classifier, "coef_"):
            importance = classifier.coef_[0]
        report = {"run_id": run_id, "algorithm": name, "created_utc": datetime.now(timezone.utc).isoformat(),
                  "model_version": "network-ids-model-1", "status": BENCHMARK,
                  "run_type": "EXPERIMENTAL" if val_cap else "FULL_EVALUATION",
                  "dataset_id": dataset["id"], "dataset_version": dataset["version"], "dataset": dataset,
                  "dataset_manifest_sha256": manifest_sha,
                  "schema": contract(cfg["features"]), "scaling": scaling, "imputer": "TRAIN_MEDIAN",
                  "label_mapping": {"NORMAL":0,"ATTACK":1}, "threshold": threshold, "config": cfg,
                  "rows": {"used_train": len(yt), "fitted_train": fitted_rows, "early_stopping": stopping_rows,
                           "available_train": manifest["splits"]["TRAIN"]["rows"],
                           "used_validation": len(yv), "available_validation": manifest["splits"]["VALIDATION"]["rows"],
                           "train_normal": int((yt == 0).sum()), "train_attack": int((yt == 1).sum())},
                  "training_seconds": elapsed, "inference_ms_per_row_batch": latency,
                  "train": metrics(yt, ts, threshold), "validation": report_metrics(yv, vs, threshold, val_table),
                  "test": None, "model_bytes": artifact.stat().st_size, "model_sha256": digest(artifact),
                  "versions": {p: version(p) for p in ("numpy","scikit-learn","xgboost-cpu","lightgbm","duckdb")},
                  "feature_importance": dict(zip(columns, map(float, importance))) if importance is not None else None}
        write_json(folder / "report.json", report)
        run_ids.append(run_id)
        validation = report["validation"]
        validation_rank.append((validation["attack_recall"], -validation["fpr"], validation["f1"], run_id))
        progress(f"completed {name} {run_id}")
    best = max(validation_rank, key=lambda item: item[:3])[-1]
    progress(f"best validation run: {best}")
    result = {"runs": run_ids, "best_validation_run": best, "selection_uses": "VALIDATION_ONLY"}
    if cfg["auto_test"]:
        result["automatic_test"] = test_after_training(work, best, cfg, progress)
        folder = runs / best
        report = read_json(folder / "report.json")
        report["automatic_test"] = result["automatic_test"]
        write_json(folder / "report.json", report)
    return result


def test_after_training(work, best, cfg, progress):
    if cfg["preset"] == "QUICK_EXPERIMENT":
        progress("auto test: validation only (QUICK_EXPERIMENT)")
        return {"status": "VALIDATION_ONLY_QUICK"}
    try:
        existing = read_test_selection(work)
        if existing and existing["run_id"] != best and not cfg["reuse_test"]:
            progress(f"auto test skipped: test locked to {existing['run_id']}")
            return {"status": "SKIPPED_LOCKED", "locked_run_id": existing["run_id"]}
        progress(f"auto test started for validation winner {best}")
        return final_evaluation(work, best, progress, selection="AUTOMATIC_VALIDATION_ONLY",
                                reuse_test=cfg["reuse_test"])
    except (OSError, ValueError) as error:
        if str(error) == "TEST_ALREADY_SEALED_TO_ANOTHER_RUN":
            selection = read_test_selection(work)
            if selection and selection["run_id"] != best:
                progress(f"auto test skipped: test locked to {selection['run_id']}")
                return {"status": "SKIPPED_LOCKED", "locked_run_id": selection["run_id"]}
        progress(f"auto test failed: {error}")
        return {"status": "TEST_FAILED", "run_id": best, "reason": str(error)}


def repeat_files(folder):
    return sorted((folder / "test-repeats").glob("*.json"), reverse=True)


def report_with_repeated_tests(folder, report):
    files = repeat_files(folder)
    latest = read_json(files[0]) if files else None
    history = []
    for path in files[:20]:
        attempt = latest if path == files[0] else read_json(path)
        history.append({**{key: attempt[key] for key in ("id", "evaluated_utc", "threshold", "selection")},
                        "attack_recall": attempt["metrics"]["attack_recall"],
                        "fpr": attempt["metrics"]["fpr"]})
    return {**report, "repeated_test": latest, "test_repeat_count": len(files), "test_repeat_history": history}


def registry(work):
    path = work / "runs"
    if not path.is_dir():
        return []
    if not (work / "manifest.json").is_file():
        return []
    manifest, manifest_sha = dataset_manifest(work)
    results = []
    for report in path.glob("*/report.json"):
        row = read_json(report)
        if not matches_dataset(row, manifest_sha):
            continue
        results.append({k: row[k] for k in ("run_id","algorithm","created_utc","rows","scaling","model_bytes",
                        "training_seconds","inference_ms_per_row_batch","validation","test","model_sha256",
                        "threshold","schema","run_type","dataset_id")})
        results[-1]["dataset_version"] = manifest["version"]
        results[-1]["test_repeat_count"] = len(repeat_files(report.parent))
        results[-1]["validation"] = {k: v for k,v in row["validation"].items()
                                    if k not in ("curves","scenarios","groups","thresholds")}
    return sorted(results, key=lambda item: item["created_utc"], reverse=True)


def read_test_selection(work):
    path = work / "test-selection.json"
    if not path.is_file():
        return None
    selection = read_json(path)
    if (not isinstance(selection, dict) or not isinstance(selection.get("run_id"), str)
            or selection.get("dataset_id") != identifier(work)):
        raise ValueError("TEST_LOCK_DATASET_MISMATCH")
    return selection


def seal_test(work, record):
    path = work / "test-selection.json"
    try:
        with path.open("x", encoding="utf-8") as file:
            json.dump(record, file, indent=2, allow_nan=False)
            file.write("\n")
    except FileExistsError:
        current = read_test_selection(work)
        if (not current or current["run_id"] != record["run_id"]
                or current.get("threshold") != record["threshold"]):
            raise ValueError("TEST_ALREADY_SEALED_TO_ANOTHER_RUN") from None


def final_evaluation(work, run_id, progress=print, *, selection="OPERATOR_VALIDATION_ONLY", reuse_test=False):
    folder, selected = verify_run(work / "runs", run_id)
    manifest, manifest_sha = dataset_manifest(work)
    if not matches_dataset(selected, manifest_sha):
        raise ValueError("DATASET_MISMATCH")
    if selected["run_type"] == "EXPERIMENTAL":
        raise ValueError("QUICK_RUN_VALIDATION_ONLY")
    current = read_test_selection(work)
    repeated = reuse_test and current is not None
    if not repeated:
        if current and current["run_id"] != run_id:
            raise ValueError("TEST_ALREADY_SEALED_TO_ANOTHER_RUN")
        if selected["test"] is not None:
            return {"run_id": run_id, "status": "TEST_COMPLETE"}
    check_evaluation_memory(manifest["splits"]["TEST"]["rows"], selected["config"])
    if not repeated:
        # Before reading test rows, reserve this fixed model and validation threshold.
        seal_test(work, {"run_id": run_id, "dataset_id": selected["dataset_id"],
                         "threshold": selected["threshold"], "selection": selection})
    columns = selected["schema"]["order"]
    table = sample_table(work, "TEST", columns, selected["config"], 0, manifest=manifest)
    x, y = as_xy(table, columns)
    progress(f"{'reused' if repeated else 'sealed'} TEST rows={len(y):,}")
    model = joblib.load(folder / "model.joblib")
    cfg = selected["config"]
    jobs = cfg["hyperparameters"].get(selected["algorithm"], {}).get("n_jobs")
    with threadpool_limits(limits=jobs or cfg["threads"]):
        scores, delay = predict(model, x)
    evaluated = report_metrics(y, scores, selected["threshold"], table, compare_thresholds=False)
    evaluated["inference_ms_per_row_batch"] = delay
    if repeated:
        now = datetime.now(timezone.utc)
        attempt = f"{now:%Y%m%dT%H%M%S%f}-{uuid4().hex}"
        target = folder / "test-repeats"
        target.mkdir(exist_ok=True)
        write_json(target / f"{attempt}.json", {
            "id": attempt, "status": "REUSED_TEST_BENCHMARK", "selection": selection,
            "evaluated_utc": now.isoformat(), "dataset_id": selected["dataset_id"],
            "original_test_run_id": current["run_id"], "threshold": selected["threshold"], "metrics": evaluated,
        })
        return {"run_id": run_id, "status": "TEST_COMPLETE_REUSED", "attempt_id": attempt}
    selected["test"] = evaluated
    write_json(folder / "report.json", selected)
    return {"run_id": run_id, "status": "TEST_COMPLETE"}


def export(work, run_id):
    folder, report = verify_run(work / "runs", run_id)
    report = run_dataset_view(work, report)
    exports = work / "exports"
    exports.mkdir(exist_ok=True)
    destination = exports / run_id
    pending = exports / f"{run_id}-{uuid4().hex[:8]}"
    pending.mkdir()
    shutil.copy2(folder / "model.joblib", pending / "model.joblib")
    write_json(pending / "evaluation.json", report_with_repeated_tests(folder, report))
    shutil.copy2(work / "manifest.json", pending / "dataset_manifest.json")
    write_json(pending / "feature_contract.json", {**report["schema"], "threshold": report["threshold"],
                                                  "label_mapping": report["label_mapping"]})
    write_json(pending / "sha256.json", {f.name: digest(f) for f in pending.iterdir() if f.is_file()})
    if not destination.exists():
        pending.rename(destination)
    else:
        destination = pending
    return {"path": str(destination), "status": BENCHMARK}
