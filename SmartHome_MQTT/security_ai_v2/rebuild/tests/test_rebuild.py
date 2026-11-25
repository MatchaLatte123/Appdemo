from collections import Counter
import csv
from http.client import HTTPConnection
import json
from pathlib import Path
from shutil import copy2
from threading import Event, Thread
from time import monotonic, sleep

import joblib
import numpy as np
import pytest

from security_ai_v2.rebuild.contract import BASIC_8, BASIC_10, Packet, contract, from_tshark, windows
from security_ai_v2.rebuild.data import (digest, inferred, inventory, read_json, split_groups, stage_dataset)
from security_ai_v2.rebuild import studio
from security_ai_v2.rebuild.studio import Jobs, make_server
from security_ai_v2.rebuild.train import (ALGORITHMS, config, decision_threshold, estimator, export, final_evaluation, fit_estimator,
                                           sample_table, train, verify_run)


@pytest.fixture(scope="module")
def prepared(tmp_path_factory):
    root = tmp_path_factory.mktemp("cic_source")
    work = tmp_path_factory.mktemp("cic_output")
    for scenario, base in (("BenignTraffic", 80), ("DoS-SYN_Flood", 280), ("DDoS-TCP_Flood", 480),
                           ("Recon-PortScan", 680)):
        target = root / ("Benign_Final" if scenario == "BenignTraffic" else scenario)
        target.mkdir()
        for k in range(6):
            with (target / f"{scenario}{k}.pcap.csv").open("w", newline="") as stream:
                out = csv.writer(stream)
                out.writerow([*BASIC_10, "Number", "label"])
                for i in range(32):
                    length = base + k * 40 + i
                    out.writerow([0,.1,.2,0,0,length,length+8,length+4,2,.025,10,scenario])
                if k == 0:
                    out.writerow([0,.1,.2,0,0,base,base+8,base+4,2,.025,10,scenario])
                    out.writerow([0,.1,.2,0,0,"",base+8,base+4,2,.025,10,scenario])
                    out.writerow([0,.1,.2,0,0,base,base+8,base+4,2,"Inf",10,scenario])
                    out.writerow([0,.1,.2,0,0,base,base+8,base+4,2,"bad",10,scenario])
                    out.writerow(["malformed"])
    # Place the same attack signature in every source so it must route to one split.
    for path in (root / "DoS-SYN_Flood").glob("*.csv"):
        with path.open("a", newline="") as stream:
            csv.writer(stream).writerow([0,.1,.2,0,0,999,1007,1003,2,.025,10,"DoS-SYN_Flood"])
    before = {path: digest(path) for path in inventory(root)}
    manifest = stage_dataset(root, work, chunk_mib=1, disk_gib=2, memory_mib=256, progress=lambda _: None)
    original_files = {path: digest(path) for path in inventory(root)}
    assert before == original_files
    return root, work, manifest


def test_inventory_quality_dedup_and_manifest(prepared):
    root, work, info = prepared
    assert len(inventory(root)) == info["files"] == 24
    assert info["malformed"] == 4
    assert info["group_policy"] == "EXPLORATORY_FILE_GROUP_SPLIT"
    assert info["group_overlap"] == 0
    for label in (0, 1):
        counts = [sum(row["rows"] for row in info["splits"][split]["scenarios"] if row["y"] == label)
                  for split in ("TRAIN", "VALIDATION", "TEST")]
        assert counts[0] * 3 == counts[1] * 14
        assert counts[1] == counts[2] > 0
    assert info["dedup"]["cross_split_signatures_before"] >= 1
    assert info["dedup"]["cross_split_signatures_after"] == 0
    assert info["dedup"]["exact_duplicate_rows_B8"] > 0
    assert all(r["y"] in (0, 1) and r["scenario"] != "Recon-PortScan"
               for split in info["splits"].values() for r in split["scenarios"])
    assert all(len({r["y"] for r in split["scenarios"]}) == 2 for split in info["splits"].values())
    assert all(f["sha256"] == digest(root / f["file"]) for f in info["sources"])
    assert sum(f["quality"]["IAT"]["infinity"] for f in info["sources"] if f["quality"]["IAT"]) == 4
    assert sum(f["quality"]["Min"]["missing"] for f in info["sources"] if f["quality"]["Min"]) == 4
    assert inferred(Path("Benign_Final/BenignTraffic_1.pcap.csv"))[2] == 0
    assert inferred(Path("Benign_Final/DDoS-SYN_Flood.csv")) is None
    assert len(info["feature_audit"]["stats"]) == 10
    assert len(contract("BASIC_8")["order"]) == 8


