"""Collect scoped physical evidence without prescribing network load."""
from time import monotonic, time
from uuid import uuid4

from .evaluator import physical
from .feature_audit import source_evidence, window_identity

# label, lane category, minimum boards, maximum boards, operator-triggered mode
SCENARIOS = {
    "N01": ("Observe MQTT idle", "MQTT", 1, 1, None),
    "N02": ("Normal MQTT telemetry", "MQTT", 1, 1, "NORMAL_MQTT"),
    "N03": ("Observe multiple MQTT devices", "MQTT", 2, 8, "NORMAL_MQTT"),
    "N04": ("Legitimate MQTT reconnect", "MQTT", 1, 1, "NORMAL_RECONNECT"),
    "N05": ("ESP32 Video API health request", "VIDEO", 1, 1, "NORMAL_HTTP"),
    "N06": ("Observe multiple HTTP clients", "VIDEO", 2, 8, "NORMAL_HTTP"),
    "N07": ("Observe existing camera stream", "CAMERA", 0, 0, None),
    "N08": ("Observe another authorized video client", "VIDEO", 0, 0, None),
    "S01": ("Bounded single-source protocol behavior", "MQTT", 1, 1, "BOUNDED_PROTOCOL_TEST"),
    "S02": ("Observe legitimate multi-source activity", "ANY", 2, 8, None),
    "R01": ("Check simulator recovery", "ANY", 1, 1, "RECOVERY"),
}
CATEGORIES = {"MQTT": "MQTT_BROKER", "VIDEO": "VIDEO_API"}


