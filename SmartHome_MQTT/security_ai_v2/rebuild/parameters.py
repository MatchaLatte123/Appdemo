"""Studio parameter contract for the CPU binary-classification pipeline."""
from math import isfinite

ALGORITHMS = ("LOGISTIC", "RANDOM_FOREST", "EXTRA_TREES", "XGBOOST", "LIGHTGBM", "HIST_GRADIENT")
LEVELS = ("quick", "default", "strong")

PRESET_VALUES = {
    "LOGISTIC": {"C": (.5, 1., 3.), "max_iter": (200, 400, 800)},
    "RANDOM_FOREST": {"n_estimators": (40, 120, 240), "max_depth": (10, 16, 24), "min_samples_leaf": (6, 3, 2)},
    "EXTRA_TREES": {"n_estimators": (32, 100, 180), "max_depth": (10, 16, 24), "min_samples_leaf": (6, 3, 2)},
    "XGBOOST": {"n_estimators": (50, 150, 300), "max_depth": (4, 6, 8), "learning_rate": (.1, .07, .04)},
    "LIGHTGBM": {"n_estimators": (50, 150, 300), "num_leaves": (15, 31, 63), "learning_rate": (.1, .07, .04)},
    "HIST_GRADIENT": {"max_iter": (50, 125, 250), "max_leaf_nodes": (15, 31, 63), "learning_rate": (.12, .08, .05)},
}


def number(default, minimum, maximum, kind="float"):
    return {"kind": kind, "default": default, "min": minimum, "max": maximum}


def integer(default, minimum, maximum):
    return number(default, minimum, maximum, "integer")


def choice(default, *choices):
    return {"kind": "choice", "default": default, "choices": choices}


def boolean(default):
    return choice(default, False, True)


def structured(default, kind, hint):
    return {"kind": kind, "default": default, "hint": hint}


COUNT_OR_RATE = "Số nguyên đếm mẫu hoặc tỷ lệ dạng thập phân."
FEATURE_COUNT = "Số nguyên đếm đặc trưng, tỷ lệ nhỏ hơn 1, hoặc sqrt/log2 khi hỗ trợ."
MONOTONIC = structured(None, "monotone", "Mảng -1, 0, 1 theo thứ tự các đặc trưng.")
INTERACTIONS = structured(None, "interactions", "Mảng các nhóm chỉ số đặc trưng bắt đầu từ 0, ví dụ [[0,1],[2,3]].")
WEIGHTS = structured(None, "class_weight", 'Nhập "balanced" hoặc {"0":1,"1":2}. Giá trị riêng thay cho cân bằng chung.')
REPRODUCIBILITY = {
    "random_state": integer(None, 0, 2**31 - 1),
    "n_jobs": integer(None, 1, 8),
}

FOREST = {
    "n_estimators": integer(100, 1, 4000),
    "criterion": choice("gini", "gini", "entropy", "log_loss"),
    "max_depth": integer(None, 1, 128),
    "min_samples_split": structured(2, "min_split", COUNT_OR_RATE),
    "min_samples_leaf": structured(1, "sample_count", COUNT_OR_RATE),
    "min_weight_fraction_leaf": number(0., 0., .5),
    "max_features": structured("sqrt", "feature_count", FEATURE_COUNT),
    "max_leaf_nodes": integer(None, 2, 1_000_000),
    "min_impurity_decrease": number(0., 0., 1_000_000),
    "max_samples": structured(None, "sample_count", COUNT_OR_RATE),
    "oob_score": boolean(False),
    "class_weight": {**WEIGHTS, "hint": WEIGHTS["hint"] + ' Forest cũng hỗ trợ "balanced_subsample".'},
    "ccp_alpha": number(0., 0., 1_000_000),
    "monotonic_cst": MONOTONIC,
    "verbose": integer(0, 0, 10),
}

EARLY_STOP = {
    "early_stopping_rounds": integer(None, 1, 100_000),
    "early_stopping_validation_fraction": number(.15, .01, .5),
}