def test_group_joins_move_as_unit():
    data = [{"file": f"case/{i}.csv", "sha256": str(i), "eligible": 40,
             "scenarios": {"BenignTraffic" if i < 4 else "DDoS-TCP_Flood": 40}} for i in range(12)]
    split_groups(data, authority={"case/4.csv":"same","case/5.csv":"same"})
    assert data[4]["group"] == data[5]["group"] and data[4]["split"] == data[5]["split"]
    assert {s["split"] for s in data} == {"TRAIN","VALIDATION","TEST"}


def test_threshold_and_config():
    y = np.array([0,0,1,1])
    score = np.array([.1,.7,.8,.9])
    assert decision_threshold(y, score, 0) == .8
    with pytest.raises(ValueError, match="max_depth"):
        config({"algorithms":["XGBOOST"],"hyperparameters":{"XGBOOST":{"max_depth":500}}})
    with pytest.raises(ValueError):
        config({"algorithms":["UNKNOWN"]})


def test_train_all_algorithms_export_and_vault(prepared):
    _, work, _ = prepared
    overrides = {
        "LOGISTIC": {"solver": "newton-cholesky", "tol": 1e-5},
        "RANDOM_FOREST": {"min_samples_split": .2},
        "EXTRA_TREES": {"bootstrap": True, "max_samples": .75},
        "XGBOOST": {"subsample": .75, "reg_lambda": 2.},
        "LIGHTGBM": {"feature_fraction_bynode": .8},
        "HIST_GRADIENT": {"max_bins": 32},
    }
    settings = {"algorithms": list(ALGORITHMS), "preset": "FULL_TRAINING", "train_cap_per_class": 40,
                "validation_cap": 100, "hyperparameters": overrides}
    ids = train(work, settings, progress=lambda _: None)["runs"]
    assert len(ids) == 6
    for name, run in zip(ALGORITHMS, ids):
        folder, info = verify_run(work / "runs", run)
        assert info["algorithm"] == name and info["test"] is None and info["status"] == "BENCHMARK_ONLY"
        assert info["dataset_version"] == read_json(work / "manifest.json")["version"]
        assert info["dataset"]["manifest_sha256"] == digest(work / "manifest.json")
        assert info["dataset"]["splits"]["TRAIN"]["rows"] == info["rows"]["available_train"]
        assert info["model_version"] != info["dataset_version"]
        assert info["rows"]["train_normal"] == info["rows"]["train_attack"] == 40
        assert info["validation"]["fpr"] <= .01
        actual = joblib.load(folder / "model.joblib")["classifier"].get_params()
        assert all(actual[key] == value for key, value in overrides[name].items())
    folder, result = verify_run(work / "runs", ids[0])
    model = joblib.load(folder / "model.joblib")
    sample = sample_table(work, "TRAIN", BASIC_8, result["config"], 40)
    assert model["scaler"].mean_[5] == pytest.approx(float(np.mean(np.asarray(sample["Min"]))))
    final_evaluation(work, ids[0])
    assert verify_run(work / "runs", ids[0])[1]["test"]["rows"] == read_json(work / "manifest.json")["splits"]["TEST"]["rows"]
    with pytest.raises(ValueError, match="SEALED"):
        final_evaluation(work, ids[1])
    package = Path(export(work, ids[0])["path"])
    assert all(digest(package / f) == value for f,value in read_json(package / "sha256.json").items())
    artifact = folder / "model.joblib"
    with artifact.open("ab") as f:
        f.write(b"tamper")
    with pytest.raises(ValueError, match="MISMATCH"):
        verify_run(work / "runs", ids[0])