class SessionManager:
    def __init__(self, db, devices):
        self.db = db
        self.devices = devices
        self.active = None
        self.windows = {}

    def start(self, scenario, scope, ports, duration, notes, audit, services, verified_normal=False):
        if self.active:
            raise ValueError("Close the current session first")
        label, category, minimum, maximum, _ = SCENARIOS[scenario]
        if (not minimum <= len(set(ports)) == len(ports) <= maximum
                or not 10 <= duration <= 300):
            raise ValueError(f"{scenario} requires {minimum}ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã…â€œ{maximum} selected boards and 10ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã…â€œ300 seconds")
        if ((category in CATEGORIES and scope != CATEGORIES[category])
                or (category == "CAMERA" and not scope.startswith("CAMERA["))):
            raise ValueError("Select the service lane required by this scenario")
        participants = self.devices.participants(ports)
        if any(s["mode"] != "IDLE" or s.get("busy") for s in participants):
            raise ValueError("Participants must be idle before observation starts")
        self.deadline = monotonic() + duration
        self.windows.clear()
        self.active = {
            "id": uuid4().hex, "scenario": scenario, "name": label, "scope": scope, "status": "ACTIVE",
            "kind": "PHYSICAL_OBSERVATION", "started": time(), "planned_seconds": duration,
            "model_id": audit["model_id"], "model_sha256": audit["sha256"], "threshold": audit["threshold"],
            "initial_completed": audit["completed"], "reference_features": audit.get("features"),
            "latest_audit": audit, "participants": participants,
            "operator_notes": notes, "services_before": services,
            "ground_truth": "CONTROLLED_PROTOCOL_TEST" if scenario.startswith("S") else (
                "VERIFIED_NORMAL" if verified_normal else "UNKNOWN"),
            "ground_truth_provenance": "OPERATOR_TEST_CONTEXT" if verified_normal or scenario.startswith("S") else "UNVERIFIED",
            "decision_eligible": audit["decision_eligible"], "enforcement": audit["enforcement"],
        }
        self.db.save(self.active)
        return self.active.copy()

    def start_board(self, port):
        session = self.active
        if not session or monotonic() >= self.deadline:
            raise ValueError("Open an active physical session first")
        mode = SCENARIOS[session["scenario"]][-1]
        if session["scenario"] == "S02":
            mode = {"MQTT_BROKER": "NORMAL_MQTT", "VIDEO_API": "NORMAL_HTTP"}.get(session["scope"])
        participant = next((item for item in session["participants"] if item["port"] == port), None)
        if not mode or not participant:
            raise ValueError("This board or scenario has no active workload")
        state, = self.devices.participants([port])
        if state["boot_id"] != participant["boot_id"] or state["device_id"] != participant["device_id"]:
            raise ValueError("Simulator identity changed during observation")
        result = self.devices.command(port, "START " + mode)
        self.db.append(session["id"], time(), "SERIAL_START_RESULT", result)
        if result["result"] != "OK":
            raise ValueError("Firmware rejected start; inspect Wi-Fi, cooldown and safety state")
        return result

    def observe(self, audit, states, peers, health, incidents=()):
        session = self.active
        if not session:
            return False
        stamp = time()
        if audit["scope"] == session["scope"]:
            if (audit["model_id"], audit["sha256"], audit["threshold"]) != (
                    session["model_id"], session["model_sha256"], session["threshold"]):
                raise ValueError("Model or threshold changed during the session")
            session["latest_audit"] = audit
            key = window_identity(audit["window"])
            if (audit["status"] == "PREDICTED" and key and key[0] >= session["started"]
                    and key not in self.windows):
                self.windows[key] = audit
                self.db.append(session["id"], stamp, "PREDICTION", audit)
        by_port = {s["port"]: s for s in states}
        selected = [by_port.get(x["port"], {}) for x in session["participants"]]
        lost = any(not st.get("online") or not st.get("wifi") or st.get("rebooted")
                   or st.get("device_id") != old["device_id"] or st.get("boot_id") != old["boot_id"]
                   for st, old in zip(selected, session["participants"], strict=True))
        evidence = source_evidence(session["scope"], selected, peers, incidents, session["started"])
        self.db.append(session["id"], stamp, "TELEMETRY",
                       {"participants": selected, "source": evidence, "health": health,
                        "scope": audit["scope"], "window_status": audit["status"],
                        "pending_packets": audit["pending_packets"], "discarded_windows": audit["discarded_windows"],
                        "capture_counters": audit["capture_counters"]})
        session["last_source_evidence"] = evidence
        session["last_device_states"] = selected
        return lost

    def expired(self):
        return self.active is not None and monotonic() >= self.deadline

    def close(self, services, reason="OPERATOR_CLOSED"):
        session = self.active
        if not session:
            return None
        failed = []
        for device in session["participants"]:
            try:
                self.devices.command(device["port"], "STOP")
            except (KeyError, OSError, RuntimeError, TimeoutError):
                failed.append(device["port"])
        session.update(status=reason, ended=time(), services_after=services, stop_unacknowledged_ports=failed)
        session["result"] = physical(session, list(self.windows.values()))
        last = session["latest_audit"]["completed"]
        initial = session["initial_completed"]
        session["result"]["final_pending_packets"] = session["latest_audit"]["pending_packets"]
        session["result"]["discarded_windows"] = session["latest_audit"]["discarded_windows"]
        session["result"]["http_success_by_port"] = {
            state["port"]: {"http_status": state.get("http_status"), "successes": state.get("successes")}
            for state in session.get("last_device_states", []) if "port" in state and state.get("http_status")}
        session["result"]["recovery_status_by_port"] = {
            state["port"]: state["safety"] for state in session.get("last_device_states", []) if "port" in state}
        if last is not None and initial is not None:
            session["result"]["collector_completed_delta"] = max(0, last - initial)
            session["result"]["completed_without_matched_prediction"] = max(0, last - initial - len(self.windows))
        self.db.save(session)
        self.active = None
        self.windows.clear()
        return session

    def save_offline(self, report):
        entries = report.pop("records")
        row = {"id": uuid4().hex, "kind": "OFFLINE_REPLAY", "model_id": report["run_id"],
               "started": time(), "status": "VERIFIED_OFFLINE_EXECUTION", "scenario": report["method"],
               "scope": report["scope"], "result": report, "decision_eligible": False}
        row["ended"] = row["started"]
        self.db.save(row, ((row["ended"], "OFFLINE_PREDICTION", entry) for entry in entries))
        return row
