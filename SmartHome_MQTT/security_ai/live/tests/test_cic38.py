from ctypes import c_float
from math import isclose
import json
from threading import Lock
from types import SimpleNamespace

from security_ai.live.cic38 import Cic38
from security_ai.live.cic_input_probe import attach_model_probe
from security_ai.live.cic38_shadow import Cic38Shadow
from security_ai.live.live_monitor import LiveMonitor
from security_ai.web_ui.cic_full import CIC_38


def test_cic38_uses_training_order_and_normalization_for_ten_packet_windows(tmp_path):
    (tmp_path / "manifest.json").write_text(json.dumps({"features": CIC_38}), encoding="utf-8")
    (tmp_path / "preprocessing.json").write_text(json.dumps({
        "features": CIC_38, "method": "z-score", "fit_on": "TRAIN",
        "parameters": {name: {"mean": i, "scale": i+1} for i, name in enumerate(CIC_38)}
    }), encoding="utf-8")
    scaler = Cic38(tmp_path)
    monitor = LiveMonitor(host="192.0.2.1", interface="Ethernet", seconds=None, video_port=8765,
                          cameras=(("192.0.2.3", 81),), cic38=scaler)
    assert monitor.capture_filter().startswith("ip and host 192.0.2.1 and tcp")
    probe = attach_model_probe(monitor, sizes=(10,))
    assert monitor.cic39_snapshot() is None
    assert monitor.cic38_snapshot()["lanes"]["MQTT_BROKER"]["windows"][10]["latest"] is None
    for i in range(13):
        frame = {"frame.time_epoch": str(1700000000 + i/1000), "frame.len": "60", "frame.cap_len": "60",
                 "eth.type": "0x0800", "ip.proto": "6", "ip.ttl": "64", "tcp.hdr_len": "20",
                 "tcp.flags": "0x0010", "tcp.srcport": "55000", "tcp.dstport": "1883",
                 "frame.protocols": "eth:ip:tcp", "ip.src": "192.0.2.2", "ip.dst": "192.0.2.1"}
        frames = [frame, {**frame, "tcp.dstport": "8888"}]
        if i < 10:
            frames.append({**frame, "tcp.dstport": "8765", "tcp.flags": "0x0002"})
        if i < 9:
            frames.append({**frame, "ip.src": "192.0.2.3", "tcp.srcport": "81", "tcp.dstport": "55000"})
        for row in frames:
            monitor.consume(chr(9).join(row.get(name, "") for name in monitor.fields))
    result = monitor.cic38_snapshot()
    assert result["feature_order"] == CIC_38 and result["normalization"] == "TRAIN_Z_SCORE"
    assert result["decision_eligible"] is False
    assert result["window_sizes"] == (10,)
    mqtt = result["lanes"]["MQTT_BROKER"]["windows"][10]
    video = result["lanes"]["VIDEO_API"]["windows"][10]
    camera = result["lanes"]["CAMERA[192.0.2.3]:81"]["windows"][10]
    assert (mqtt["completed"], mqtt["pending"]) == (1, 3)
    assert (video["completed"], video["pending"]) == (1, 0)
    assert video["latest"]["features"]["syn_count"] == 10
    assert (camera["completed"], camera["pending"], camera["latest"]) == (0, 9, None)
    assert set(probe.snapshot()["lanes"]["MQTT_BROKER"]["windows"]) == {10}
    latest = mqtt["latest"]
    assert tuple(latest["features"]) == CIC_38 and len(latest["normalized_vector"]) == 38
    assert latest["features"]["ack_count"] == 10
    assert all(isclose(value, (c_float(latest["features"][name]).value - mean) / scale)
               for name, value, (mean, scale) in zip(CIC_38, latest["normalized_vector"], scaler.scales, strict=True))


def test_cic38_shadow_scores_latest_window_by_argmax_and_expires_it():
    state = {"pending": 0, "completed": 7,
             "latest": {"start": 98., "end": 100., "normalized_vector": [0.] * 38}}
    video = {**state, "latest": {**state["latest"], "normalized_vector": [1.] * 38}}
    monitor = SimpleNamespace(cic38_snapshot=lambda: {"lanes": {
        "MQTT_BROKER": {"service": "MQTT", "windows": {10: state}},
        "VIDEO_API": {"service": "Video", "windows": {10: video}},
    }})
    calls = []
    def predict(values):
        calls.append(values)
        return [.40, .35, .25] if values[0] == 0. else [.10, .80, .10]

    worker = SimpleNamespace(predict_cic38=predict)
    shadow = object.__new__(Cic38Shadow)
    shadow.monitor, shadow.max_age, shadow.scores, shadow.lock = monitor, 30, {}, Lock()
    shadow.tick(worker, now=101.)
    lane = shadow.state["lanes"]["MQTT_BROKER"]
    assert lane["prediction"] == "Benign"
    assert isclose(lane["score"], .60)
    assert lane["probabilities"] == {"Benign": .40, "DoS": .35, "DDoS": .25}
    assert shadow.state["lanes"]["VIDEO_API"]["prediction"] == "DoS"
    shadow.tick(worker, now=102.)
    assert len(calls) == 2
    shadow.tick(worker, now=132.)
    assert all(lane["status"] == "STALE_WINDOW" and lane["score"] is None
               for lane in shadow.state["lanes"].values())
