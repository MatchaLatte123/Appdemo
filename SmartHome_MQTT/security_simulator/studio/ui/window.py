"""PySide6 validation console. Device / API operations run off the GUI thread."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
from time import time

from PySide6 import QtCore, QtWidgets as Qt
import pyqtgraph as pg

from security_ai_v3.live_monitor import camera_endpoints

from ..ai_client import AiClient, PROJECT
from ..database import Database
from ..device_manager import DeviceManager
from ..feature_audit import inspect
from ..offline_replay import OfflineReplay
from ..report_exporter import export
from ..serial_transport import available_ports
from ..service_monitor import snapshot
from ..session_manager import SCENARIOS, SessionManager


def plain():
    widget = Qt.QPlainTextEdit(readOnly=True)
    widget.setLineWrapMode(Qt.QPlainTextEdit.LineWrapMode.NoWrap)
    return widget


def show_json(widget, value):
    widget.setPlainText(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False))


def utc(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat(timespec="seconds")


class Window(Qt.QMainWindow):
    def __init__(self, deployment):
        super().__init__()
        self.setWindowTitle("SmartHome Â· IoT Simulation Studio")
        self.resize(1220, 790)
        self.deployment = deployment
        self.ai = AiClient(deployment)
        self.db = Database(deployment.state_path)
        self.devices = DeviceManager()
        self.sessions = SessionManager(self.db, self.devices)
        self.replay = OfflineReplay(deployment)
        self.io = ThreadPoolExecutor(max_workers=1, thread_name_prefix="studio-observe")
        self.offline = ThreadPoolExecutor(max_workers=1, thread_name_prefix="studio-replay")
        self.pending = []
        self.poll_job = None
        self.offline_job = None
        self.services = {}
        self.history_rows = []
        self.points = deque(maxlen=180)
        self.last_plot = None
        self.last_scope = None
        self.closing = False

        self.tabs = Qt.QTabWidget()
        self.setCentralWidget(self.tabs)
        self.scopes = ["MQTT_BROKER", "VIDEO_API"] + [
            f"CAMERA[{ip}]:{port}" for ip, port in camera_endpoints(deployment.video_config)]
        self.setStyleSheet("""
            QMainWindow { background: #eef3f8; }
            QTabWidget::pane { background: #ffffff; border: 1px solid #cad6e4; }
            QTabBar::tab { padding: 10px 18px; color: #31445b; }
            QTabBar::tab:selected { background: #ffffff; color: #075170; }
            QPushButton { padding: 7px 10px; }
            QPlainTextEdit, QTableWidget { border: 1px solid #cad6e4; background: #f8fbfe; color: #162c41; }
            QLabel#headline { color: #113952; font-size: 21px; font-weight: 600; }
        """)
        self.build_overview()
        self.build_devices()
        self.build_physical()
        self.build_offline()
        self.build_feature()
        self.build_evaluation()
        self.build_history()
        self.refresh_ports()
        self.refresh_history()
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(100)
        self.next_poll = 0
        self.submit(lambda: snapshot(self.deployment), self.render_services)

    def tab(self, title):
        widget = Qt.QWidget()
        layout = Qt.QVBoxLayout(widget)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)
        self.tabs.addTab(widget, title)
        return layout

    def actions(self, layout, buttons):
        bar = Qt.QHBoxLayout()
        for label, handler in buttons:
            button = Qt.QPushButton(label)
            button.clicked.connect(handler)
            bar.addWidget(button)
        bar.addStretch()
        layout.addLayout(bar)
        return bar

    def table(self, columns):
        table = Qt.QTableWidget(0, len(columns))
        table.setHorizontalHeaderLabels(columns)
        table.setEditTriggers(Qt.QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(Qt.QAbstractItemView.SelectionBehavior.SelectRows)
        table.horizontalHeader().setStretchLastSection(True)
        return table

    def build_overview(self):
        layout = self.tab("Overview")
        self.ai_headline = Qt.QLabel("Connecting to AI Server")
        self.ai_headline.setObjectName("headline")
        layout.addWidget(self.ai_headline)
        self.active_label = Qt.QLabel("Physical session idle")
        layout.addWidget(self.active_label)
        self.health_label = Qt.QLabel("Checking service health")
        layout.addWidget(self.health_label)
        self.actions(layout, [("Check service health", lambda: self.submit(
            lambda: snapshot(self.deployment), self.render_services))])
        self.overview = plain()
        layout.addWidget(self.overview)

    def build_devices(self):
        layout = self.tab("Devices")
        selection = Qt.QHBoxLayout()
        self.ports = Qt.QComboBox()
        selection.addWidget(self.ports, 1)
        for title, handler in (("Scan COM", self.refresh_ports), ("Connect selected COM", self.connect_port)):
            button = Qt.QPushButton(title)
            button.clicked.connect(handler)
            selection.addWidget(button)
        layout.addLayout(selection)
        self.device_table = self.table(("COM", "Device ID", "IP", "Version", "Link", "Mode",
                                        "Success / attempts", "HTTP", "Safety", "Last seen UTC"))
        self.device_table.setSelectionMode(Qt.QAbstractItemView.SelectionMode.ExtendedSelection)
        layout.addWidget(self.device_table)
        self.actions(layout, [("Set device ID", self.set_id), ("Reset counters", lambda: self.board_command("RESET")),
                              ("Stop selected", lambda: self.board_command("STOP")),
                              ("Disconnect selected", self.disconnect_selected), ("Stop all", self.stop_all)])

    def build_physical(self):
        layout = self.tab("Physical Tests")
        form = Qt.QFormLayout()
        self.scenario = Qt.QComboBox()
        for code, spec in SCENARIOS.items():
            self.scenario.addItem(f"{code} Â· {spec[0]}", code)
        form.addRow("Scenario", self.scenario)
        self.scope = Qt.QComboBox()
        self.scope.addItems(self.scopes)
        form.addRow("Protected lane", self.scope)
        self.duration = Qt.QSpinBox()
        self.duration.setRange(10, 300)
        self.duration.setValue(45)
        form.addRow("Observation seconds", self.duration)
        self.notes = Qt.QLineEdit()
        form.addRow("Operator / external client notes", self.notes)
        self.normal = Qt.QCheckBox("Operator verified the expected activity was normal")
        form.addRow(self.normal)
        layout.addLayout(form)
        guide = Qt.QLabel("Select the participant rows in Devices. Open observation, then start each active board separately.")
        guide.setWordWrap(True)
        layout.addWidget(guide)
        self.actions(layout, [("Open observation", self.open_observation), ("Start one board", self.start_board),
                              ("Close observation", self.close_observation), ("Stop all", self.stop_all)])
        self.physical_text = plain()
        layout.addWidget(self.physical_text)

    def build_offline(self):
        layout = self.tab("Offline Attack Tests")
        form = Qt.QFormLayout()
        self.input_path = Qt.QLineEdit(placeholderText="Blank uses the pinned TEST Parquet")
        picker = Qt.QHBoxLayout()
        picker.addWidget(self.input_path, 1)
        choose = Qt.QPushButton("Browse")
        choose.clicked.connect(self.pick_file)
        picker.addWidget(choose)
        form.addRow("Local capture / feature file", picker)
        self.offline_scope = Qt.QComboBox()
        self.offline_scope.addItems(self.scopes)
        form.addRow("Read-only threshold profile", self.offline_scope)
        self.family = Qt.QComboBox()
        self.family.addItems(["ALL", "BENIGN", "DOS", "DDOS"])
        form.addRow("Family", self.family)
        self.original_scenario = Qt.QComboBox(editable=True)
        self.original_scenario.addItems(["", *self.replay.scenarios()])
        form.addRow("Original label / capture provenance", self.original_scenario)
        self.recorded_host = Qt.QLineEdit(placeholderText="Blank uses the deployment host")
        form.addRow("Recorded protected IPv4 for PCAP", self.recorded_host)
        self.cap = Qt.QSpinBox()
        self.cap.setRange(1, 5000)
        self.cap.setValue(1000)
        form.addRow("Feature rows per selected family", self.cap)
        layout.addLayout(form)
        self.actions(layout, [("Run offline", self.run_offline)])
        self.offline_text = plain()
        layout.addWidget(self.offline_text)

    def build_feature(self):
        layout = self.tab("Feature Inspector")
        self.feature_scope = Qt.QComboBox()
        self.feature_scope.addItems(self.scopes)
        layout.addWidget(self.feature_scope)
        self.feature_text = plain()
        layout.addWidget(self.feature_text)

    def build_evaluation(self):
        layout = self.tab("Evaluation")
        self.chart = pg.PlotWidget(background="#f7fafc")
        self.chart.setLabel("left", "Attack score")
        self.chart.setLabel("bottom", "Observed prediction index")
        self.chart.setYRange(0, 1)
        self.score_line = self.chart.plot(pen=pg.mkPen("#006e91", width=2))
        self.threshold_line = self.chart.plot(pen=pg.mkPen("#cb6b12", width=2, style=QtCore.Qt.PenStyle.DashLine))
        layout.addWidget(self.chart, 2)
        self.evaluation = plain()
        layout.addWidget(self.evaluation, 3)

    def build_history(self):
        layout = self.tab("History")
        self.history_table = self.table(("Session ID", "Scenario", "Devices", "Model", "Started UTC", "Ended UTC", "Status"))
        layout.addWidget(self.history_table)
        self.actions(layout, [("Refresh", self.refresh_history), ("Export CSV / JSON / HTML", self.export_selected)])

    def submit(self, func, handler=None):
        future = self.io.submit(func)
        self.pending.append((future, handler))

    def result(self, future, handler):
        try:
            result = future.result()
            if handler is not None:
                handler(result)
        except (OSError, RuntimeError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as error:
            self.statusBar().showMessage(str(error), 15000)
        except Exception as error:
            self.statusBar().showMessage(f"{type(error).__name__}: {error}", 15000)

    def tick(self):
        done, self.pending = self.pending, []
        for future, handler in done:
            if future.done():
                self.result(future, handler)
            else:
                self.pending.append((future, handler))
        if self.poll_job and self.poll_job.done():
            self.result(self.poll_job, self.render_poll)
            self.poll_job = None
        if self.offline_job and self.offline_job.done():
            self.result(self.offline_job, self.render_finished)
            self.offline_job = None
        if self.closing:
            if not self.pending and not self.poll_job and not self.offline_job:
                self.timer.stop()
                self.close()
            return
        if not self.poll_job and not self.pending and time() >= self.next_poll:
            scope = self.feature_scope.currentText()
            self.poll_job = self.io.submit(self.collect, scope)
            self.next_poll = time() + self.deployment.poll_seconds

    def collect(self, scope):
        states = self.devices.poll()
        session = self.sessions.active
        try:
            sample = self.ai.sample()
            prediction = sample["xgboost-shadow"]
            overview = sample["overview"]
            features = sample["collector-features"]
            audit = inspect(scope, prediction, features, self.replay.transformer, enforcement=overview["enforcement"])
            finished = None
            if session:
                scoped = audit if scope == session["scope"] else inspect(
                    session["scope"], prediction, features, self.replay.transformer, enforcement=overview["enforcement"])
                peers = self.ai.get("devices")["devices"]
                incidents = self.ai.get("incidents")["incidents"]
                lost = self.sessions.observe(scoped, states, peers, overview.get("services", []), incidents)
                if lost or self.sessions.expired():
                    finished = self.finish_session("PARTICIPANT_LOST" if lost else "TIME_LIMIT")
            return {"device_states": states, "audit": audit, "prediction": prediction, "overview": overview,
                    "active": self.sessions.active.copy() if self.sessions.active else None, "finished": finished}
        except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
            finished = self.finish_session("OBSERVATION_INTERRUPTED") if session else None
            return {"device_states": states, "error": str(error), "active": None, "finished": finished}

    def render_services(self, result):
        self.services = result
        self.health_label.setText(
            f"MQTT TCP {result['mqtt_tcp']}   Â·   Video /health {result['video_http']}   Â·   Probe from Studio host")

    def render_poll(self, data):
        self.render_devices(data["device_states"])
        if data["finished"]:
            self.render_finished(data["finished"])
        active = data["active"]
        self.active_label.setText(f"Active {active['scenario']} Â· {active['scope']}" if active else "Physical session idle")
        if "error" in data:
            self.ai_headline.setText("AI observation unavailable")
            self.statusBar().showMessage(data["error"], 5000)
            return
        prediction, overview = data["prediction"], data["overview"]
        info = prediction["model"]
        monitoring = overview["monitoring"]
        self.ai_headline.setText(f"{monitoring['status']}   Â·   XGBoost {prediction['worker']}   Â·   {info['run_id'][:12]}")
        show_json(self.overview, {
            "capture_interface": self.deployment.interface, "model": info, "parameters": self.replay.model_parameters,
            "compatibility": prediction["parity"], "decision_eligible": prediction["decision_eligible"],
            "enforcement": overview["enforcement"], "isolation_assessment": prediction.get("isolation_assessment"),
            "services": self.services, "active_session": active["id"] if active else None,
        })
        audit = data["audit"]
        if audit["scope"] != self.feature_scope.currentText():
            return
        show_json(self.feature_text, audit)
        if self.last_scope != audit["scope"]:
            self.points.clear()
            self.last_plot = None
            self.last_scope = audit["scope"]
        if audit["status"] == "PREDICTED" and audit["window"] != self.last_plot:
            self.last_plot = audit["window"]
            self.points.append((audit["score"], audit["threshold"]))
        xs = list(range(len(self.points)))
        self.score_line.setData(xs, [p[0] for p in self.points])
        self.threshold_line.setData(xs, [p[1] for p in self.points])
        if active:
            show_json(self.physical_text, {"session_id": active["id"], **active["latest_audit"]})

    def refresh_ports(self):
        selected = self.ports.currentData()
        self.ports.clear()
        for port, description in available_ports():
            self.ports.addItem(f"{port} Â· {description}", port)
        index = self.ports.findData(selected)
        if index >= 0:
            self.ports.setCurrentIndex(index)

    def connect_port(self):
        port = self.ports.currentData()
        if port:
            self.submit(lambda: self.devices.connect(port))

    def selected_ports(self):
        return [self.device_table.item(i.row(), 0).text()
                for i in self.device_table.selectionModel().selectedRows()]

    def one_port(self):
        ports = self.selected_ports()
        if len(ports) != 1:
            self.statusBar().showMessage("Select exactly one device row in Devices", 6000)
            return None
        return ports[0]

    def render_devices(self, states):
        selected = set(self.selected_ports())
        self.device_table.setRowCount(len(states))
        selection = self.device_table.selectionModel()
        selection.clearSelection()
        for index, state in enumerate(states):
            link = "Offline" if not state["online"] else "Rebooted" if state["rebooted"] else "USB + Wi-Fi" if state["wifi"] else "USB"
            row = (state["port"], state["device_id"] or "Unassigned", state["ip"], state["version"], link, state["mode"],
                   f"{state.get('successes', 0)}/{state.get('attempts', 0)}", state.get("http_status", 0),
                   state.get("safety"), state["last_seen"])
            for col, value in enumerate(row):
                self.device_table.setItem(index, col, Qt.QTableWidgetItem(str(value)))
            if state["port"] in selected:
                flags = QtCore.QItemSelectionModel.SelectionFlag
                selection.select(self.device_table.model().index(index, 0), flags.Select | flags.Rows)

    def set_id(self):
        port = self.one_port()
        if port:
            value, accepted = Qt.QInputDialog.getText(self, "Set simulator ID", "ID such as ESP32-SIM-01")
            if accepted:
                self.submit(lambda: self.devices.configure_id(port, value.strip()))

    def board_command(self, command):
        port = self.one_port()
        if port:
            self.submit(lambda: self.devices.command(port, command))

    def disconnect_selected(self):
        port = self.one_port()
        if port:
            self.submit(lambda: self.devices.disconnect(port))

    def stop_all(self):
        def stop():
            failed = self.devices.stop_all()
            result = self.finish_session("STOP_REQUESTED")
            return result, failed
        self.submit(stop, self.render_stopped)

    def render_stopped(self, result):
        session, failed = result
        if session:
            self.render_finished(session)
        self.statusBar().showMessage("Stop sent" if not failed else f"Stop unacknowledged: {', '.join(failed)}", 15000)

    def open_observation(self):
        scenario, scope = self.scenario.currentData(), self.scope.currentText()
        args = (scenario, scope, self.selected_ports(), self.duration.value(), self.notes.text())
        verified = self.normal.isChecked()

        def start():
            sample = self.ai.sample()
            if sample["xgboost-shadow"]["worker"] != "READY":
                raise ValueError("Wait for the XGBoost worker to become ready")
            audit = inspect(scope, sample["xgboost-shadow"], sample["collector-features"], self.replay.transformer,
                            enforcement=sample["overview"]["enforcement"])
            return self.sessions.start(*args, audit, snapshot(self.deployment), verified)
        self.submit(start, lambda row: show_json(self.physical_text, row))

    def start_board(self):
        port = self.one_port()
        if port:
            self.submit(lambda: self.sessions.start_board(port), lambda row: show_json(self.physical_text, row))

    def finish_session(self, reason):
        result = self.sessions.close({}, reason)
        if result:
            result["services_after"] = snapshot(self.deployment)
            self.db.save(result)
        return result

    def close_observation(self):
        self.submit(lambda: self.finish_session("OPERATOR_CLOSED"), self.render_finished)

    def pick_file(self):
        path, _ = Qt.QFileDialog.getOpenFileName(self, "Select a local input file", str(PROJECT),
                                               "Capture or features (*.pcap *.pcapng *.csv *.parquet)")
        if path:
            self.input_path.setText(path)

    def run_offline(self):
        if self.offline_job:
            return
        path, scope, family = self.input_path.text().strip(), self.offline_scope.currentText(), self.family.currentText()
        scenario, host = self.original_scenario.currentText().strip(), self.recorded_host.text().strip()
        cap = self.cap.value()
        self.offline_text.setPlainText("Evaluating local dataâ€¦")

        def run():
            if Path(path).suffix.lower() in (".pcap", ".pcapng"):
                result = self.replay.packet(path, scope, family, scenario, host)
            else:
                result = self.replay.dataset(path, scope, family, scenario, cap)
            return self.sessions.save_offline(result)
        self.offline_job = self.offline.submit(run)

    def render_finished(self, row):
        if row is None:
            return
        show_json(self.evaluation, row["result"])
        if row["kind"] == "OFFLINE_REPLAY":
            show_json(self.offline_text, row["result"])
        else:
            show_json(self.physical_text, row)
        self.refresh_history()

    def refresh_history(self):
        self.history_rows = self.db.history()
        self.history_table.setRowCount(len(self.history_rows))
        for index, session in enumerate(self.history_rows):
            participants = ", ".join(d["device_id"] for d in session.get("participants", ()))
            row = (session["id"], session["scenario"], participants, session["model_id"][:12],
                   utc(session["started"]), utc(session["ended"]) if "ended" in session else "", session["status"])
            for col, value in enumerate(row):
                self.history_table.setItem(index, col, Qt.QTableWidgetItem(str(value)))

    def export_selected(self):
        selected = self.history_table.selectionModel().selectedRows()
        if len(selected) != 1:
            self.statusBar().showMessage("Select one session", 5000)
            return
        session_id = self.history_table.item(selected[0].row(), 0).text()
        path = Qt.QFileDialog.getExistingDirectory(self, "Select export destination", str(PROJECT / "reports"))
        if path:
            self.submit(lambda: export(self.db.report(session_id), path),
                        lambda folder: self.statusBar().showMessage(f"Exported {folder}", 12000))

    def closeEvent(self, event):
        if not self.closing:
            self.closing = True
            self.setEnabled(False)

            def shutdown():
                self.finish_session("STUDIO_CLOSED")
                self.devices.close()
            self.submit(shutdown)
            event.ignore()
        elif self.pending or self.poll_job or self.offline_job:
            event.ignore()
        else:
            self.io.shutdown(wait=False)
            self.offline.shutdown(wait=False)
            event.accept()
