import csv

import pytest

from security_ai.web_ui.datasets import import_dataset
from security_ai.web_ui.train import class_summary, registry, train


def make_csv(path, offset=0):
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(("size", "protocol", "target_name"))
        for index, label in enumerate(("Benign", "DoS", "DDoS")):
            for row in range(20):
                writer.writerow((offset + index * 100 + row, ("MQTT", "HTTP", "Other")[index], label))


def test_load_and_train_on_custom_three_class_csv(tmp_path, monkeypatch):
    monkeypatch.setenv("SECURITY_AI_STUDIO_TRAIN", str(tmp_path / "train"))
    source = tmp_path / "my dataset.csv"
    make_csv(source)
    work, manifest = import_dataset(source, target="target_name", destination=tmp_path / "imports")
    assert set(manifest["categorical_encoders"]) == {"protocol"}
    assert set(manifest["label_mapping"]) == {"Benign", "DoS", "DDoS"}
    assert all(set(entry["labels"]) == set(manifest["label_mapping"]) for entry in manifest["splits"].values())
    result = train(work, {"algorithms":["LOGISTIC"], "features":"DATASET", "preset":"FULL_TRAINING",
                          "scaling":"STANDARD", "train_cap_per_class":0, "auto_test":True})
    assert result["automatic_test"]["status"] == "TEST_COMPLETE"
    run = registry(work)[0]
    assert run["test"]["rows"] == manifest["splits"]["TEST"]["rows"]
    assert len(run["test"]["per_class"]) == 3
    assert len(list((tmp_path / "train").glob("*/runs/*/model.joblib"))) == 1


def test_explicit_split_files_and_metrics(tmp_path):
    paths = {}
    for i, name in enumerate(("TRAIN", "VALIDATION", "TEST")):
        path = tmp_path / f"capture {i}.csv"
        make_csv(path, offset=1000*i)
        paths[name] = str(path)
    _, manifest = import_dataset(tmp_path, target="target_name", destination=tmp_path/"import", split_paths=paths)
    assert {label: split["rows"] for label, split in manifest["splits"].items()} == dict.fromkeys(paths, 60)
    scores = class_summary([[8, 2, 0],[1, 1, 0],[0, 1, 0]], {"Benign":0,"DoS":1,"DDoS":2})
    assert scores["per_class"][0]["precision"] == pytest.approx(8/9)
    assert scores["per_class"][1]["recall"] == .5
    assert scores["per_class"][2]["f1"] == 0
    assert scores["macro_f1"] == pytest.approx((16/19+2/6)/3)
    assert scores["weighted_f1"] == pytest.approx(((16/19)*10+(2/6)*2)/13)
