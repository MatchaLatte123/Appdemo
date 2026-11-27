"""Authenticated local Studio API with one bounded worker process."""
import argparse
from collections import Counter, defaultdict, deque
from hmac import compare_digest
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
from queue import SimpleQueue, Empty
import secrets
import socket
import subprocess
import sys
from threading import Lock, Thread
from time import monotonic, sleep
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4
import webbrowser

import psutil

from .contract import BENCHMARK, PRESETS, contract
from .data import CIC_21_WORK, DEFAULT_DATA, ROOT, SPLITS, inventory, read_json, workspace_feature_preset, write_json
from .datasets import DATASETS, IMPORTS, TRAIN_ROOT, import_dataset, tabular_layout
from .parameters import ALGORITHMS, FIXED, presets, schema
from .train import (COMMON, config, dataset_manifest, export, final_evaluation, read_test_selection, registry,
                    repeat_files, report_with_repeated_tests, run_dataset_view, run_directory, train, verify_run)

UI = Path(__file__).with_name("ui")


class Jobs:
    def __init__(self, work):
        self.work = work
        self.lock = Lock()
        self.process = None
        self.record = {"state": "IDLE"}
        self.logs = deque(maxlen=110)
        self.token = secrets.token_urlsafe(32)

    def snapshot(self):
        with self.lock:
            return {**self.record, "logs": list(self.logs)}

    def launch(self, action, data, memory=2048, timeout=1800, *, work=None):
        with self.lock:
            if self.process is not None:
                raise RuntimeError("JOB_ALREADY_RUNNING")
            work = self.work if work is None else work
            job = uuid4().hex
            root = os.environ.get("SECURITY_AI_STUDIO_TRAIN")
            folder = (Path(root) if root else work) / "jobs"
            folder.mkdir(parents=True, exist_ok=True)
            request = folder / f"{job}.json"
            write_json(request, {"action": action, "data": data})
            env = {**os.environ, "OPENBLAS_NUM_THREADS": str(data.get("threads", 2)),
                   "OMP_NUM_THREADS": str(data.get("threads", 2))}
            process = subprocess.Popen([sys.executable, "-u", "-B", "-m", "security_ai.web_ui.studio",
                                        "--work", str(work), "--worker", str(request)],
                                       cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       env=env, encoding="utf-8", errors="replace")
            self.process = process
            self.record = {"state": "RUNNING", "id": job, "action": action}
            self.logs.clear()
        Thread(target=self.watch, args=(process, memory, timeout, request), daemon=True).start()
        return {"id": job, "state": "RUNNING"}

    def watch(self, process, memory, timeout, request):
        messages = SimpleQueue()
        log_path = request.with_suffix(".log")

        def read():
            with process.stdout, log_path.open("w", encoding="utf-8") as log:
                for line in process.stdout:
                    log.write(line)
                    log.flush()
                    messages.put(line.strip())

        reader = Thread(target=read, daemon=True)
        reader.start()
        try:
            observed = psutil.Process(process.pid)
        except psutil.NoSuchProcess:
            observed = None
        start = monotonic()
        peak = 0
        while process.poll() is None:
            while True:
                try:
                    line = messages.get_nowait()
                except Empty:
                    break
                if line:
                    with self.lock:
                        self.logs.append(line)
            try:
                rss = observed.memory_info().rss + sum(c.memory_info().rss for c in observed.children()) if observed else 0
            except psutil.Error:
                rss = 0
            peak = max(peak, rss)
            if rss > memory * 2**20 or monotonic() - start > timeout:
                self.cancel("MEMORY_LIMIT" if rss > memory * 2**20 else "TIMEOUT")
            sleep(.15)
        reader.join(timeout=1)
        with self.lock:
            while not messages.empty():
                self.logs.append(messages.get_nowait())
            if self.record["state"] == "RUNNING":
                self.record["state"] = "COMPLETE" if process.returncode == 0 else "FAILED"
            result_file = request.with_suffix(".result.json")
            if result_file.is_file():
                try:
                    self.record["result"] = read_json(result_file)
                    if self.record["state"] == "COMPLETE" and self.record["action"] == "import":
                        self.work = Path(self.record["result"]["workspace"])
                except (OSError, ValueError):
                    self.logs.append("Could not read the structured job result.")
            self.record.update(exit_code=process.returncode, peak_rss_mib=round(peak / 2**20, 1),
                               elapsed_seconds=round(monotonic() - start, 2))
            self.process = None

    def cancel(self, reason="CANCELLED"):
        with self.lock:
            if self.process and self.process.poll() is None:
                try:
                    children = psutil.Process(self.process.pid).children(recursive=True)
                except psutil.NoSuchProcess:
                    children = []
                for child in children:
                    try:
                        child.terminate()
                    except psutil.NoSuchProcess:
                        pass
                self.process.terminate()
                self.record["state"] = reason
            return {"state": self.record["state"]}


