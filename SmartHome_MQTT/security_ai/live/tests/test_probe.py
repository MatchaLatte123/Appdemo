from decimal import Decimal

import pytest

from security_ai.web_ui.contract import BASIC_10, PARITY_BLOCK, from_tshark, vector, windows
from security_ai.live.cic_input_probe import attach_model_probe
from security_ai.live.live_monitor import FIELDS, LiveMonitor

HOST = "192.0.2.1"
CAMERA = "192.0.2.2"
CAMERA_SCOPE = f"CAMERA[{CAMERA}]:80"
OPTIONS = {"host": HOST, "interface": "fixture", "seconds": None,
           "video_port": 8765, "cameras": [(CAMERA, 80)]}


def packet(index, scope="MQTT_BROKER"):
    if scope == "MQTT_BROKER":
        src, sport, dst, dport = "192.0.2.3", 55000, HOST, 1883
    elif scope == "VIDEO_API":
        src, sport, dst, dport = "192.0.2.3", 55000, HOST, 8765
    else:
        src, sport, dst, dport = CAMERA, 80, HOST, 54000
    size = str(100 + index % 10)
    return {"frame.time_epoch": str(Decimal("1760000000.0") + Decimal(index) / 10**7),
            "frame.len": size, "frame.cap_len": size, "tcp.flags": "0x02" if index % 2 else "0x10",
            "ip.src": src, "ip.dst": dst, "tcp.srcport": str(sport), "tcp.dstport": str(dport)}


def line(fields, item):
    return "\t".join(item.get(key, "") for key in fields)


def test_model_window_order_and_scope_with_existing_capture_metadata():
    monitor = LiveMonitor(**OPTIONS)
    probe = attach_model_probe(monitor)
    rows = {scope: [] for scope in ("MQTT_BROKER", "VIDEO_API", CAMERA_SCOPE)}
    for index in range(100):
        for scope in rows:
            entry = packet(index, scope)
            rows[scope].append(from_tshark(entry, HOST, cameras={(CAMERA, 80)}))
            monitor.consume(line(FIELDS, entry))

    data = probe.snapshot()
    assert data["feature_order"] == BASIC_10
    assert data["parity"] == PARITY_BLOCK
    assert data["decision_eligible"] is False
    assert monitor.snapshot()["services"]["Video"]["frames"] == 200
    for scope, packets in rows.items():
        lane = data["lanes"][scope]["windows"]
        for size, count in ((10, 10), (100, 1)):
            latest = lane[size]["latest"]
            assert lane[size]["completed"] == count
            assert latest["window"]["packet_count"] == size
            assert latest["vector"] == vector(list(windows(packets, size))[-1]["features"])
            assert latest["features"]["IAT"] == pytest.approx(1e-7, rel=1e-12)
            # Duplicated 0..9 lengths have sum of squared deviations = 82.5 per ten packets.
            assert latest["features"]["Std"] == pytest.approx((82.5 * (size // 10) / (size - 1)) ** .5)
            assert latest["decision_eligible"] is False


def test_packet_dashboard_counters_keep_camera_endpoints_separate():
    second = "192.0.2.4"
    monitor = LiveMonitor(**{**OPTIONS, "cameras": [(CAMERA, 80), (second, 80)]})
    for scope in ("MQTT_BROKER", "VIDEO_API", CAMERA_SCOPE):
        monitor.consume(line(FIELDS, packet(0, scope)))
    captured = packet(1, CAMERA_SCOPE)
    captured["ip.src"] = second
    monitor.consume(line(FIELDS, captured))
    state = monitor.snapshot()
    assert state["services"]["Video"]["frames"] == 3
    assert state["video_paths"]["camera_to_server"]["frames"] == 2
    assert state["packet_lanes"][CAMERA_SCOPE]["bytes"] == 100
    assert state["packet_lanes"][f"CAMERA[{second}]:80"]["bytes"] == 101
    assert state["packet_lanes"]["VIDEO_API"]["frames"] == 1
    assert state["packet_lanes"]["MQTT_BROKER"]["frames"] == 1


def test_probe_resets_gap_and_capture_boundaries():
    probe = attach_model_probe(LiveMonitor(**OPTIONS))
    for i in range(9):
        probe.observe(packet(i), "MQTT")
    bad = packet(9)
    bad["frame.cap_len"] = "54"
    assert not probe.observe(bad, "MQTT")
    for i in range(10, 20):
        probe.observe(packet(i), "MQTT")
    assert probe.snapshot()["lanes"]["MQTT_BROKER"]["windows"][10]["completed"] == 1
    probe.invalidate("MQTT", reason="KERNEL_DROPS", hold=True)
    for i in range(20, 30):
        assert not probe.observe(packet(i), "MQTT")
    assert probe.snapshot()["lanes"]["MQTT_BROKER"]["windows"][10]["latest"] is None
    probe.start_capture("MQTT")
    for i in range(30, 39):
        probe.observe(packet(i), "MQTT")
    assert not probe.observe(packet(31), "MQTT")
    assert probe.snapshot()["lanes"]["MQTT_BROKER"]["windows"][10]["pending"] == 0


def test_window_preserves_bidirectional_ports_and_multiple_peers_without_assigning_attack_source():
    probe = attach_model_probe(LiveMonitor(**OPTIONS))
    for index in range(10):
        entry = packet(index)
        if index == 0:
            entry.update({"ip.src": HOST, "ip.dst": "192.0.2.3", "tcp.srcport": "1883",
                          "tcp.dstport": "55000", "tcp.flags": "0x12"})
        elif index >= 5:
            entry["ip.src"] = "192.0.2.4"
        probe.observe(entry, "MQTT")
    latest = probe.snapshot()["lanes"]["MQTT_BROKER"]["windows"][10]["latest"]
    evidence = latest["source_evidence"]
    assert evidence["peer_count"] == evidence["inbound_peer_count"] == 2
    assert evidence["attack_source_attribution"] == "UNKNOWN"
    first, second = evidence["peers"]
    assert (first["ip"], first["packets_to_host"], first["packets_from_host"]) == ("192.0.2.3", 4, 1)
    assert second["packets_to_host"] == 5 and second["packets_from_host"] == 0
    connection, = first["connections"]
    assert (connection["remote_port"], connection["local_port"], connection["syn_to_host"]) == (55000, 1883, 2)
    assert sum(p["packets_to_host"] + p["packets_from_host"] for p in evidence["peers"]) == 10



def test_peer_windows_do_not_mix_ips_and_reset_with_capture_quality():
    probe = attach_model_probe(LiveMonitor(**OPTIONS))
    probe.enable_peer_windows(10, 30)
    for index in range(20):
        row = packet(index)
        if index % 2:
            row["ip.src"] = "192.0.2.4"
        probe.observe(row, "MQTT")
        if index == 9:
            current = probe.snapshot()
            assert current["lanes"]["MQTT_BROKER"]["windows"][10]["completed"] == 1
            assert all(p["latest"] is None and p["pending"] == 5
                       for p in current["peer_windows"]["MQTT_BROKER"].values())
    peers = probe.snapshot()["peer_windows"]["MQTT_BROKER"]
    assert set(peers) == {"192.0.2.3", "192.0.2.4"}
    for ip, state in peers.items():
        assert state["completed"] == 1
        evidence = state["latest"]["source_evidence"]
        assert evidence["peer_count"] == 1 and evidence["peers"][0]["ip"] == ip
        assert evidence["peers"][0]["packets_to_host"] == 10
    probe.invalidate("MQTT", reason="CAPTURE_ERROR", hold=True)
    assert probe.snapshot()["peer_windows"]["MQTT_BROKER"] == {}