def test_collector_fixture_both_services():
    def seq(service):
        return [Packet(service, 1+i*.01, 100+i, 2 if i<5 else 16, "192.0.2.1", "192.0.2.2", 9300, 1883)
                for i in range(10)]
    rows = list(windows([p for pair in zip(seq("MQTT"), seq("Video")) for p in pair]))
    assert {r["metadata"]["service"] for r in rows} == {"MQTT","Video"}
    for item in rows:
        assert item["features"]["syn_flag_number"] == .5
        assert item["features"]["AVG"] == 104.5
        assert item["features"]["IAT"] == pytest.approx(.01)
        assert set(item["features"]) == set(BASIC_10)
    assert not list(windows(seq("Video")[:9]))
    assert set(contract("BASIC_10")["parity"]) == {"MQTT","Video"}


def test_studio_auth_assets_and_cancel(prepared, tmp_path):
    _, original, _ = prepared
    for name in ("manifest.json", "train.parquet", "validation.parquet", "test.parquet"):
        copy2(original / name, tmp_path / name)
    server, jobs = make_server(0, tmp_path)
    thread = Thread(target=server.serve_forever)
    thread.start()

    def request(method, path, data=None, token=None):
        c = HTTPConnection("127.0.0.1", server.server_port, timeout=8)
        try:
            headers = {"Content-Type":"application/json"}
            if token:
                headers["X-Studio-Token"] = token
            c.request(method, path, json.dumps(data) if data is not None else None, headers=headers)
            r = c.getresponse()
            return r.status, r.read()
        finally:
            c.close()

    try:
        assert b'data-tab="registry"' in request("GET", "/")[1]
        assert b"plot(" in request("GET", "/studio.js")[1]
        assert request("GET","/api/dataset")[0] == 200
        assert request("POST","/api/train", {})[0] == 403
        options = json.loads(request("GET", "/api/options")[1])
        assert len(options["algorithms"]) == len(options["parameter_schema"]) == 6
        assert "reg_alpha" in options["parameter_schema"]["LIGHTGBM"]
        assert "colsample_bylevel" in options["parameter_schema"]["XGBOOST"]
        expected_sha = digest(tmp_path / "manifest.json")
        assert json.loads(request("GET", "/api/dataset")[1])["dataset_manifest_sha256"] == expected_sha
        assert request("POST", "/api/train", {"algorithms":["LOGISTIC"], "dataset_manifest_sha256":"0"*64},
                       options["token"])[0] == 400
        assert request("POST", "/api/train", {"algorithms":["LOGISTIC"], "dataset_manifest_sha256":expected_sha},
                       options["token"])[0] == 200
        assert request("POST","/api/cancel", {}, options["token"])[0] == 200
        deadline = monotonic()+8
        while jobs.process is not None and monotonic()<deadline:
            sleep(.05)
        assert jobs.snapshot()["state"] == "CANCELLED"
    finally:
        jobs.cancel()
        server.shutdown()
        thread.join()
        server.server_close()


def test_tshark_scope_ignores_labels_and_drops_truncated_capture():
    fields={"frame.time_epoch":"1.0", "frame.len":"100", "frame.cap_len":"90", "tcp.flags":"0x0012",
            "tcp.srcport":"9310", "tcp.dstport":"1883", "ip.src":"192.0.2.31", "ip.dst":"192.0.2.11"}
    assert from_tshark(fields, "192.0.2.11").quality == "LOSS"
    assert from_tshark(fields, "192.0.2.11").service == "MQTT"
    fields.update({"tcp.srcport":"80", "tcp.dstport":"9310", "frame.cap_len":"100"})
    assert from_tshark(fields, "192.0.2.11") is None
    assert from_tshark(fields, "192.0.2.11", cameras={("192.0.2.31",80)}).service == "Video"

