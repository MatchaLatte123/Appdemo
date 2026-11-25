"""Score recorded features or read local capture metadata completely offline."""
from ipaddress import IPv4Address
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory

import numpy as np

from security_ai_v2.rebuild.contract import BASIC_10, PARITY_BLOCK, from_tshark
from security_ai_v2.rebuild.data import connect, digest, sql_column
from security_ai_v2.rebuild.train import as_xy, dataset_manifest, matches_dataset, predict, verify_run
from security_ai_v3.capture import TSHARK
from security_ai_v3.cic_input_probe import CicInputProbe
from security_ai_v3.live_monitor import FIELDS, camera_endpoints
from security_ai_v3.xgboost_shadow import load_model, probabilities

from .ai_client import ROOT
from .evaluator import binary

FAMILIES = {"BENIGN": 0, "DOS": 1, "DDOS": 1}
MAX_ROWS = 5000


class OfflineReplay:
    def __init__(self, deployment):
        self.deployment = deployment
        self.settings = deployment.ai_config["xgboost_shadow"]
        artifact = (ROOT / self.settings["artifact"]).resolve()
        self.work = artifact.parents[2]
        folder, self.training = verify_run(self.work / "runs", self.settings["run_id"])
        if (artifact != folder / "model.joblib" or self.training["model_sha256"] != self.settings["sha256"]
                or tuple(self.training["schema"]["order"]) != BASIC_10
                or tuple(self.settings["feature_order"]) != BASIC_10):
            raise ValueError("Model artifact, configuration and Studio report disagree")
        self.model = load_model({"artifact": artifact, "sha256": self.settings["sha256"]})
        self.model_parameters = {key: self.model.named_steps["classifier"].get_params()[key]
                                 for key in ("n_estimators", "max_depth", "learning_rate")}
        expected = self.training["config"]["hyperparameters"][self.training["algorithm"]]
        if any(expected.get(k) != v for k, v in self.model_parameters.items()):
            raise ValueError("Artifact hyperparameters disagree with the training report")
        self.transformer = self.model[:-1]

    def threshold(self, scope):
        return self.settings.get("idle_thresholds", {}).get(scope, self.settings["benchmark_threshold"])

    def scenarios(self):
        manifest, manifest_hash = dataset_manifest(self.work)
        if not matches_dataset(self.training, manifest_hash):
            raise ValueError("Pinned test manifest no longer matches model report")
        return sorted({row["scenario"] for row in manifest["splits"]["TEST"]["scenarios"]})

    def dataset(self, path, scope, family, scenario="", limit=1000):
        manifest, identity = dataset_manifest(self.work)
        if not matches_dataset(self.training, identity):
            raise ValueError("Pinned test manifest no longer matches model report")
        source = Path(path) if path else self.work / "test.parquet"
        if source.suffix.lower() not in (".parquet", ".csv") or not source.is_file():
            raise ValueError("Select a local CSV or Parquet file")
        source_hash = digest(source)
        verified = source.suffix.lower() == ".parquet" and source_hash == manifest["splits"]["TEST"]["sha256"]
        if source.suffix.lower() == ".parquet" and not verified:
            raise ValueError("Parquet SHA-256 differs from the pinned TEST split")
        if family not in ("ALL", *FAMILIES) or not 1 <= limit <= MAX_ROWS:
            raise ValueError("Invalid family or row limit")
        reader = "read_parquet(?)" if verified else "read_csv_auto(?)"
        columns = ", ".join(map(sql_column, (*BASIC_10, "y", "family", "scenario")))
        order = " ORDER BY hash(source_sha256,row_id,42),source_name,row_id" if verified else ""
        selection = tuple(FAMILIES) if family == "ALL" else (family,)
        y, predicted, records = [], [], []
        limit = int(limit)
        threshold = self.threshold(scope)
        with TemporaryDirectory(prefix="iot-studio-") as tmp, connect(Path(tmp), 256, 2, 2) as db:
            for group in selection:
                table = db.sql(f"SELECT {columns} FROM {reader} WHERE family=? AND (?='' OR scenario=?) {order} LIMIT ?",
                               params=[str(source.resolve()), group, scenario, scenario, limit]).to_arrow_table()
                if not len(table):
                    continue
                values, labels = as_xy(table, BASIC_10)
                if not np.all(labels == FAMILIES[group]):
                    raise ValueError("Selected rows contradict the declared family labels")
                scores, elapsed = predict(self.model, values)
                guessed = scores >= threshold
                y.extend(int(label) for label in labels)
                predicted.extend(int(label) for label in guessed)
                records.extend({"family": group, "scenario": name, "expected": int(label), "prediction": int(guess),
                                "score": float(score), "batch_ms_per_row": elapsed,
                                "ground_truth": label_source(group, verified)}
                               for name, label, guess, score in zip(table["scenario"].to_pylist(), labels, guessed, scores,
                                                                    strict=True))
        if not records:
            raise ValueError("No matching labelled rows")
        return self.result("FEATURE_LEVEL_REPLAY", source_hash, scope, records, {
            "dataset_id": identity[:16] if verified else None,
            "test_status": "REUSED_TEST_BENCHMARK" if verified else "OPERATOR_LABELS_HOLDOUT_UNVERIFIED",
            "metrics": binary(y, predicted), "independent_incidents": None,
        })

    def packet(self, path, scope, family, scenario, observed_host=""):
        source = Path(path)
        if (source.suffix.lower() not in (".pcap", ".pcapng") or not source.is_file()
                or source.stat().st_size > 128 * 1024**2):
            raise ValueError("Select a local PCAP/PCAPNG up to 128 MiB")
        if family not in FAMILIES or not scenario.strip():
            raise ValueError("Enter the capture's original label and provenance")
        host = str(IPv4Address(observed_host or self.deployment.host))
        probe = CicInputProbe(host, self.deployment.video_port, camera_endpoints(self.deployment.video_config))
        if scope not in probe.lanes:
            raise ValueError("Select a service lane from the configured protected endpoints")
        argv = [TSHARK, "-r", str(source.resolve()), "-n", "-c", "120000", "-M", "10000",
                "-o", "tcp.desegment_tcp_streams:TRUE", "-Y", "ip && tcp", "-T", "fields",
                "-E", "occurrence=a", "-E", "aggregator=;"]
        for field in FIELDS:
            argv.extend(("-e", field))
        decoded = subprocess.run(argv, capture_output=True, encoding="ascii", errors="replace",
                                 timeout=self.deployment.capture_timeout, check=True)
        size = self.settings["window_packets"]
        threshold = self.threshold(scope)
        windows, records = [], []
        for line in decoded.stdout.splitlines():
            fields = dict(zip(FIELDS, line.split("\t"), strict=True))
            try:
                endpoint = from_tshark(fields, host, video_port=self.deployment.video_port, cameras=probe.cameras)
            except (ValueError, TypeError):
                # Match the probe's conservative reset for undecodable protected metadata.
                probe.invalidate(probe.lanes[scope], reason="INVALID_METADATA")
                continue
            if endpoint:
                windows.extend(row for lane, count, row in probe.observe(fields, endpoint.service)
                               if lane == scope and count == size)
            if len(windows) >= MAX_ROWS:
                break
        for offset in range(0, len(windows), 128):
            batch = windows[offset:offset + 128]
            scores = probabilities(self.model, [list(row["vector"]) for row in batch])
            records.extend({"family": family, "scenario": scenario, "expected": FAMILIES[family],
                            "prediction": int(score >= threshold), "score": score,
                            "ground_truth": "OPERATOR_CAPTURE_LABEL_UNVERIFIED_PER_WINDOW", **row}
                           for row, score in zip(batch, scores, strict=True))
        return self.result("PACKET_FILE_REPLAY", digest(source), scope, records, {
            "test_status": "OPERATOR_CAPTURE_LABEL_UNVERIFIED_PER_WINDOW",
            "metrics": None, "packet_probe": probe.snapshot(), "independent_incidents": None,
            "visibility": "OBSERVED_WINDOW" if records else "WAITING_FOR_PACKETS",
            "observed_capture_host": host, "decode_frame_limit": 120000,
        })

    def result(self, method, source_hash, scope, records, details):
        per_scenario = {}
        if details.get("metrics") is not None:
            groups = {}
            for row in records:
                groups.setdefault(row["scenario"], []).append(row)
            per_scenario = {scenario: binary((r["expected"] for r in rows), (r["prediction"] for r in rows))
                            for scenario, rows in groups.items()}
        families = {}
        for name in FAMILIES:
            rows = [r for r in records if r["family"] == name]
            if rows:
                flagged = sum(r["prediction"] for r in rows)
                families[name] = {"rows": len(rows), "predicted_attack": flagged}
                if details.get("metrics") is not None:
                    measure = "attack_recall" if FAMILIES[name] else "normal_false_positive_rate"
                    families[name][measure] = flagged / len(rows)
        return {"method": method, "source_sha256": source_hash, "scope": scope, "threshold": self.threshold(scope),
                "run_id": self.settings["run_id"], "model_sha256": self.settings["sha256"],
                "model_parameters": self.model_parameters, "parity": PARITY_BLOCK, "decision_eligible": False,
                "per_scenario": per_scenario, "families": families, "records": records, **details}


def label_source(group, verified):
    if not verified:
        return "OPERATOR_LABELS_HOLDOUT_UNVERIFIED"
    return "VERIFIED_DATASET_BENIGN_LABEL" if group == "BENIGN" else f"LABELED_OFFLINE_{group}"