SCHEMAS = {
    "LOGISTIC": {
        "C": {**number(1., 1e-12, 1e12), "nullable": True, "hint": "null tương ứng không regularization."},
        "l1_ratio": number(0., 0., 1.),
        "solver": choice("lbfgs", "lbfgs", "liblinear", "newton-cg", "newton-cholesky", "sag", "saga"),
        "dual": boolean(False),
        "fit_intercept": boolean(True),
        "intercept_scaling": number(1., 1e-12, 1e12),
        "class_weight": WEIGHTS,
        "max_iter": integer(100, 1, 100_000),
        "tol": number(.0001, 0., 1.),
        "verbose": integer(0, 0, 10),
    },
    "RANDOM_FOREST": {"bootstrap": boolean(True), **FOREST},
    "EXTRA_TREES": {"bootstrap": boolean(False), **FOREST},
    "HIST_GRADIENT": {
        "learning_rate": number(.1, 1e-9, 10),
        "max_iter": integer(100, 1, 20_000),
        "max_leaf_nodes": integer(31, 2, 1_000_000),
        "max_depth": integer(None, 1, 128),
        "min_samples_leaf": integer(20, 1, 10_000_000),
        "l2_regularization": number(0., 0., 1e12),
        "max_features": number(1., 1e-9, 1.),
        "max_bins": integer(255, 2, 255),
        "class_weight": WEIGHTS,
        "monotonic_cst": MONOTONIC,
        "interaction_cst": {**INTERACTIONS, "hint": INTERACTIONS["hint"] + ' Hoặc "pairwise"/"no_interactions".'},
        "early_stopping": choice("auto", "auto", False, True),
        "scoring": choice("loss", "loss", "accuracy", "balanced_accuracy", "f1", "roc_auc",
                          "average_precision", "neg_log_loss"),
        "validation_fraction": structured(.1, "sample_count", COUNT_OR_RATE),
        "n_iter_no_change": integer(10, 1, 100_000),
        "tol": number(1e-7, 0., 1.),
        "verbose": integer(0, 0, 10),
    },
    "XGBOOST": {
        "n_estimators": integer(None, 1, 20_000),
        "max_depth": integer(None, 0, 128),
        "max_leaves": integer(None, 0, 1_000_000),
        "max_bin": integer(None, 2, 65536),
        "learning_rate": number(None, 0., 1.),
        "booster": choice("gbtree", "gbtree", "dart"),
        "tree_method": choice("hist", "hist", "approx", "exact"),
        "grow_policy": choice("depthwise", "depthwise", "lossguide"),
        "subsample": number(None, 1e-9, 1.),
        "colsample_bytree": number(None, 1e-9, 1.),
        "colsample_bylevel": number(None, 1e-9, 1.),
        "colsample_bynode": number(None, 1e-9, 1.),
        "min_child_weight": number(None, 0., 1e12),
        "max_delta_step": number(None, 0., 1e12),
        "gamma": number(None, 0., 1e12),
        "reg_alpha": number(None, 0., 1e12),
        "reg_lambda": number(None, 0., 1e12),
        "base_score": number(None, 1e-9, 1. - 1e-9),
        "scale_pos_weight": number(None, 1e-9, 1e12),
        "num_parallel_tree": integer(None, 1, 256),
        "feature_weights": structured(None, "feature_weights", "Mảng trọng số dương theo thứ tự đặc trưng."),
        "monotone_constraints": MONOTONIC,
        "interaction_constraints": INTERACTIONS,
        "eval_metric": choice(None, "logloss", "error", "auc", "aucpr", "rmse", "mae"),
        "importance_type": choice(None, "gain", "weight", "cover", "total_gain", "total_cover"),
        "verbosity": integer(None, 0, 3),
        "sample_type": choice(None, "uniform", "weighted"),
        "normalize_type": choice(None, "tree", "forest"),
        "rate_drop": number(None, 0., 1.),
        "skip_drop": number(None, 0., 1.),
        "one_drop": boolean(False),
        **EARLY_STOP,
    },
    "LIGHTGBM": {
        "boosting_type": choice("gbdt", "gbdt", "dart", "rf"),
        "data_sample_strategy": choice("bagging", "bagging", "goss"),
        "n_estimators": integer(100, 1, 20_000),
        "num_leaves": integer(31, 2, 131072),
        "max_depth": integer(-1, -1, 128),
        "learning_rate": number(.1, 1e-9, 10),
        "min_child_samples": integer(20, 1, 10_000_000),
        "min_child_weight": number(.001, 0., 1e12),
        "min_split_gain": number(0., 0., 1e12),
        "colsample_bytree": number(1., 1e-9, 1.),
        "feature_fraction_bynode": number(1., 1e-9, 1.),
        "subsample": number(1., 1e-9, 1.),
        "subsample_freq": integer(0, 0, 100_000),
        "pos_bagging_fraction": number(1., 1e-9, 1.),
        "neg_bagging_fraction": number(1., 1e-9, 1.),
        "subsample_for_bin": integer(200000, 1, 10_000_000),
        "max_bin": integer(255, 2, 65536),
        "max_bin_by_feature": structured(None, "feature_bins", "Mảng số bin từ 2 theo thứ tự đặc trưng."),
        "min_data_in_bin": integer(3, 1, 10_000_000),
        "reg_alpha": number(0., 0., 1e12),
        "reg_lambda": number(0., 0., 1e12),
        "max_delta_step": number(0., 0., 1e12),
        "path_smooth": number(0., 0., 1e12),
        "linear_tree": boolean(False),
        "linear_lambda": number(0., 0., 1e12),
        "extra_trees": boolean(False),
        "feature_fraction_seed": integer(2, 0, 2**31 - 1),
        "bagging_seed": integer(3, 0, 2**31 - 1),
        "data_random_seed": integer(1, 0, 2**31 - 1),
        "extra_seed": integer(6, 0, 2**31 - 1),
        "drop_seed": integer(4, 0, 2**31 - 1),
        "feature_pre_filter": boolean(True),
        "enable_bundle": boolean(True),
        "use_missing": boolean(True),
        "zero_as_missing": boolean(False),
        "histogram_pool_size": number(-1., -1., 8192),
        "boost_from_average": boolean(True),
        "class_weight": WEIGHTS,
        "is_unbalance": boolean(False),
        "scale_pos_weight": number(1., 1e-9, 1e12),
        "sigmoid": number(1., 1e-9, 100),
        "top_rate": number(.2, 1e-9, 1.),
        "other_rate": number(.1, 1e-9, 1.),
        "drop_rate": number(.1, 0., 1.),
        "max_drop": integer(50, -1, 20_000),
        "skip_drop": number(.5, 0., 1.),
        "xgboost_dart_mode": boolean(False),
        "uniform_drop": boolean(False),
        "monotone_constraints": MONOTONIC,
        "monotone_constraints_method": choice("basic", "basic", "intermediate", "advanced"),
        "monotone_penalty": number(0., 0., 128),
        "interaction_constraints": INTERACTIONS,
        "metric": choice(None, "binary_logloss", "binary_error", "auc", "average_precision"),
        "importance_type": choice("split", "split", "gain"),
        "verbosity": integer(-1, -1, 3),
        **EARLY_STOP,
    },
}
# Constructor and backend-only controls that have no independent meaning for a fit on the prepared dataset.
FIXED = {
    "LOGISTIC": ["penalty (dùng C và l1_ratio)", "warm_start"],
    "RANDOM_FOREST": ["warm_start"],
    "EXTRA_TREES": ["warm_start"],
    "HIST_GRADIENT": ["loss (log_loss)", "categorical_features (đặc trưng số)", "warm_start"],
    "XGBOOST": ["device (CPU)", "objective (binary:logistic)", "sampling_method (uniform trên CPU)",
                "categorical parameters", "callbacks (do Studio quản lý)", "native aliases"],
    "LIGHTGBM": ["device (CPU)", "objective (binary)", "callbacks (do Studio quản lý)",
                 "deterministic CPU settings", "CLI, file, distributed và native aliases"],
}


