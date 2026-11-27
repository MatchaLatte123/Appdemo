import csv
from io import StringIO
from pathlib import Path
import struct

import pytest

from security_ai.live.live_monitor import LiveMonitor
from security_ai.web_ui.cic_full import CIC_39, FIELDS, TSHARK, extract, write_rows


def frame_row(stamp, **fields):
    defaults = {"frame.time_epoch": str(stamp), "frame.len": "60", "frame.cap_len": "60",
                "eth.type": "0x0800", "ip.proto": "6", "ip.ttl": "64",
                "tcp.hdr_len": "20", "tcp.flags": "0x0010", "tcp.srcport": "443", "tcp.dstport": "50000"}
    defaults.update(fields)
    return "\t".join(defaults.get(name, "") for name in FIELDS)


def test_reject_truncated_frames_and_discard_partial_windows():
    output = StringIO()
    rows = [frame_row(0), frame_row(1, **{"frame.cap_len": "40"}),
            frame_row(2), frame_row(3), frame_row(4)]
    counts = write_rows(rows, output, 2)
    assert counts["windows"] == 1
    assert counts["invalid_frames"] == 1
    assert counts["discarded_frames"] == 3
    result = next(csv.DictReader(StringIO(output.getvalue())))
    assert float(result["IAT"]) == .5
    assert float(result["Rate"]) == 2
    assert float(result["HTTPS"]) == 1
    assert float(result["ack_count"]) == 2


@pytest.mark.skipif(not Path(TSHARK).is_file(), reason="Wireshark/TShark not installed")
def test_export_mixed_capture_all_39_columns(tmp_path):
    eth = bytes.fromhex("020000000001020000000002")
    ip_tcp = bytes.fromhex("450000280001000040060000c0000201c0000202")
    ip_udp = bytes.fromhex("4500001c0001000040110000c0000201c0000202")
    tcp = struct.pack("!HHIIBBHHH", 50000, 443, 1, 1, 0x50, 0x10, 4096, 0, 0)
    udp = struct.pack("!HHHH", 50001, 12345, 8, 0)
    arp = bytes.fromhex("0001080006040001" "020000000002" "c0000201" "000000000000" "c0000202")
    frames = [eth + b"\x08\x00" + ip_tcp + tcp, eth + b"\x08\x00" + ip_udp + udp, eth + b"\x08\x06" + arp]
    source = tmp_path / "mixed.pcap"
    with source.open("wb") as out:
        out.write(struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1))
        for i, frame in enumerate((frames * 34)[:100]):
            out.write(struct.pack("<IIII", 1700000000, i * 1000, len(frame), len(frame)))
            out.write(frame)
    for size in (10, 100):
        output = tmp_path / f"cic-{size}.csv"
        result = extract(output, pcap=source, window_size=size)
        assert result["windows"] == 100 // size and result["columns"] == 39
        assert result["schema"] == "cic39-author-aligned-v2"
        with output.open(newline="", encoding="utf-8") as stream:
            reader = csv.DictReader(stream)
            assert tuple(reader.fieldnames) == CIC_39
            row = next(reader)
            rest = list(reader)
        n_tcp = (size + 2) // 3
        n_udp = (size + 1) // 3
        assert int(row["Number"]) == size
        assert float(row["Tot sum"]) == n_tcp * 54 + (size - n_tcp) * 42
        assert float(row["Std"]) ** 2 == pytest.approx(float(row["Variance"]))
        assert float(row["TCP"]) == n_tcp / size and float(row["UDP"]) == n_udp / size
        assert float(row["ARP"]) == (size - n_tcp - n_udp) / size
        assert float(row["ack_count"]) == n_tcp
        assert float(row["Header_Length"]) == (n_tcp * 20 + n_udp * 8) / size
        base = 1700000000
        duration = (base + (size-1)*.001) - base
        assert float(row["IAT"]) == pytest.approx(duration / size)
        assert float(row["Rate"]) == pytest.approx(size / duration)
        assert float(row["IPv"]) == float(row["LLC"]) == (n_tcp+n_udp)/size
        assert all(float(window["IAT"]) == pytest.approx(.001, abs=3e-8) for window in rest)
        with pytest.raises(FileExistsError):
            extract(output, pcap=source, window_size=size)


def test_live_full_windows_alongside_endpoint_counters():
    host = "192.0.2.1"
    monitor = LiveMonitor(host=host, interface="Ethernet", seconds=None, video_port=8765, collect_cic39=True)
    assert monitor.capture_filter() == "ip or arp"
    for index in range(100):
        packet = dict(zip(FIELDS, frame_row(index + 1).split(chr(9)), strict=True))
        packet.update({"ip.src": "192.0.2.2", "ip.dst": host})
        if index % 2:
            packet.update({"eth.type": "0x0806", "ip.proto": "", "tcp.srcport": "", "tcp.dstport": ""})
        else:
            packet.update({"tcp.srcport": "50000", "tcp.dstport": "1883"})
        monitor.consume(chr(9).join(packet.get(name, "") for name in monitor.fields))
    full = monitor.cic39_snapshot()
    assert full["feature_order"] == CIC_39
    assert full["windows"][10]["completed"] == 10 and full["windows"][100]["completed"] == 1
    assert full["windows"][100]["latest"]["features"]["ARP"] == .5
    assert full["windows"][100]["latest"]["features"]["Protocol Type"] == 0
    assert monitor.snapshot()["packet_lanes"]["MQTT_BROKER"]["frames"] == 50
