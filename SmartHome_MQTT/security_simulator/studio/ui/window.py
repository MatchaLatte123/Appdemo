"""PySide6 validation console. Device / API operations run off the GUI thread."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
from time import time

from PySide6 import QtCore, QtWidgets as Qt

from security_ai.live.cic38 import WINDOW_PACKETS as CIC38_WINDOW_PACKETS
from security_ai.live.live_monitor import camera_endpoints

from ..ai_client import AiClient, ROOT
from ..csv_demo import LABELS, evaluate_files
from ..live_model import load_live_model
from ..service_monitor import snapshot
from .capture_dashboard import CaptureDashboard
from .presentation import STYLE, attack_score, card, label, lane_name, number, percent, status


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
        self.setWindowTitle("SmartHome · IoT Simulation Studio")
        self.resize(1280, 800)
        self.deployment = deployment
        self.ai = AiClient(deployment)
        self.local_model_error = None
        try:
            _, self.model_parameters = load_live_model(deployment)
        except (OSError, ValueError, KeyError) as error:
            self.model_parameters = {}
            self.local_model_error = str(error)
        self.io = ThreadPoolExecutor(max_workers=1, thread_name_prefix="studio-observe")
        self.demo_io = ThreadPoolExecutor(max_workers=1, thread_name_prefix="studio-demo")
        self.demo_job = None
        self.demo_paths = ()
        self.pending = []
        self.poll_job = None
        self.services = {}
        self.closing = False

        self.setStyleSheet(STYLE)
        shell = Qt.QWidget(objectName="workspace")
        row = Qt.QHBoxLayout(shell)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        sidebar = Qt.QWidget(objectName="sidebar")
        sidebar.setFixedWidth(204)
        rail = Qt.QVBoxLayout(sidebar)
        rail.setContentsMargins(12, 20, 12, 16)
        rail.setSpacing(8)
        rail.addWidget(label("SmartHome", "brand"))
        rail.addWidget(label("Simulation Studio", "brandCaption"))
        self.nav = Qt.QListWidget(objectName="navigation")
        self.nav.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        rail.addWidget(self.nav, 1)
        self.tabs = Qt.QStackedWidget()
        self.nav.currentRowChanged.connect(self.tabs.setCurrentIndex)
        row.addWidget(sidebar)
        row.addWidget(self.tabs, 1)
        self.setCentralWidget(shell)
        self.scopes = ["MQTT_BROKER", "VIDEO_API"] + [
            f"CAMERA[{ip}]:{port}" for ip, port in camera_endpoints(deployment.video_config)]
        self.build_overview()
        self.build_live()
        self.build_demo()
        if deployment.ai_port is None:
            self.nav.item(self.tabs.indexOf(self.live_tabs.parentWidget())).setHidden(True)
            self.ai_headline.setText("Chưa cấu hình")
            self.runtime_label.setText("Kiểm tra kết nối các dịch vụ tại đây.")
            self.live_title.hide()
            self.live_table.hide()
        self.nav.setCurrentRow(0)
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(100)
        self.next_poll = 0
        self.submit(lambda: snapshot(self.deployment), self.render_services)

    def tab(self, title, subtitle):
        page = Qt.QWidget(objectName="workspace")
        layout = Qt.QVBoxLayout(page)
        layout.setContentsMargins(24, 18, 24, 18)
        layout.setSpacing(12)
        heading = label(title, "pageTitle")
        heading.setWordWrap(True)
        layout.addWidget(heading)
        description = label(subtitle, "muted")
        description.setWordWrap(True)
        layout.addWidget(description)
        self.nav.addItem(title)
        self.tabs.addWidget(page)
        return layout

    def actions(self, layout, buttons):
        bar = Qt.QHBoxLayout()
        for text, handler, *role in buttons:
            button = Qt.QPushButton(text)
            if role:
                button.setProperty("role", role[0])
            button.clicked.connect(handler)
            bar.addWidget(button)
        bar.addStretch()
        layout.addLayout(bar)
        return bar

    def cards(self, layout, titles):
        line = Qt.QHBoxLayout()
        values = []
        for title in titles:
            widget, value = card(title)
            line.addWidget(widget, 1)
            values.append(value)
        layout.addLayout(line)
        return values

    def detail(self, layout):
        pane = plain()
        button = Qt.QPushButton("Hiện dữ liệu JSON")
        button.setCheckable(True)
        button.toggled.connect(pane.setVisible)
        button.toggled.connect(lambda open_: button.setText(
            "Ẩn dữ liệu JSON" if open_ else "Hiện dữ liệu JSON"))
        layout.addWidget(button, alignment=QtCore.Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(pane, 1)
        pane.hide()
        return pane

    def fill(self, table, rows):
        table.setRowCount(len(rows))
        for row, values in enumerate(rows):
            for col, value in enumerate(values):
                item = Qt.QTableWidgetItem(str(value))
                item.setToolTip(str(value))
                table.setItem(row, col, item)

    def table(self, columns):
        table = Qt.QTableWidget(0, len(columns))
        table.setHorizontalHeaderLabels(columns)
        table.setAlternatingRowColors(True)
        table.setShowGrid(False)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(33)
        table.setEditTriggers(Qt.QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(Qt.QAbstractItemView.SelectionBehavior.SelectRows)
        table.horizontalHeader().setStretchLastSection(True)
        return table

    def build_overview(self):
        subtitle = ("Tình trạng AI Server và dự đoán mới nhất trên từng luồng Ethernet."
                    if self.deployment.ai_port is not None else "Trạng thái kết nối các dịch vụ.")
        layout = self.tab("Tổng quan", subtitle)
        self.ai_headline, self.worker_label, self.broker_label, self.recorder_label = self.cards(
            layout, ("AI Server", "XGBoost", "MQTT Broker", "Video Server"))
        self.ai_headline.setText("Đang kết nối")
        self.runtime_label = label(self.deployment.interface, "muted")
        self.runtime_label.setWordWrap(True)
        layout.addWidget(self.runtime_label)
        if self.local_model_error:
            warning = label(self.local_model_error, "muted")
            warning.setWordWrap(True)
            layout.addWidget(warning)
        self.live_title = label("Kết quả theo luồng", "sectionTitle")
        layout.addWidget(self.live_title)
        self.live_table = self.table(("Luồng", "Trạng thái", "Dự đoán", "Điểm attack", "Ngưỡng", "Gói / cửa sổ"))
        self.live_table.horizontalHeader().setSectionResizeMode(Qt.QHeaderView.ResizeMode.Stretch)
        self.live_table.horizontalHeader().setSectionResizeMode(0, Qt.QHeaderView.ResizeMode.ResizeToContents)
        self.live_table.setMaximumHeight(260)
        layout.addWidget(self.live_table, 2)
        self.health_label = label("Đang kiểm tra các dịch vụ…", "muted")
        layout.addWidget(self.health_label)
        self.actions(layout, [("Kiểm tra kết nối", lambda: self.submit(
            lambda: snapshot(self.deployment), self.render_services))])
        self.overview = self.detail(layout)
        layout.addStretch()

    def build_live(self):
        layout = self.tab("Live", "Theo dõi gói Ethernet và cửa sổ dự đoán theo luồng.")
        self.live_tabs = Qt.QTabWidget()
        layout.addWidget(self.live_tabs, 1)
        self.capture_dashboard = CaptureDashboard(self.scopes, self.table, self.fill)
        self.live_tabs.addTab(self.capture_dashboard, "Dashboard")
        cic38_page = Qt.QWidget()
        self.live_tabs.addTab(cic38_page, "CIC_38")
        layout = Qt.QVBoxLayout(cic38_page)
        self.cic38_scope = Qt.QComboBox()
        for scope in self.scopes:
            self.cic38_scope.addItem(lane_name(scope), scope)
        layout.addWidget(self.cic38_scope)
        self.cic38_status = label("Đang chờ dữ liệu CIC_38.", "muted")
        self.cic38_status.setWordWrap(True)
        layout.addWidget(self.cic38_status)
        self.cic38_result = label("Đang chờ điểm mô hình.", "sectionTitle")
        self.cic38_result.setWordWrap(True)
        layout.addWidget(self.cic38_result)
        self.cic38_probabilities = self.cards(layout, ("Benign", "DoS", "DDoS"))
        self.cic38_scores = {}
        self.cic38_values = self.table(("Đặc trưng", "Giá trị thô", "Z-score của Train"))
        layout.addWidget(self.cic38_values, 1)
        self.cic38_data = None
        self.cic38_scope.currentIndexChanged.connect(self.render_cic38)

    def render_cic38(self):
        data = self.cic38_data
        selected = self.cic38_scope.currentData()
        score = self.cic38_scores.get(selected, {})
        probabilities = score.get("probabilities", {})
        if probabilities:
            scored_end = utc(score["window"]["end"])
            self.cic38_result.setText(f"Dự đoán {score['prediction']} · Cửa sổ đã chấm lúc {scored_end}")
        else:
            self.cic38_result.setText(status(score.get("status", "WAITING_FOR_WINDOW")))
        for label_name, card in zip(("Benign", "DoS", "DDoS"), self.cic38_probabilities, strict=True):
            card.setText(attack_score(probabilities.get(label_name)))
        if data is None:
            self.cic38_status.setText("Chưa có dữ liệu CIC_38 từ AI Server.")
            self.cic38_values.setRowCount(0)
            return
        size = CIC38_WINDOW_PACKETS
        windows = data["lanes"].get(selected, {}).get("windows", {})
        state = windows.get(str(size), windows.get(size, {}))
        newest = state.get("latest")
        scope = f"{lane_name(selected)} · {state.get('pending', 0)}/{size} gói đang gom"
        if newest is None:
            self.cic38_status.setText(scope + " · Chưa có cửa sổ hoàn chỉnh")
            self.cic38_values.setRowCount(0)
            return
        self.cic38_status.setText(
            scope + f" · {state['completed']:,} cửa sổ · Gói cuối {utc(newest['end'])}")
        self.fill(self.cic38_values, [
            (name, number(newest["features"][name]), number(value))
            for name, value in zip(data["feature_order"], newest["normalized_vector"], strict=True)
        ])

    def build_demo(self):
        layout = self.tab("Demo", "Chấm CSV offline bằng model CIC_38. Mỗi dòng cần đủ 38 đặc trưng đã chuẩn hóa theo tập Train.")
        toolbar = Qt.QHBoxLayout()
        self.demo_browse = Qt.QPushButton("Chọn CSV…")
        self.demo_browse.clicked.connect(self.choose_demo_files)
        self.demo_run = Qt.QPushButton("Chạy dự đoán")
        self.demo_run.setProperty("role", "primary")
        self.demo_run.setEnabled(False)
        self.demo_run.clicked.connect(self.run_demo)
        toolbar.addWidget(self.demo_browse)
        toolbar.addWidget(self.demo_run)
        toolbar.addStretch()
        layout.addLayout(toolbar)
        self.demo_status = label("Chọn một hoặc nhiều file CSV.", "muted")
        self.demo_status.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.demo_status.setWordWrap(True)
        layout.addWidget(self.demo_status)
        self.demo_cards = self.cards(layout, ("Tổng số dòng", *LABELS, "Điểm attack TB"))
        layout.addWidget(label("Tổng hợp theo file", "sectionTitle"))
        self.demo_table = self.table(("File CSV", "Số dòng", *LABELS, "Điểm attack TB"))
        self.demo_table.horizontalHeader().setSectionResizeMode(0, Qt.QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.demo_table, 2)

    def reset_demo_results(self):
        for card_value in self.demo_cards:
            card_value.setText("—")
        self.fill(self.demo_table, [(path.name, "—", "—", "—", "—", "—") for path in self.demo_paths])

    def choose_demo_files(self):
        folder = self.demo_paths[0].parent if self.demo_paths else ROOT / "security_ai" / "demo" / "output"
        files, _ = Qt.QFileDialog.getOpenFileNames(self, "Chọn file đặc trưng CIC_38", str(folder), "CSV (*.csv)")
        if files:
            self.demo_paths = tuple(Path(file) for file in files)
            self.reset_demo_results()
            self.demo_status.setText(f"Đã chọn {len(files)} file CSV.")
            self.demo_run.setEnabled(True)

    def run_demo(self):
        if self.demo_job or not self.demo_paths:
            return
        self.reset_demo_results()
        self.demo_browse.setEnabled(False)
        self.demo_run.setEnabled(False)
        self.demo_status.setText(f"Đang chấm {len(self.demo_paths)} file CSV…")
        self.demo_job = self.demo_io.submit(evaluate_files, self.deployment, self.demo_paths)

    def finish_demo(self):
        future, self.demo_job = self.demo_job, None
        self.demo_browse.setEnabled(True)
        self.demo_run.setEnabled(True)
        try:
            result = future.result()
        except Exception as error:
            self.demo_status.setText(f"Không thể chấm CSV. {error}")
            return
        rows = result["rows"]
        cards = (f"{rows:,}", *(f"{count:,}" for count in result["counts"]), percent(result["mean_attack"]))
        for widget, value in zip(self.demo_cards, cards, strict=True):
            widget.setText(value)
        self.demo_status.setText(f"XGBoost {result['run_id'][:8]} đã chấm {len(result['files'])} file.")
        self.fill(self.demo_table, [
            (Path(item["path"]).name, f"{item['rows']:,}", *(f"{count:,}" for count in item["counts"]),
             percent(item["attack_sum"] / item["rows"]))
            for item in result["files"]
        ])

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
        if self.demo_job and self.demo_job.done():
            self.finish_demo()
        if self.closing:
            if not self.pending and not self.poll_job and not self.demo_job:
                self.timer.stop()
                self.close()
            return
        if self.deployment.ai_port is not None and not self.poll_job and not self.pending and time() >= self.next_poll:
            self.poll_job = self.io.submit(self.collect)
            self.next_poll = time() + self.deployment.poll_seconds

    def collect(self):
        try:
            sample = self.ai.sample()
            prediction = sample["xgboost-shadow"]
            overview = sample["overview"]
            features = sample["collector-features"]
            return {"prediction": prediction, "overview": overview, "features": features}
        except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
            return {"error": str(error)}

    def render_services(self, result):
        self.services = result
        self.broker_label.setText(status(result["mqtt_tcp"]))
        code = result["video_http"]
        self.recorder_label.setText(f"HTTP {code}" if isinstance(code, int) else status(code))
        self.health_label.setText("Kiểm tra kết nối từ máy chạy Studio.")

    def render_poll(self, data):
        if "error" in data:
            self.ai_headline.setText("Không đọc được")
            self.worker_label.setText("—")
            self.live_table.setRowCount(0)
            self.capture_dashboard.unavailable()
            self.cic38_data = None
            self.cic38_scores = {}
            self.render_cic38()
            self.runtime_label.setText(self.deployment.interface)
            self.overview.clear()
            self.statusBar().showMessage(data["error"], 5000)
            return
        prediction, overview = data["prediction"], data["overview"]
        self.capture_dashboard.update_snapshot(overview["monitoring"], data["features"], prediction)
        self.cic38_data = data["features"].get("cic38")
        self.cic38_scores = prediction["lanes"] if self.cic38_data else {}
        self.render_cic38()
        info = prediction["model"]
        self.ai_headline.setText(status(overview["monitoring"]["status"]))
        self.worker_label.setText(status(prediction["worker"]))
        action_state = "Áp dụng" if overview["enforcement"] else "Chỉ quan sát"
        self.runtime_label.setText(
            f"{self.deployment.interface}   ·   XGBoost {info['run_id'][:12]}   ·   {action_state}")
        self.fill(self.live_table, [
            (lane_name(scope), status(lane["status"]), status(lane.get("prediction")),
             attack_score(lane.get("score")), attack_score(lane.get("threshold")), lane.get("window_packets", "—"))
            for scope, lane in prediction["lanes"].items()
        ])
        show_json(self.overview, {
            "capture_interface": self.deployment.interface, "model": info, "parameters": self.model_parameters,
            "compatibility": prediction["parity"], "decision_eligible": prediction["decision_eligible"],
            "enforcement": overview["enforcement"], "isolation_assessment": prediction.get("isolation_assessment"),
            "services": self.services,
        })

    def closeEvent(self, event):
        if self.pending or self.poll_job or self.demo_job:
            self.closing = True
            self.setEnabled(False)
            event.ignore()
            return
        self.timer.stop()
        self.io.shutdown(wait=False)
        self.demo_io.shutdown(wait=False)
        event.accept()
