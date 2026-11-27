"""Loopback API for V2 live XGBoost scores and Ethernet diagnostics."""
import argparse
from hmac import compare_digest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
from urllib.parse import urlsplit

from .cic38 import Cic38
from .cic38_shadow import Cic38Shadow
from .cic_input_probe import attach_model_probe
from .live_monitor import LiveMonitor, camera_endpoints
from .xgboost_shadow import ROOT, XGBoostShadow, preset_for_order

API = "/api/security/v2/"
MODE = "LIVE_V2"


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def operator_token_path():
    state = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local/state")
    return state / "SmartHomeAI/LiveV2/operator.token"


def get_token(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="ascii") as out:
            out.write(secrets.token_urlsafe(32))
    except FileExistsError:
        pass
    token = path.read_text(encoding="ascii").strip()
    if len(token) < 32 or not token.isascii() or any(ch.isspace() for ch in token):
        raise ValueError("Invalid live V2 operator token")
    return token


class Runtime:
    def __init__(self, monitor, probe, shadow, registry):
        self.monitor, self.probe, self.shadow, self.registry = monitor, probe, shadow, registry

    def predictions(self):
        data = self.shadow.snapshot()
        try:
            registry = read_json(self.registry)
        except (OSError, ValueError):
            registry = {}
        by_ip = {}
        if isinstance(registry, dict):
            for device_id, item in registry.items():
                if isinstance(item, dict) and isinstance(item.get("ip"), str):
                    by_ip.setdefault(item["ip"], []).append({"device_id": device_id, "name": item.get("name") or device_id})

        def describe(row):
            evidence = row.get("source_evidence")
            if not evidence:
                return row
            peers = []
            for peer in evidence["peers"]:
                matches = by_ip.get(peer["ip"], ())
                peers.append({**peer, "registry_hint": matches[0] if len(matches) == 1 else None,
                              "registry_ip_ambiguous": len(matches) > 1})
            return {**row, "source_evidence": {**evidence, "peers": peers}}

        data["lanes"] = {scope: describe(row) for scope, row in data["lanes"].items()}
        data["peers"] = {scope: {ip: describe(row) for ip, row in rows.items()}
                         for scope, rows in data.get("peers", {}).items()}
        return data

    def get(self, resource):
        if resource == "xgboost-shadow":
            return self.predictions()
        if resource == "collector-features":
            cic38 = self.monitor.cic38_snapshot()
            snapshot = self.probe.snapshot() if self.probe is not None else {
                "feature_order": cic38["feature_order"], "window_sizes": cic38["window_sizes"],
                "input_schema": cic38["schema"], "lanes": cic38["lanes"], "decision_eligible": False}
            return {**snapshot, "cic39": self.monitor.cic39_snapshot(), "cic38": cic38}
        if resource in ("health", "overview"):
            capture = self.monitor.snapshot()
            if resource == "health":
                return capture
            status = "MONITORING" if capture["status"] in ("ACTIVE", "WAITING_FOR_TRAFFIC") else capture["status"]
            return {"mode": MODE, "monitoring": {**capture, "status": status, "capture_status": capture["status"]}, "enforcement": False,
                    "services": [{"service": name, **value} for name, value in capture["services"].items()]}
        if resource == "devices":
            return {"devices": [{"key": f"{scope}/{ip}", "service": row["service"], "role": "OBSERVED_PEER",
                                 "identity": {"ip": ip, "status": "UNKNOWN"}}
                                for scope, rows in self.shadow.snapshot().get("peers", {}).items()
                                for ip, row in rows.items()]}
        raise KeyError(resource)


def make_server(port, runtime, token):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            self.request.settimeout(3)
            super().setup()

        def emit(self, value, status=200):
            data = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/health":
                shadow = runtime.shadow.snapshot()
                state = {"mode": MODE, "worker": shadow["worker"],
                         "capture": runtime.monitor.snapshot()["status"]}
                if shadow.get("reason"):
                    state["reason"] = shadow["reason"]
                self.emit(state)
                return
            if not path.startswith(API):
                self.emit({"error": "NOT_FOUND"}, 404)
                return
            header = self.headers.get("Authorization", "")
            if (self.headers.get("Origin") is not None or not header.isascii()
                    or not compare_digest(header, f"Bearer {token}")):
                self.emit({"error": "LOCAL_OPERATOR_AUTH_REQUIRED"}, 401)
                return
            try:
                data = runtime.get(path.removeprefix(API))
            except KeyError:
                self.emit({"error": "NOT_FOUND"}, 404)
                return
            self.emit(data)

        def log_message(self, *_args):
            pass

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--video-config", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--interface", required=True)
    parser.add_argument("--host", required=True)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("Invalid API port")
    try:
        config = read_json(args.config)
        video = read_json(args.video_config)
        collect_cic39 = config.get("collect_cic39", False)
        if type(collect_cic39) is not bool:
            raise ValueError("collect_cic39 must be a boolean")
        dataset = config.get("cic38_dataset")
        if dataset is not None and (not isinstance(dataset, str) or not dataset.strip()):
            raise ValueError("cic38_dataset must be a path to the prepared CIC_38 dataset")
        cic38 = Cic38(ROOT / dataset) if dataset is not None else None
        monitor = LiveMonitor(host=args.host, interface=args.interface, seconds=None,
                              video_port=video["http_port"], cameras=camera_endpoints(video),
                              collect_cic39=collect_cic39, cic38=cic38)
        model_settings = config["xgboost_shadow"]
        preset = preset_for_order(model_settings["feature_order"])
        if preset == "CIC_38":
            probe = None
            shadow = Cic38Shadow(monitor, model_settings)
        else:
            probe = attach_model_probe(monitor, preset=preset, sizes=(model_settings.get("window_packets", 10),))
            shadow = XGBoostShadow(probe, model_settings)
            if shadow.peer_predictions:
                probe.enable_peer_windows(shadow.group_size, shadow.max_age)
        runtime = Runtime(monitor, probe, shadow, ROOT / config.get("registry", "appdemo/device_profiles.json"))
        server = make_server(args.port, runtime, get_token(operator_token_path()))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    with server, monitor.start(), shadow.start():
        print(f"AI Security V2 127.0.0.1:{args.port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
