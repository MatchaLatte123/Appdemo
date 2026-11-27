"""Read the local V2 inference API."""
from dataclasses import dataclass
from http.client import HTTPConnection
from ipaddress import IPv4Address
import json
from pathlib import Path

from security_ai.live.server import API, operator_token_path

ROOT = Path(__file__).resolve().parents[2]
PROJECT = ROOT / "security_simulator"
RESOURCES = {"health", "overview", "xgboost-shadow", "collector-features", "devices"}


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


@dataclass
class Deployment:
    settings: dict
    ai_config: dict
    video_config: dict
    host: str
    interface: str
    ai_port: int | None
    broker_port: int
    video_host: str
    video_port: int
    state_path: Path
    poll_seconds: float

    @classmethod
    def read(cls, config):
        opts = read_json(config)
        launcher = read_json(ROOT / opts["launcher"])
        ai = launcher.get("ai")
        video = read_json(ROOT / launcher["video"]["config"])
        video_host = str(IPv4Address(video["http_host"]))
        host = str(IPv4Address(ai["live"]["host"] if ai else
                              opts.get("observed_host", video_host if video_host != "0.0.0.0" else "127.0.0.1")))
        model = read_json(ROOT / ai["config"]) if ai else {}
        return cls(opts, model, video, host, ai["live"]["interface"] if ai else "",
                   int(ai["port"]) if ai else None, int(opts["broker_port"]),
                   host if video_host == "0.0.0.0" else video_host,
                   int(video["http_port"]), ROOT / opts["state_path"], max(0.5, min(10, float(opts["poll_seconds"]))))


class AiClient:
    def __init__(self, deployment, token_file=None):
        self.deployment = deployment
        self.token_file = token_file or operator_token_path()

    def get(self, resource):
        if resource not in RESOURCES:
            raise ValueError("Unknown read-only resource")
        if self.deployment.ai_port is None:
            raise ValueError("No live AI runtime is configured")
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
        if expected.get("label_mapping") and actual.get("label_mapping") != expected["label_mapping"]:
            raise ValueError("Active model class labels differ from the launcher configuration")
        if actual.get("idle_thresholds", {}) != expected.get("idle_thresholds", {}):
            raise ValueError("Applied idle thresholds differ from launcher configuration")