def worker(work, request):
    from .data import stage_dataset
    job = read_json(request)
    data = job["data"]
    if job["action"] == "import":
        destination = Path(data.pop("destination"))
        directory, manifest = import_dataset(**data, destination=destination)
        result = {"workspace": str(directory), "rows": manifest["total_rows"]}
    elif job["action"] == "prepare":
        prepared = stage_dataset(data.pop("directory"), work, **data)
        result = {"dataset": prepared["version"], "rows": prepared["total_rows"]}
    elif job["action"] == "train":
        result = train(work, data)
    else:
        result = final_evaluation(work, data["run_id"], reuse_test=data.get("reuse_test", False))
    write_json(request.with_suffix(".result.json"), result)
    print(json.dumps(result))


def tabular_overview(raw, path, identity):
    return {"status": "PREPARED", "title": raw.get("title", path.parent.name), "version": raw["version"],
            "source_dir": raw["source"], "default_directory": str(DEFAULT_DATA),
            "label_mapping": raw["label_mapping"], "total_rows": raw["total_rows"],
            "files": raw["input"]["files"], "malformed": raw["input"].get("malformed_csv_rows"),
            "eligible": raw["dedup"].get("unique_eligible_rows", raw["input"]["rows"]),
            "group_policy": "Theo tập có sẵn hoặc phân tầng theo nhãn",
            "splits": raw["splits"], "sources": raw["source_files"], "split_policy": raw.get("split_policy"),
            "dedup": raw["dedup"], "manifest": str(path), "dataset_id": identity[:16],
            "dataset_manifest_sha256": identity, "test_lock": test_lock(path.parent)}


def catalog(work):
    entries = {path: title for title, path in DATASETS.items() if (path / "manifest.json").is_file()}
    for path in IMPORTS.glob("*/manifest.json"):
        if not path.parent.name.endswith(".pending"):
            entries[path.parent] = read_json(path).get("title", path.parent.name)
    entries.setdefault(work, work.name)
    return {"active": str(work), "datasets": [{"path": str(path), "title": title} for path,title in entries.items()]}


def catalog_workspace(current, requested):
    for entry in catalog(current)["datasets"]:
        if entry["path"] == requested:
            return Path(entry["path"])
    raise ValueError("Dataset không có trong danh sách đã nạp.")


def test_lock(work):
    try:
        return read_test_selection(work)
    except (OSError, ValueError):
        return {"unavailable": True}


def feature_options(work, manifest):
    layout = tabular_layout(manifest)
    if layout:
        order, _ = layout
        defaults = {**COMMON, "features": "DATASET", "preset": "FULL_TRAINING",
                    "scaling": "NONE" if manifest["version"].startswith(("cic38-", "cic39-")) else "STANDARD"}
        return defaults, {"DATASET": {"order": order, "status": "Đặc trưng của dataset",
                                       "candidate_formulas": {name: {"formula": "—", "unit": "—"} for name in order}}}
    dataset_preset = workspace_feature_preset(work)
    defaults = ({**COMMON, "features": "CIC_21", "scaling": "STANDARD"} if dataset_preset == "CIC_21" else COMMON)
    return defaults, {name: contract(name) for name, columns in PRESETS.items()
                      if set(columns) <= set(PRESETS[dataset_preset])}


