from decimal import Decimal

import pytest

from security_ai_v2.rebuild.contract import BASIC_10, PARITY_BLOCK, from_tshark, vector, windows
from security_ai_v3.cic_input_probe import attach_model_probe
from security_ai_v3.isolated_capture import BRANCH_FIELDS, CaptureBranch, CaptureBudget, IsolatedMonitor
from security_ai_v3.live_monitor import FIELDS, LiveMonitor

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


def test_isolated_decoder_feeds_the_same_probe():
    monitor = IsolatedMonitor(**OPTIONS)
    probe = attach_model_probe(monitor)
    for name in ("MQTT", "Video"):
        branch = monitor.branches[name]
        assert isinstance(branch, CaptureBranch)
        branch.session.update(decoder_frames=0, first_packet_epoch=None,
                              http_requests=0, http_errors=0, tcp_retransmissions=0)
        scope = "MQTT_BROKER" if name == "MQTT" else "VIDEO_API"
        for index in range(10):
            branch.consume(line(BRANCH_FIELDS[name], packet(index, scope)))
        assert probe.snapshot()["lanes"][scope]["windows"][10]["completed"] == 1
