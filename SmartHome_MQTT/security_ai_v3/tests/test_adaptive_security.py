import json
from http.client import HTTPConnection
from threading import Thread
from time import time

import pytest

from security_ai_v3.adaptive_evidence import correlate
from security_ai_v3.adaptive_profiles import detect, exclusion, fit, grouped, isolated_samples, load_artifact, partition, profile_key
from security_ai_v3.adaptive_security import Adaptive, Conflict, OPEN_WINDOWS, RECOVERY_WINDOWS
from security_ai_v3.adaptive_telemetry import FEATURES, Identity, SCHEMA, Telemetry
from security_ai_v3.ai_server import make_server


def sample(start, *, burst=False, quality="UNVERIFIED", status="HEALTHY", service="MQTT"):
    start -= start % 5
    role = "MQTT_BROKER" if service == "MQTT" else "VIDEO_SERVER"
    values = dict.fromkeys(FEATURES[role])
    values.update(packet_rate=100 if burst else 2, byte_rate=40000 if burst else 400,
                  connection_rate=15 if burst else .2, rst_rate=0)
    if service == "MQTT":
        values.update(publish_rate=12 if burst else .2, subscribe_rate=0, clients=3, broker_health=1)
    return {"key": f"service:{service}", "role": role, "service": service, "schema": SCHEMA,
            "start": start, "end": start + 5, "mode": "unknown", "values": values,
            "rolling": values.copy(), "quality": quality, "service_state": status,
            "capture": {}, "identity": {}, "contributors": [], "audit_version": 1}


def accepted_records():
    start = int(time()) - 3000
    return [(str(session), sample(start + session * 300 + 5 * i))
            for session in range(4) for i in range(40)]


def adaptive(tmp_path):
    return Adaptive({}, None, {}, ("127.0.0.1", 8765), tmp_path)


def commit_sessions(runtime):
    with runtime.store.db:
        for session, row in accepted_records():
            runtime.store.db.execute(
                "INSERT OR IGNORE INTO sessions VALUES (?, ?, ?, 'ACCEPTED', 'test', 'reviewed test fixture', ?)",
                (session, row["start"], row["start"] + 200, SCHEMA))
            runtime.store.db.execute("INSERT INTO samples VALUES (?, ?, ?, ?)",
                                     (session, row["start"], row["key"], json.dumps(row)))


def test_identity_uses_session_time_tuple_and_recorder_mapping():
    registry = {"plug": {"type": "relay"}, "cam": {"type": "camera"}}
    ip = "192.0.2.10"
    binding = {"source": "BROKER_AUTH_SESSION", "ip": ip, "port": 3080, "device_id": "plug",
               "start": 100, "end": 120, "username": "relay1", "client_id": "plug1", "session_id": "authenticated"}
    cameras = [{"id": "cam", "stream_url": f"http://{ip}:80/stream"}]
    resolver = Identity(registry, cameras, [binding])
    assert resolver.resolve("MQTT_DEVICE", ip, 3081, 105, 110, {})["status"] == "UNKNOWN"
    assert resolver.resolve("MQTT_DEVICE", ip, 3080, 100, 105, {})["device_id"] == "plug"
    assert resolver.resolve("MQTT_DEVICE", ip, 3080, 115, 125, {})["status"] == "AMBIGUOUS"
    assert resolver.resolve("CAMERA_DEVICE", ip, 80, 105, 110, {"cam": {}})["status"] == "PROBABLE"
    camera_binding = {**binding, "source": "VERIFIED_RECORDER_MAPPING", "port": 80, "device_id": "cam"}
    resolver = Identity(registry, cameras, [binding, camera_binding])
    assert resolver.resolve("CAMERA_DEVICE", ip, 80, 105, 110, {})["status"] == "PROBABLE"
    assert resolver.resolve("CAMERA_DEVICE", ip, 80, 105, 110, {"cam": {}})["status"] == "VERIFIED"