def feature_report(work, title):
    manifest_path = work / "manifest.json"
    manifest = read_json(manifest_path) if manifest_path.is_file() else None
    defaults, features = feature_options(work, manifest)
    report = {"title": title, "features": features, "default": defaults["features"]}
    if tabular_layout(manifest):
        report["excluded_feature"] = manifest.get("excluded_feature")
        path = work / "preprocessing.json"
        if path.is_file():
            scaler = read_json(path)
            if (scaler.get("fit_on") == "TRAIN" and scaler.get("method") == "z-score"
                    and scaler.get("features") == manifest["features"]):
                report["scaler"] = scaler["parameters"]
        return report
    data = overview(work)
    return {**report, "audit": data.get("feature_audit"), "quality": data.get("quality")}


def cic_comparison():
    result = {"datasets": {}, "paired_test": False}
    manifests, identities = {}, {}
    metric_keys = ("rows", "accuracy", "macro_f1", "weighted_f1", "per_class",
                   "confusion_labels", "confusion_matrix", "inference_ms_per_row_batch")
    for title, work in DATASETS.items():
        result["datasets"][title] = []
        if not (work / "manifest.json").is_file():
            continue
        manifest, identities[title] = dataset_manifest(work)
        manifests[title] = manifest
        for run in registry(work):
            if run["schema"]["order"] != manifest["features"]:
                continue
            test, source = run["test"], "original"
            repeats = repeat_files(run_directory(work, run["run_id"]) / run["run_id"])
            if repeats:
                latest = read_json(repeats[0])
                if latest["dataset_id"] == run["dataset_id"]:
                    test, source = latest["metrics"], "reused"
            result["datasets"][title].append({
                **{key: run[key] for key in ("run_id", "algorithm", "created_utc", "training_seconds", "model_bytes")},
                "feature_count": len(run["schema"]["order"]),
                "test_source": source if test else None,
                "test": {key: test[key] for key in metric_keys if key in test} if test else None,
                "validation": {key: run["validation"][key] for key in metric_keys if key in run["validation"]},
            })
    baseline = identities.get("CIC_38")
    pairing = manifests.get("CIC_39", {}).get("comparison", {})
    result["paired_test"] = bool(baseline and pairing.get("baseline_manifest_sha256") == baseline
                                 and pairing.get("same_rows_and_splits") is True)
    return result


def overview(work):
    manifest = work / "manifest.json"
    path = manifest if manifest.exists() else work / "inventory.json"
    if manifest.is_file():
        raw, manifest_sha = dataset_manifest(work)
    else:
        raw, manifest_sha = (read_json(path) if path.is_file() else None), None
    if not raw:
        return {"status": "UNPREPARED" if DEFAULT_DATA.is_dir() else "DATASET_NOT_FOUND",
                "default_directory": str(DEFAULT_DATA)}
    if manifest_sha and tabular_layout(raw):
        return tabular_overview(raw, path, manifest_sha)
    keys = ("version", "source_dir", "files", "bytes", "rows", "eligible", "malformed", "splits",
            "feature_preset", "total_rows", "removed_rows", "group_overlap", "group_policy", "dedup", "feature_audit")
    summary = {k: raw[k] for k in keys if k in raw}
    summary["sources"] = [{k: row[k] for k in ("file", "sha256", "rows", "eligible", "group", "split", "label_provenance")
                           if k in row} for row in raw["sources"]]
    quality, labels = defaultdict(Counter), Counter()
    for source in raw["sources"]:
        labels.update(source["eligible_labels"])
        for key, counts in source["quality"].items():
            quality[key].update(counts)
    summary.update(eligible_labels=labels, quality=quality)
    if manifest_sha:
        summary.update(dataset_id=manifest_sha[:16], dataset_manifest_sha256=manifest_sha)
        summary["test_lock"] = test_lock(work)
    return {"status": "PREPARED" if manifest.exists() else "INVENTORIED", **summary,
            "manifest": str(path), "default_directory": str(DEFAULT_DATA)}


