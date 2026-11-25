"""Export the stored evidence as JSON, CSV and standalone HTML."""
import csv
from html import escape
import json
from pathlib import Path
from tempfile import TemporaryDirectory


def export(report, folder):
    base = Path(folder)
    target = base / report["session"]["id"]
    base.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(f"Choose a different output folder: {target}")
    text = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    with TemporaryDirectory(prefix=".iot-report-", dir=base) as scratch:
        stage = Path(scratch) / "report"
        stage.mkdir()
        (stage / "report.json").write_text(text + "\n", encoding="utf-8")
        with (stage / "observations.csv").open("w", newline="", encoding="utf-8-sig") as file:
            writer = csv.writer(file)
            writer.writerow(("stamp_epoch", "kind", "evidence_json"))
            for item in report["observations"]:
                writer.writerow((item["stamp"], item["kind"], json.dumps(item["data"], ensure_ascii=False, allow_nan=False)))
        (stage / "report.html").write_text(
            "<!doctype html><html lang='en'><meta charset='utf-8'><title>IoT validation</title>"
            "<style>body{font:16px system-ui;margin:3rem;color:#162438}pre{white-space:pre-wrap}</style>"
            "<h1>IoT validation report</h1><pre>" + escape(text) + "</pre></html>", encoding="utf-8")
        stage.rename(target)
    return target