def test_packet_telemetry_quality_and_rolling():
    collector = Telemetry(Identity({}, []))
    start = int(time()) // 5 * 5 - 100
    collector.next_start = start
    for index in range(12):
        stamp = start + index * 5
        packet = {"frame.time_epoch": str(stamp + 1), "ip.src": "192.0.2.1", "ip.dst": "192.0.2.2",
                  "tcp.srcport": "9000", "tcp.dstport": "1883", "tcp.flags": "0x0002",
                  "frame.len": "80", "frame.cap_len": "80", "mqtt.msgtype": "3;8"}
        collector.observe(packet, "MQTT", "192.0.2.2", ())
        batch, = collector.drain(stamp + 7, {"status": "ACTIVE"}, {}, {})
        broker = next(r for r in batch if r["role"] == "MQTT_BROKER")
        video = next(r for r in batch if r["role"] == "VIDEO_SERVER")
        assert broker["quality"] == "UNVERIFIED"
        assert broker["values"]["connection_rate"] == .2
        assert broker["values"]["publish_rate"] == .2
        assert broker["values"]["clients"] is None
        assert video["quality"] == "UNAVAILABLE"
        assert video["values"]["packet_rate"] is None
        assert broker["rolling"]["packet_rate"] == (pytest.approx(.2) if index == 11 else None)
    collector.observe({**packet, "frame.time_epoch": str(start + 61), "frame.cap_len": "40"},
                      "MQTT", "192.0.2.2", ())
    batch, = collector.drain(start + 67, {"status": "ACTIVE"}, {}, {})
    broker = next(r for r in batch if r["role"] == "MQTT_BROKER")
    assert broker["quality"] == "LOSS"
    assert broker["values"]["packet_rate"] is None


def test_profiles_require_independent_sessions_and_detect_behavior(tmp_path):
    output = tmp_path / "profile.joblib"
    report, digest = fit(accepted_records()[:120], output)
    assert digest is None and not output.exists()
    records = accepted_records()
    report, digest = fit(records, output)
    key = profile_key(records[0][1])
    assert report[key]["status"] == "READY"
    assert report[key]["false_anomaly_fraction"] == 0
    model = load_artifact(output, digest)[key]
    assert detect(model, records[0][1])["anomaly"] is False
    shifted = sample(0, burst=True)
    assert {r["feature"] for r in detect(model, shifted)["abnormal"]} >= {"packet_rate", "publish_rate"}
    assert detect(model, {**shifted, "schema": "unknown"}) is None
    assert detect(model, {**shifted, "quality": "LOSS"}) is None
    with pytest.raises(ValueError, match="hash"):
        load_artifact(output, "bad")