def start_job(jobs, action, request):
    if action == "select-dataset":
        if set(request) != {"path"} or not isinstance(request["path"], str) or not request["path"].strip():
            raise ValueError("Specify a prepared dataset folder")
        path = Path(request["path"]).expanduser().resolve()
        if path != jobs.work:
            manifest = read_json(path / "manifest.json")
            if not tabular_layout(manifest) and "sources" not in manifest:
                raise ValueError("Unsupported prepared dataset manifest")
            if not all((path / f"{split}.parquet").is_file() for split in ("train","validation","test")):
                raise ValueError("Prepared dataset is missing a split file")
        with jobs.lock:
            if jobs.process is not None:
                raise RuntimeError("JOB_ALREADY_RUNNING")
            jobs.work = path
        return {"workspace": str(path)}
    if action == "import":
        allowed = {"path", "target", "features", "split_paths"}
        if set(request) - allowed or not all(isinstance(request.get(key, ""), str) for key in ("path", "target", "features")):
            raise ValueError("Invalid import options")
        split_paths = request.get("split_paths")
        if split_paths is not None and (not isinstance(split_paths, dict) or set(split_paths) != set(SPLITS)
                                        or not all(isinstance(value, str) and value.strip() for value in split_paths.values())):
            raise ValueError("Specify all three split paths")
        source = request.get("path", "").strip().strip('"')
        if not source and split_paths is None:
            raise ValueError("Specify a source path")
        path = Path(source).expanduser().resolve()
        target = request.get("target", "").strip()
        features = [name.strip() for name in request.get("features", "").split(",") if name.strip()]
        if split_paths is None and not target and not features and path.is_dir() and (path / "manifest.json").is_file():
            return start_job(jobs, "select-dataset", {"path": str(path)})
        return jobs.launch(action, {"path": str(path), "target": target, "features": features,
                                    "destination": str(IMPORTS), "split_paths": split_paths}, 2048, 7200)
    if action == "prepare":
        path = jobs.work / "manifest.json"
        if path.is_file() and tabular_layout(read_json(path)):
            raise ValueError("Use the Import action to replace the active tabular dataset")
        directory = request.get("directory")
        if (not isinstance(directory, str) or not directory.strip()
                or set(request) - {"directory", "chunk_mib", "disk_gib", "memory_mib", "threads"}):
            raise ValueError("INVALID_PREPARATION_CONFIG")
        return jobs.launch(action, {**request, "directory": str(Path(directory).resolve()),
                                    "feature_preset": workspace_feature_preset(jobs.work)}, 4096, 7200)
    if action == "train":
        if not (jobs.work / "manifest.json").is_file():
            raise ValueError("DATASET_NOT_PREPARED")
        manifest, manifest_sha = dataset_manifest(jobs.work)
        layout = tabular_layout(manifest)
        cfg = config({"auto_test": True, **request}, layout[0] if layout else None)
        if not layout and not set(PRESETS[cfg["features"]]) <= set(PRESETS[manifest.get("feature_preset", "BASIC_10")]):
            raise ValueError("FEATURES_NOT_IN_DATASET")
        if cfg["dataset_manifest_sha256"] not in (None, manifest_sha):
            raise ValueError("DATASET_CHANGED_REFRESH_STUDIO")
        cfg["dataset_manifest_sha256"] = manifest_sha
        return jobs.launch(action, cfg, cfg["memory_mib"], cfg["timeout_seconds"])
    if action == "evaluate":
        if set(request) - {"run_id", "reuse_test", "dataset"} or type(request.get("reuse_test", False)) is not bool:
            raise ValueError("INVALID_TEST_EVALUATION_CONFIG")
        work = catalog_workspace(jobs.work, request["dataset"]) if "dataset" in request else jobs.work
        run_id = request.get("run_id")
        _, report = verify_run(run_directory(work, run_id), run_id)
        report = run_dataset_view(work, report)
        cfg = report["config"]
        return jobs.launch(action, {"run_id": run_id, "reuse_test": request.get("reuse_test", False)},
                           cfg["memory_mib"], cfg["timeout_seconds"], work=work)
    if action == "cancel":
        return jobs.cancel()
    if action == "export":
        return export(jobs.work, request.get("run_id"))
    raise KeyError(action)


class StudioHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = not hasattr(socket, "SO_EXCLUSIVEADDRUSE")

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def make_server(port, work):
    jobs = Jobs(work)

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            self.request.settimeout(5)
            super().setup()

        def emit(self, payload, content_type="application/json; charset=utf-8", status=200):
            body = payload if isinstance(payload, bytes) else json.dumps(payload, allow_nan=False, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; object-src 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def trusted_host(self):
            return self.headers.get("Host") in (f"127.0.0.1:{self.server.server_port}",
                                                f"localhost:{self.server.server_port}")

        def do_GET(self):
            if not self.trusted_host():
                self.emit({"error": "LOCAL_ONLY"}, status=403)
                return
            url = urlsplit(self.path)
            try:
                self.get_resource(url.path, url.query)
            except (KeyError, ValueError, OSError, TypeError) as error:
                self.emit({"error": str(error)}, status=400)

        def get_resource(self, route, query):
            current_work = jobs.work
            assets = {"/": ("index.html", "text/html; charset=utf-8"),
                      "/studio.js": ("studio.js", "application/javascript; charset=utf-8"),
                      "/studio.css": ("studio.css", "text/css; charset=utf-8")}
            if route in assets:
                file, mime = assets[route]
                self.emit((UI / file).read_bytes(), mime)
            elif route == "/api/launcher":
                self.emit({"app": "ciciot2023-studio", "workspace": str(current_work.resolve()),
                           "startup_workspace": str(work.resolve())})
            elif route == "/api/datasets":
                self.emit(catalog(current_work))
            elif route == "/api/cic-comparison":
                self.emit(cic_comparison())
            elif route == "/api/feature-report":
                requested = parse_qs(query).get("dataset", [])
                for entry in catalog(current_work)["datasets"]:
                    if requested == [entry["path"]]:
                        self.emit(feature_report(Path(entry["path"]), entry["title"]))
                        return
                self.emit({"error": "Dataset không có trong danh sách đã nạp."}, status=404)
            elif route == "/api/options":
                manifest_path = current_work / "manifest.json"
                manifest = read_json(manifest_path) if manifest_path.is_file() else None
                defaults, features = feature_options(current_work, manifest)
                self.emit({"token": jobs.token, "features": features,
                           "workspace": str(current_work), "algorithms": ALGORITHMS, "defaults": defaults, "presets": presets(),
                           "parameter_schema": schema(), "managed_parameters": FIXED})
            elif route == "/api/job":
                self.emit(jobs.snapshot())
            elif route == "/api/dataset":
                self.emit(overview(current_work))
            elif route == "/api/registry":
                requested = parse_qs(query, keep_blank_values=True).get("dataset")
                target = catalog_workspace(current_work, requested[0]) if requested else current_work
                self.emit({"runs": registry(target)})
            elif route.startswith("/api/run/"):
                try:
                    requested = parse_qs(query, keep_blank_values=True).get("dataset")
                    target = catalog_workspace(current_work, requested[0]) if requested else current_work
                    run_id = route.removeprefix("/api/run/")
                    folder, report = verify_run(run_directory(target, run_id), run_id)
                    view = run_dataset_view(target, report)
                    self.emit({**report_with_repeated_tests(folder, view), "test_lock": test_lock(target)})
                except (ValueError, OSError):
                    self.emit({"error":"RUN_NOT_FOUND_OR_INVALID"}, status=404)
            else:
                self.emit({"error":"NOT_FOUND"}, status=404)

        def do_POST(self):
            token = self.headers.get("X-Studio-Token", "")
            origin = self.headers.get("Origin")
            if (not self.trusted_host() or (origin and origin != "http://" + self.headers["Host"])
                    or not token.isascii() or not compare_digest(token, jobs.token)):
                self.emit({"error": "LOCAL_OPERATOR_TOKEN_REQUIRED"}, status=403)
                return
            if not self.path.startswith("/api/"):
                self.emit({"error": "NOT_FOUND"}, status=404)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 2 <= length <= 12_288 or self.headers.get_content_type() != "application/json":
                    raise ValueError("INVALID_BODY")
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError("INVALID_BODY")
                value = start_job(jobs, self.path.removeprefix("/api/"), data)
            except (KeyError, ValueError, OSError, TypeError, RuntimeError) as error:
                self.emit({"error": str(error)}, status=400)
                return
            self.emit(value)

        def log_message(self, *_args):
            pass

    server = StudioHTTPServer(("127.0.0.1", port), Handler)
    return server, jobs


def read_local_json(port, path):
    connection = HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        if response.status != 200:
            return None
        data = json.loads(response.read(2 * 1024 * 1024 + 1))
        return data if isinstance(data, dict) else None
    except (HTTPException, OSError, ValueError):
        return None
    finally:
        connection.close()


def same_studio(port, work):
    launcher = read_local_json(port, "/api/launcher")
    if launcher:
        return (launcher.get("app") == "ciciot2023-studio"
                and str(work) in (launcher.get("workspace"), launcher.get("startup_workspace")))
    # The already-running Studio can predate the launcher endpoint.
    options = read_local_json(port, "/api/options")
    if not options or options.get("algorithms") != list(ALGORITHMS):
        return False
    dataset = read_local_json(port, "/api/dataset")
    manifest = dataset and dataset.get("manifest")
    return isinstance(manifest, str) and Path(manifest).resolve().parent == work


def open_ui(url):
    try:
        if webbrowser.open(url):
            return
    except (OSError, webbrowser.Error):
        pass
    print(f"Could not open the default browser. Open {url}", file=sys.stderr, flush=True)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--port", type=int, default=8766)
    prepared = DATASETS["CIC_38"]
    default_work = prepared if not (CIC_21_WORK / "manifest.json").is_file() and (prepared / "manifest.json").is_file() else CIC_21_WORK
    p.add_argument("--work", type=Path, default=default_work)
    p.add_argument("--worker", type=Path)
    p.add_argument("--open-browser", dest="open_browser", action="store_true",
                   help="Open the Studio in the default browser")
    p.add_argument("--no-browser", dest="open_browser", action="store_false",
                   help="Keep the Studio in the console")
    p.set_defaults(open_browser=False)
    a = p.parse_args(argv)
    if not 0 <= a.port <= 65535:
        p.error("Port must be between 0 and 65535.")
    work = a.work.resolve()
    if a.worker:
        worker(work, a.worker)
        return
    if __name__ == "__main__":
        os.environ.setdefault("SECURITY_AI_STUDIO_TRAIN", str(TRAIN_ROOT))
    try:
        server, jobs = make_server(a.port, work)
    except OSError as error:
        if a.port and same_studio(a.port, work):
            url = f"http://127.0.0.1:{a.port}/"
            print(f"CICIoT2023 Training Studio is already running at {url}", flush=True)
            if a.open_browser:
                open_ui(url)
            return
        p.error(f"Cannot start Studio on port {a.port}: {error}. Check the port or use --port.")

    with server:
        url = f"http://127.0.0.1:{server.server_port}/"
        print(f"CICIoT2023 Training Studio {url} [{BENCHMARK}]", flush=True)
        if a.open_browser:
            Thread(target=open_ui, args=(url,), daemon=True).start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            jobs.cancel()


if __name__ == "__main__":
    main()