def schema():
    return {name: {**items, **REPRODUCIBILITY} for name, items in SCHEMAS.items()}


def presets():
    return {name: {level: {key: values[i] for key, values in fields.items()}
                   for i, level in enumerate(LEVELS)} for name, fields in PRESET_VALUES.items()}


def numeric(value, low, high, *, integer_only=False):
    return (type(value) in ((int,) if integer_only else (int, float)) and
            -1e300 <= value <= 1e300 and isfinite(value) and low <= value <= high)


def valid(name, key, rule, value, n_features):
    kind = rule["kind"]
    if value is None:
        return (rule["default"] is None or key in ("max_leaf_nodes", "validation_fraction")
                or rule.get("nullable") or kind == "feature_count")
    if kind == "choice":
        return any(type(value) is type(option) and value == option for option in rule["choices"])
    if kind in ("integer", "float"):
        return numeric(value, rule["min"], rule["max"], integer_only=kind == "integer")
    if kind in ("feature_count", "min_split", "sample_count"):
        max_count = n_features if kind == "feature_count" else 10_000_000
        options = ("sqrt", "log2") if kind == "feature_count" else ()
        return (value in options or numeric(value, 2 if kind == "min_split" else 1, max_count, integer_only=True)
                or type(value) is float and numeric(value, 1e-12, 1.0))
    if kind == "class_weight":
        allowed = ("balanced", "balanced_subsample") if name in ("RANDOM_FOREST", "EXTRA_TREES") else ("balanced",)
        return (value in allowed or isinstance(value, dict) and set(value) == {"0", "1"}
                and all(numeric(v, 1e-12, 1e12) for v in value.values()))
    if kind == "monotone":
        return isinstance(value, list) and len(value) == n_features and all(
            numeric(v, -1, 1, integer_only=True) for v in value)
    if kind == "interactions":
        return (name == "HIST_GRADIENT" and value in ("pairwise", "no_interactions")
                or isinstance(value, list) and 1 <= len(value) <= n_features and all(
                    isinstance(group, list) and 1 <= len(group) <= n_features and
                    all(numeric(v, 0, n_features - 1, integer_only=True) for v in group)
                    for group in value))
    if kind == "feature_bins":
        return isinstance(value, list) and len(value) == n_features and all(
            numeric(v, 2, 65536, integer_only=True) for v in value)
    if kind == "feature_weights":
        return isinstance(value, list) and len(value) == n_features and all(
            numeric(v, 1e-12, 1e12) for v in value)
    return False


