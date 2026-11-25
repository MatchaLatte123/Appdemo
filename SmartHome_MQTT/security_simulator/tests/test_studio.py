"""Studio fixtures. No COM is opened and no traffic is sent by these tests."""
from pathlib import Path
import csv
import struct
from tempfile import TemporaryDirectory
from time import time
import unittest

from security_ai_v2.rebuild.contract import BASIC_10
from security_ai_v3.capture import TSHARK
from security_simulator.studio.ai_client import AiClient, Deployment, PROJECT
from security_simulator.studio.database import Database
from security_simulator.studio.device_manager import DeviceManager
from security_simulator.studio.evaluator import binary
from security_simulator.studio.feature_audit import inspect, source_evidence
from security_simulator.studio.offline_replay import OfflineReplay
from security_simulator.studio.report_exporter import export
from security_simulator.studio.serial_transport import parse_reply
from security_simulator.studio.session_manager import SessionManager


def simulator(identity="ESP32-SIM-01", boot="aabbccdd"):
    return {"kind": "SMART_HOME_SIM", "protocol": 1, "version": "1.0.0", "device_id": identity,
            "boot_id": boot, "mode": "IDLE", "wifi": True, "ip": "192.0.2.12", "result": "OK", "safety": "READY"}


class FakeLink:
    def __init__(self, port):
        self.response = simulator("ESP32-SIM-01" if port == "COM1" else "ESP32-SIM-02")
        self.commands = []
        self.closed = False

    def request(self, command, **_options):
        self.commands.append(command)
        if self.response is None:
            raise TimeoutError("Unplugged")
        return self.response.copy()

    def close(self):
        self.closed = True


def write_benign_fixture(path, count):
    """A local Ethernet/TCP ACK fixture in documentation IPv4 ranges."""
    eth = bytes.fromhex("0200000000010200000000020800")
    ip = bytes.fromhex("450000280001000040060000c000020ac6336414")
    tcp = struct.pack("!HHIIBBHHH", 50000, 1883, 1, 1, 0x50, 0x10, 4096, 0, 0)
    packet = eth + ip + tcp
    with path.open("wb") as stream:
        stream.write(struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1))
        for i in range(count):
            stream.write(struct.pack("<IIII", 1700000000, 1000 * i, len(packet), len(packet)))
            stream.write(packet)


class SimulatorTest(unittest.TestCase):
    def test_serial_protocol_version_and_boot(self):
        import json
        self.assertEqual(parse_reply(json.dumps(simulator()))["device_id"], "ESP32-SIM-01")
        version = simulator() | {"version": "9.0.0"}
        with self.assertRaisesRegex(RuntimeError, "version"):
            parse_reply(json.dumps(version))
        invalid = simulator() | {"device_id": "real-camera"}
        with self.assertRaisesRegex(ValueError, "Malformed"):
            parse_reply(json.dumps(invalid))

    def test_com_disconnect_duplicate_and_emergency_stop(self):
        manager = DeviceManager(FakeLink)
        manager.connect("COM1")
        manager.connect("COM2")
        self.assertEqual(len(manager.participants(["COM1", "COM2"])), 2)
        first, other = manager.links.values()
        other.response["device_id"] = first.response["device_id"]
        manager.poll()
        with self.assertRaisesRegex(ValueError, "unique"):
            manager.participants(["COM1", "COM2"])
        other.response["boot_id"] = "11223344"
        self.assertTrue(manager.poll()[1]["rebooted"])
        other.response = None
        self.assertEqual(manager.stop_all(), ["COM2"])
        self.assertTrue(other.closed)
        self.assertEqual(first.commands[-1], "STOP")
        self.assertFalse(manager.states["COM2"]["online"])

    def test_feature_match_and_waiting_do_not_fabricate_prediction(self):
        order = list(BASIC_10)
        key = {"start": 1, "end": 2, "packet_count": 10}
        raw = {"window": key, "features": {name: 0.0 for name in order}, "vector": [0.0] * 10}
        f = {"feature_order": order, "lanes": {"MQTT_BROKER": {"windows": {"10": {"latest": raw}}}}}
        d = {"model": {"feature_order": order, "window_packets": 10, "run_id": "x", "sha256": "y"}, "updated_at": 3,
             "decision_eligible": False, "lanes": {"MQTT_BROKER": {"service": "MQTT", "window": key,
                        "status": "PREDICTED", "prediction": "NORMAL", "score": .2, "threshold": .3}}}
        row = inspect("MQTT_BROKER", d, f)
        self.assertEqual(row["window_age_at_score_seconds"], 1)
        self.assertEqual(row["features"]["IAT"], 0.0)
        changed = key | {"end": 2.1}
        d["lanes"]["MQTT_BROKER"]["window"] = changed
        self.assertIsNone(inspect("MQTT_BROKER", d, f)["features"])
        d["lanes"]["MQTT_BROKER"].update(status="WAITING_FOR_WINDOW", prediction=None, score=None, window=None)
        self.assertIsNone(inspect("MQTT_BROKER", d, f)["prediction"])

    def test_attribution_does_not_turn_serial_ip_into_a_source(self):
        row = {"service": "MQTT", "identity": {"ip": "192.0.2.12", "status": "VERIFIED"}}
        attribution = source_evidence("MQTT_BROKER", [simulator()], [row])
        self.assertEqual(len(attribution["observed_service_peers"]), 1)
        self.assertEqual(attribution["source_attribution"], "UNKNOWN")
        self.assertIsNone(attribution["precision"])

    def test_ground_truth_persistence_and_export(self):
        audit = {"scope": "MQTT_BROKER", "status": "WAITING_FOR_WINDOW", "window": None, "model_id": "run",
                 "sha256": "sha", "threshold": .02, "completed": 0, "pending_packets": 0, "discarded_windows": 0,
                 "capture_counters": {}, "decision_eligible": False, "enforcement": False}
        with TemporaryDirectory() as temp:
            root = Path(temp)
            db = Database(root / "records.db")
            devices = DeviceManager(FakeLink)
            devices.connect("COM1")
            devices.connect("COM2")
            sessions = SessionManager(db, devices)
            with self.assertRaisesRegex(ValueError, "requires"):
                sessions.start("N03", "MQTT_BROKER", ["COM1"], 30, "", audit, {})
            row = sessions.start("N03", "MQTT_BROKER", ["COM1", "COM2"], 30, "test", audit, {}, False)
            self.assertEqual(row["ground_truth"], "UNKNOWN")
            self.assertNotIn("START NORMAL_MQTT", devices.links["COM1"].commands)
            sessions.start_board("COM1")
            self.assertNotIn("START NORMAL_MQTT", devices.links["COM2"].commands)
            stamp = time()
            next_audit = audit | {"status": "PREDICTED", "prediction": "ATTACK", "score": .4,
                                 "window": {"start": stamp, "end": stamp, "packet_count": 10},
                                 "updated_at": stamp, "window_age_at_score_seconds": 0.0, "quality": "OBSERVED"}
            states = devices.poll()
            sessions.observe(next_audit, states, [], {})
            sessions.observe(next_audit, states, [], {})
            with self.assertRaisesRegex(ValueError, "changed"):
                sessions.observe(next_audit | {"threshold": .01}, states, [], {})
            outcome = sessions.close({})
            self.assertEqual(outcome["result"]["eligible_windows_observed"], 1)
            self.assertIsNone(outcome["result"]["lane_alert_rate_during_normal_session"])
            self.assertIsNone(outcome["result"]["attack_recall"])
            stored = Database(root / "records.db").report(row["id"])
            self.assertEqual(len([x for x in stored["observations"] if x["kind"] == "PREDICTION"]), 1)
            files = export(stored, root / "out")
            self.assertEqual({p.name for p in files.iterdir()}, {"report.json", "report.html", "observations.csv"})

    def test_binary_metric_uses_explicit_attack_labels(self):
        result = binary([0, 0, 1, 1], [0, 1, 1, 0])
        self.assertEqual(result["confusion_matrix"], [[1, 1], [1, 1]])
        self.assertEqual(result["attack_recall"], .5)

    def test_api_get_only_and_threshold_identity(self):
        deployment = Deployment.read(PROJECT / "config/studio.example.json")
        client = AiClient(deployment)
        with self.assertRaisesRegex(ValueError, "read-only"):
            client.get("operator/activate")
        model = deployment.ai_config["xgboost_shadow"].copy()
        client.verify_model({"model": model})
        model["benchmark_threshold"] = .3
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            client.verify_model({"model": model})


class ActualArtifactTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.deployment = Deployment.read(PROJECT / "config/studio.example.json")
        cls.replay = OfflineReplay(cls.deployment)

    def test_pinned_dataset_dos_and_ddos(self):
        selected = self.replay.dataset("", "MQTT_BROKER", "ALL", limit=8)
        self.assertEqual(selected["test_status"], "REUSED_TEST_BENCHMARK")
        self.assertEqual(selected["method"], "FEATURE_LEVEL_REPLAY")
        self.assertEqual(set(selected["families"]), {"BENIGN", "DOS", "DDOS"})
        self.assertEqual(selected["metrics"]["rows"], 24)
        self.assertTrue(selected["per_scenario"])
        self.assertFalse(selected["decision_eligible"])

    def test_csv_labels_are_not_claimed_as_a_verified_holdout(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "labels.csv"
            with path.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.writer(stream)
                writer.writerow((*BASIC_10, "y", "family", "scenario"))
                writer.writerow((*([1] * len(BASIC_10)), 0, "BENIGN", "fixture"))
            selected = self.replay.dataset(path, "VIDEO_API", "BENIGN")
            self.assertEqual(selected["test_status"], "OPERATOR_LABELS_HOLDOUT_UNVERIFIED")
            self.assertIsNone(selected["dataset_id"])

    @unittest.skipUnless(Path(TSHARK).is_file(), "TShark not installed")
    def test_local_capture_produces_one_window_and_keeps_partial_pending(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "benign.pcap"
            write_benign_fixture(path, 13)
            measured = self.replay.packet(path, "MQTT_BROKER", "BENIGN", "fixture only", "198.51.100.20")
            self.assertEqual(measured["method"], "PACKET_FILE_REPLAY")
            self.assertEqual(len(measured["records"]), 1)
            window = measured["records"][0]
            self.assertEqual(window["window"]["packet_count"], 10)
            self.assertEqual(window["features"]["IAT"], .001)
            self.assertEqual(measured["packet_probe"]["lanes"]["MQTT_BROKER"]["windows"][10]["pending"], 3)
            self.assertIsNone(measured["metrics"])
            write_benign_fixture(path, 3)
            measured = self.replay.packet(path, "MQTT_BROKER", "BENIGN", "fixture only", "198.51.100.20")
            self.assertEqual(measured["visibility"], "WAITING_FOR_PACKETS")
            self.assertFalse(measured["records"])


if __name__ == "__main__":
    unittest.main()