def test_launcher_opens_same_workspace_without_starting_second_server(tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(studio, "open_ui", opened.append)
    server, _ = make_server(0, tmp_path)
    with server:
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            args = ["--port", str(server.server_port), "--work", str(tmp_path), "--open-browser"]
            studio.main(args)
            studio.main([*args, "--no-browser"])
            assert opened == [f"http://127.0.0.1:{server.server_port}/"]
            with pytest.raises(SystemExit):
                studio.main([*args, "--work", str(tmp_path / "other")])
        finally:
            server.shutdown()
            thread.join()


def test_launcher_opens_browser_on_new_bound_port(tmp_path, monkeypatch):
    opened = []
    ready = Event()
    make_server_original = studio.make_server

    def open_browser(url):
        opened.append(url)
        ready.set()

    def bound_server(port, work):
        server, jobs = make_server_original(port, work)
        server.serve_forever = lambda: ready.wait(3)
        return server, jobs

    monkeypatch.setattr(studio, "open_ui", open_browser)
    monkeypatch.setattr(studio, "make_server", bound_server)
    studio.main(["--port", "0", "--work", str(tmp_path), "--open-browser"])
    assert len(opened) == 1 and opened[0].startswith("http://127.0.0.1:") and ":0/" not in opened[0]

@pytest.mark.parametrize("name", ["XGBOOST", "LIGHTGBM"])
def test_early_stopping_uses_training_groups(name):
    rng = np.random.default_rng(17)
    x = rng.random((96, 8))
    groups = np.repeat(np.arange(12), 8)
    labels = groups % 2
    cfg = config({"algorithms": [name], "hyperparameters": {
        name: {"early_stopping_rounds": 4, "early_stopping_validation_fraction": .3,
               "n_estimators": 10, "n_jobs": 1, "random_state": 0}}})
    model, _ = estimator(name, cfg)
    fitted, stopped = fit_estimator(model, name, cfg, x, labels, groups)
    assert fitted + stopped == len(labels) and stopped % 8 == 0 and stopped
    assert model["classifier"].get_params()["random_state"] == 0
    assert model.predict_proba(x[:2]).shape == (2, 2)


@pytest.mark.parametrize(("name", "params", "text"), [
    ("LOGISTIC", {"solver": "lbfgs", "l1_ratio": .5}, "l1_ratio"),
    ("EXTRA_TREES", {"oob_score": True}, "bootstrap"),
    ("RANDOM_FOREST", {"n_jobs": 8}, "n_jobs"),
    ("LIGHTGBM", {"scale_pos_weight": 2, "is_unbalance": True}, "scale_pos_weight"),
    ("XGBOOST", {"interaction_constraints": [[0, 8]]}, "interaction_constraints"),
    ("HIST_GRADIENT", {"max_features": None}, "max_features"),
])
def test_invalid_parameter_combinations_are_rejected(name, params, text):
    with pytest.raises(ValueError, match=text):
        config({"algorithms": [name], "hyperparameters": {name: params}})

def test_studio_full_job_auto_tests_validation_winner_and_preserves_lock(prepared, tmp_path):
    _, original, _ = prepared
    for name in ("manifest.json", "train.parquet", "validation.parquet", "test.parquet"):
        copy2(original / name, tmp_path / name)
    jobs = studio.Jobs(tmp_path)
    assert studio.overview(tmp_path)["test_lock"] is None

    def wait_job():
        deadline = monotonic() + 25
        snapshot = jobs.snapshot()
        while snapshot["state"] == "RUNNING" and monotonic() < deadline:
            sleep(.1)
            snapshot = jobs.snapshot()
        assert snapshot["state"] == "COMPLETE", snapshot
        return snapshot["result"]

    base = {"algorithms":["LOGISTIC"],"train_cap_per_class":40,"validation_cap":100}
    studio.start_job(jobs, "train", base)
    quick = wait_job()
    assert quick["automatic_test"]["status"] == "VALIDATION_ONLY_QUICK"
    assert not (tmp_path / "test-selection.json").exists()

    studio.start_job(jobs, "train", {**base, "algorithms":["LOGISTIC","RANDOM_FOREST"],"preset":"FULL_TRAINING"})
    evaluated = wait_job()
    best = evaluated["best_validation_run"]
    assert evaluated["automatic_test"] == {"run_id": best, "status":"TEST_COMPLETE"}
    sealed = read_json(tmp_path / "test-selection.json")
    assert sealed["run_id"] == best and sealed["selection"] == "AUTOMATIC_VALIDATION_ONLY"
    assert studio.overview(tmp_path)["test_lock"]["run_id"] == best
    for run_id in evaluated["runs"]:
        _, report = verify_run(tmp_path / "runs", run_id)
        assert bool(report["test"]) is (run_id == best)
        if run_id == best:
            assert report["threshold"] == sealed["threshold"]
            assert report["test"]["rows"] == read_json(tmp_path / "manifest.json")["splits"]["TEST"]["rows"]

    studio.start_job(jobs, "train", {**base, "preset":"FULL_TRAINING"})
    later = wait_job()
    assert later["automatic_test"] == {"status":"SKIPPED_LOCKED","locked_run_id":best}
    assert read_json(tmp_path / "test-selection.json") == sealed
    assert verify_run(tmp_path / "runs", later["runs"][0])[1]["test"] is None

    first_folder, first_report = verify_run(tmp_path / "runs", best)
    previous_report_sha = digest(first_folder / "report.json")
    lock_sha = digest(tmp_path / "test-selection.json")
    studio.start_job(jobs, "evaluate", {"run_id": best, "reuse_test": True})
    repeat = wait_job()
    assert repeat["status"] == "TEST_COMPLETE_REUSED"
    replayed = studio.report_with_repeated_tests(first_folder, first_report)
    assert replayed["test_repeat_count"] == 1
    assert replayed["repeated_test"]["status"] == "REUSED_TEST_BENCHMARK"
    assert replayed["repeated_test"]["threshold"] == first_report["threshold"]

    studio.start_job(jobs, "evaluate", {"run_id": best, "reuse_test": True})
    second = wait_job()
    assert second["attempt_id"] != repeat["attempt_id"]
    assert studio.report_with_repeated_tests(first_folder, first_report)["test_repeat_count"] == 2

    studio.start_job(jobs, "train", {**base, "preset":"FULL_TRAINING", "reuse_test":True})
    automatic = wait_job()
    assert automatic["automatic_test"]["status"] == "TEST_COMPLETE_REUSED"
    other = automatic["best_validation_run"]
    assert other != best
    other_folder, other_report = verify_run(tmp_path / "runs", other)
    assert other_report["test"] is None
    reused = studio.report_with_repeated_tests(other_folder, other_report)["repeated_test"]
    assert reused["original_test_run_id"] == best
    assert reused["threshold"] == other_report["threshold"]
    assert len(reused["metrics"]["thresholds"]) == 1
    assert digest(tmp_path / "test-selection.json") == lock_sha
    assert digest(first_folder / "report.json") == previous_report_sha

    exported = Path(export(tmp_path, other)["path"])
    assert read_json(exported / "evaluation.json")["repeated_test"]["id"] == reused["id"]

def test_training_worker_rejects_changed_manifest(prepared, tmp_path):
    _, original, _ = prepared
    copy2(original / "manifest.json", tmp_path / "manifest.json")
    with pytest.raises(ValueError, match="DATASET_CHANGED_REFRESH_STUDIO"):
        train(tmp_path, {"algorithms": ["LOGISTIC"], "dataset_manifest_sha256": "0" * 64})
    assert not (tmp_path / "runs").exists()

def test_registry_ignores_run_from_other_dataset(prepared, tmp_path):
    _, original, _ = prepared
    copy2(original / "manifest.json", tmp_path / "manifest.json")
    run = tmp_path / "runs" / ("a" * 32)
    run.mkdir(parents=True)
    (run / "report.json").write_text(json.dumps({"dataset_id": "0" * 16, "dataset_manifest_sha256": "0" * 64}))
    assert studio.registry(tmp_path) == []