def validate_parameters(algorithm, edited, level, n_features, threads):
    rules = schema()[algorithm]
    if not isinstance(edited, dict):
        raise ValueError(f"{algorithm}: hyperparameters phải là đối tượng JSON.")
    for key, value in edited.items():
        if key not in rules:
            raise ValueError(f"{algorithm}: không hỗ trợ tham số {key}.")
        if not valid(algorithm, key, rules[key], value, n_features):
            raise ValueError(f"{algorithm}: giá trị {key} không hợp lệ.")
    params = {**presets()[algorithm][level], **edited}
    params_current = {key: field["default"] for key, field in rules.items()} | params
    if (params_current["n_jobs"] or threads) > threads:
        raise ValueError(f"{algorithm}: n_jobs vượt giới hạn luồng CPU của lượt chạy.")
    if algorithm == "LOGISTIC":
        solver, ratio = params_current["solver"], params_current["l1_ratio"]
        if (0 < ratio < 1 and solver != "saga" or ratio == 1 and solver not in ("liblinear", "saga")
                or params_current["dual"] and (solver != "liblinear" or ratio != 0)
                or params_current["C"] is None and (solver == "liblinear" or ratio != 0)):
            raise ValueError(f"{algorithm}: solver, dual và l1_ratio không tương thích.")
    if algorithm in ("RANDOM_FOREST", "EXTRA_TREES") and not params_current["bootstrap"]:
        if params_current["oob_score"] or params_current["max_samples"] is not None:
            raise ValueError(f"{algorithm}: oob_score và max_samples yêu cầu bootstrap=true.")
    if algorithm == "HIST_GRADIENT" and params_current["validation_fraction"] == 1:
        raise ValueError(f"{algorithm}: validation_fraction phải nhỏ hơn 1 hoặc có ít nhất 2 mẫu.")
    if algorithm == "XGBOOST":
        if params_current["max_depth"] == 0 and params_current["tree_method"] == "exact":
            raise ValueError(f"{algorithm}: tree_method=exact yêu cầu max_depth > 0.")
        if params_current["booster"] != "dart" and any(
                key in edited and edited[key] not in (None, False)
                for key in ("sample_type", "normalize_type", "rate_drop", "skip_drop", "one_drop")):
            raise ValueError(f"{algorithm}: tham số dropout yêu cầu booster=dart.")
    if algorithm == "LIGHTGBM":
        mode = params_current["boosting_type"]
        if (params_current["class_weight"] is not None) + params_current["is_unbalance"] + (
                params_current["scale_pos_weight"] != 1.) > 1:
            raise ValueError(f"{algorithm}: chỉ chọn một trong class_weight, is_unbalance, scale_pos_weight.")
        if mode == "dart" and params_current["early_stopping_rounds"]:
            raise ValueError(f"{algorithm}: early stopping không hỗ trợ boosting_type=dart.")
        if mode == "rf" and not (params_current["colsample_bytree"] < 1 or
                params_current["subsample"] < 1 and params_current["subsample_freq"] > 0):
            raise ValueError(f"{algorithm}: boosting_type=rf cần feature fraction hoặc bagging.")
        if params_current["data_sample_strategy"] == "goss" and (
                params_current["top_rate"] + params_current["other_rate"] > 1
                or params_current["subsample_freq"] > 0 and params_current["subsample"] < 1):
            raise ValueError(f"{algorithm}: GOSS yêu cầu top_rate + other_rate ≤ 1 và không dùng bagging.")
    return params
