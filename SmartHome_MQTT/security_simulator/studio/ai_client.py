"""Read the existing local V3 operator API."""
from dataclasses import dataclass
from http.client import HTTPConnection
from ipaddress import IPv4Address
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROJECT = ROOT / "security_simulator"
API = "/api/security/adaptive/"
RESOURCES = {"health", "overview", "xgboost-shadow", "collector-features", "devices", "incidents"}


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


@dataclass
class Deployment:
    settings: dict
    ai_config: dict
    video_config: dict
    host: str
    interface: str
    ai_port: int
    broker_port: int
    video_host: str
    video_port: int
    state_path: Path
    poll_seconds: float
    capture_timeout: int

    @classmethod
    def read(cls, config):
        opts = read_json(config)
        launcher = read_json(ROOT / opts["launcher"])
        ai = launcher["ai"]
        video = read_json(ROOT / launcher["video"]["config"])
        host = str(IPv4Address(ai["live"]["host"]))
        video_host = str(IPv4Address(video["http_host"]))
        return cls(opts, read_json(ROOT / ai["config"]), video, host, ai["live"]["interface"],
                   int(ai["port"]), int(opts["broker_port"]), host if video_host == "0.0.0.0" else video_host,
                   int(video["http_port"]), ROOT / opts["state_path"], max(0.5, min(10, float(opts["poll_seconds"]))),
                   max(5, min(180, int(opts["capture_read_timeout_seconds"]))))


class AiClient:
    def __init__(self, deployment, token_file=None):
        self.deployment = deployment
        state = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local/state")
        self.token_file = token_file or state / "SmartHomeAI/AdaptiveSecurity/operator.token"

    def get(self, resource):
        if resource not in RESOURCES:
            raise ValueError("Unknown read-only resource")
        token = self.token_file.read_text(encoding="ascii").strip()
        conn = HTTPConnection("127.0.0.1", self.deployment.ai_port, timeout=2)
        try:
            conn.request("GET", API + resource, headers={"Authorization": f"Bearer {token}"})
            response = conn.getresponse()
            if response.status != 200:
                raise ValueError(f"AI API {resource}: HTTP {response.status}")
            content = response.read(4 * 1024**2 + 1)
            if len(content) > 4 * 1024**2:
                raise ValueError("AI API response exceeds 4 MiB")
            return json.loads(content)
        finally:
            conn.close()

    def sample(self):
        responses = {name: self.get(name) for name in ("xgboost-shadow", "collector-features", "overview")}
        self.verify_model(responses["xgboost-shadow"])
        return responses

    def verify_model(self, prediction):
        actual = prediction.get("model", {})
        expected = self.deployment.ai_config["xgboost_shadow"]
        for key in ("run_id", "sha256", "feature_order", "window_packets", "benchmark_threshold"):
            if actual.get(key) != expected.get(key):
                raise ValueError(f"Model identity mismatch between launcher config and active API: {key}")
        if actual.get("idle_thresholds", {}) != expected.get("idle_thresholds", {}):
            raise ValueError("Applied idle thresholds differ from launcher configuration")
