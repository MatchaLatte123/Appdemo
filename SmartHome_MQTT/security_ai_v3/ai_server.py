"""Serve the local Adaptive AI Security V3 dashboard and operator API."""
import argparse
from contextlib import nullcontext
from hmac import compare_digest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import IPv4Address
import json
from pathlib import Path
import sqlite3
from urllib.parse import unquote, urlsplit

from .adaptive_security import Adaptive, Conflict
from .isolated_capture import CaptureBudget, IsolatedMonitor
from .live_monitor import LiveMonitor, camera_endpoints

DEFAULT_PORT = 8767


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def make_server(port, adaptive):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            self.request.settimeout(3)
            super().setup()

        def emit_json(self, data, status=200):
            payload = json.dumps(data, allow_nan=False, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(payload)

        def adaptive_route(self, write=False):
            path = urlsplit(self.path).path.removeprefix("/api/security/adaptive/")
            authorization = self.headers.get("Authorization", "")
            expected = f"Bearer {adaptive.token}"
            if (self.headers.get("Origin") is not None or not authorization.isascii()
                    or not compare_digest(authorization, expected)):
                self.emit_json({"error": "LOCAL_OPERATOR_AUTH_REQUIRED"}, 401)
                return
            parts = [unquote(part) for part in path.split("/")]
            try:
                if write:
                    if len(parts) != 2 or parts[0] != "operator":
                        raise KeyError(path)
                    length = self.headers.get("Content-Length", "")
                    if (self.headers.get("Transfer-Encoding") or not length.isascii()
                            or not length.isdecimal() or not 2 <= int(length) <= 4096
                            or self.headers.get_content_type() != "application/json"):
                        raise ValueError("INVALID_REQUEST_BODY")
                    data = json.loads(self.rfile.read(int(length)))
                    if not isinstance(data, dict):
                        raise ValueError("INVALID_REQUEST_BODY")
                    result = adaptive.command(parts[1], data)
                else:
                    if len(parts) > 2:
                        raise KeyError(path)
                    result = adaptive.get(*parts)
            except KeyError:
                self.emit_json({"error": "NOT_FOUND"}, 404)
                return
            except Conflict as exc:
                self.emit_json({"error": str(exc)}, 409)
                return
            except (ValueError, UnicodeError, TimeoutError):
                self.emit_json({"error": "INVALID_REQUEST_OR_PROFILE"}, 400)
                return
            self.emit_json(result)

        def do_POST(self):
            if self.path.startswith("/api/security/adaptive/"):
                self.adaptive_route(write=True)
            else:
                self.send_error(404)

        def do_GET(self):
            if self.path.startswith("/api/security/adaptive/"):
                self.adaptive_route()
                return
            if self.path != "/api/security/dashboard":
                self.send_error(404)
                return
            self.emit_json({"mode": "BEHAVIORAL_V3", "monitoring": adaptive.get("health")["status"]})

        def log_message(self, *_args):
            pass

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    live = parser.add_mutually_exclusive_group(required=True)
    live.add_argument("--live-continuous", action="store_true")
    live.add_argument("--live-seconds", type=int)
    parser.add_argument("--live-interface", required=True)
    parser.add_argument("--live-host", required=True)
    parser.add_argument("--isolated-capture", action="store_true")
    parser.add_argument("--capture-budget", type=Path)
    parser.add_argument("--live-video-port", type=int, default=8765)
    parser.add_argument("--live-video-config", type=Path,
                        default=Path(__file__).resolve().parents[1] / "recording_service/config.json")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or (args.capture_budget and not args.isolated_capture):
        parser.error("Invalid AI port or capture budget")

    try:
        video = read_json(args.live_video_config)
        video_host = str(IPv4Address(video.get("http_host", "127.0.0.1")))
        endpoint = ("127.0.0.1" if video_host == "0.0.0.0" else video_host, args.live_video_port)
        options = {"host": args.live_host, "interface": args.live_interface, "seconds": args.live_seconds,
                   "video_port": args.live_video_port, "cameras": camera_endpoints(video)}
        if args.isolated_capture:
            budget = CaptureBudget(**read_json(args.capture_budget)) if args.capture_budget else CaptureBudget()
            monitor = IsolatedMonitor(budget, **options)
        else:
            monitor = LiveMonitor(**options)
        settings = read_json(args.config)
        if settings.get("cic_input_probe", False):
            from .cic_input_probe import attach_model_probe

            attach_model_probe(monitor)
        adaptive = Adaptive(settings, monitor, video, endpoint)
        if shadow_settings := settings.get("xgboost_shadow"):
            from .xgboost_shadow import XGBoostShadow

            probe = getattr(monitor, "model_inputs", None)
            if probe is None:
                raise ValueError("xgboost_shadow requires cic_input_probe")
            adaptive.xgboost_shadow = XGBoostShadow(probe, shadow_settings)
    except (ValueError, OSError, KeyError, TypeError, sqlite3.Error) as exc:
        parser.error(str(exc))
    shadow = adaptive.xgboost_shadow.start() if adaptive.xgboost_shadow else nullcontext()
    with make_server(args.port, adaptive) as server, adaptive.start(), shadow, monitor.start():
        print(f"AI Security V3 127.0.0.1:{args.port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