def test_learning_incidents_acknowledgment_recovery_and_rollback(tmp_path):
    runtime = adaptive(tmp_path)
    try:
        session = runtime.command("start", {"note": "normal operation"})["id"]
        stamp = int(time()) + 5
        runtime.process([sample(stamp, quality="LOSS")])
        runtime.process([sample(stamp + 5)])
        runtime.command("stop", {"id": session})
        review = runtime.get("learning", session)
        assert review["coverage"] == {"MQTT_BROKER": 1}
        assert review["excluded_windows"] == 1
        assert review["audit_complete"] is True
        assert review["groups"][0]["exclusion_reasons"] == {"LOSS": 1}
        runtime.command("reject", {"id": session, "note": "planned exclusion"})
        accepted = runtime.command("start", {"note": "normal traffic observed"})["id"]
        for index in range(40):
            runtime.process([sample(stamp + 5 * index)])
        runtime.command("stop", {"id": accepted})
        assert runtime.command("accept", {"id": accepted, "note": "operator observed normal"})["status"] == "ACCEPTED"
        assert runtime.command("build", {})["status"] == "NEEDS_MORE_NORMAL_DATA"
        commit_sessions(runtime)
        v1 = runtime.command("build", {})["id"]
        overview = runtime.get("overview")
        assert overview["baseline"] == "NOT_TRAINED"
        assert overview["service_readiness"]["MQTT_BROKER"]["status"] == "READY_TO_BUILD"
        assert overview["service_readiness"]["VIDEO_SERVER"]["status"] == "NEEDS_MORE_NORMAL_DATA"
        runtime.command("activate", {"id": v1})
        assert runtime.get("overview")["baseline"] == "READY"
        next_version = runtime.command("build", {})["id"]
        runtime.command("activate", {"id": next_version})
        runtime.command("rollback", {"id": v1})
        assert runtime.get("overview")["active_version"] == v1
        # Reusing the wrong operation for the active version cannot modify state.
        with pytest.raises(Conflict):
            runtime.command("activate", {"id": v1})
        letter = runtime.command("start", {"note": "freeze test"})["id"]
        for i in range(OPEN_WINDOWS + 2):
            runtime.process([sample(stamp + 10 + i * 5, burst=True, status="DEGRADED")])
        incidents = runtime.get("incidents")["incidents"]
        assert len(incidents) == 1
        assert incidents[0]["status"] == "ACTIVE"
        assert incidents[0]["behavior"] == "SUSPECTED_MQTT_FLOOD"
        assert runtime.get("learning", letter)["coverage"] == {}
        assert incidents[0]["consecutive_anomalous_windows"] == OPEN_WINDOWS + 2
        assert incidents[0]["profile_key"]
        assert incidents[0]["evidence"]["statistical_score"] > 0
        for i in range(RECOVERY_WINDOWS):
            runtime.process([sample(stamp + 10 + (OPEN_WINDOWS + 2 + i) * 5, quality="LOSS")])
        assert runtime.get("incidents")["incidents"][0]["status"] == "ACTIVE"
        for i in range(RECOVERY_WINDOWS):
            runtime.process([sample(stamp + 10 + (OPEN_WINDOWS + 2 + RECOVERY_WINDOWS + i) * 5)])
        assert runtime.get("incidents")["incidents"][0]["status"] == "RECOVERED"
        runtime.store.close()
        runtime = adaptive(tmp_path)
        assert runtime.get("overview")["active_version"] == v1
        assert runtime.get("incidents")["incidents"][0]["status"] == "RECOVERED"
    finally:
        runtime.store.close()


def test_evidence_keeps_health_and_identity_separate():
    video = sample(100, status="DEGRADED", service="Video")
    result = correlate(video, None, video)
    assert result["behavior"] == "SERVICE_DEGRADATION"
    assert result["suspected_sources"] == []
    assert result["affected_service"]["role"] == "AFFECTED_SERVICE"
    mqtt = sample(100, burst=True, status="DEGRADED")
    contribution = [{"identity": {"status": "UNKNOWN", "ip": f"192.0.2.{i}"}, "activity": 10}
                    for i in (1, 2)]
    mqtt["contributors"] = contribution
    result = correlate(mqtt, {"anomaly": True, "isolation_score": .7,
                       "abnormal": [{"feature": "publish_rate", "deviation": 8}]}, mqtt)
    assert result["behavior"] == "MULTI_SOURCE_OVERLOAD"
    assert len(result["suspected_sources"]) == 2
    assert all("device_id" not in r["target"] for r in result["recommendations"])


def test_api_authentication_and_dashboard_compatibility(tmp_path):
    runtime = adaptive(tmp_path)
    with make_server(0, runtime) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        prefix = "/api/security/adaptive/"
        headers = {"Authorization": f"Bearer {runtime.token}", "Content-Type": "application/json"}

        def request(method, path, hdr=None, body=None):
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            try:
                connection.request(method, path, body=body, headers=hdr or {})
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()

        try:
            assert request("GET", "/api/security/dashboard") == (
                200, {"mode": "BEHAVIORAL_V3", "monitoring": "STARTING"})
            assert request("GET", prefix + "overview")[0] == 401
            collector = prefix + "collector-features"
            assert request("GET", collector)[0] == 401
            assert request("GET", collector, headers) == (200, {"status": "DISABLED", "decision_eligible": False})
            from security_ai_v3.cic_input_probe import attach_model_probe
            from security_ai_v3.live_monitor import LiveMonitor
            runtime.monitor = LiveMonitor(host="192.0.2.1", interface="fixture", seconds=None, video_port=8765)
            attach_model_probe(runtime.monitor)
            code, features = request("GET", collector, headers)
            assert code == 200 and features["status"] == "DIAGNOSTIC_ONLY"
            assert features["window_sizes"] == [10, 100] and not features["decision_eligible"]
            status, overview = request("GET", prefix + "overview", headers)
            assert status == 200 and not overview["enforcement"]
            assert request("POST", prefix + "operator/start", headers, "[]")[0] == 400
            cross_origin = {**headers, "Origin": "https://example.invalid"}
            assert request("POST", prefix + "operator/start", cross_origin, '{"note":"blocked"}')[0] == 401
            assert request("POST", prefix + "operator/start", headers, '{"note":"offline test"}')[0] == 200
        finally:
            server.shutdown()
            thread.join()
            runtime.store.close()


