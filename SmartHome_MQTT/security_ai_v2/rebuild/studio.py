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
from urllib.parse import urlsplit
from uuid import uuid4
import webbrowser

import psutil

from .contract import BENCHMARK, PRESETS, contract
from .data import DEFAULT_DATA, ROOT, WORK, inventory, read_json, write_json
from .parameters import ALGORITHMS, FIXED, presets, schema
from .train import (COMMON, config, dataset_manifest, export, final_evaluation, read_test_selection, registry,
                    report_with_repeated_tests, run_dataset_view, train, verify_run)

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

    def launch(self, action, data, memory=2048, timeout=1800):
        with self.lock:
            if self.process is not None:
                raise RuntimeError("JOB_ALREADY_RUNNING")
            job = uuid4().hex
            folder = self.work / "jobs"
            folder.mkdir(parents=True, exist_ok=True)
            request = folder / f"{job}.json"
            write_json(request, {"action": action, "data": data})
            env = {**os.environ, "OPENBLAS_NUM_THREADS": str(data.get("threads", 2)),
                   "OMP_NUM_THREADS": str(data.get("threads", 2))}
            process = subprocess.Popen([sys.executable, "-u", "-B", "-m", "security_ai_v2.rebuild.studio",
                                        "--work", str(self.work), "--worker", str(request)],
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
    if job["action"] == "prepare":
        prepared = stage_dataset(data.pop("directory"), work, **data)
        result = {"dataset": prepared["version"], "rows": prepared["total_rows"]}
    elif job["action"] == "train":
        result = train(work, data)
    else:
        result = final_evaluation(work, data["run_id"], reuse_test=data.get("reuse_test", False))
    write_json(request.with_suffix(".result.json"), result)
    print(json.dumps(result))


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
    keys = ("version", "source_dir", "files", "bytes", "rows", "eligible", "malformed", "splits",
            "total_rows", "removed_rows", "group_overlap", "group_policy", "dedup", "feature_audit")
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
        try:
            lock = read_test_selection(work)
            summary["test_lock"] = {"run_id": lock["run_id"], "selection": lock.get("selection")} if lock else None
        except (OSError, ValueError):
            summary["test_lock"] = {"unavailable": True}
    return {"status": "PREPARED" if manifest.exists() else "INVENTORIED", **summary,
            "manifest": str(path), "default_directory": str(DEFAULT_DATA)}


def start_job(jobs, action, request):
    if action == "prepare":
        directory = request.get("directory")
        if (not isinstance(directory, str) or not directory.strip()
                or set(request) - {"directory", "chunk_mib", "disk_gib", "memory_mib", "threads"}):
            raise ValueError("INVALID_PREPARATION_CONFIG")
        return jobs.launch(action, {**request, "directory": str(Path(directory).resolve())}, 4096, 7200)
    if action == "train":
        cfg = config({"auto_test": True, **request})
        if not (jobs.work / "manifest.json").is_file():
            raise ValueError("DATASET_NOT_PREPARED")
        _, manifest_sha = dataset_manifest(jobs.work)
        if cfg["dataset_manifest_sha256"] not in (None, manifest_sha):
            raise ValueError("DATASET_CHANGED_REFRESH_STUDIO")
        cfg["dataset_manifest_sha256"] = manifest_sha
        return jobs.launch(action, cfg, cfg["memory_mib"], cfg["timeout_seconds"])
    if action == "evaluate":
        if set(request) - {"run_id", "reuse_test"} or type(request.get("reuse_test", False)) is not bool:
            raise ValueError("INVALID_TEST_EVALUATION_CONFIG")
        _, report = verify_run(jobs.work / "runs", request.get("run_id"))
        cfg = report["config"]
        return jobs.launch(action, request, cfg["memory_mib"], cfg["timeout_seconds"])
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
            route = urlsplit(self.path).path
            assets = {"/": ("index.html", "text/html; charset=utf-8"),
                      "/studio.js": ("studio.js", "application/javascript; charset=utf-8"),
                      "/studio.css": ("studio.css", "text/css; charset=utf-8")}
            if route in assets:
                file, mime = assets[route]
                self.emit((UI / file).read_bytes(), mime)
            elif route == "/api/launcher":
                self.emit({"app": "ciciot2023-studio", "workspace": str(work.resolve())})
            elif route == "/api/options":
                self.emit({"token": jobs.token, "features": {n: contract(n) for n in PRESETS},
                           "algorithms": ALGORITHMS, "defaults": COMMON, "presets": presets(),
                           "parameter_schema": schema(), "managed_parameters": FIXED})
            elif route == "/api/job":
                self.emit(jobs.snapshot())
            elif route == "/api/dataset":
                self.emit(overview(work))
            elif route == "/api/registry":
                self.emit({"runs": registry(work)})
            elif route.startswith("/api/run/"):
                try:
                    folder, report = verify_run(work / "runs", route.removeprefix("/api/run/"))
                    self.emit(report_with_repeated_tests(folder, run_dataset_view(work, report)))
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
                and launcher.get("workspace") == str(work))
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
    p.add_argument("--work", type=Path, default=WORK)
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
