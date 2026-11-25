"""Run repeatable XGBoost detection evaluations from a pinned labeled Parquet split."""
import argparse
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import pyarrow as pa

from security_ai_v2.rebuild.contract import BASIC_10, PARITY_BLOCK
from security_ai_v2.rebuild.data import WORK, connect, digest, read_json, sql_column, write_json
from security_ai_v2.rebuild.train import as_xy, dataset_manifest, matches_dataset, metrics, predict, verify_run
from security_ai_v3.xgboost_shadow import ROOT, load_model

FAMILIES = {"BENIGN": 0, "DOS": 1, "DDOS": 1}
CONFIG = ROOT / "security_ai_v3/config/ai_server.json"
SAMPLE_ORDER = ("sample_order", "source_sha256", "source_name", "row_id")


def load_sample(work, model_report, seed, per_family):
    manifest, manifest_hash = dataset_manifest(work)
    source = work / "test.parquet"
    test_hash = manifest["splits"]["TEST"]["sha256"]
    if not matches_dataset(model_report, manifest_hash) or digest(source) != test_hash:
        raise ValueError("Test dataset does not match the pinned model report")

    columns = ", ".join(map(sql_column, (*BASIC_10, "y", "family", "group_id")))
    query = f"""
        SELECT {columns}, source_sha256, source_name, row_id, hash(source_sha256, row_id, ?) AS sample_order
        FROM read_parquet(?)
        WHERE family = ?
        ORDER BY sample_order, source_sha256, source_name, row_id
        LIMIT ?
    """
    samples = {}
    with TemporaryDirectory(prefix="xgboost-offline-") as scratch, connect(Path(scratch), 256, 2, 2) as db:
        for family, label in FAMILIES.items():
            table = db.sql(query, params=[seed, str(source), family, per_family]).to_arrow_table()
            if len(table) != per_family:
                raise ValueError(f"{family} has only {len(table)} of {per_family} requested test rows")
            if not all(value == label for value in table["y"].to_pylist()):
                raise ValueError(f"Unexpected binary labels for {family}")
            # Stabilize round boundaries even when the engine streams parallel Arrow batches.
            samples[family] = table.sort_by([(column, "ascending") for column in SAMPLE_ORDER]).drop_columns(SAMPLE_ORDER)
    return samples, {
        "manifest_sha256": manifest_hash,
        "test_sha256": test_hash,
        "group_policy": manifest.get("group_policy"),
        "dataset_version": manifest["version"],
        "prior_test_evaluation_in_run_report": bool(model_report.get("test")),
    }


def summarize(table, scores, thresholds):
    truth = table["y"].to_numpy(zero_copy_only=False)
    families = np.asarray(table["family"].to_pylist())
    results = {}
    for name, threshold in thresholds.items():
        result = metrics(truth, scores, threshold)
        alerts = scores >= threshold
        result["threshold"] = threshold
        result["families"] = {}
        for family in FAMILIES:
            mask = families == family
            count = int(np.count_nonzero(mask))
            flagged = int(np.count_nonzero(alerts & mask))
            result["families"][family] = {"rows": count, "flagged_attack": flagged,
                                         "attack_flag_rate": flagged / count}
        results[name] = result
    return {"groups_represented": len(set(table["group_id"].to_pylist())), "profiles": results}


def evaluate(work, config_path, rounds, rows_per_family, seed):
    settings = read_json(config_path)["xgboost_shadow"]
    folder, model_report = verify_run(work / "runs", settings["run_id"])
    artifact = (ROOT / settings["artifact"]).resolve()
    if (artifact != (folder / "model.joblib").resolve() or model_report["model_sha256"] != settings["sha256"]
            or tuple(settings["feature_order"]) != BASIC_10 or tuple(model_report["schema"]["order"]) != BASIC_10):
        raise ValueError("Model, runtime configuration and Training Studio run disagree")

    samples, dataset = load_sample(work, model_report, seed, rounds * rows_per_family)
    model = load_model({"artifact": artifact, "sha256": settings["sha256"]})
    thresholds = {"benchmark": settings["benchmark_threshold"], **settings.get("idle_thresholds", {})}
    tables, score_batches, results = [], [], []
    for index in range(rounds):
        offset = index * rows_per_family
        table = pa.concat_tables([sample.slice(offset, rows_per_family) for sample in samples.values()])
        values, _ = as_xy(table, BASIC_10)
        scores, prediction_ms_per_row = predict(model, values)
        tables.append(table)
        score_batches.append(scores)
        results.append({"round": index + 1, "prediction_ms_per_row": prediction_ms_per_row,
                        **summarize(table, scores, thresholds)})

    return {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "status": "OFFLINE_LABELLED_TEST",
        "decision_eligible": False,
        "live_feature_parity": PARITY_BLOCK,
        "model": {"run_id": settings["run_id"], "sha256": settings["sha256"], "feature_order": BASIC_10},
        "dataset": dataset,
        "sampling": {"split": "TEST", "seed": seed, "rows_per_family_per_round": rows_per_family,
                     "rounds": rounds, "total_rows": rounds * rows_per_family * len(FAMILIES),
                     "families": list(FAMILIES), "row_reuse_between_rounds": False},
        "limitations": [
            "Rounds can contain different rows from the same dataset group.",
            "Precision depends on this balanced family mix and does not estimate production precision.",
            "Runtime thresholds are compared on the same dataset rows; profile names do not identify live devices.",
            "Results do not test the live collector, target attribution or isolation decisions.",
        ],
        "rounds": results,
        "pooled": summarize(pa.concat_tables(tables), np.concatenate(score_batches), thresholds),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, default=WORK)
    parser.add_argument("--config", type=Path, default=CONFIG)
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--per-family", type=int, default=512, help="Test rows per family in each round.")
    parser.add_argument("--seed", type=int, default=20261124)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not (1 <= args.rounds <= 100 and 1 <= args.per_family and args.rounds * args.per_family <= 50000
            and 0 <= args.seed < 2**31):
        parser.error("Require 1 to 100 rounds and 1 to 50000 total sampled rows per family")
    if args.output.suffix.lower() != ".json" or args.output.exists():
        parser.error("Choose a new JSON report path")
    try:
        report = evaluate(args.work.resolve(), args.config, args.rounds, args.per_family, args.seed)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        write_json(args.output, report)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        parser.error(str(exc))
    print(f"{report['sampling']['total_rows']} rows across {args.rounds} rounds. Report saved to {args.output}")


if __name__ == "__main__":
    main()