def test_v31_retains_excluded_denominator_and_session_time():
    data = accepted_records()
    key = profile_key(data[0][1])
    stats, (train, cal, val) = partition(grouped(data)[key])
    assert stats["status"] == "READY_TO_BUILD"
    assert stats["partitions"]["calibration"]["session_sha256"]["2"]
    assert [len(train), len(cal), len(val)] == [80, 40, 40]
    assert cal[0]["rolling"]["packet_rate"] is None
    assert val[0]["rolling"]["packet_rate"] is None
    assert val[11]["rolling"]["packet_rate"] == 2
    validation = data[-40:]
    lowered = data[:-40] + [
        (session, {**row, "quality": "LOSS"}) if i < 5 else (session, row)
        for i, (session, row) in enumerate(validation)]
    stats, _ = partition(grouped(lowered)[key])
    assert stats["status"] == "NEEDS_MORE_NORMAL_DATA"
    assert stats["partitions"]["validation"]["excluded"] == 5

    extended = data + [(validation[-1][0], sample(validation[-1][1]["end"] + offset * 5, quality="LOSS"))
                       for offset in range(5)]
    assert partition(grouped(extended)[key])[0]["status"] == "VALIDATION_INPUT_ELIGIBILITY"
    legacy = [(session, {**row, "audit_version": None}) if session == "3" else (session, row)
              for session, row in data]
    assert partition(grouped(legacy)[key])[0]["status"] == "INCOMPLETE_QUALITY_AUDIT"
    overlapping = [(session, {**row, "start": row["start"] - 200, "end": row["end"] - 200})
                   if session == "3" else (session, row) for session, row in data]
    assert partition(grouped(overlapping)[key])[0]["status"] == "OVERLAPPING_SESSIONS"
    assert exclusion({**train[0], "capture": {"decoder_gaps": 1}}) == "CAPTURE_EVIDENCE"


def test_v31_modes_are_independent_and_missing_window_resets_history():
    rows = [sample(5 * i) for i in range(14)]
    rows[1]["quality"] = "UNAVAILABLE"
    assert isolated_samples(rows)[-2]["rolling"]["packet_rate"] is None
    assert isolated_samples(rows)[-1]["rolling"]["packet_rate"] == 2
    changed = [(sid, {**row, "mode": "test:alternate"} if sid == "3" else row)
               for sid, row in accepted_records()]
    modes = grouped(changed)
    assert len(modes) == 2
    assert all(partition(g)[0]["status"] == "NEEDS_MORE_NORMAL_DATA" for g in modes.values())

def test_v31_normal_workloads_hold_out_both_services(tmp_path):
    rows = []
    for session, base in accepted_records():
        tick = (base["start"] // 5) % 10
        mqtt = {**base, "values": {**base["values"], "packet_rate": [0.2, 0.4, 4.0, 2.0, 1.0][tick % 5],
                                   "connection_rate": .2 if tick == 4 else 0}}
        video = sample(base["start"], service="Video")
        video["mode"] = "recording:1"
        video["values"].update(packet_rate=75 + tick, byte_rate=25000 + 1000 * tick,
                               camera_fps=12 + tick % 3, http_rate=.2 if tick in (3, 8) else None,
                               ffmpeg_health=1, recorder_error=0)
        rows.extend(((session, mqtt), (session, video)))
    report, digest = fit(rows, tmp_path / "both.joblib")
    assert digest and len(report) == 2
    for stats in report.values():
        assert stats["status"] == "READY"
        assert stats["false_incidents"] == 0
        assert stats["validation_input_eligibility"] == 1
        assert len(stats["dataset_sha256"]) == 64
        assert stats["validation_batch_inference_ms_per_window"] > 0
